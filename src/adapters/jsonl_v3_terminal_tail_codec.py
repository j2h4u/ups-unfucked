"""Strict v3 codecs for terminal anchors and blackout ends."""

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
from src.domain.blackout_terminal import (
    BlackoutEnd,
    BlackoutTermination,
    BudgetKind,
    ContinuationKind,
)
from src.domain.fragments import (
    AnchorKind,
    AnchorProvenance,
    EndpointAnchor,
    ObservationOrigin,
)

ENDPOINT_ANCHOR_RECORD_TYPE = "endpoint_anchor"
BLACKOUT_END_RECORD_TYPE = "blackout_end"
PHYSICAL_PROVENANCE = frozenset(item.value for item in AnchorProvenance)
TERMINAL_ANCHOR_ROLE = "terminal"
INTERMEDIATE_ANCHOR_ROLE = "intermediate"
TERMINAL_TAIL_MAX_LINE_BYTES = 2 * 1024
ENDPOINT_ANCHOR_MAX_LINE_BYTES = TERMINAL_TAIL_MAX_LINE_BYTES
BLACKOUT_END_MAX_LINE_BYTES = TERMINAL_TAIL_MAX_LINE_BYTES
ENDPOINT_ANCHOR_SCHEMA = "endpoint-anchor-v1"
BLACKOUT_END_SCHEMA = "blackout-end-v1"

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_UTC_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z\Z")
_ANCHOR_ROLE_BY_KIND = {
    AnchorKind.TRANSFER_TO_BATTERY: INTERMEDIATE_ANCHOR_ROLE,
    AnchorKind.RAW_FIRMWARE_LB: INTERMEDIATE_ANCHOR_ROLE,
    AnchorKind.MODELED_SAFE_SHUTDOWN: TERMINAL_ANCHOR_ROLE,
    AnchorKind.POWER_RESTORED: TERMINAL_ANCHOR_ROLE,
    AnchorKind.SERVICE_STOP: TERMINAL_ANCHOR_ROLE,
    AnchorKind.BOOT_BOUNDARY: TERMINAL_ANCHOR_ROLE,
    AnchorKind.CHARGE_STABILIZED: TERMINAL_ANCHOR_ROLE,
    AnchorKind.GAP: TERMINAL_ANCHOR_ROLE,
    AnchorKind.CORRUPTION: TERMINAL_ANCHOR_ROLE,
}
_INTERMEDIATE_ANCHOR_KINDS = frozenset(
    {
        AnchorKind.TRANSFER_TO_BATTERY,
        AnchorKind.RAW_FIRMWARE_LB,
    }
)
_ANCHOR_FIELDS = frozenset(
    {
        "schema",
        "anchor_role",
        "canonical_hash",
        "kind",
        "provenance",
        "boot_id",
        "wall_time_utc",
        "monotonic_ns",
        "source_sample_hash",
        "blackout_id",
        "physical_episode_id",
        "segment_id",
    }
)
_END_FIELDS = frozenset(
    {
        "schema",
        "blackout_id",
        "physical_episode_id",
        "battery_epoch_id",
        "segment_id",
        "termination",
        "terminal_anchor_record_hash",
        "observation_origin",
        "wall_time_utc",
        "monotonic_ns",
        "boot_id",
        "budget_kind",
        "continued_by",
        "continuation_kind",
        "uat_intent_id",
    }
)


def encode_endpoint_anchor(
    anchor: EndpointAnchor,
    *,
    seq: int = 0,
    previous_record_sha256: str | None = None,
) -> EncodedV3Record:
    """Encode one intermediate- or terminal-role endpoint anchor in the reserved tail."""
    _validate_terminal_anchor(anchor)
    payload = _anchor_payload(anchor)
    envelope = V3RecordEnvelope(
        schema_version=3,
        record_type=ENDPOINT_ANCHOR_RECORD_TYPE,
        provenance=anchor.provenance.value,
        blackout_id=anchor.blackout_id,
        segment_id=anchor.segment_id,
        seq=seq,
        boot_id=anchor.boot_id,
        wall_time_utc=_utc(anchor.wall_time_utc),
        monotonic_ns=anchor.monotonic_ns,
        prev_record_sha256=previous_record_sha256,
        payload=payload,
    )
    _validate_anchor_chain(envelope, anchor.kind)
    return _encode_bounded(envelope, ENDPOINT_ANCHOR_MAX_LINE_BYTES, "endpoint anchor")


