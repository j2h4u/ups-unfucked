"""Contract tests for the immutable v3 physical capture values."""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone
from math import inf, nan
from typing import Any, cast

import pytest

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
    canonical_discharge_sample_hash,
)
from src.domain.blackout_terminal import ContinuationKind
from src.domain.fragment_primitives import MAX_MONOTONIC_NS, UINT64_MAX, ReadinessProvenance
from src.domain.fragments import ObservationOrigin, StartReadinessContext
from src.domain.values import PhysicalObservation

H = "a" * 64
UTC = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)


def _identity(
    *, origin: ObservationOrigin = ObservationOrigin.NATURAL, intent: str | None = None
) -> DischargeSampleIdentity:
    return DischargeSampleIdentity(
        "blackout-1", "episode-1", "epoch-a", "segment-1", origin, intent
    )


def _captured(
    observation_factory,
    *,
    tokens: tuple[RawNutToken, ...] | None = None,
    voltage_v: float | None = None,
) -> CapturedTelemetry:
    return CapturedTelemetry(
        observation_factory(0)
        if voltage_v is None
        else observation_factory(0, voltage_v=voltage_v),
        tokens
        if tokens is not None
        else (
            RawNutToken("battery.voltage", "13.200", "13.200"),
            RawNutToken("input.voltage", "0.0", "0.0"),
            RawNutToken("ups.load", "20.0", "20.0"),
            RawNutToken("ups.status", "OB DISCHRG", "OB DISCHRG"),
        ),
    )


def _start(frozen_snapshot, **overrides: object) -> BlackoutStart:
    values: dict[str, object] = {
        "blackout_id": "blackout-1",
        "physical_episode_id": "episode-1",
        "battery_epoch_id": "epoch-a",
        "segment_id": "segment-1",
        "observation_origin": ObservationOrigin.NATURAL,
        "wall_time_utc": UTC,
        "monotonic_ns": 0,
        "boot_id": "boot-a",
        "policy_revision": "policy-v1",
        "capability_baseline_hash": H,
        "frozen_model_capture": FrozenModelCapture(frozen_snapshot, H),
    }
    values.update(overrides)
    return BlackoutStart(**cast(Any, values))


def _gap(**overrides: object) -> DischargeGap:
    values: dict[str, object] = {
        "blackout_id": "blackout-1",
        "physical_episode_id": "episode-1",
        "battery_epoch_id": "epoch-a",
        "segment_id": "segment-1",
        "observation_origin": ObservationOrigin.NATURAL,
        "reason": DischargeGapReason.TELEMETRY_REPLY_LOST,
        "count": 1,
        "first_boot_id": "boot-a",
        "last_boot_id": "boot-b",
        "first_monotonic_ns": 10,
        "last_monotonic_ns": 20,
        "receipt_boot_id": "boot-a",
        "receipt_monotonic_ns": 30,
        "receipt_wall_time_utc": UTC,
    }
    values.update(overrides)
    return DischargeGap(**cast(Any, values))


def test_capture_values_are_frozen_and_slot_based(observation_factory, frozen_snapshot) -> None:
    values = (
        RawNutToken("k", "v", "v"),
        _captured(observation_factory),
        _start(frozen_snapshot),
        DischargeSample.from_telemetry(0, _captured(observation_factory), _identity()),
        _gap(),
        _identity(),
    )
    for value in values:
        assert not hasattr(value, "__dict__")
        assert all(field.name for field in fields(value))
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, None)


@pytest.mark.parametrize("key", ("", "bad key", "bad\nkey", "x" * 8193, "é" * 4097))
def test_raw_nut_key_is_bounded_and_control_free(key: str) -> None:
    with pytest.raises(ValueError):
        RawNutToken(key, "value", "value")


@pytest.mark.parametrize("token", ("bad\x00token", "x" * 8193, "é" * 4097))
def test_raw_nut_token_is_bounded_and_control_free(token: str) -> None:
    with pytest.raises(ValueError):
        RawNutToken("key", token, token)


