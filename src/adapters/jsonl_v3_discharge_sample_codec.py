"""Strict schema-3 codec for complete physical discharge samples."""

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
    canonical_json_bytes,
    decode_v3_record,
    encode_v3_record,
)
from src.domain.blackout_capture import (
    CapturedTelemetry,
    DischargeSample,
    DischargeSampleIdentity,
    RawNutToken,
    canonical_discharge_sample_hash,
)
from src.domain.fragments import ObservationOrigin
from src.domain.values import PhysicalObservation

DISCHARGE_SAMPLE_RECORD_TYPE = "discharge_sample"
DISCHARGE_SAMPLE_SCHEMA = "discharge_sample-v1"
DISCHARGE_SAMPLE_PROVENANCE = "physical"
MAX_RAW_TOKEN_MAP_BYTES = 16 * 1024
MAX_PHYSICAL_RECORD_BYTES = 20 * 1024

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_UTC_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_FIELDS = frozenset(
    {
        "schema",
        "sequence",
        "canonical_hash",
        "blackout_id",
        "physical_episode_id",
        "battery_epoch_id",
        "segment_id",
        "observation_origin",
        "uat_intent_id",
        "raw_status",
        "battery_voltage_raw",
        "battery_voltage_v",
        "voltage_token_quantum_v",
        "load_percent",
        "input_voltage_v",
        "raw_tokens",
    }
)
_TOKEN_FIELDS = frozenset({"key", "token", "wire_lexeme"})


def encode_discharge_sample(
    value: DischargeSample,
    *,
    seq: int | None = None,
    previous_record_sha256: str | None = None,
) -> EncodedV3Record:
    """Encode every typed field and every original raw token without truncation."""
    if not isinstance(value, DischargeSample):
        raise TypeError("discharge sample codec requires DischargeSample")
    expected_hash = canonical_discharge_sample_hash(
        value.sequence,
        value.captured,
        _identity(value),
    )
    if value.canonical_hash != expected_hash:
        raise V3CodecError("sample canonical hash does not match raw content")
    payload = _payload(value)
    _check_token_map(payload["raw_tokens"])
    observation = value.captured.observation
    record = encode_v3_record(
        V3RecordEnvelope(
            3,
            DISCHARGE_SAMPLE_RECORD_TYPE,
            DISCHARGE_SAMPLE_PROVENANCE,
            value.blackout_id,
            value.segment_id,
            0 if seq is None else seq,
            observation.boot_id,
            _utc(observation.wall_time_utc),
            observation.monotonic_ns,
            previous_record_sha256,
            payload,
        )
    )
    if len(record.line) > MAX_PHYSICAL_RECORD_BYTES:
        raise V3CodecError("discharge sample exceeds 20 KiB")
    return record


def decode_discharge_sample(line: bytes) -> DischargeSample:
    """Decode a complete sample and verify its domain canonical identity."""
    record = decode_discharge_sample_record(line)
    payload = _payload_exact(record.envelope.payload)
    try:
        raw_tokens = _tokens(payload["raw_tokens"])
        _check_token_map(payload["raw_tokens"])
        observation = PhysicalObservation(
            boot_id=record.envelope.boot_id,
            monotonic_ns=record.envelope.monotonic_ns,
            wall_time_utc=_parse_utc(record.envelope.wall_time_utc),
            raw_status=_text(payload["raw_status"], "raw status", allow_empty=True),
            battery_voltage_raw=_optional_text(payload["battery_voltage_raw"]),
            battery_voltage_v=_optional_number(payload["battery_voltage_v"]),
            voltage_token_quantum_v=_optional_number(payload["voltage_token_quantum_v"]),
            load_percent=_optional_number(payload["load_percent"]),
            input_voltage_v=_optional_number(payload["input_voltage_v"]),
        )
        captured = CapturedTelemetry(observation, raw_tokens)
        value = DischargeSample(
            sequence=_nonnegative(payload["sequence"]),
            captured=captured,
            blackout_id=_text(payload["blackout_id"], "blackout ID"),
            physical_episode_id=_text(payload["physical_episode_id"], "physical episode ID"),
            battery_epoch_id=_text(payload["battery_epoch_id"], "battery epoch ID"),
            segment_id=_text(payload["segment_id"], "segment ID"),
            observation_origin=_enum(payload["observation_origin"], ObservationOrigin),
            canonical_hash=_hash(payload["canonical_hash"]),
            uat_intent_id=_optional_text(payload["uat_intent_id"]),
        )
        expected = canonical_discharge_sample_hash(
            value.sequence,
            value.captured,
            _identity(value),
        )
        if value.canonical_hash != expected:
            raise ValueError("sample canonical hash does not match raw content")
        _validate_envelope(record.envelope, value)
    except (KeyError, TypeError, ValueError) as exc:
        raise V3CodecError("discharge sample payload is invalid") from exc
    return value