def decode_endpoint_anchor(line: bytes) -> EndpointAnchor:
    """Decode one intermediate- or terminal-role anchor and reconstruct its value."""
    record = _decode_bounded(line, ENDPOINT_ANCHOR_MAX_LINE_BYTES, "endpoint anchor")
    envelope = record.envelope
    if envelope.record_type != ENDPOINT_ANCHOR_RECORD_TYPE:
        raise V3CodecError("record is not an endpoint anchor")
    payload = _exact_payload(envelope.payload, _ANCHOR_FIELDS, "endpoint anchor")
    try:
        anchor = _anchor_from_payload(payload)
        _validate_anchor_envelope(envelope, anchor)
        _validate_anchor_chain(envelope, anchor.kind)
    except (TypeError, ValueError, KeyError) as exc:
        raise V3CodecError("endpoint anchor payload is invalid") from exc
    return anchor


def decode_endpoint_anchor_record(line: bytes) -> EncodedV3Record:
    """Strictly decode and validate an intermediate or terminal endpoint record."""
    record = _decode_bounded(line, ENDPOINT_ANCHOR_MAX_LINE_BYTES, "endpoint anchor")
    decode_endpoint_anchor(record.line)
    return record


def encode_blackout_end(
    value: BlackoutEnd,
    *,
    seq: int = 0,
    previous_record_sha256: str | None = None,
) -> EncodedV3Record:
    """Encode one domain-validated blackout terminal boundary."""
    if not isinstance(value, BlackoutEnd):
        raise V3CodecError("blackout end must be BlackoutEnd")
    payload = _end_payload(value)
    envelope = V3RecordEnvelope(
        schema_version=3,
        record_type=BLACKOUT_END_RECORD_TYPE,
        provenance=AnchorProvenance.OPERATIONAL.value,
        blackout_id=value.blackout_id,
        segment_id=value.segment_id,
        seq=seq,
        boot_id=value.boot_id,
        wall_time_utc=_utc(value.wall_time_utc),
        monotonic_ns=value.monotonic_ns,
        prev_record_sha256=previous_record_sha256,
        payload=payload,
    )
    if value.terminal_anchor_record_hash is None:
        if envelope.seq == 0 and envelope.prev_record_sha256 is not None:
            raise V3CodecError("anchorless budget END root must not carry a previous hash")
        if envelope.seq > 0 and envelope.prev_record_sha256 is None:
            raise V3CodecError("linked anchorless budget END must carry a previous hash")
    elif (
        envelope.seq == 0
        or envelope.prev_record_sha256 is None
        or envelope.prev_record_sha256 != value.terminal_anchor_record_hash
    ):
        raise V3CodecError("linked blackout end must follow its terminal anchor record")
    return _encode_bounded(envelope, BLACKOUT_END_MAX_LINE_BYTES, "blackout end")


def decode_blackout_end(
    line: bytes,
    *,
    terminal_anchor: EndpointAnchor | None = None,
    terminal_anchor_record: EncodedV3Record | bytes | None = None,
) -> BlackoutEnd:
    """Decode one blackout end and reconstruct all terminal domain invariants."""
    record = _decode_bounded(line, BLACKOUT_END_MAX_LINE_BYTES, "blackout end")
    envelope = record.envelope
    if envelope.record_type != BLACKOUT_END_RECORD_TYPE:
        raise V3CodecError("record is not a blackout end")
    payload = _exact_payload(envelope.payload, _END_FIELDS, "blackout end")
    try:
        if payload["schema"] != BLACKOUT_END_SCHEMA:
            raise ValueError("unsupported blackout end schema")
        value = BlackoutEnd(
            blackout_id=_text(payload["blackout_id"], "blackout ID"),
            physical_episode_id=_text(payload["physical_episode_id"], "physical episode ID"),
            battery_epoch_id=_text(payload["battery_epoch_id"], "battery epoch ID"),
            segment_id=_text(payload["segment_id"], "segment ID"),
            termination=_enum(payload["termination"], BlackoutTermination, "termination"),
            observation_origin=_enum(
                payload["observation_origin"], ObservationOrigin, "observation origin"
            ),
            wall_time_utc=_parse_utc(payload["wall_time_utc"]),
            monotonic_ns=_nonnegative(payload["monotonic_ns"], "monotonic time"),
            boot_id=_text(payload["boot_id"], "boot ID"),
            terminal_anchor_record_hash=_hash(
                payload["terminal_anchor_record_hash"], "terminal anchor record hash"
            )
            if payload["terminal_anchor_record_hash"] is not None
            else None,
            budget_kind=_optional_enum(payload["budget_kind"], BudgetKind, "budget kind"),
            continued_by=_optional_text(payload["continued_by"], "continued-by blackout ID"),
            continuation_kind=_optional_enum(
                payload["continuation_kind"], ContinuationKind, "continuation kind"
            ),
            uat_intent_id=_optional_text(payload["uat_intent_id"], "UAT intent ID"),
        )
        supplied_anchor, supplied_record_hash, supplied_record_seq = _supplied_terminal_anchor(
            terminal_anchor, terminal_anchor_record
        )
        _validate_end_semantics(value, supplied_anchor, supplied_record_hash)
        _validate_end_chain(envelope, value, supplied_record_hash, supplied_record_seq)
        _validate_end_envelope(envelope, value)
    except (TypeError, ValueError, KeyError) as exc:
        raise V3CodecError("blackout end payload is invalid") from exc
    return value


