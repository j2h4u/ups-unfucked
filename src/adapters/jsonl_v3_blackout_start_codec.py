"""Strict schema-3 codec for the physical blackout START record."""

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
from src.battery_math.lut import LutPoint
from src.domain.blackout_capture import BlackoutStart, FrozenModelCapture
from src.domain.blackout_terminal import ContinuationKind
from src.domain.fragments import ObservationOrigin, ReadinessProvenance, StartReadinessContext
from src.domain.values import FrozenModelSnapshot, IrLearningPolicy

BLACKOUT_START_RECORD_TYPE = "blackout_start"
BLACKOUT_START_SCHEMA = "blackout_start-v1"
BLACKOUT_START_PROVENANCE = "physical"
BLACKOUT_START_MAX_LINE_BYTES = 20 * 1024

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_UTC_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_FIELDS = frozenset(
    {
        "schema",
        "blackout_id",
        "physical_episode_id",
        "battery_epoch_id",
        "segment_id",
        "observation_origin",
        "uat_intent_id",
        "wall_time_utc",
        "monotonic_ns",
        "boot_id",
        "policy_revision",
        "capability_baseline_hash",
        "frozen_model_capture",
        "readiness_context",
        "continued_from",
        "continuation_kind",
    }
)
_READINESS_FIELDS = frozenset({"ready", "reason", "provenance"})
_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_revision",
        "evaluation_revision",
        "battery_epoch_id",
        "scientific_fingerprint",
        "rated_capacity_ah",
        "nominal_voltage_v",
        "nominal_power_watts",
        "soh",
        "peukert_exponent",
        "ir_k_v_per_pp",
        "ir_reference_load_percent",
        "lut",
        "learning_policy",
    }
)
_LUT_POINT_FIELDS = frozenset({"voltage_v", "soc", "source"})
_POLICY_FIELDS = frozenset(
    {
        "revision",
        "deadband_v_per_pp",
        "min_k_v_per_pp",
        "max_k_v_per_pp",
        "max_single_commit_fraction",
        "max_epoch_decrease_fraction",
        "min_commit_interval_days",
        "max_consumed_step_hashes",
    }
)


def encode_blackout_start(
    value: BlackoutStart,
    *,
    seq: int = 0,
    previous_record_sha256: str | None = None,
) -> EncodedV3Record:
    """Encode one immutable physical START."""
    if not isinstance(value, BlackoutStart):
        raise TypeError("blackout start codec requires BlackoutStart")
    if seq != 0:
        raise V3CodecError("blackout start envelope seq must be zero")
    if previous_record_sha256 is not None:
        raise V3CodecError("blackout start must be a chain root")
    record = encode_v3_record(
        V3RecordEnvelope(
            3,
            BLACKOUT_START_RECORD_TYPE,
            BLACKOUT_START_PROVENANCE,
            value.blackout_id,
            value.segment_id,
            seq,
            value.boot_id,
            _utc(value.wall_time_utc),
            value.monotonic_ns,
            previous_record_sha256,
            _payload(value),
        )
    )
    return _bounded(record, "blackout start")


def decode_blackout_start(line: bytes) -> BlackoutStart:
    """Decode and reconstruct a strictly validated START value."""
    record = decode_blackout_start_record(line)
    payload = _payload_exact(record.envelope.payload)
    try:
        value = BlackoutStart(
            blackout_id=_text(payload["blackout_id"], "blackout ID"),
            physical_episode_id=_text(payload["physical_episode_id"], "physical episode ID"),
            battery_epoch_id=_text(payload["battery_epoch_id"], "battery epoch ID"),
            segment_id=_text(payload["segment_id"], "segment ID"),
            observation_origin=_enum(payload["observation_origin"], ObservationOrigin),
            wall_time_utc=_parse_utc(payload["wall_time_utc"]),
            monotonic_ns=_nonnegative(payload["monotonic_ns"]),
            boot_id=_text(payload["boot_id"], "boot ID"),
            policy_revision=_text(payload["policy_revision"], "policy revision"),
            capability_baseline_hash=_hash(payload["capability_baseline_hash"]),
            frozen_model_capture=_model_capture(payload["frozen_model_capture"]),
            readiness_context=_readiness(payload["readiness_context"]),
            uat_intent_id=_optional_text(payload["uat_intent_id"]),
            continued_from=_optional_text(payload["continued_from"]),
            continuation_kind=_optional_enum(payload["continuation_kind"], ContinuationKind),
        )
        _validate_envelope(record.envelope, value)
    except (KeyError, TypeError, ValueError) as exc:
        raise V3CodecError("blackout start payload is invalid") from exc
    return value