def test_complete_raw_tokens_are_unique_sorted_and_canonical(observation_factory) -> None:
    tokens = (
        RawNutToken("battery.voltage", "13.200", "13.200"),
        RawNutToken("input.voltage", "0.0", "0.0"),
        RawNutToken("ups.load", "20.0", "20.0"),
        RawNutToken("ups.status", "OB DISCHRG", "OB DISCHRG"),
    )
    captured = _captured(observation_factory, tokens=tokens)

    assert captured.canonical_raw_bytes == (
        b'{"raw_tokens":[{"key":"battery.voltage","token":"13.200",'
        b'"wire_lexeme":"13.200"},{"key":"input.voltage","token":"0.0",'
        b'"wire_lexeme":"0.0"},{"key":"ups.load","token":"20.0",'
        b'"wire_lexeme":"20.0"},{"key":"ups.status","token":"OB DISCHRG",'
        b'"wire_lexeme":"OB DISCHRG"}]}'
    )
    assert captured == CapturedTelemetry(observation_factory(0), tokens)

    with pytest.raises(ValueError, match="sorted"):
        _captured(
            observation_factory,
            tokens=(
                RawNutToken("ups.status", "OB DISCHRG", "OB DISCHRG"),
                RawNutToken("battery.voltage", "13.200", "13.200"),
                RawNutToken("input.voltage", "0.0", "0.0"),
                RawNutToken("ups.load", "20.0", "20.0"),
            ),
        )
    with pytest.raises(ValueError, match="sorted"):
        _captured(
            observation_factory,
            tokens=(
                RawNutToken("same", "one", "one"),
                RawNutToken("same", "two", "two"),
                RawNutToken("battery.voltage", "13.200", "13.200"),
                RawNutToken("input.voltage", "0.0", "0.0"),
                RawNutToken("ups.load", "20.0", "20.0"),
                RawNutToken("ups.status", "OB DISCHRG", "OB DISCHRG"),
            ),
        )
    with pytest.raises(TypeError, match="tuple"):
        CapturedTelemetry(observation_factory(0), cast(Any, []))
    with pytest.raises(ValueError, match="complete"):
        CapturedTelemetry(observation_factory(0), tokens, complete=False)


def test_raw_token_map_is_bounded_at_16_kib() -> None:
    def candidate(size: int) -> tuple[RawNutToken, ...]:
        return (
            RawNutToken("a", "x" * size, "x" * size),
            RawNutToken("b", "x" * size, "x" * size),
            RawNutToken("battery.voltage", "13.2", "13.2"),
            RawNutToken("input.voltage", "0.0", "0.0"),
            RawNutToken("ups.load", "20.0", "20.0"),
            RawNutToken("ups.status", "OB DISCHRG", "OB DISCHRG"),
        )

    # The two-token shape makes the bound deterministic without relying on a
    # private serializer: find the largest accepted payload by constructing it.
    low, high = 1, 8193
    while low + 1 < high:
        probe = (low + high) // 2
        try:
            CapturedTelemetry(
                PhysicalObservation("boot-a", 0, UTC, "OB DISCHRG", "13.2", 13.2, 0.1, 20.0, 0.0),
                candidate(probe),
            )
        except ValueError:
            high = probe
        else:
            low = probe
    accepted = low
    assert (
        len(
            CapturedTelemetry(
                PhysicalObservation("boot-a", 0, UTC, "OB DISCHRG", "13.2", 13.2, 0.1, 20.0, 0.0),
                candidate(accepted),
            ).canonical_raw_bytes
        )
        <= 16 * 1024
    )
    with pytest.raises(ValueError, match="16 KiB"):
        CapturedTelemetry(
            PhysicalObservation("boot-a", 0, UTC, "OB", "13.2", 13.2, 0.1, 20.0, 0.0),
            candidate(accepted + 1),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("boot_id", ""),
        ("boot_id", "bad\nboot"),
        ("boot_id", "x" * 129),
        ("monotonic_ns", -1),
        ("monotonic_ns", MAX_MONOTONIC_NS + 1),
        ("monotonic_ns", True),
        ("wall_time_utc", datetime(2026, 1, 1)),
        ("wall_time_utc", datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=2)))),
        ("raw_status", ""),
        ("raw_status", "OB\x00"),
        ("battery_voltage_raw", "bad\nraw"),
        ("battery_voltage_v", nan),
        ("voltage_token_quantum_v", inf),
        ("load_percent", -inf),
        ("input_voltage_v", nan),
    ),
)
def test_captured_telemetry_validates_physical_observation(
    observation_factory, field: str, value: object
) -> None:
    observation = replace(observation_factory(0), **{field: value})
    with pytest.raises(ValueError):
        CapturedTelemetry(observation, ())