def decode_blackout_end_record(
    line: bytes,
    *,
    terminal_anchor_record: EncodedV3Record | bytes | None = None,
) -> EncodedV3Record:
    """Strictly decode an end and bind it to its actual endpoint record."""
    record = _decode_bounded(line, BLACKOUT_END_MAX_LINE_BYTES, "blackout end")
    if terminal_anchor_record is None:
        decode_blackout_end(record.line)
        return record
    endpoint = (
        terminal_anchor_record
        if isinstance(terminal_anchor_record, EncodedV3Record)
        else decode_endpoint_anchor_record(terminal_anchor_record)
    )
    if decode_endpoint_anchor_record(endpoint.line) != endpoint:
        raise V3CodecError("terminal endpoint record is not canonical")
    decode_blackout_end(record.line, terminal_anchor_record=endpoint)
    return record


def _validate_terminal_anchor(anchor: EndpointAnchor) -> None:
    if not isinstance(anchor, EndpointAnchor):
        raise V3CodecError("endpoint anchor must be EndpointAnchor")
    if anchor.kind not in _ANCHOR_ROLE_BY_KIND:
        raise V3CodecError("endpoint anchor kind is not closed")


def _validate_end_semantics(
    value: BlackoutEnd,
    terminal_anchor: EndpointAnchor | None = None,
    terminal_anchor_record_hash: str | None = None,
) -> None:
    required_kinds = {
        BlackoutTermination.POWER_RESTORED: AnchorKind.POWER_RESTORED,
        BlackoutTermination.SERVICE_STOP: AnchorKind.SERVICE_STOP,
        BlackoutTermination.CLOSED_RESTART_GAP: AnchorKind.BOOT_BOUNDARY,
        BlackoutTermination.SAFE_SHUTDOWN_RESTARTED: AnchorKind.MODELED_SAFE_SHUTDOWN,
    }
    damaged_kinds = {AnchorKind.GAP, AnchorKind.CORRUPTION}
    if value.termination is BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED:
        if terminal_anchor is not None:
            if terminal_anchor_record_hash is None:
                raise ValueError("budget rollover must not carry a decoded terminal anchor")
            if terminal_anchor.kind in _INTERMEDIATE_ANCHOR_KINDS:
                raise ValueError("budget END must follow a terminal-role record")
        return
    if terminal_anchor is None or terminal_anchor_record_hash is None:
        raise ValueError("decoded terminal anchor record must be supplied for this termination")
    expected = required_kinds.get(value.termination)
    if expected is not None and terminal_anchor.kind is not expected:
        raise ValueError("termination does not match terminal anchor kind")
    if value.termination is BlackoutTermination.CAPTURE_DAMAGED:
        if terminal_anchor.kind not in damaged_kinds:
            raise ValueError("capture damage requires a gap or corruption anchor")
    if terminal_anchor_record_hash is not None and (
        value.terminal_anchor_record_hash != terminal_anchor_record_hash
    ):
        raise ValueError("terminal anchor record hash is not linked")
    _validate_anchor_scope(value, terminal_anchor)


def _validate_end_chain(
    envelope: V3RecordEnvelope,
    value: BlackoutEnd,
    anchor_record_hash: str | None,
    anchor_record_seq: int | None,
) -> None:
    if value.termination is BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED:
        _validate_budget_end_chain(envelope, anchor_record_hash, anchor_record_seq)
        return
    if anchor_record_hash is None:
        raise ValueError("decoded terminal anchor record must be supplied for this termination")
    if envelope.prev_record_sha256 != anchor_record_hash:
        raise ValueError("blackout end must immediately follow its terminal anchor record")
    if anchor_record_seq is None or envelope.seq != anchor_record_seq + 1:
        raise ValueError("blackout end sequence must follow its terminal anchor record")


