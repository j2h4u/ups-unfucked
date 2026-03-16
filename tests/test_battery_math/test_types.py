"""Tests for BatteryState frozen dataclass."""

import pytest
from dataclasses import FrozenInstanceError, replace
from src.battery_math import BatteryState


VRLA_REFERENCE_LUT = (
    (12.0, 1.0, "reference"),
    (11.5, 0.75, "calibration"),
    (10.5, 0.0, "cutoff"),
)


@pytest.fixture
def initial_battery_state():
    """Healthy battery at start of Phase 12.1."""
    return BatteryState(
        soh=1.0,
        peukert_exponent=1.2,
        capacity_ah_rated=7.2,
        capacity_ah_measured=None,
        lut=VRLA_REFERENCE_LUT,
        cycle_count=0,
        cumulative_on_battery_sec=0.0
    )


def test_battery_state_frozen(initial_battery_state):
    """BatteryState is frozen — no mutation allowed."""
    with pytest.raises(FrozenInstanceError):
        initial_battery_state.soh = 0.95


def test_battery_state_immutable_lut(initial_battery_state):
    """LUT is immutable tuple — modifications raise TypeError."""
    with pytest.raises(TypeError):
        initial_battery_state.lut[0] = (12.0, 0.95, "modified")


def test_battery_state_new_with_replace():
    """Frozen dataclass uses dataclasses.replace to create new state."""
    initial = BatteryState(
        soh=1.0,
        peukert_exponent=1.2,
        capacity_ah_rated=7.2,
        capacity_ah_measured=None,
        lut=VRLA_REFERENCE_LUT,
        cycle_count=0,
        cumulative_on_battery_sec=0.0
    )
    # Create new state with updated fields
    updated = replace(initial, soh=0.95, cycle_count=1)

    assert updated.soh == 0.95
    assert updated.cycle_count == 1
    assert initial.soh == 1.0  # Original unchanged
    assert initial.cycle_count == 0


def test_battery_state_fields():
    """All required fields present."""
    state = BatteryState(
        soh=0.90,
        peukert_exponent=1.2,
        capacity_ah_rated=7.2,
        capacity_ah_measured=7.1,
        lut=VRLA_REFERENCE_LUT,
        cycle_count=5,
        cumulative_on_battery_sec=3600.0
    )
    assert state.soh == 0.90
    assert state.peukert_exponent == 1.2
    assert state.capacity_ah_rated == 7.2
    assert state.capacity_ah_measured == 7.1
    assert state.cycle_count == 5
    assert state.cumulative_on_battery_sec == 3600.0
    assert state.lut == VRLA_REFERENCE_LUT


def test_battery_state_measured_capacity_optional():
    """capacity_ah_measured can be None (Phase 12.1 before convergence)."""
    state = BatteryState(
        soh=1.0,
        peukert_exponent=1.2,
        capacity_ah_rated=7.2,
        capacity_ah_measured=None,
        lut=VRLA_REFERENCE_LUT,
        cycle_count=0,
        cumulative_on_battery_sec=0.0
    )
    assert state.capacity_ah_measured is None
