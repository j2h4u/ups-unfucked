"""Strict v3 codec for the pure IR learning decision record."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from math import isfinite
from typing import Any

from src.adapters.jsonl_v3_canonical import (
    EncodedV3Record,
    V3CodecError,
    V3RecordEnvelope,
    decode_v3_record,
    encode_v3_record,
)
from src.domain.ir_learning_decision import (
    IrLearningDecision,
    IrLearningDisposition,
)
from src.domain.learning import ObservedLoadSagIncrease
from src.domain.reasons import LearningReason
from src.domain.values import ModelChange

LEARNING_DECISION_RECORD_TYPE = "learning_decision"
LEARNING_DECISION_PROVENANCE = "derived"
LEARNING_DECISION_SCHEMA = "ir-learning-decision-v1"
LEARNING_DECISION_MAX_LINE_BYTES = 8 * 1024
MAX_LEARNING_DECISION_LINE_BYTES = LEARNING_DECISION_MAX_LINE_BYTES
MAX_EVIDENCE_HASHES = 64
MAX_LEARNING_REASONS = 8

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_UTC_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z\Z")
_PAYLOAD_FIELDS = frozenset(
    {
        "schema",
        "blackout_id",
        "segment_id",
        "boot_id",
        "wall_time_utc",
        "monotonic_ns",
        "disposition",
        "evidence_hashes",
        "evidence_set_id",
        "reasons",
        "change",
        "observed_load_sag_increase",
    }
)
_CHANGE_FIELDS = frozenset(
    {
        "parameter",
        "value_before",
        "measured_estimate",
        "value_after",
        "bound_applied",
    }
)
_OBSERVATION_FIELDS = frozenset(
    {
        "parameter",
        "value_before",
        "measured_estimate",
        "evidence_set_id",
    }
)


def encode_learning_decision(
    decision: IrLearningDecision,
    **options: object,
) -> EncodedV3Record:
    """Encode one fully validated decision with an explicit terminal scope."""
    scope = _encode_scope(options)
    if not isinstance(decision, IrLearningDecision):
        raise V3CodecError("learning decision must be IrLearningDecision")
    _validate_decision_bound(decision)
    payload = _decision_payload(decision, scope)
    envelope = V3RecordEnvelope(
        schema_version=3,
        record_type=LEARNING_DECISION_RECORD_TYPE,
        provenance=LEARNING_DECISION_PROVENANCE,
        blackout_id=scope[0],
        segment_id=scope[1],
        seq=scope[5],
        boot_id=scope[2],
        wall_time_utc=_utc(scope[3]),
        monotonic_ns=scope[4],
        prev_record_sha256=scope[6],
        payload=payload,
    )
    try:
        encoded = encode_v3_record(envelope)
    except (TypeError, ValueError) as exc:
        raise V3CodecError("learning decision cannot be canonically encoded") from exc
    if len(encoded.line) > LEARNING_DECISION_MAX_LINE_BYTES:
        raise V3CodecError("learning decision exceeds 8 KiB")
    return encoded


def decode_learning_decision(line: bytes) -> IrLearningDecision:
    """Decode and reconstruct one pure learning decision from canonical bytes."""
    if not isinstance(line, bytes) or len(line) > LEARNING_DECISION_MAX_LINE_BYTES:
        raise V3CodecError("learning decision exceeds 8 KiB")
    try:
        record = decode_v3_record(line)
    except (TypeError, ValueError) as exc:
        raise V3CodecError("invalid learning decision envelope") from exc
    envelope = record.envelope
    if (
        envelope.record_type != LEARNING_DECISION_RECORD_TYPE
        or envelope.provenance != LEARNING_DECISION_PROVENANCE
    ):
        raise V3CodecError("record is not a derived learning decision")
    payload = _exact_payload(envelope.payload, _PAYLOAD_FIELDS, "learning decision")
    try:
        _validate_scope_payload(envelope, payload)
        decision = _decision_from_payload(payload)
        _validate_decision_bound(decision)
    except (TypeError, ValueError, KeyError) as exc:
        raise V3CodecError("learning decision payload is invalid") from exc
    return decision


def decode_learning_decision_record(line: bytes) -> EncodedV3Record:
    """Strictly decode a linked decision record after domain reconstruction."""
    decode_learning_decision(line)
    record = decode_v3_record(line)
    if record.envelope.record_type != LEARNING_DECISION_RECORD_TYPE:
        raise V3CodecError("record is not a learning decision")
    return record


def _encode_scope(
    options: dict[str, object],
) -> tuple[str, str, str, datetime, int, int, str | None]:
    blackout_id = _pop_text(options, "blackout_id")
    segment_id = _pop_text(options, "segment_id")
    boot_id = _pop_text(options, "boot_id")
    wall_time_utc = options.pop("wall_time_utc", None)
    if not isinstance(wall_time_utc, datetime) or wall_time_utc.tzinfo is None:
        raise V3CodecError("wall_time_utc must be timezone-aware UTC datetime")
    if wall_time_utc.utcoffset() != timezone.utc.utcoffset(wall_time_utc):
        raise V3CodecError("wall_time_utc must be UTC")
    monotonic_ns = _pop_nonnegative(options, "monotonic_ns")
    seq = options.pop("seq", 0)
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise V3CodecError("seq must be a nonnegative integer")
    previous = options.pop("previous_record_sha256", None)
    if previous is not None and (
        not isinstance(previous, str) or _HASH_RE.fullmatch(previous) is None
    ):
        raise V3CodecError("previous_record_sha256 must be lowercase SHA-256 or None")
    if options:
        raise V3CodecError(f"unknown learning decision options: {tuple(options)}")
    return blackout_id, segment_id, boot_id, wall_time_utc, monotonic_ns, seq, previous


def _decision_payload(
    decision: IrLearningDecision,
    scope: tuple[str, str, str, datetime, int, int, str | None],
) -> dict[str, Any]:
    return {
        "schema": LEARNING_DECISION_SCHEMA,
        "blackout_id": scope[0],
        "segment_id": scope[1],
        "boot_id": scope[2],
        "wall_time_utc": _utc(scope[3]),
        "monotonic_ns": scope[4],
        "disposition": decision.disposition.value,
        "evidence_hashes": list(decision.evidence_hashes),
        "evidence_set_id": decision.evidence_set_id,
        "reasons": [reason.value for reason in decision.reasons],
        "change": _change_payload(decision.change),
        "observed_load_sag_increase": _observation_payload(decision.observed_load_sag_increase),
    }


def _change_payload(value: ModelChange | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "parameter": value.parameter,
        "value_before": value.value_before,
        "measured_estimate": value.measured_estimate,
        "value_after": value.value_after,
        "bound_applied": value.bound_applied,
    }


def _observation_payload(value: ObservedLoadSagIncrease | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "parameter": value.parameter,
        "value_before": value.value_before,
        "measured_estimate": value.measured_estimate,
        "evidence_set_id": value.evidence_set_id,
    }


def _decision_from_payload(payload: Mapping[str, Any]) -> IrLearningDecision:
    disposition = _enum(payload["disposition"], IrLearningDisposition, "disposition")
    evidence_hashes = _hashes(payload["evidence_hashes"], "evidence hashes")
    reasons = _reasons(payload["reasons"])
    evidence_set = _optional_hash(payload["evidence_set_id"], "evidence-set ID")
    change = _change_from_payload(payload["change"], evidence_hashes)
    observation = _observation_from_payload(
        payload["observed_load_sag_increase"], evidence_hashes, evidence_set
    )
    return IrLearningDecision(
        disposition=disposition,
        evidence_hashes=evidence_hashes,
        evidence_set_id=evidence_set,
        reasons=reasons,
        change=change,
        observed_load_sag_increase=observation,
    )


def _change_from_payload(value: Any, evidence_hashes: tuple[str, ...]) -> ModelChange | None:
    if value is None:
        return None
    payload = _exact_payload(value, _CHANGE_FIELDS, "model change")
    bound = payload["bound_applied"]
    if not isinstance(bound, bool):
        raise ValueError("model change bound flag must be bool")
    return ModelChange(
        parameter=_text(payload["parameter"], "model-change parameter"),
        value_before=_finite(payload["value_before"], "model value before"),
        measured_estimate=_finite(payload["measured_estimate"], "measured estimate"),
        value_after=_finite(payload["value_after"], "model value after"),
        evidence_hashes=evidence_hashes,
        bound_applied=bound,
    )


def _observation_from_payload(
    value: Any, evidence_hashes: tuple[str, ...], evidence_set: str | None
) -> ObservedLoadSagIncrease | None:
    if value is None:
        return None
    payload = _exact_payload(value, _OBSERVATION_FIELDS, "observed load-sag increase")
    nested_evidence_set = _hash(payload["evidence_set_id"], "observation evidence-set ID")
    if nested_evidence_set != evidence_set:
        raise ValueError("observation evidence-set ID differs from decision")
    return ObservedLoadSagIncrease(
        parameter=_text(payload["parameter"], "observation parameter"),
        value_before=_finite(payload["value_before"], "observation value before"),
        measured_estimate=_finite(payload["measured_estimate"], "observation estimate"),
        evidence_set_id=nested_evidence_set,
        evidence_hashes=evidence_hashes,
    )


def _validate_scope_payload(envelope: V3RecordEnvelope, payload: Mapping[str, Any]) -> None:
    if payload["schema"] != LEARNING_DECISION_SCHEMA:
        raise ValueError("unsupported learning decision schema")
    for key in ("blackout_id", "segment_id", "boot_id"):
        _text(payload[key], key)
    wall = _parse_utc(payload["wall_time_utc"])
    monotonic = _nonnegative(payload["monotonic_ns"], "monotonic time")
    if (
        envelope.blackout_id != payload["blackout_id"]
        or envelope.segment_id != payload["segment_id"]
        or envelope.boot_id != payload["boot_id"]
        or envelope.wall_time_utc != _utc(wall)
        or envelope.monotonic_ns != monotonic
    ):
        raise ValueError("learning decision envelope scope is not bound")


def _validate_decision_bound(decision: IrLearningDecision) -> None:
    if len(decision.evidence_hashes) > MAX_EVIDENCE_HASHES:
        raise V3CodecError("learning decision evidence exceeds 64 hashes")
    if len(decision.reasons) > MAX_LEARNING_REASONS:
        raise V3CodecError("learning decision reasons exceed 8 values")


def _exact_payload(value: Any, fields: frozenset[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise V3CodecError(f"{name} fields are not exact")
    return value


def _pop_text(options: dict[str, object], name: str) -> str:
    value = options.pop(name, None)
    return _text(value, name)


def _pop_nonnegative(options: dict[str, object], name: str) -> int:
    return _nonnegative(options.pop(name, None), name)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 128:
        raise V3CodecError(f"{name} must be a bounded non-empty ID")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise V3CodecError(f"{name} must not contain control characters")
    return value


def _hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be lowercase SHA-256")
    return value


def _optional_hash(value: Any, name: str) -> str | None:
    return None if value is None else _hash(value, name)


def _hashes(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_EVIDENCE_HASHES:
        raise ValueError(f"{name} must be a list of at most 64 hashes")
    result = tuple(_hash(item, name) for item in value)
    if tuple(sorted(result)) != result or len(set(result)) != len(result):
        raise ValueError(f"{name} must be sorted and unique")
    return result


def _reasons(value: Any) -> tuple[LearningReason, ...]:
    if not isinstance(value, list) or len(value) > MAX_LEARNING_REASONS:
        raise ValueError("learning reasons must be a list of at most 8 values")
    try:
        result = tuple(LearningReason(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ValueError("learning reasons are not closed") from exc
    if len(set(result)) != len(result):
        raise ValueError("learning reasons must be unique")
    return result


def _enum(value: Any, enum_type: type[Any], name: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not closed") from exc


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _nonnegative(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise V3CodecError(f"{name} must be a nonnegative integer")
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