def _validate_budget_end_chain(
    envelope: V3RecordEnvelope,
    anchor_record_hash: str | None,
    anchor_record_seq: int | None,
) -> None:
    if envelope.seq == 0 and envelope.prev_record_sha256 is not None:
        raise ValueError("anchorless budget END root must not carry a previous hash")
    if envelope.seq > 0 and envelope.prev_record_sha256 is None:
        raise ValueError("linked anchorless budget END must carry a previous hash")
    if anchor_record_hash is None:
        return
    if envelope.prev_record_sha256 != anchor_record_hash:
        raise ValueError("budget END does not follow its terminal cursor")
    if anchor_record_seq is None or envelope.seq != anchor_record_seq + 1:
        raise ValueError("budget END sequence must follow its terminal cursor")


def _anchor_payload(anchor: EndpointAnchor) -> dict[str, Any]:
    return {
        "schema": ENDPOINT_ANCHOR_SCHEMA,
        "anchor_role": _ANCHOR_ROLE_BY_KIND[anchor.kind],
        "canonical_hash": anchor.canonical_hash,
        "kind": anchor.kind.value,
        "provenance": anchor.provenance.value,
        "boot_id": anchor.boot_id,
        "wall_time_utc": _utc(anchor.wall_time_utc),
        "monotonic_ns": anchor.monotonic_ns,
        "source_sample_hash": anchor.source_sample_hash,
        "blackout_id": anchor.blackout_id,
        "physical_episode_id": anchor.physical_episode_id,
        "segment_id": anchor.segment_id,
    }


def _anchor_from_payload(payload: Mapping[str, Any]) -> EndpointAnchor:
    if payload["schema"] != ENDPOINT_ANCHOR_SCHEMA:
        raise ValueError("unsupported endpoint anchor schema")
    kind = _enum(payload["kind"], AnchorKind, "anchor kind")
    if payload["anchor_role"] != _ANCHOR_ROLE_BY_KIND.get(kind):
        raise ValueError("endpoint anchor role does not match kind")
    anchor = EndpointAnchor(
        canonical_hash=_hash(payload["canonical_hash"], "anchor canonical hash"),
        kind=kind,
        provenance=_enum(payload["provenance"], AnchorProvenance, "anchor provenance"),
        boot_id=_text(payload["boot_id"], "anchor boot ID"),
        wall_time_utc=_parse_utc(payload["wall_time_utc"]),
        monotonic_ns=_nonnegative(payload["monotonic_ns"], "anchor monotonic time"),
        source_sample_hash=_optional_hash(payload["source_sample_hash"], "source sample hash"),
        blackout_id=_text(payload["blackout_id"], "anchor blackout ID"),
        physical_episode_id=_text(payload["physical_episode_id"], "anchor physical-episode ID"),
        segment_id=_text(payload["segment_id"], "anchor segment ID"),
    )
    _validate_terminal_anchor(anchor)
    return anchor


def _end_payload(value: BlackoutEnd) -> dict[str, Any]:
    return {
        "schema": BLACKOUT_END_SCHEMA,
        "blackout_id": value.blackout_id,
        "physical_episode_id": value.physical_episode_id,
        "battery_epoch_id": value.battery_epoch_id,
        "segment_id": value.segment_id,
        "termination": value.termination.value,
        "terminal_anchor_record_hash": value.terminal_anchor_record_hash,
        "observation_origin": value.observation_origin.value,
        "wall_time_utc": _utc(value.wall_time_utc),
        "monotonic_ns": value.monotonic_ns,
        "boot_id": value.boot_id,
        "budget_kind": None if value.budget_kind is None else value.budget_kind.value,
        "continued_by": value.continued_by,
        "continuation_kind": (
            None if value.continuation_kind is None else value.continuation_kind.value
        ),
        "uat_intent_id": value.uat_intent_id,
    }


def _validate_anchor_envelope(envelope: V3RecordEnvelope, anchor: EndpointAnchor) -> None:
    if envelope.provenance != anchor.provenance.value:
        raise ValueError("anchor envelope provenance is not bound")
    if (
        envelope.blackout_id != anchor.blackout_id
        or envelope.segment_id != anchor.segment_id
        or envelope.boot_id != anchor.boot_id
        or envelope.wall_time_utc != _utc(anchor.wall_time_utc)
        or envelope.monotonic_ns != anchor.monotonic_ns
    ):
        raise ValueError("anchor envelope scope is not bound")


