"""Canonical JSONL record bytes and strict schema decoding."""

import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from src.adapters.jsonl_errors import (
    EventConflictError,
    EventCorruptionError,
    EventPathError,
    EventValidationError,
)
from src.application.storage_values import (
    EventHandle,
    EventKind,
    ProcessingRef,
    ProjectedEventRecord,
)

SCHEMA_VERSION = 2
MAX_LINE_BYTES = 128 * 1024
# A single logical blackout is capped at the existing 64 MiB journal policy.
# Capture stops 2 MiB early so GAP/END/outcome recovery remains durable without
# sharding one blackout indefinitely.
MAX_EVENT_BYTES = 64 * 1024 * 1024
CAPTURE_APPEND_LIMIT = MAX_EVENT_BYTES - (2 * 1024 * 1024)
MAX_SNAPSHOT_BYTES = 64 * 1024
MAX_REGISTRY_BYTES = 128 * 1024
MAX_FIXED_LINE_BYTES = 4 * 1024
MAX_REASON_BYTES = 64
MAX_REASONS = 8
MAX_DIAGNOSTIC_ERROR_BYTES = 512
MAX_PENDING_PROCESSING = 8
MAX_SEGMENT_REFS = 64
EVENT_FILENAME_RE = re.compile(
    r"^evt-\d{8}T\d{6}\.\d{3}Z-(?P<blackout>[0-9a-f]{32})(?:-seg-(?P<ordinal>\d{6})-(?P<segment>[0-9a-f]{32}))?\.jsonl$"
)
PHYSICAL_RECORD_TYPES = frozenset({"start", "observation", "end"})
SYSTEM_RECORD_TYPES = frozenset({"gap"})
DERIVED_RECORD_TYPES = frozenset(
    {"assessment", "comparison", "ir_estimate", "learning_decision", "model_commit", "outcome"}
)
RECORD_TYPES = PHYSICAL_RECORD_TYPES | SYSTEM_RECORD_TYPES | DERIVED_RECORD_TYPES
EVENT_KINDS = frozenset({"blackout", "recharge"})
PROVENANCE_BY_RECORD_TYPE = {
    **dict.fromkeys(PHYSICAL_RECORD_TYPES, "physical"),
    **dict.fromkeys(SYSTEM_RECORD_TYPES, "system"),
    **dict.fromkeys(DERIVED_RECORD_TYPES, "derived"),
}
type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


@dataclass(frozen=True)
class _EnvelopeParts:
    record_type: str
    provenance: str
    blackout_id: str
    segment_id: str
    seq: int
    boot_id: str
    wall_time_utc: str
    monotonic_ns: int
    payload: Mapping[str, JsonValue]
    event_kind: EventKind = "blackout"