def decode_discharge_sample_record(line: bytes) -> EncodedV3Record:
    """Decode only a canonical physical sample record."""
    if not isinstance(line, bytes) or len(line) > MAX_PHYSICAL_RECORD_BYTES:
        raise V3CodecError("discharge sample exceeds 20 KiB")
    try:
        record = decode_v3_record(line)
    except (TypeError, ValueError) as exc:
        raise V3CodecError("invalid discharge sample envelope") from exc
    if record.envelope.record_type != DISCHARGE_SAMPLE_RECORD_TYPE:
        raise V3CodecError("record is not a discharge sample")
    if record.envelope.provenance != DISCHARGE_SAMPLE_PROVENANCE:
        raise V3CodecError("discharge sample provenance is not physical")
    payload = _payload_exact(record.envelope.payload)
    if payload["schema"] != DISCHARGE_SAMPLE_SCHEMA:
        raise V3CodecError("unsupported discharge sample schema")
    return record


def _payload(value: DischargeSample) -> dict[str, Any]:
    observation = value.captured.observation
    return {
        "schema": DISCHARGE_SAMPLE_SCHEMA,
        "sequence": value.sequence,
        "canonical_hash": value.canonical_hash,
        "blackout_id": value.blackout_id,
        "physical_episode_id": value.physical_episode_id,
        "battery_epoch_id": value.battery_epoch_id,
        "segment_id": value.segment_id,
        "observation_origin": value.observation_origin.value,
        "uat_intent_id": value.uat_intent_id,
        "raw_status": observation.raw_status,
        "battery_voltage_raw": observation.battery_voltage_raw,
        "battery_voltage_v": observation.battery_voltage_v,
        "voltage_token_quantum_v": observation.voltage_token_quantum_v,
        "load_percent": observation.load_percent,
        "input_voltage_v": observation.input_voltage_v,
        "raw_tokens": [
            {"key": item.key, "token": item.token, "wire_lexeme": item.wire_lexeme}
            for item in value.captured.raw_tokens
        ],
    }


def _identity(value: DischargeSample) -> DischargeSampleIdentity:
    return DischargeSampleIdentity(
        blackout_id=value.blackout_id,
        physical_episode_id=value.physical_episode_id,
        battery_epoch_id=value.battery_epoch_id,
        segment_id=value.segment_id,
        observation_origin=value.observation_origin,
        uat_intent_id=value.uat_intent_id,
    )


def _tokens(value: Any) -> tuple[RawNutToken, ...]:
    if not isinstance(value, list):
        raise ValueError("raw token map must be a list")
    tokens: list[RawNutToken] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != _TOKEN_FIELDS:
            raise ValueError("raw token entry fields are not exact")
        tokens.append(RawNutToken(item["key"], item["token"], item["wire_lexeme"]))
    return tuple(tokens)


def _check_token_map(value: Any) -> None:
    if len(canonical_json_bytes({"raw_tokens": value})) > MAX_RAW_TOKEN_MAP_BYTES:
        raise V3CodecError("raw NUT token map exceeds 16 KiB")


def _payload_exact(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise V3CodecError("discharge sample fields are not exact")
    return value


def _validate_envelope(envelope: V3RecordEnvelope, value: DischargeSample) -> None:
    observation = value.captured.observation
    if (
        envelope.blackout_id != value.blackout_id
        or envelope.segment_id != value.segment_id
        or envelope.boot_id != observation.boot_id
        or envelope.wall_time_utc != _utc(observation.wall_time_utc)
        or envelope.monotonic_ns != observation.monotonic_ns
    ):
        raise ValueError("discharge sample envelope scope is not bound")


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or len(value.encode("utf-8")) > 8192
    ):
        raise ValueError(f"{field} is not bounded text")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} contains a control character")
    return value


def _optional_text(value: Any) -> str | None:
    return None if value is None else _text(value, "optional text")


def _hash(value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError("value must be lowercase SHA-256")
    return value


def _nonnegative(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("value must be nonnegative integer")
    return value


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise ValueError("value must be finite number")
    return float(value)


def _optional_number(value: Any) -> float | None:
    return None if value is None else _number(value)


def _enum(value: Any, enum_type: type[Any]) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("value is not a closed enum") from exc


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        raise ValueError("timestamp is not canonical UTC")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamp is not UTC")
    return parsed


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