def decode_blackout_start_record(line: bytes) -> EncodedV3Record:
    """Decode only after strict record type, schema, provenance, and scope checks."""
    record = _bounded_decode(line, "blackout start")
    envelope = record.envelope
    if envelope.record_type != BLACKOUT_START_RECORD_TYPE:
        raise V3CodecError("record is not a blackout start")
    if envelope.provenance != BLACKOUT_START_PROVENANCE:
        raise V3CodecError("blackout start provenance is not physical")
    payload = _payload_exact(envelope.payload)
    if envelope.seq != 0:
        raise V3CodecError("blackout start envelope seq must be zero")
    if envelope.prev_record_sha256 is not None:
        raise V3CodecError("blackout start must be a chain root")
    if payload["schema"] != BLACKOUT_START_SCHEMA:
        raise V3CodecError("unsupported blackout start schema")
    return record


def _payload(value: BlackoutStart) -> dict[str, Any]:
    readiness = value.readiness_context
    return {
        "schema": BLACKOUT_START_SCHEMA,
        "blackout_id": value.blackout_id,
        "physical_episode_id": value.physical_episode_id,
        "battery_epoch_id": value.battery_epoch_id,
        "segment_id": value.segment_id,
        "observation_origin": value.observation_origin.value,
        "uat_intent_id": value.uat_intent_id,
        "wall_time_utc": _utc(value.wall_time_utc),
        "monotonic_ns": value.monotonic_ns,
        "boot_id": value.boot_id,
        "policy_revision": value.policy_revision,
        "capability_baseline_hash": value.capability_baseline_hash,
        "frozen_model_capture": _model_capture_payload(value.frozen_model_capture),
        "readiness_context": None
        if readiness is None
        else {
            "ready": readiness.ready,
            "reason": readiness.reason,
            "provenance": None if readiness.provenance is None else readiness.provenance.value,
        },
        "continued_from": value.continued_from,
        "continuation_kind": None
        if value.continuation_kind is None
        else value.continuation_kind.value,
    }


def _readiness(value: Any) -> StartReadinessContext | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != _READINESS_FIELDS:
        raise ValueError("readiness context fields are not exact")
    return StartReadinessContext(
        ready=value["ready"],
        reason=_optional_text(value["reason"]),
        provenance=_optional_enum(value["provenance"], ReadinessProvenance),
    )


def _snapshot_payload(value: FrozenModelSnapshot) -> dict[str, Any]:
    if not isinstance(value, FrozenModelSnapshot):
        raise TypeError("frozen model snapshot must be FrozenModelSnapshot")
    return {
        "schema_revision": value.schema_revision,
        "evaluation_revision": value.evaluation_revision,
        "battery_epoch_id": value.battery_epoch_id,
        "scientific_fingerprint": value.scientific_fingerprint,
        "rated_capacity_ah": value.rated_capacity_ah,
        "nominal_voltage_v": value.nominal_voltage_v,
        "nominal_power_watts": value.nominal_power_watts,
        "soh": value.soh,
        "peukert_exponent": value.peukert_exponent,
        "ir_k_v_per_pp": value.ir_k_v_per_pp,
        "ir_reference_load_percent": value.ir_reference_load_percent,
        "lut": [
            {"voltage_v": point.voltage_v, "soc": point.soc, "source": point.source}
            for point in value.lut
        ],
        "learning_policy": {
            "revision": value.learning_policy.revision,
            "deadband_v_per_pp": value.learning_policy.deadband_v_per_pp,
            "min_k_v_per_pp": value.learning_policy.min_k_v_per_pp,
            "max_k_v_per_pp": value.learning_policy.max_k_v_per_pp,
            "max_single_commit_fraction": value.learning_policy.max_single_commit_fraction,
            "max_epoch_decrease_fraction": value.learning_policy.max_epoch_decrease_fraction,
            "min_commit_interval_days": value.learning_policy.min_commit_interval_days,
            "max_consumed_step_hashes": value.learning_policy.max_consumed_step_hashes,
        },
    }


def _model_capture_payload(value: FrozenModelCapture) -> dict[str, Any]:
    if not isinstance(value, FrozenModelCapture):
        raise TypeError("frozen model capture must be FrozenModelCapture")
    return {
        "snapshot": _snapshot_payload(value.snapshot),
        "persisted_model_sha256": value.persisted_model_sha256,
    }


def _model_capture(value: Any) -> FrozenModelCapture:
    if not isinstance(value, Mapping) or set(value) != {"snapshot", "persisted_model_sha256"}:
        raise ValueError("frozen model capture fields are not exact")
    persisted_hash = _hash(value["persisted_model_sha256"])
    capture = FrozenModelCapture(_snapshot(value["snapshot"]), persisted_hash)
    return capture


