"""Frozen contracts, ordered reasons, and raw lifecycle behavior."""

from dataclasses import FrozenInstanceError

import pytest

from src.domain.lifecycle import (
    LifecycleSignal,
    LifecycleState,
    advance_lifecycle,
    classify_physical_observation,
    is_capture_candidate,
    is_unknown_outage_candidate,
)
from src.domain.reasons import (
    ComparisonReason,
    EvidenceReason,
    ReadinessReason,
    order_reasons,
)
from src.domain.values import BlackoutKind, TerminationFact


def test_reason_codes_are_typed_ordered_deduplicated_and_bounded():
    ordered = order_reasons(
        (
            ComparisonReason.COMPARISON_NOT_ATTEMPTED,
            EvidenceReason.REBOOT_GAP,
            ReadinessReason.BOOT_CHANGED,
            EvidenceReason.REBOOT_GAP,
        )
    )
    assert ordered.values == (
        ReadinessReason.BOOT_CHANGED,
        EvidenceReason.REBOOT_GAP,
        ComparisonReason.COMPARISON_NOT_ATTEMPTED,
    )
    assert ordered.overflow_count == 0
    with pytest.raises(TypeError, match="unknown reason code"):
        order_reasons(("free_form",))  # type: ignore[arg-type]


def test_physical_observation_is_frozen(observation_factory):
    observation = observation_factory(0)
    with pytest.raises(FrozenInstanceError):
        observation.raw_status = "OL"


def test_missing_input_voltage_fails_closed_as_real_blackout(observation_factory):
    observation = observation_factory(0, raw_status="CAL DISCHRG", input_voltage_v=None)
    assert classify_physical_observation(observation) == BlackoutKind.BLACKOUT_REAL


@pytest.mark.parametrize("input_voltage_v", (None, 0.0, 99.9, float("nan")))
def test_unknown_low_or_missing_line_voltage_is_a_capture_candidate(
    observation_factory, input_voltage_v
):
    observation = observation_factory(
        0,
        raw_status="COMMFAULT",
        input_voltage_v=input_voltage_v,
    )

    assert is_unknown_outage_candidate(observation)
    assert is_capture_candidate(observation)


def test_unknown_with_healthy_line_voltage_is_not_a_capture_candidate(observation_factory):
    observation = observation_factory(0, raw_status="COMMFAULT", input_voltage_v=230.0)

    assert not is_unknown_outage_candidate(observation)
    assert not is_capture_candidate(observation)


def test_positive_test_evidence_requires_input_voltage(observation_factory):
    observation = observation_factory(0, raw_status="CAL DISCHRG", input_voltage_v=230.0)
    assert classify_physical_observation(observation) == BlackoutKind.BLACKOUT_TEST


def test_raw_lb_is_diagnostic_in_lifecycle_transition(observation_factory):
    observation = observation_factory(0, raw_status="OB DISCHRG LB")
    transition = advance_lifecycle(
        LifecycleState.IDLE,
        observation,
        LifecycleSignal.OBSERVATION,
    )
    assert transition.state_after == LifecycleState.PREPARING
    assert transition.raw_lb_observed is True
    assert not hasattr(transition, "virtual_lb")


@pytest.mark.parametrize(
    "state",
    (LifecycleState.PREPARING, LifecycleState.CAPTURING),
)
def test_capture_failure_marks_active_capture_damaged(state, observation_factory):
    transition = advance_lifecycle(
        state,
        observation_factory(0, raw_status="OB DISCHRG"),
        LifecycleSignal.CAPTURE_FAILURE,
    )

    assert transition.state_after == LifecycleState.CAPTURE_DAMAGED
    assert transition.termination == TerminationFact.CAPTURE_DAMAGED


def test_service_stop_closes_capture_for_processing(observation_factory):
    transition = advance_lifecycle(
        LifecycleState.CAPTURING,
        observation_factory(0, raw_status="OB DISCHRG"),
        LifecycleSignal.SERVICE_STOP,
    )

    assert transition.state_after == LifecycleState.PROCESSING
    assert transition.termination == TerminationFact.SERVICE_STOP


def test_reboot_gap_is_recorded_without_ending_capture(observation_factory):
    transition = advance_lifecycle(
        LifecycleState.CAPTURING,
        observation_factory(0, raw_status="OB DISCHRG"),
        LifecycleSignal.REBOOT_GAP,
    )

    assert transition.state_after == LifecycleState.CAPTURING
    assert transition.record_gap is True
    assert transition.termination is None


def test_prepared_capture_enters_capturing(observation_factory):
    transition = advance_lifecycle(
        LifecycleState.PREPARING,
        observation_factory(0, raw_status="OB DISCHRG"),
        LifecycleSignal.CAPTURE_PREPARED,
    )

    assert transition.state_after == LifecycleState.CAPTURING


def test_online_observation_ends_capture_as_power_restored(observation_factory):
    transition = advance_lifecycle(
        LifecycleState.CAPTURING,
        observation_factory(0, raw_status="OL"),
        LifecycleSignal.OBSERVATION,
    )

    assert transition.state_after == LifecycleState.PROCESSING
    assert transition.blackout_kind == BlackoutKind.ONLINE
    assert transition.termination == TerminationFact.POWER_RESTORED


@pytest.mark.parametrize(
    ("raw_status", "kind"),
    (("OL", BlackoutKind.ONLINE), ("", BlackoutKind.UNKNOWN)),
)
def test_non_blackout_observation_keeps_idle(raw_status, kind, observation_factory):
    input_voltage_v = 230.0 if kind == BlackoutKind.UNKNOWN else 0.0
    transition = advance_lifecycle(
        LifecycleState.IDLE,
        observation_factory(0, raw_status=raw_status, input_voltage_v=input_voltage_v),
        LifecycleSignal.OBSERVATION,
    )

    assert transition.state_after == LifecycleState.IDLE
    assert transition.blackout_kind == kind


def test_irrelevant_signal_is_a_total_noop(observation_factory):
    transition = advance_lifecycle(
        LifecycleState.IDLE,
        observation_factory(0, raw_status="OL"),
        LifecycleSignal.CAPTURE_FAILURE,
    )

    assert transition.state_after == LifecycleState.IDLE
    assert transition.termination is None
    assert transition.record_gap is False


@pytest.mark.parametrize(
    ("state", "signal", "expected"),
    (
        (LifecycleState.IDLE, LifecycleSignal.RECOVERED_CAPTURE_ATTACH, LifecycleState.CAPTURING),
        (
            LifecycleState.CAPTURING,
            LifecycleSignal.CAPTURE_END_SUBMITTED,
            LifecycleState.PROCESSING,
        ),
        (LifecycleState.PROCESSING, LifecycleSignal.CAPTURE_END_DURABLE, LifecycleState.IDLE),
        (
            LifecycleState.CAPTURING,
            LifecycleSignal.STICKY_RECOVERY_TIMEOUT,
            LifecycleState.CAPTURE_DAMAGED,
        ),
        (LifecycleState.PREPARING, LifecycleSignal.START_REJECTED, LifecycleState.IDLE),
    ),
)
def test_control_signals_are_the_runtime_transition_table_authority(state, signal, expected):
    transition = advance_lifecycle(state, None, signal)

    assert transition.state_after == expected
    assert transition.blackout_kind == BlackoutKind.UNKNOWN
