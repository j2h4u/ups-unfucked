"""Strict schema-3 codec for bounded blackout loss receipts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from src.adapters.jsonl_v3_canonical import (
    EncodedV3Record,
    V3CodecError,
    V3RecordEnvelope,
    decode_v3_record,
    encode_v3_record,
)
from src.domain.blackout_capture import (
    DischargeGap,
    DischargeGapReason,
    GapSubreasonCount,
)
from src.domain.fragments import ObservationOrigin

DISCHARGE_GAP_RECORD_TYPE = "discharge_gap"
DISCHARGE_GAP_SCHEMA = "discharge_gap-v1"
DISCHARGE_GAP_PROVENANCE = "system"
DISCHARGE_GAP_MAX_LINE_BYTES = 20 * 1024

_UTC_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_BOUNDARY_KINDS = frozenset(
    {"power_restored", "modeled_safe_shutdown", "service_stop", "boot_boundary"}
)
_FIELDS = frozenset(
    {
        "schema",
        "blackout_id",
        "physical_episode_id",
        "battery_epoch_id",
        "segment_id",
        "observation_origin",
        "uat_intent_id",
        "reason",
        "count",
        "first_boot_id",
        "last_boot_id",
        "first_monotonic_ns",
        "last_monotonic_ns",
        "receipt_boot_id",
        "receipt_monotonic_ns",
        "receipt_wall_time_utc",
        "first_wall_time_utc",
        "last_wall_time_utc",
        "failed_command",
        "error_type",
        "loss_terminal_boundary_kind",
        "loss_terminal_boundary_wall_time_utc",
        "subreason_counts",
    }
)


def encode_discharge_gap(
    value: DischargeGap,
    *,
    seq: int = 0,
    previous_record_sha256: str | None = None,
) -> EncodedV3Record:
    """Encode one complete, bounded loss receipt without inventing samples."""
    if not isinstance(value, DischargeGap):
        raise TypeError("discharge gap codec requires DischargeGap")
    record = encode_v3_record(
        V3RecordEnvelope(
            3,
            DISCHARGE_GAP_RECORD_TYPE,
            DISCHARGE_GAP_PROVENANCE,
            value.blackout_id,
            value.segment_id,
            seq,
            value.receipt_boot_id,
            _utc_text(value.receipt_wall_time_utc),
            value.receipt_monotonic_ns,
            previous_record_sha256,
            _payload(value),
        )
    )
    if len(record.line) > DISCHARGE_GAP_MAX_LINE_BYTES:
        raise V3CodecError("discharge gap exceeds 20 KiB")
    return record


def decode_discharge_gap(line: bytes) -> DischargeGap:
    """Decode and strictly reconstruct one bounded loss receipt."""
    record = decode_discharge_gap_record(line)
    payload = _payload_exact(record.envelope.payload)
    try:
        value = DischargeGap(
            blackout_id=_text(payload["blackout_id"], "blackout ID"),
            physical_episode_id=_text(payload["physical_episode_id"], "physical episode ID"),
            battery_epoch_id=_text(payload["battery_epoch_id"], "battery epoch ID"),
            segment_id=_text(payload["segment_id"], "segment ID"),
            observation_origin=_enum(payload["observation_origin"], ObservationOrigin),
            reason=_enum(payload["reason"], DischargeGapReason),
            count=_positive_int(payload["count"], "gap count"),
            first_boot_id=_text(payload["first_boot_id"], "first boot ID"),
            last_boot_id=_text(payload["last_boot_id"], "last boot ID"),
            first_monotonic_ns=_nonnegative_int(
                payload["first_monotonic_ns"], "first monotonic time"
            ),
            last_monotonic_ns=_nonnegative_int(payload["last_monotonic_ns"], "last monotonic time"),
            receipt_boot_id=_text(payload["receipt_boot_id"], "receipt boot ID"),
            receipt_monotonic_ns=_nonnegative_int(
                payload["receipt_monotonic_ns"], "receipt monotonic time"
            ),
            receipt_wall_time_utc=_parse_factual_utc(payload["receipt_wall_time_utc"]),
            first_wall_time_utc=_optional_utc(payload["first_wall_time_utc"]),
            last_wall_time_utc=_optional_utc(payload["last_wall_time_utc"]),
            failed_command=_optional_text(payload["failed_command"], "failed command"),
            error_type=_optional_text(payload["error_type"], "error type"),
            loss_terminal_boundary_kind=_optional_boundary(payload["loss_terminal_boundary_kind"]),
            loss_terminal_boundary_wall_time_utc=_optional_factual_utc(
                payload["loss_terminal_boundary_wall_time_utc"]
            ),
            uat_intent_id=_optional_text(payload["uat_intent_id"], "UAT intent ID"),
            subreason_counts=_subreason_counts(payload["subreason_counts"]),
        )
        _validate_envelope(record.envelope, value)
    except (KeyError, TypeError, ValueError) as exc:
        raise V3CodecError("discharge gap payload is invalid") from exc
    return value


def decode_discharge_gap_record(line: bytes) -> EncodedV3Record:
    """Decode only a canonical physical loss receipt envelope."""
    if not isinstance(line, bytes) or len(line) > DISCHARGE_GAP_MAX_LINE_BYTES:
        raise V3CodecError("discharge gap exceeds 20 KiB")
    try:
        record = decode_v3_record(line)
    except (TypeError, ValueError) as exc:
        raise V3CodecError("invalid discharge gap envelope") from exc
    if record.envelope.record_type != DISCHARGE_GAP_RECORD_TYPE:
        raise V3CodecError("record is not a discharge gap")
    if record.envelope.provenance != DISCHARGE_GAP_PROVENANCE:
        raise V3CodecError("discharge gap provenance is not system")
    payload = _payload_exact(record.envelope.payload)
    if payload["schema"] != DISCHARGE_GAP_SCHEMA:
        raise V3CodecError("unsupported discharge gap schema")
    return record


def _payload(value: DischargeGap) -> dict[str, Any]:
    return {
        "schema": DISCHARGE_GAP_SCHEMA,
        "blackout_id": value.blackout_id,
        "physical_episode_id": value.physical_episode_id,
        "battery_epoch_id": value.battery_epoch_id,
        "segment_id": value.segment_id,
        "observation_origin": value.observation_origin.value,
        "uat_intent_id": value.uat_intent_id,
        "reason": value.reason.value,
        "count": value.count,
        "first_boot_id": value.first_boot_id,
        "last_boot_id": value.last_boot_id,
        "first_monotonic_ns": value.first_monotonic_ns,
        "last_monotonic_ns": value.last_monotonic_ns,
        "receipt_boot_id": value.receipt_boot_id,
        "receipt_monotonic_ns": value.receipt_monotonic_ns,
        "receipt_wall_time_utc": _utc_text(value.receipt_wall_time_utc),
        "first_wall_time_utc": _optional_utc_text(value.first_wall_time_utc),
        "last_wall_time_utc": _optional_utc_text(value.last_wall_time_utc),
        "failed_command": value.failed_command,
        "error_type": value.error_type,
        "loss_terminal_boundary_kind": value.loss_terminal_boundary_kind,
        "loss_terminal_boundary_wall_time_utc": _optional_utc_text(
            value.loss_terminal_boundary_wall_time_utc
        ),
        "subreason_counts": [
            {"reason": item.reason.value, "count": item.count} for item in value.subreason_counts
        ],
    }


def _payload_exact(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise V3CodecError("discharge gap fields are not exact")
    return value


def _validate_envelope(envelope: V3RecordEnvelope, value: DischargeGap) -> None:
    if (
        envelope.blackout_id != value.blackout_id
        or envelope.segment_id != value.segment_id
        or envelope.boot_id != value.receipt_boot_id
        or envelope.monotonic_ns != value.receipt_monotonic_ns
        or envelope.wall_time_utc != _utc_text(value.receipt_wall_time_utc)
    ):
        raise ValueError("discharge gap envelope scope is not bound")


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 128:
        raise ValueError(f"{field} must be bounded text")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} contains a control character")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _positive_int(value: Any, field: str) -> int:
    result = _nonnegative_int(value, field)
    if result == 0:
        raise ValueError(f"{field} must be positive")
    return result


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _enum(value: Any, enum_type: type[Any]) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("value is not a closed enum") from exc


def _optional_boundary(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _BOUNDARY_KINDS:
        raise ValueError("loss terminal boundary kind is not closed")
    return value


def _optional_utc(value: Any) -> datetime | None:
    return None if value is None else _parse_utc(value)


def _parse_factual_utc(value: Any) -> datetime:
    parsed = _parse_utc(value)
    if parsed.year == 1970:
        raise ValueError("factual UTC must not use epoch sentinel")
    return parsed


def _optional_factual_utc(value: Any) -> datetime | None:
    return None if value is None else _parse_factual_utc(value)


def _optional_utc_text(value: datetime | None) -> str | None:
    return None if value is None else _utc_text(value)


def _subreason_counts(value: Any) -> tuple[GapSubreasonCount, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("subreason counts must be a non-empty list")
    result = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"reason", "count"}:
            raise ValueError("subreason count fields are not exact")
        result.append(
            GapSubreasonCount(
                _enum(item["reason"], DischargeGapReason),
                _nonnegative_int(item["count"], "subreason count"),
            )
        )
    return tuple(result)


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        raise ValueError("timestamp is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("timestamp is not a valid UTC timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamp is not UTC")
    return parsed


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
