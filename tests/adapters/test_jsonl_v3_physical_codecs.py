"""Focused strict-wire tests for physical blackout codecs."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.adapters.jsonl_v3_blackout_start_codec import (
    BLACKOUT_START_MAX_LINE_BYTES,
    decode_blackout_start,
    decode_blackout_start_record,
    encode_blackout_start,
)
from src.adapters.jsonl_v3_canonical import (
    V3CodecError,
    V3RecordEnvelope,
    canonical_json_bytes,
    encode_v3_record,
)
from src.adapters.jsonl_v3_discharge_gap_codec import (
    DISCHARGE_GAP_MAX_LINE_BYTES,
    decode_discharge_gap,
    decode_discharge_gap_record,
    encode_discharge_gap,
)
from src.adapters.jsonl_v3_discharge_sample_codec import (
    MAX_PHYSICAL_RECORD_BYTES,
    decode_discharge_sample,
    decode_discharge_sample_record,
    encode_discharge_sample,
)
from src.battery_math.lut import LutPoint
from src.domain.blackout_capture import (
    BlackoutStart,
    CapturedTelemetry,
    DischargeGap,
    DischargeGapReason,
    DischargeSample,
    DischargeSampleIdentity,
    FrozenModelCapture,
    GapSubreasonCount,
    RawNutToken,
)
from src.domain.fragments import ObservationOrigin, ReadinessProvenance, StartReadinessContext
from src.domain.values import FrozenModelSnapshot, PhysicalObservation

UTC = datetime(2026, 8, 18, tzinfo=timezone.utc)
HASH = "a" * 64


def _snapshot() -> FrozenModelSnapshot:
    return FrozenModelSnapshot(
        "model-v3",
        "evaluation-v3",
        "epoch-a",
        HASH,
        7.2,
        12.0,
        510.0,
        1.0,
        1.2,
        0.015,
        0.0,
        (LutPoint(13.7, 1.0, "standard"), LutPoint(10.8, 0.0, "anchor")),
    )


def _start(**changes: object) -> BlackoutStart:
    fields: dict[str, object] = {
        "blackout_id": "blackout-a",
        "physical_episode_id": "episode-a",
        "battery_epoch_id": "epoch-a",
        "segment_id": "segment-a",
        "observation_origin": ObservationOrigin.NATURAL,
        "wall_time_utc": UTC,
        "monotonic_ns": 5,
        "boot_id": "boot-a",
        "policy_revision": "capture-v3",
        "capability_baseline_hash": HASH,
        "frozen_model_capture": FrozenModelCapture(_snapshot(), "b" * 64),
        "readiness_context": StartReadinessContext(
            True, "known_full", ReadinessProvenance.PHYSICAL
        ),
    }
    fields.update(changes)
    return BlackoutStart(**fields)  # type: ignore[arg-type]


def _sample(raw_tokens: tuple[RawNutToken, ...] | None = None, *, sequence: int = 2**64 - 1):
    observation = PhysicalObservation(
        "boot-a",
        10,
        UTC,
        "OB DISCHRG",
        "12.30",
        12.3,
        0.01,
        20.0,
        0.0,
    )
    captured = CapturedTelemetry(
        observation,
        raw_tokens
        or (
            RawNutToken("battery.voltage", "12.30", "12.30"),
            RawNutToken("input.voltage", "0.0", "0.0"),
            RawNutToken("ups.load", "20.0", "20.0"),
            RawNutToken("ups.status", "OB DISCHRG", "OB DISCHRG"),
        ),
    )
    return DischargeSample.from_telemetry(
        sequence,
        captured,
        DischargeSampleIdentity(
            "blackout-a", "episode-a", "epoch-a", "segment-a", ObservationOrigin.NATURAL
        ),
    )


def _gap(**changes: object) -> DischargeGap:
    fields: dict[str, object] = {
        "blackout_id": "blackout-a",
        "physical_episode_id": "episode-a",
        "battery_epoch_id": "epoch-a",
        "segment_id": "segment-a",
        "observation_origin": ObservationOrigin.NATURAL,
        "reason": DischargeGapReason.MALFORMED_REPLY,
        "count": 2,
        "first_boot_id": "boot-a",
        "last_boot_id": "boot-a",
        "first_monotonic_ns": 10,
        "last_monotonic_ns": 20,
        "receipt_boot_id": "boot-a",
        "receipt_monotonic_ns": 30,
        "receipt_wall_time_utc": UTC,
        "first_wall_time_utc": UTC,
        "last_wall_time_utc": UTC,
        "failed_command": "upsc",
        "error_type": "ValueError",
        "subreason_counts": (GapSubreasonCount(DischargeGapReason.MALFORMED_REPLY, 2),),
    }
    fields.update(changes)
    return DischargeGap(**fields)  # type: ignore[arg-type]


def _rehash(record, *, payload: dict[str, object] | None = None, **changes: object) -> bytes:
    value = json.loads(record.line)
    return encode_v3_record(
        V3RecordEnvelope(
            **{
                **value,
                "record_sha256": None,
                "payload": value["payload"] if payload is None else payload,
                **changes,
            }
        )
    ).line


def test_blackout_start_round_trip_preserves_snapshot_and_canonical_bytes() -> None:
    value = _start()
    encoded = encode_blackout_start(value)

    assert decode_blackout_start(encoded.line) == value
    assert decode_blackout_start_record(encoded.line) == encoded
    assert encoded.line == encode_blackout_start(value).line
    assert encoded.envelope.seq == 0
    assert len(encoded.line) <= BLACKOUT_START_MAX_LINE_BYTES

    with pytest.raises(V3CodecError):
        encode_blackout_start(value, seq=1)
    with pytest.raises(V3CodecError):
        encode_blackout_start(value, previous_record_sha256=HASH)


@pytest.mark.parametrize("field", ("schema", "frozen_model_capture", "readiness_context"))
def test_blackout_start_decode_rejects_unknown_or_missing_payload_fields(field: str) -> None:
    encoded = encode_blackout_start(_start())
    payload = dict(encoded.envelope.payload)
    if field == "schema":
        payload[field] = "blackout_start-v2"
    elif field == "frozen_model_capture":
        del payload[field]
    else:
        payload[field] = {"ready": True, "reason": None, "provenance": None, "extra": 1}

    with pytest.raises(V3CodecError):
        decode_blackout_start(_rehash(encoded, payload=payload))


def test_blackout_start_decode_rejects_origin_intent_inconsistency() -> None:
    encoded = encode_blackout_start(_start())
    payload = {**encoded.envelope.payload, "uat_intent_id": "uat-a"}

    with pytest.raises(V3CodecError):
        decode_blackout_start(_rehash(encoded, payload=payload))


def test_blackout_start_decode_rejects_wrong_record_scope_and_one_byte_over_line() -> None:
    encoded = encode_blackout_start(_start())

    with pytest.raises(V3CodecError):
        decode_blackout_start(_rehash(encoded, record_type="discharge_sample"))
    oversized = (
        encoded.line[:-1] + b" " * (BLACKOUT_START_MAX_LINE_BYTES - len(encoded.line) + 1) + b"\n"
    )
    with pytest.raises(V3CodecError):
        decode_blackout_start(oversized)


def test_discharge_sample_round_trip_keeps_uint64_sequence_separate_from_envelope_seq() -> None:
    value = _sample(sequence=2**64 - 1)
    encoded = encode_discharge_sample(value, seq=11)

    assert decode_discharge_sample(encoded.line) == value
    assert decode_discharge_sample_record(encoded.line) == encoded
    assert encoded.envelope.seq == 11
    assert encoded.envelope.payload["sequence"] == 2**64 - 1


def test_discharge_sample_encode_rejects_directly_constructed_mismatched_hash() -> None:
    value = _sample()
    mismatched = DischargeSample(
        value.sequence,
        value.captured,
        value.blackout_id,
        value.physical_episode_id,
        value.battery_epoch_id,
        value.segment_id,
        value.observation_origin,
        "f" * 64,
        value.uat_intent_id,
    )
    with pytest.raises(V3CodecError):
        encode_discharge_sample(mismatched, seq=1)


@pytest.mark.parametrize("mutation", ("hash", "number", "provenance", "schema"))
def test_discharge_sample_decode_rejects_rehashed_semantic_mutations(mutation: str) -> None:
    encoded = encode_discharge_sample(_sample(), seq=3)
    payload = dict(encoded.envelope.payload)
    changes: dict[str, object] = {}
    if mutation == "hash":
        payload["canonical_hash"] = "c" * 64
    elif mutation == "number":
        payload["load_percent"] = True
    elif mutation == "provenance":
        changes["provenance"] = "model"
    else:
        payload["schema"] = "discharge_sample-v2"

    with pytest.raises(V3CodecError):
        decode_discharge_sample(_rehash(encoded, payload=payload, **changes))


def test_discharge_sample_line_one_byte_over_maximum_is_refused() -> None:
    encoded = encode_discharge_sample(_sample(), seq=1)
    oversized = (
        encoded.line[:-1] + b" " * (MAX_PHYSICAL_RECORD_BYTES - len(encoded.line) + 1) + b"\n"
    )

    with pytest.raises(V3CodecError):
        decode_discharge_sample(oversized)


def test_discharge_gap_round_trip_preserves_nullable_boundaries_and_system_provenance() -> None:
    value = _gap(first_wall_time_utc=None, last_wall_time_utc=None)
    encoded = encode_discharge_gap(value, seq=9)

    assert decode_discharge_gap(encoded.line) == value
    assert decode_discharge_gap_record(encoded.line) == encoded
    assert encoded.line == encode_discharge_gap(value, seq=9).line
    assert encoded.envelope.provenance == "system"
    assert len(encoded.line) <= DISCHARGE_GAP_MAX_LINE_BYTES


@pytest.mark.parametrize(
    ("kind", "wall_time"),
    (("power_restored", None), ("modeled_safe_shutdown", UTC)),
)
def test_discharge_gap_round_trip_preserves_terminal_kind_and_optional_wall(
    kind: str, wall_time: datetime | None
) -> None:
    value = _gap(
        loss_terminal_boundary_kind=kind,
        loss_terminal_boundary_wall_time_utc=wall_time,
    )
    encoded = encode_discharge_gap(value)

    assert decode_discharge_gap(encoded.line) == value
    assert encoded.envelope.payload["loss_terminal_boundary_kind"] == kind
    assert encoded.envelope.payload["loss_terminal_boundary_wall_time_utc"] == (
        None if wall_time is None else "2026-08-18T00:00:00.000000Z"
    )


def test_discharge_gap_decode_rejects_terminal_wall_without_kind() -> None:
    encoded = encode_discharge_gap(_gap())
    payload = {
        **encoded.envelope.payload,
        "loss_terminal_boundary_kind": None,
        "loss_terminal_boundary_wall_time_utc": "2026-08-18T00:00:00.000000Z",
    }

    with pytest.raises(V3CodecError):
        decode_discharge_gap(_rehash(encoded, payload=payload))


@pytest.mark.parametrize("field", ("reason", "loss_terminal_boundary_kind", "failed_command"))
def test_discharge_gap_decode_rejects_invalid_boundary_or_error_pairings(field: str) -> None:
    encoded = encode_discharge_gap(_gap())
    payload = dict(encoded.envelope.payload)
    if field == "reason":
        payload[field] = "not-a-reason"
    elif field == "loss_terminal_boundary_kind":
        payload[field] = "not-a-boundary"
    else:
        payload[field] = None

    with pytest.raises(V3CodecError):
        decode_discharge_gap(_rehash(encoded, payload=payload))


def test_discharge_gap_decode_rejects_wrong_provenance_and_one_byte_over_line() -> None:
    encoded = encode_discharge_gap(_gap())

    with pytest.raises(V3CodecError):
        decode_discharge_gap(_rehash(encoded, provenance="physical"))
    oversized = (
        encoded.line[:-1] + b" " * (DISCHARGE_GAP_MAX_LINE_BYTES - len(encoded.line) + 1) + b"\n"
    )
    with pytest.raises(V3CodecError):
        decode_discharge_gap(oversized)


def test_discharge_sample_maximum_legal_raw_token_map_is_retained_without_truncation() -> None:
    def candidate(size: int) -> tuple[RawNutToken, ...]:
        return (
            RawNutToken("a", "x" * size, "x" * size),
            RawNutToken("b", "x" * size, "x" * size),
            RawNutToken("battery.voltage", "12.30", "12.30"),
            RawNutToken("input.voltage", "0.0", "0.0"),
            RawNutToken("ups.load", "20.0", "20.0"),
            RawNutToken("ups.status", "OB DISCHRG", "OB DISCHRG"),
        )

    low, high = 1, 8193
    while low + 1 < high:
        probe = (low + high) // 2
        tokens = candidate(probe)
        encoded_tokens = {
            "raw_tokens": [
                {"key": item.key, "token": item.token, "wire_lexeme": item.wire_lexeme}
                for item in tokens
            ]
        }
        if len(canonical_json_bytes(encoded_tokens)) <= 16 * 1024:
            low = probe
        else:
            high = probe
    value = _sample(candidate(low))
    encoded = encode_discharge_sample(value, seq=2)

    assert (
        len(
            canonical_json_bytes(
                {
                    "raw_tokens": [
                        {
                            "key": item.key,
                            "token": item.token,
                            "wire_lexeme": item.wire_lexeme,
                        }
                        for item in value.captured.raw_tokens
                    ]
                }
            )
        )
        <= 16 * 1024
    )
    assert decode_discharge_sample(encoded.line) == value