def test_captured_telemetry_rejects_wrong_observation_and_token_members() -> None:
    with pytest.raises(TypeError, match="PhysicalObservation"):
        CapturedTelemetry(cast(Any, object()), ())
    with pytest.raises(TypeError, match="RawNutToken"):
        CapturedTelemetry(
            cast(Any, PhysicalObservation("boot-a", 0, UTC, "OB", None, None, None, None, None)),
            cast(Any, ("raw",)),
        )


@pytest.mark.parametrize(
    "field",
    (
        "raw_status",
        "battery_voltage_raw",
        "battery_voltage_v",
        "voltage_token_quantum_v",
        "load_percent",
        "input_voltage_v",
    ),
)
def test_captured_telemetry_rejects_every_known_typed_field_mismatch(
    observation_factory, field: str
) -> None:
    observation = observation_factory(0)
    updates: dict[str, object] = {
        "raw_status": "OL",
        "battery_voltage_raw": "13.201",
        "battery_voltage_v": 13.201,
        "voltage_token_quantum_v": 0.01,
        "load_percent": 21.0,
        "input_voltage_v": 221.0,
    }
    with pytest.raises(ValueError, match="match|token|reply"):
        CapturedTelemetry(
            replace(observation, **{field: updates[field]}),
            (
                RawNutToken("battery.voltage", "13.200", "13.200"),
                RawNutToken("input.voltage", "0.0", "0.0"),
                RawNutToken("ups.load", "20.0", "20.0"),
                RawNutToken("ups.status", "OB DISCHRG", "OB DISCHRG"),
            ),
        )


@pytest.mark.parametrize("missing", ("battery.voltage", "ups.load", "input.voltage"))
def test_captured_telemetry_keeps_absent_optional_physical_fields_unavailable(
    observation_factory, missing: str
) -> None:
    observation = observation_factory(0)
    updates = {
        "battery.voltage": {
            "battery_voltage_raw": None,
            "battery_voltage_v": None,
            "voltage_token_quantum_v": None,
        },
        "ups.load": {"load_percent": None},
        "input.voltage": {"input_voltage_v": None},
    }
    tokens = tuple(
        token
        for token in (
            RawNutToken("battery.voltage", "13.200", "13.200"),
            RawNutToken("input.voltage", "0.0", "0.0"),
            RawNutToken("ups.load", "20.0", "20.0"),
            RawNutToken("ups.status", "OB DISCHRG", "OB DISCHRG"),
        )
        if token.key != missing
    )
    captured = CapturedTelemetry(
        observation=replace(observation, **updates[missing]), raw_tokens=tokens
    )
    assert missing not in {item.key for item in captured.raw_tokens}


@pytest.mark.parametrize("field", ("battery.voltage", "ups.load", "input.voltage"))
def test_captured_telemetry_keeps_unusable_optional_tokens_raw_but_unavailable(
    observation_factory, field: str
) -> None:
    observation = observation_factory(0)
    updates = {
        "battery.voltage": {
            "battery_voltage_raw": "N/A",
            "battery_voltage_v": None,
            "voltage_token_quantum_v": None,
        },
        "ups.load": {"load_percent": None},
        "input.voltage": {"input_voltage_v": None},
    }
    tokens = tuple(
        RawNutToken(key, "N/A" if key == field else token, "N/A" if key == field else token)
        for key, token in (
            ("battery.voltage", "13.200"),
            ("input.voltage", "0.0"),
            ("ups.load", "20.0"),
            ("ups.status", "OB DISCHRG"),
        )
    )
    captured = CapturedTelemetry(
        observation=replace(observation, **updates[field]), raw_tokens=tokens
    )
    assert next(item for item in captured.raw_tokens if item.key == field).token == "N/A"


def test_start_reuses_snapshot_and_readiness_context(frozen_snapshot) -> None:
    readiness = StartReadinessContext(True, "online_stable", ReadinessProvenance.PHYSICAL)
    value = _start(frozen_snapshot, readiness_context=readiness)

    assert value.frozen_model_capture.snapshot is frozen_snapshot
    assert value.frozen_model_capture.snapshot.battery_epoch_id == value.battery_epoch_id
    assert value.readiness_context is readiness