@dataclass(frozen=True)
class _StoredRecord(ProjectedEventRecord):
    """Private codec record retaining exact bytes for duplicate adjudication."""

    canonical_line: bytes = b""


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Return the plan's canonical UTF-8 JSON representation without newline."""
    ready = _json_ready(value)
    if not isinstance(ready, dict):
        raise EventValidationError("canonical JSON root must be an object")
    try:
        return json.dumps(
            ready, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EventValidationError(f"value is not canonical JSON: {_bounded_error(exc)}") from exc


def canonical_record_line(record: Mapping[str, Any]) -> bytes:
    """Return one canonical newline-terminated JSONL record."""
    line = canonical_json_bytes(record) + b"\n"
    if len(line) > MAX_LINE_BYTES:
        raise EventValidationError("encoded record exceeds 128 KiB")
    return line


def _json_ready(value: Any) -> JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EventValidationError("non-finite floats are forbidden")
        return value
    if isinstance(value, Enum):
        return _json_ready(value.value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise EventValidationError("naive datetimes are forbidden")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return _json_ready_composite(value)


def _json_ready_composite(value: Any) -> JsonValue:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_ready(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        ready: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise EventValidationError("JSON object keys must be strings")
            ready[key] = _json_ready(item)
        _validate_bounded_fields(ready)
        return ready
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    raise EventValidationError(f"unsupported JSON value type: {type(value).__name__}")


def _json_mapping(value: Mapping[str, Any], name: str) -> Mapping[str, JsonValue]:
    ready = _json_ready(value)
    if not isinstance(ready, dict):
        raise EventValidationError(f"{name} must be an object")
    return ready


def _validate_bounded_fields(value: Mapping[str, JsonValue]) -> None:
    reasons = value.get("reasons")
    if reasons is not None:
        if not isinstance(reasons, list) or len(reasons) > MAX_REASONS:
            raise EventValidationError("reasons must contain at most eight codes")
        for reason in reasons:
            if not isinstance(reason, str):
                raise EventValidationError("reason codes must be strings")
            _validate_short_ascii(reason, "reason code", MAX_REASON_BYTES)
    for key in ("bounded_error", "diagnostic_error", "error"):
        error = value.get(key)
        if isinstance(error, str) and len(error.encode("utf-8")) > MAX_DIAGNOSTIC_ERROR_BYTES:
            raise EventValidationError(f"{key} exceeds 512 bytes")


def _bounded_start_payload(payload: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    bounded = dict(payload)
    exceeded = False
    for key in ("frozen_model", "frozen_model_snapshot"):
        snapshot = bounded.get(key)
        if snapshot is None:
            continue
        if not isinstance(snapshot, dict):
            raise EventValidationError(f"{key} must be an object")
        encoded = canonical_json_bytes(snapshot)
        if len(encoded) <= MAX_SNAPSHOT_BYTES:
            continue
        exceeded = True
        bounded[key] = {
            "snapshot_budget_exceeded": True,
            "original_bytes": len(encoded),
        }
    if not exceeded:
        return bounded
    bounded["snapshot_budget_exceeded"] = True
    bounded["comparison_allowed"] = False
    bounded["comparison_available"] = False
    bounded["commit_allowed"] = False
    reasons = bounded.get("reasons")
    if reasons is None:
        bounded["reasons"] = ["snapshot_budget_exceeded"]
    elif isinstance(reasons, list) and "snapshot_budget_exceeded" not in reasons:
        if len(reasons) < MAX_REASONS:
            bounded["reasons"] = [*reasons, "snapshot_budget_exceeded"]
        else:
            bounded["reason_overflow"] = (
                _optional_nonnegative_int(
                    bounded.get("reason_overflow"),
                    "reason_overflow",
                    fallback=0,
                )
                + 1
            )
    _validate_bounded_fields(bounded)
    return bounded


def _validate_optional_count(value: int | None, name: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError(f"{name} must be a non-negative integer or None")


def _optional_nonnegative_int(value: JsonValue, name: str, *, fallback: int) -> int:
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EventValidationError(f"{name} must be a non-negative integer")
    return value


def _validate_record(obj: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "record_type",
        "provenance",
        "blackout_id",
        "segment_id",
        "seq",
        "boot_id",
        "wall_time_utc",
        "monotonic_ns",
        "payload",
    }
    if set(obj) not in (required, required | {"event_kind"}):
        raise EventValidationError("record envelope fields do not match schema v2")
    if obj["schema_version"] != SCHEMA_VERSION:
        raise EventValidationError("record schema version is not 2")
    record_type = obj["record_type"]
    provenance = obj["provenance"]
    if not isinstance(record_type, str) or record_type not in RECORD_TYPES:
        raise EventValidationError("unknown record type")
    if provenance != PROVENANCE_BY_RECORD_TYPE[record_type]:
        raise EventValidationError("record provenance does not match record type")
    event_kind = obj.get("event_kind", "blackout")
    if event_kind not in EVENT_KINDS:
        raise EventValidationError("event kind must be blackout or recharge")
    _validate_record_identity_and_clocks(obj)
    if not isinstance(obj["payload"], dict):
        raise EventValidationError("record payload must be an object")


def _validate_record_identity_and_clocks(obj: Mapping[str, Any]) -> None:
    _validate_uuid4_hex(obj["blackout_id"], "blackout_id")
    _validate_uuid4_hex(obj["segment_id"], "segment_id")
    seq = obj["seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0 or seq > 2**31 - 1:
        raise EventValidationError("record sequence is invalid")
    boot_id = obj["boot_id"]
    if not isinstance(boot_id, str) or not boot_id or len(boot_id.encode("utf-8")) > 4096:
        raise EventValidationError("boot ID is invalid")
    _parse_utc(obj["wall_time_utc"])
    monotonic_ns = obj["monotonic_ns"]
    if (
        isinstance(monotonic_ns, bool)
        or not isinstance(monotonic_ns, int)
        or monotonic_ns < 0
        or monotonic_ns > 2**63 - 1
    ):
        raise EventValidationError("monotonic_ns is invalid")


def _decode_record_line(line: bytes) -> _StoredRecord:
    if not line.endswith(b"\n"):
        raise EventCorruptionError("record line is not newline terminated")
    if len(line) > MAX_LINE_BYTES:
        raise EventCorruptionError("record line exceeds 128 KiB")
    try:
        obj = _strict_json_loads(line[:-1])
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EventCorruptionError("record line is not strict JSON") from exc
    if not isinstance(obj, dict):
        raise EventCorruptionError("record fields do not match schema v2")
    try:
        _validate_record(obj)
    except EventValidationError as exc:
        raise EventCorruptionError(str(exc)) from exc
    canonical_line = canonical_json_bytes(obj) + b"\n"
    if line != canonical_line:
        raise EventCorruptionError("record line is not canonical JSON")
    payload = obj["payload"]
    if not isinstance(payload, dict):
        raise EventCorruptionError("record payload is not an object")
    event_kind = obj.get("event_kind", "blackout")
    return _StoredRecord(
        obj["schema_version"],
        obj["record_type"],
        obj["provenance"],
        obj["blackout_id"],
        obj["segment_id"],
        obj["seq"],
        obj["boot_id"],
        obj["wall_time_utc"],
        obj["monotonic_ns"],
        payload,
        event_kind,
        canonical_line,
    )


def _deduplicate_and_validate_sequence(
    records: Sequence[_StoredRecord],
) -> tuple[_StoredRecord, ...]:
    by_seq: dict[int, _StoredRecord] = {}
    ordered: list[_StoredRecord] = []
    for record in records:
        existing = by_seq.get(record.seq)
        if existing is not None:
            if existing.canonical_line != record.canonical_line:
                raise EventConflictError("same sequence has differing canonical records")
            continue
        by_seq[record.seq] = record
        ordered.append(record)
    for expected_seq, record in enumerate(ordered):
        if record.seq != expected_seq:
            raise EventCorruptionError("record sequence is not contiguous")
    return tuple(ordered)


def _projected_record(record: _StoredRecord) -> ProjectedEventRecord:
    return ProjectedEventRecord(
        record.schema_version,
        record.record_type,
        record.provenance,
        record.blackout_id,
        record.segment_id,
        record.seq,
        record.boot_id,
        record.wall_time_utc,
        record.monotonic_ns,
        record.payload,
        record.event_kind,
    )


def _processing_handle(ref: ProcessingRef, last: _StoredRecord) -> EventHandle:
    return EventHandle(
        ref.blackout_id,
        last.segment_id,
        ref.final_path_token,
        last.seq + 1,
        last.event_kind,
    )


def _flatten_records[T: ProjectedEventRecord](
    prefixes: Sequence[Sequence[T]],
) -> tuple[T, ...]:
    return tuple(record for prefix in prefixes for record in prefix)


def _first_record[T: ProjectedEventRecord](
    records: Sequence[T],
    record_type: str,
) -> T | None:
    return next((record for record in records if record.record_type == record_type), None)


def _records_of_type[T: ProjectedEventRecord](
    records: Sequence[T],
    record_type: str,
) -> tuple[T, ...]:
    return tuple(record for record in records if record.record_type == record_type)


def _validate_segmented_record_order(
    prefixes: Sequence[Sequence[_StoredRecord]],
    *,
    start: _StoredRecord | None,
    end: _StoredRecord | None,
    outcome: _StoredRecord | None,
) -> None:
    records = tuple(record for prefix in prefixes for record in prefix)
    if start is None or records[0].record_type != "start":
        raise EventCorruptionError("event must begin with exactly one start record")
    _validate_segment_boundaries(prefixes)
    _validate_terminal_record_order(records, end=end, outcome=outcome)


def _validate_segment_boundaries(
    prefixes: Sequence[Sequence[_StoredRecord]],
) -> None:
    nonempty_prefixes = tuple(prefix for prefix in prefixes if prefix)
    for position, prefix in enumerate(nonempty_prefixes[1:], start=1):
        if prefix[0].record_type == "gap":
            _validate_gap_link(prefix[0], nonempty_prefixes[position - 1][-1])
            continue
        if not _is_terminal_damage_segment(
            prefix,
            is_last=position == len(nonempty_prefixes) - 1,
            preceding=nonempty_prefixes[:position],
        ):
            raise EventCorruptionError("continuation segment must begin with a gap record")
    for prefix in nonempty_prefixes:
        _validate_single_segment(prefix)


def _validate_gap_link(
    gap: _StoredRecord,
    previous: _StoredRecord,
) -> None:
    payload = gap.payload
    linked_segment = payload.get("previous_segment_id")
    if linked_segment is not None and linked_segment != previous.segment_id:
        raise EventCorruptionError("continuation GAP links the wrong previous segment")


def _is_terminal_damage_segment(
    prefix: Sequence[_StoredRecord],
    *,
    is_last: bool,
    preceding: Sequence[Sequence[_StoredRecord]],
) -> bool:
    """Recognize the outcome-only segment emitted after a recovered END."""
    if not is_last or len(prefix) != 1:
        return False
    record = prefix[0]
    return (
        record.record_type == "outcome"
        and record.payload.get("disposition") == "rejected"
        and record.payload.get("reasons") == ["capture_damaged"]
        and _has_prior_end_record(preceding)
    )


def _has_prior_end_record(prefixes: Sequence[Sequence[_StoredRecord]]) -> bool:
    return any(record.record_type == "end" for prefix in prefixes for record in prefix)


def _validate_single_segment(prefix: Sequence[_StoredRecord]) -> None:
    if len({record.segment_id for record in prefix}) != 1:
        raise EventCorruptionError("one file contains records from multiple segments")


def _validate_terminal_record_order(
    records: Sequence[_StoredRecord],
    *,
    end: _StoredRecord | None,
    outcome: _StoredRecord | None,
) -> None:
    _validate_unique_terminal_records(records)
    _validate_outcome_position(records, end=end, outcome=outcome)
    _validate_post_end_records(records, end)


def _validate_unique_terminal_records(records: Sequence[_StoredRecord]) -> None:
    _validate_terminal_record_count(
        records,
        "start",
        minimum=1,
        maximum=1,
        error_message="event contains multiple start records",
    )
    _validate_terminal_record_count(
        records,
        "end",
        minimum=0,
        maximum=1,
        error_message="event contains multiple end records",
    )
    _validate_terminal_record_count(
        records,
        "outcome",
        minimum=0,
        maximum=1,
        error_message="event contains multiple outcome records",
    )


def _validate_terminal_record_count(
    records: Sequence[_StoredRecord],
    record_type: str,
    *,
    minimum: int,
    maximum: int,
    error_message: str,
) -> None:
    count = sum(record.record_type == record_type for record in records)
    if count < minimum:
        raise EventCorruptionError(error_message)
    if count > maximum:
        raise EventCorruptionError(error_message)


def _validate_outcome_position(
    records: Sequence[_StoredRecord],
    *,
    end: _StoredRecord | None,
    outcome: _StoredRecord | None,
) -> None:
    if outcome is None:
        return
    _validate_outcome_is_last(records)
    _validate_outcome_has_end_or_is_rejected(end, outcome)


def _validate_outcome_is_last(records: Sequence[_StoredRecord]) -> None:
    if records[-1].record_type != "outcome":
        raise EventCorruptionError("records follow terminal outcome")


def _validate_outcome_has_end_or_is_rejected(
    end: _StoredRecord | None,
    outcome: _StoredRecord,
) -> None:
    if end is not None:
        return
    if outcome.payload.get("disposition") == "rejected":
        return
    raise EventCorruptionError("non-rejected outcome requires an end record")


def _validate_post_end_records(
    records: Sequence[_StoredRecord],
    end: _StoredRecord | None,
) -> None:
    if end is None:
        return
    end_position = records.index(end)
    if any(record.record_type in {"observation", "gap"} for record in records[end_position + 1 :]):
        raise EventCorruptionError("physical/system records follow end")


def _strict_json_loads(data: bytes) -> Any:
    return json.loads(
        data.decode("utf-8"),
        parse_constant=lambda value: _raise_invalid_constant(value),
    )


def _raise_invalid_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EventValidationError("wall_time_utc must be a UTC Z timestamp")
    try:
        moment = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise EventValidationError("wall_time_utc is not an ISO timestamp") from exc
    if moment.utcoffset() != timezone.utc.utcoffset(moment):
        raise EventValidationError("wall_time_utc is not UTC")
    return moment


def _validate_uuid4_hex(value: Any, name: str) -> None:
    if not isinstance(value, str) or len(value) != 32 or value.lower() != value:
        raise EventValidationError(f"{name} must be lowercase UUIDv4 hex")
    try:
        parsed = uuid.UUID(hex=value)
    except ValueError as exc:
        raise EventValidationError(f"{name} must be lowercase UUIDv4 hex") from exc
    if parsed.version != 4 or parsed.hex != value:
        raise EventValidationError(f"{name} must be lowercase UUIDv4 hex")


def _require_uuid4_hex(value: Any, name: str) -> str:
    _validate_uuid4_hex(value, name)
    if not isinstance(value, str):
        raise EventValidationError(f"{name} must be lowercase UUIDv4 hex")
    return value


def _validate_short_ascii(value: str, name: str, max_bytes: int) -> None:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise EventValidationError(f"{name} must be ASCII") from exc
    if not encoded or len(encoded) > max_bytes:
        raise EventValidationError(f"{name} exceeds its byte bound")


def _required_short_string(payload: Mapping[str, JsonValue], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise EventValidationError(f"outcome requires string {key}")
    _validate_short_ascii(value, key, 128)
    return value


def _optional_short_string(payload: Mapping[str, JsonValue], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise EventValidationError(f"{key} must be a string or null")
    _validate_short_ascii(value, key, 128)
    return value


def _optional_bool(payload: Mapping[str, JsonValue], key: str, fallback: bool) -> bool:
    value = payload.get(key, fallback)
    if not isinstance(value, bool):
        raise EventValidationError(f"{key} must be boolean")
    return value


def _optional_finite_nonnegative(payload: Mapping[str, JsonValue], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise EventValidationError(f"{key} must be a finite non-negative number")
    return float(value)


def _bounded_error(error: BaseException | str) -> str:
    text = str(error).replace("\n", " ")
    if not text and isinstance(error, BaseException):
        text = error.__class__.__name__
    return text.encode("utf-8")[:MAX_DIAGNOSTIC_ERROR_BYTES].decode("utf-8", errors="ignore")


def _wall_time_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_path_token(value: str) -> None:
    if (
        not value
        or Path(value).name != value
        or not value.startswith("evt-")
        or not value.endswith(".jsonl")
        or "\x00" in value
    ):
        raise EventPathError("event path token is unsafe")
