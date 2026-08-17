"""Boundary tests for monotonic timeline and rolling readiness."""

import pytest

from src.domain.readiness import ReadinessState, update_readiness
from src.domain.reasons import ReadinessReason
from src.domain.timeline import summarize_timeline


def test_timeline_coverage_uses_only_accepted_increasing_edges(observation_factory):
    observations = tuple(observation_factory(second) for second in (0, 1, 1, 4, 10))
    summary = summarize_timeline(observations, 5.0)
    assert summary.duration_s == 10.0
    assert summary.accepted_duration_s == 4.0
    assert summary.coverage_ratio == pytest.approx(0.4)
    assert summary.max_gap_s == 6.0
    assert summary.non_increasing_edge_count == 1


def test_reboot_never_integrates(observation_factory):
    observations = (
        observation_factory(0, boot_id="boot-a"),
        observation_factory(1, boot_id="boot-a"),
        observation_factory(2, boot_id="boot-b"),
    )
    summary = summarize_timeline(observations, 5.0)
    assert summary.reboot_gap_observed is True
    assert summary.duration_s == 0.0
    assert summary.coverage_ratio == 0.0


@pytest.mark.parametrize(
    ("continuous_s", "ready"),
    ((43_199.0, False), (43_200.0, True), (43_201.0, True)),
)
def test_readiness_twelve_hour_boundary(observation_factory, continuous_s, ready):
    state = ReadinessState(
        boot_id="boot-a",
        continuous_online_start_ns=0,
        last_monotonic_ns=int((continuous_s - 1.0) * 1_000_000_000),
        trailing_voltage_points=(
            (int((continuous_s - 1_800.0) * 1_000_000_000), 13.4),
            (int((continuous_s - 1.0) * 1_000_000_000), 13.5),
        ),
        last_reset_reason=None,
    )
    _, snapshot = update_readiness(
        state,
        observation_factory(continuous_s, raw_status="OL", voltage_v=13.45),
    )
    assert snapshot.ready is ready


@pytest.mark.parametrize(
    ("raw_status", "voltage_v", "reason"),
    (
        ("OB DISCHRG", 13.4, ReadinessReason.NOT_ONLINE),
        ("OL CAL", 13.4, ReadinessReason.CALIBRATION_ACTIVE),
        ("OL", None, ReadinessReason.VOLTAGE_UNAVAILABLE),
        ("OL", 12.99, ReadinessReason.VOLTAGE_OUT_OF_RANGE),
    ),
)
def test_readiness_resets_on_disqualifying_raw_fact(
    observation_factory,
    raw_status,
    voltage_v,
    reason,
):
    state = ReadinessState("boot-a", 0, 1_000_000_000, ((1_000_000_000, 13.4),), None)
    next_state, snapshot = update_readiness(
        state,
        observation_factory(2, raw_status=raw_status, voltage_v=voltage_v),
    )
    assert snapshot.ready is False
    if raw_status.startswith("OB"):
        assert next_state.last_reset_reason == reason
    else:
        assert reason in snapshot.reasons.values


def test_first_battery_sample_freezes_completed_online_readiness_before_reset(
    observation_factory,
):
    state = ReadinessState(
        "boot-a",
        0,
        43_200_000_000_000,
        ((41_400_000_000_000, 13.4), (43_200_000_000_000, 13.5)),
        None,
    )
    next_state, snapshot = update_readiness(
        state,
        observation_factory(43_201, raw_status="OB DISCHRG", voltage_v=13.45),
    )
    assert snapshot.ready is True
    assert snapshot.continuous_online_s == 43_200.0
    assert next_state.continuous_online_start_ns is None


def test_first_battery_sample_does_not_preserve_readiness_across_acquisition_gap(
    observation_factory,
):
    state = ReadinessState(
        "boot-a",
        0,
        43_100_000_000_000,
        ((41_400_000_000_000, 13.4), (43_100_000_000_000, 13.5)),
        None,
    )
    _, snapshot = update_readiness(
        state,
        observation_factory(43_201, raw_status="OB DISCHRG", voltage_v=13.45),
    )
    assert snapshot.ready is False