def test_model_capture_requires_independent_hashes_and_start_epoch(frozen_snapshot) -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        FrozenModelCapture(frozen_snapshot, "not-a-hash")
    with pytest.raises(ValueError, match="epochs"):
        _start(
            replace(frozen_snapshot, battery_epoch_id="epoch-other"),
        )


@pytest.mark.parametrize(
    "changes",
    (
        {"blackout_id": ""},
        {"physical_episode_id": "bad\nepisode"},
        {"battery_epoch_id": "x" * 129},
        {"segment_id": ""},
        {"wall_time_utc": datetime(2026, 1, 1)},
        {"monotonic_ns": -1},
        {"monotonic_ns": MAX_MONOTONIC_NS + 1},
        {"boot_id": "bad\x00boot"},
        {"frozen_model_capture": object()},
        {"readiness_context": object()},
    ),
)
def test_start_rejects_invalid_scope_time_and_context(
    frozen_snapshot, changes: dict[str, object]
) -> None:
    with pytest.raises((TypeError, ValueError)):
        _start(frozen_snapshot, **changes)


@pytest.mark.parametrize(
    "changes",
    (
        {"observation_origin": ObservationOrigin.UAT},
        {"observation_origin": ObservationOrigin.NATURAL, "uat_intent_id": "uat-1"},
        {"continued_from": "blackout-previous"},
        {"continuation_kind": ContinuationKind.SIZE_ROLLOVER},
        {"continuation_kind": "size_rollover"},
        {"capability_baseline_hash": "A" * 64},
        {"frozen_model_capture": object()},
        {"policy_revision": "bad\nrevision"},
    ),
)
def test_start_scope_origin_hash_and_continuation_shapes_are_strict(
    frozen_snapshot, changes: dict[str, object]
) -> None:
    with pytest.raises((TypeError, ValueError)):
        _start(frozen_snapshot, **changes)


def test_uat_start_and_continuation_start_are_valid(frozen_snapshot) -> None:
    uat = _start(
        frozen_snapshot,
        observation_origin=ObservationOrigin.UAT,
        uat_intent_id="manual-blackout-check",
    )
    continued = _start(
        frozen_snapshot,
        continued_from="blackout-previous",
        continuation_kind=ContinuationKind.REBOOT_GAP,
    )

    assert uat.uat_intent_id == "manual-blackout-check"
    assert continued.continued_from == "blackout-previous"


def test_sample_sequence_is_uint64_but_observation_time_is_signed63(
    observation_factory,
) -> None:
    captured = _captured(observation_factory)
    at_limits = DischargeSample.from_telemetry(UINT64_MAX, captured, _identity())
    assert at_limits.sequence == UINT64_MAX
    with pytest.raises(ValueError):
        DischargeSample.from_telemetry(UINT64_MAX + 1, captured, _identity())
    with pytest.raises(ValueError):
        CapturedTelemetry(replace(observation_factory(0), monotonic_ns=MAX_MONOTONIC_NS + 1), ())


@pytest.mark.parametrize("sequence", (-1, UINT64_MAX + 1, True))
def test_canonical_hash_rejects_invalid_unsigned_sequence(
    observation_factory, sequence: object
) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_discharge_sample_hash(
            cast(Any, sequence), _captured(observation_factory), _identity()
        )


def test_identity_hash_is_deterministic_and_changes_with_raw_evidence(
    observation_factory,
) -> None:
    captured = _captured(observation_factory)
    identity = _identity()
    digest = canonical_discharge_sample_hash(3, captured, identity)
    assert digest == canonical_discharge_sample_hash(3, captured, identity)
    assert len(digest) == 64 and digest == digest.lower()

    changed = _captured(
        observation_factory,
        tokens=(
            RawNutToken("battery.voltage", "13.201", "13.201"),
            RawNutToken("input.voltage", "0.0", "0.0"),
            RawNutToken("ups.load", "20.0", "20.0"),
            RawNutToken("ups.status", "OB DISCHRG", "OB DISCHRG"),
        ),
        voltage_v=13.201,
    )
    assert canonical_discharge_sample_hash(3, changed, identity) != digest
    with pytest.raises(TypeError, match="DischargeSampleIdentity"):
        canonical_discharge_sample_hash(3, captured, cast(Any, object()))