def _snapshot(value: Any) -> FrozenModelSnapshot:
    if not isinstance(value, Mapping) or set(value) != _SNAPSHOT_FIELDS:
        raise ValueError("frozen model snapshot fields are not exact")
    lut_value = value["lut"]
    if not isinstance(lut_value, list) or not lut_value:
        raise ValueError("frozen model snapshot LUT must be non-empty")
    lut = []
    for item in lut_value:
        if not isinstance(item, Mapping) or set(item) != _LUT_POINT_FIELDS:
            raise ValueError("frozen model snapshot LUT fields are not exact")
        lut.append(
            LutPoint(
                _number(item["voltage_v"], "LUT voltage"),
                _number(item["soc"], "LUT SoC"),
                _text(item["source"], "LUT source"),
            )
        )
    policy_value = value["learning_policy"]
    if not isinstance(policy_value, Mapping) or set(policy_value) != _POLICY_FIELDS:
        raise ValueError("frozen model learning policy fields are not exact")
    policy = IrLearningPolicy(
        revision=_text(policy_value["revision"], "learning policy revision"),
        deadband_v_per_pp=_number(policy_value["deadband_v_per_pp"], "learning policy deadband"),
        min_k_v_per_pp=_number(policy_value["min_k_v_per_pp"], "learning policy minimum"),
        max_k_v_per_pp=_number(policy_value["max_k_v_per_pp"], "learning policy maximum"),
        max_single_commit_fraction=_number(
            policy_value["max_single_commit_fraction"], "learning policy single commit"
        ),
        max_epoch_decrease_fraction=_number(
            policy_value["max_epoch_decrease_fraction"], "learning policy epoch decrease"
        ),
        min_commit_interval_days=_integer(
            policy_value["min_commit_interval_days"], "learning policy interval"
        ),
        max_consumed_step_hashes=_integer(
            policy_value["max_consumed_step_hashes"], "learning policy evidence budget"
        ),
    )
    return FrozenModelSnapshot(
        schema_revision=_text(value["schema_revision"], "snapshot schema revision"),
        evaluation_revision=_text(value["evaluation_revision"], "snapshot evaluation revision"),
        battery_epoch_id=_text(value["battery_epoch_id"], "snapshot battery epoch ID"),
        scientific_fingerprint=_text(value["scientific_fingerprint"], "scientific fingerprint"),
        rated_capacity_ah=_number(value["rated_capacity_ah"], "rated capacity"),
        nominal_voltage_v=_number(value["nominal_voltage_v"], "nominal voltage"),
        nominal_power_watts=_number(value["nominal_power_watts"], "nominal power"),
        soh=_number(value["soh"], "state of health"),
        peukert_exponent=_number(value["peukert_exponent"], "Peukert exponent"),
        ir_k_v_per_pp=_number(value["ir_k_v_per_pp"], "IR coefficient"),
        ir_reference_load_percent=_number(value["ir_reference_load_percent"], "IR reference load"),
        lut=tuple(lut),
        learning_policy=policy,
    )


def _validate_envelope(envelope: V3RecordEnvelope, value: BlackoutStart) -> None:
    if (
        envelope.blackout_id != value.blackout_id
        or envelope.segment_id != value.segment_id
        or envelope.boot_id != value.boot_id
        or envelope.wall_time_utc != _utc(value.wall_time_utc)
        or envelope.monotonic_ns != value.monotonic_ns
    ):
        raise ValueError("blackout start envelope scope is not bound")


def _payload_exact(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise V3CodecError("blackout start fields are not exact")
    return value


def _bounded(record: EncodedV3Record, name: str) -> EncodedV3Record:
    if len(record.line) > BLACKOUT_START_MAX_LINE_BYTES:
        raise V3CodecError(f"{name} exceeds {BLACKOUT_START_MAX_LINE_BYTES} bytes")
    return record


def _bounded_decode(line: bytes, name: str) -> EncodedV3Record:
    if not isinstance(line, bytes) or len(line) > BLACKOUT_START_MAX_LINE_BYTES:
        raise V3CodecError(f"{name} exceeds {BLACKOUT_START_MAX_LINE_BYTES} bytes")
    try:
        return decode_v3_record(line)
    except (TypeError, ValueError) as exc:
        raise V3CodecError(f"invalid {name} envelope") from exc


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 128:
        raise ValueError(f"{field} must be a bounded ID")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} contains a control character")
    return value


def _optional_text(value: Any) -> str | None:
    return None if value is None else _text(value, "text value")


def _hash(value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError("value must be lowercase SHA-256")
    return value


def _nonnegative(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("value must be nonnegative integer")
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise ValueError(f"{field} must be a finite number")
    return float(value)


def _enum(value: Any, enum_type: type[Any]) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("value is not a closed enum") from exc


def _optional_enum(value: Any, enum_type: type[Any]) -> Any:
    return None if value is None else _enum(value, enum_type)


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        raise ValueError("timestamp is not canonical UTC")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamp is not UTC")
    return parsed


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
