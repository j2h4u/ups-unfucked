import inspect
from datetime import datetime, timezone

import pytest

from src.application.safety import (
    NUT_STATUS_DISCHARGING,
    NUT_STATUS_LOW_BATTERY,
    NUT_STATUS_ONLINE,
    SafetyInputs,
    SafetyLatch,
    VirtualLbSource,
    calculate_safety,
    conservative_safety_kind,
    make_safety_publication,
    safety_decision,
)
from src.battery_math.lut import LutPoint
from src.domain.safety_policy import decide_unlatched_safety_status
from src.domain.values import BlackoutKind, FrozenModelSnapshot, PhysicalObservation


@pytest.mark.parametrize(
    ("kind", "remaining", "expected"),
    [
        *(
            (BlackoutKind.ONLINE, remaining, NUT_STATUS_ONLINE)
            for remaining in (None, 0.0, 1.999, 2.0, 4.999, 5.0, 60.0)
        ),
        *(
            (BlackoutKind.BLACKOUT_REAL, remaining, NUT_STATUS_LOW_BATTERY)
            for remaining in (None, 0.0, 1.999, 2.0, 4.999)
        ),
        (BlackoutKind.BLACKOUT_REAL, 5.0, NUT_STATUS_DISCHARGING),
        (BlackoutKind.BLACKOUT_REAL, 60.0, NUT_STATUS_DISCHARGING),
        *(
            (BlackoutKind.BLACKOUT_TEST, remaining, NUT_STATUS_LOW_BATTERY)
            for remaining in (None, 0.0, 1.999)
        ),
        *(
            (BlackoutKind.BLACKOUT_TEST, remaining, NUT_STATUS_DISCHARGING)
            for remaining in (2.0, 4.999, 5.0, 60.0)
        ),
    ],
)
def test_safety_decision_matches_pinned_release_a_golden(kind, remaining, expected) -> None:
    assert safety_decision(kind, remaining, 5, SafetyLatch()).virtual_status == expected


def test_safety_signature_cannot_accept_raw_firmware_lb() -> None:
    assert "raw_status" not in inspect.signature(calculate_safety).parameters


def test_unknown_status_is_real_for_safety_and_missing_reserve_fails_closed() -> None:
    safety_kind = conservative_safety_kind(BlackoutKind.UNKNOWN)
    result = safety_decision(safety_kind, None, 5, SafetyLatch())

    assert safety_kind == BlackoutKind.BLACKOUT_REAL
    assert result.virtual_status == NUT_STATUS_LOW_BATTERY
    assert result.hard_floor_lb


def test_domain_policy_unknown_is_not_online() -> None:
    unknown = decide_unlatched_safety_status(BlackoutKind.UNKNOWN, None, 5)
    real = decide_unlatched_safety_status(BlackoutKind.BLACKOUT_REAL, None, 5)

    assert unknown == real
    assert unknown.virtual_status == NUT_STATUS_LOW_BATTERY


def test_calculation_uses_one_frozen_snapshot() -> None:
    snapshot = FrozenModelSnapshot(
        schema_revision="2",
        evaluation_revision="1",
        battery_epoch_id="a" * 32,
        scientific_fingerprint="b" * 64,
        rated_capacity_ah=7.2,
        nominal_voltage_v=12.0,
        nominal_power_watts=510.0,
        soh=1.0,
        peukert_exponent=1.2,
        ir_k_v_per_pp=0.015,
        ir_reference_load_percent=0.0,
        lut=(
            LutPoint(13.7, 1.0, "standard"),
            LutPoint(10.8, 0.0, "anchor"),
        ),
    )

    result = calculate_safety(
        inputs=SafetyInputs(
            voltage_v=12.0,
            load_percent=20.0,
            blackout_kind=BlackoutKind.BLACKOUT_REAL,
            shutdown_threshold_minutes=5,
        ),
        snapshot=snapshot,
    )

    assert 0.0 <= result.soc <= 1.0
    assert result.charge_percent == round(result.soc * 100)
    assert result.runtime_minutes > 0.0


def test_virtual_lb_stays_set_until_online_even_if_runtime_recovers() -> None:
    latched = SafetyLatch(True, VirtualLbSource.MODELED_THRESHOLD)

    still_low = safety_decision(BlackoutKind.BLACKOUT_REAL, 60.0, 5, latched)
    unknown = safety_decision(BlackoutKind.UNKNOWN, 60.0, 5, latched)
    online = safety_decision(BlackoutKind.ONLINE, 60.0, 5, latched)

    assert still_low.virtual_status == NUT_STATUS_LOW_BATTERY
    assert still_low.next_latch == latched
    assert still_low.modeled_lb
    assert not still_low.hard_floor_lb
    assert unknown.virtual_status == NUT_STATUS_LOW_BATTERY
    assert online.virtual_status == NUT_STATUS_ONLINE
    assert not online.next_latch.virtual_lb


@pytest.mark.parametrize(
    ("kind", "remaining", "expected_status", "expected_source"),
    [
        (
            BlackoutKind.BLACKOUT_REAL,
            4.0,
            NUT_STATUS_LOW_BATTERY,
            VirtualLbSource.MODELED_THRESHOLD,
        ),
        (
            BlackoutKind.BLACKOUT_REAL,
            1.0,
            NUT_STATUS_LOW_BATTERY,
            VirtualLbSource.HARD_FLOOR,
        ),
        (
            BlackoutKind.BLACKOUT_TEST,
            4.0,
            NUT_STATUS_DISCHARGING,
            None,
        ),
        (
            BlackoutKind.BLACKOUT_TEST,
            None,
            NUT_STATUS_LOW_BATTERY,
            VirtualLbSource.HARD_FLOOR,
        ),
    ],
)
def test_lb_source_is_explicit(kind, remaining, expected_status, expected_source) -> None:
    result = safety_decision(kind, remaining, 5, SafetyLatch())

    assert result.virtual_status == expected_status
    assert result.next_latch.source == expected_source


def test_publication_keeps_raw_lb_separate_from_modeled_decision() -> None:
    raw = PhysicalObservation(
        boot_id="boot",
        monotonic_ns=1,
        wall_time_utc=datetime(2026, 8, 16, tzinfo=timezone.utc),
        raw_status="OB DISCHRG LB",
        battery_voltage_raw="13.00",
        battery_voltage_v=13.0,
        voltage_token_quantum_v=0.01,
        load_percent=20.0,
        input_voltage_v=0.0,
    )
    safe_calculation = calculate_safety(
        inputs=SafetyInputs(13.0, 20.0, BlackoutKind.BLACKOUT_REAL, 5),
        snapshot=_snapshot(),
    )

    publication = make_safety_publication(raw, safe_calculation)

    assert publication.raw_lb_observed
    assert not publication.lb
    assert not publication.modeled_lb
    assert not publication.hard_floor_lb


def _snapshot() -> FrozenModelSnapshot:
    return FrozenModelSnapshot(
        schema_revision="2",
        evaluation_revision="1",
        battery_epoch_id="a" * 32,
        scientific_fingerprint="b" * 64,
        rated_capacity_ah=7.2,
        nominal_voltage_v=12.0,
        nominal_power_watts=510.0,
        soh=1.0,
        peukert_exponent=1.2,
        ir_k_v_per_pp=0.015,
        ir_reference_load_percent=0.0,
        lut=(
            LutPoint(13.7, 1.0, "standard"),
            LutPoint(10.8, 0.0, "anchor"),
        ),
    )