def _validate_anchor_chain(envelope: V3RecordEnvelope, kind: AnchorKind) -> None:
    if kind in _INTERMEDIATE_ANCHOR_KINDS:
        if envelope.seq == 0 or envelope.prev_record_sha256 is None:
            raise V3CodecError("intermediate anchor must follow a prior physical record")
        return
    if envelope.seq == 0 and envelope.prev_record_sha256 is not None:
        raise V3CodecError("terminal root anchor must not carry a previous hash")
    if envelope.seq > 0 and envelope.prev_record_sha256 is None:
        raise V3CodecError("non-root anchor must carry its previous hash")


def _validate_end_envelope(envelope: V3RecordEnvelope, value: BlackoutEnd) -> None:
    if envelope.provenance != AnchorProvenance.OPERATIONAL.value:
        raise ValueError("blackout end envelope provenance is not bound")
    if (
        envelope.blackout_id != value.blackout_id
        or envelope.segment_id != value.segment_id
        or envelope.boot_id != value.boot_id
        or envelope.wall_time_utc != _utc(value.wall_time_utc)
        or envelope.monotonic_ns != value.monotonic_ns
    ):
        raise ValueError("blackout end envelope scope is not bound")


def _supplied_terminal_anchor(
    anchor: EndpointAnchor | None,
    record: EncodedV3Record | bytes | None,
) -> tuple[EndpointAnchor | None, str | None, int | None]:
    if record is None:
        if anchor is not None:
            raise ValueError("terminal anchor requires its decoded endpoint record")
        return anchor, None, None
    if isinstance(record, bytes):
        encoded = _decode_bounded(record, ENDPOINT_ANCHOR_MAX_LINE_BYTES, "terminal anchor")
    elif isinstance(record, EncodedV3Record):
        encoded = record
    else:
        raise TypeError("terminal anchor record must be encoded v3 bytes or record")
    decoded_record = _decode_bounded(
        encoded.line, ENDPOINT_ANCHOR_MAX_LINE_BYTES, "terminal anchor"
    )
    if decoded_record != encoded:
        raise ValueError("terminal anchor record is not its canonical decoded value")
    decoded = decode_endpoint_anchor(decoded_record.line)
    if anchor is not None and anchor != decoded:
        raise ValueError("supplied terminal anchors differ")
    return decoded, encoded.record_sha256, encoded.envelope.seq


def _validate_anchor_scope(value: BlackoutEnd, anchor: EndpointAnchor) -> None:
    anchor_scope = (
        anchor.blackout_id,
        anchor.physical_episode_id,
        anchor.segment_id,
        anchor.boot_id,
        anchor.wall_time_utc,
        anchor.monotonic_ns,
    )
    end_scope = (
        value.blackout_id,
        value.physical_episode_id,
        value.segment_id,
        value.boot_id,
        value.wall_time_utc,
        value.monotonic_ns,
    )
    if anchor_scope != end_scope:
        raise ValueError("terminal anchor scope does not match blackout end")


def _encode_bounded(envelope: V3RecordEnvelope, maximum: int, name: str) -> EncodedV3Record:
    try:
        encoded = encode_v3_record(envelope)
    except (TypeError, ValueError) as exc:
        raise V3CodecError(f"{name} cannot be canonically encoded") from exc
    if len(encoded.line) > maximum:
        raise V3CodecError(f"{name} exceeds {maximum} bytes")
    return encoded


def _decode_bounded(line: bytes, maximum: int, name: str) -> EncodedV3Record:
    if not isinstance(line, bytes) or len(line) > maximum:
        raise V3CodecError(f"{name} exceeds {maximum} bytes")
    try:
        return decode_v3_record(line)
    except (TypeError, ValueError) as exc:
        raise V3CodecError(f"invalid {name} envelope") from exc


def _exact_payload(value: Any, fields: frozenset[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise V3CodecError(f"{name} fields are not exact")
    return value


def _enum(value: Any, enum_type: type[Any], name: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not closed") from exc


def _optional_enum(value: Any, enum_type: type[Any], name: str) -> Any:
    return None if value is None else _enum(value, enum_type, name)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 128:
        raise ValueError(f"{name} must be a bounded non-empty ID")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _optional_text(value: Any, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be lowercase SHA-256")
    return value


def _optional_hash(value: Any, name: str) -> str | None:
    return None if value is None else _hash(value, name)


def _nonnegative(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be nonnegative integer")
    return value


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        raise ValueError("wall time must be canonical UTC text")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("wall time must be UTC")
    return parsed


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