def test_sample_from_telemetry_carries_identity_scope_and_canonical_hash(
    observation_factory,
) -> None:
    identity = _identity(origin=ObservationOrigin.UAT, intent="uat-1")
    captured = _captured(observation_factory)
    sample = DischargeSample.from_telemetry(4, captured, identity)

    assert sample.captured is captured
    assert sample.blackout_id == identity.blackout_id
    assert sample.observation_origin is ObservationOrigin.UAT
    assert sample.uat_intent_id == "uat-1"
    assert sample.canonical_hash == canonical_discharge_sample_hash(4, captured, identity)


@pytest.mark.parametrize(
    "changes",
    (
        {"count": 0},
        {"count": -1},
        {"count": UINT64_MAX + 1},
        {"count": True},
        {"first_wall_time_utc": datetime(2026, 1, 1)},
        {"failed_command": "upsc cyberpower"},
        {"error_type": "TimeoutError"},
        {"loss_terminal_boundary_kind": "not-a-boundary"},
        {"uat_intent_id": "manual"},
    ),
)
def test_gap_ordered_boundaries_and_optional_metadata_are_closed(
    changes: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        _gap(**changes)


def test_gap_keeps_loss_terminal_and_paired_error_metadata() -> None:
    first_wall = UTC
    last_wall = UTC + timedelta(seconds=1)
    value = _gap(
        first_wall_time_utc=first_wall,
        last_wall_time_utc=last_wall,
        failed_command="upsc cyberpower@localhost",
        error_type="TimeoutError",
        loss_terminal_boundary_kind="power_restored",
        loss_terminal_boundary_wall_time_utc=last_wall,
    )

    assert value.first_wall_time_utc is first_wall
    assert value.last_wall_time_utc is last_wall
    assert value.loss_terminal_boundary_kind == "power_restored"
    assert value.failed_command is not None and value.error_type == "TimeoutError"


def test_gap_known_terminal_boundary_may_have_unknown_wall_time() -> None:
    value = _gap(loss_terminal_boundary_kind="boot_boundary")
    assert value.loss_terminal_boundary_wall_time_utc is None
    with pytest.raises(ValueError, match="known boundary"):
        _gap(loss_terminal_boundary_wall_time_utc=UTC)


def test_gap_count_uses_unsigned_64_bit_bound() -> None:
    value = _gap(count=UINT64_MAX)
    assert value.count == UINT64_MAX


def test_gap_subreason_counts_are_typed_and_sum_to_total() -> None:
    value = _gap(
        count=3,
        subreason_counts=(
            GapSubreasonCount(DischargeGapReason.MALFORMED_REPLY, 1),
            GapSubreasonCount(DischargeGapReason.CODEC_OVERSIZE, 2),
        ),
    )
    assert value.subreason_counts[0].count == 1
    with pytest.raises(ValueError, match="sum exactly"):
        _gap(
            count=3,
            subreason_counts=(GapSubreasonCount(DischargeGapReason.MALFORMED_REPLY, 1),),
        )


def test_gap_cross_boot_monotonic_boundaries_are_not_compared() -> None:
    value = _gap(first_monotonic_ns=20, last_monotonic_ns=10)
    assert value.first_boot_id != value.last_boot_id


def test_gap_wall_boundaries_are_independently_nullable() -> None:
    assert _gap(first_wall_time_utc=UTC).first_wall_time_utc == UTC
    assert _gap(last_wall_time_utc=UTC).last_wall_time_utc == UTC
    with pytest.raises(ValueError, match="factual"):
        _gap(receipt_wall_time_utc=datetime(1970, 1, 1, tzinfo=timezone.utc))


def test_wire_lexeme_preserves_escape_spelling_and_empty_value() -> None:
    token = RawNutToken("note", 'quoted "value" \\ path', r"quoted \"value\" \\ path")
    assert token.token == 'quoted "value" \\ path'
    assert token.wire_lexeme != token.token
    assert RawNutToken("optional", "", "").token == ""
    with pytest.raises(ValueError, match="unsupported escape"):
        RawNutToken("note", "tab", r"tab\t")
