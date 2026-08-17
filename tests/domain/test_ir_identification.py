"""Synthetic raw-step recovery, cohort selection, and anti-feedback tests."""

import inspect
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.ir_identification import (
    CohortStep,
    IrCohortContext,
    IrRawObservation,
    identify_load_steps,
    select_ir_cohort,
    selected_current_event_step_count,
)
from src.domain.reasons import IdentificationReason
from src.domain.values import DEFAULT_IR_LEARNING_POLICY, StepQuality

BLACKOUT_ID = "a" * 32
SEGMENT_ID = "b" * 32
START = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _raw_step(
    *,
    upward=True,
    ineligible_status_at=None,
    ineligible_status="OB DISCHRG LB",
    early_k=0.020,
    settled_k=0.020,
):
    transition = 30
    observations = []
    for second in range(240):
        pre_load, post_load = (20.0, 40.0) if upward else (40.0, 20.0)
        load = pre_load if second < transition else post_load
        k = early_k if second < transition + 60 else settled_k
        voltage = 13.5 - 0.0002 * second - k * load
        status = ineligible_status if second == ineligible_status_at else "OB DISCHRG"
        observations.append(
            IrRawObservation(
                sequence=second + 1,
                boot_id="boot-a",
                monotonic_ns=second * 1_000_000_000,
                raw_status=status,
                battery_voltage_v=voltage,
                voltage_token_quantum_v=0.01,
                load_percent=load,
            )
        )
    return tuple(observations)


def _custom_step(
    *,
    pre_load: float = 20.0,
    post_load: float = 40.0,
    k_v_per_pp: float = 0.020,
    drift_v_per_s: float = 0.0002,
) -> tuple[IrRawObservation, ...]:
    transition = 30
    return tuple(
        IrRawObservation(
            sequence=second + 1,
            boot_id="boot-a",
            monotonic_ns=second * 1_000_000_000,
            raw_status="OB DISCHRG",
            battery_voltage_v=(
                13.5
                - drift_v_per_s * second
                - k_v_per_pp * (pre_load if second < transition else post_load)
            ),
            voltage_token_quantum_v=0.01,
            load_percent=pre_load if second < transition else post_load,
        )
        for second in range(240)
    )


def _two_step_event(
    *,
    second_transition: int = 210,
    first_transition_loads: dict[int, float] | None = None,
    unstable_until: int | None = None,
):
    observations = []
    first_transition_loads = first_transition_loads or {}
    for second in range(370):
        if second < 30:
            load = 20.0
        elif second < second_transition:
            load = 40.0
        else:
            load = 20.0
        if second in first_transition_loads:
            load = first_transition_loads[second]
        if unstable_until is not None and 150 < second <= unstable_until:
            load = 36.0 if second % 2 == 0 else 44.0
        observations.append(
            IrRawObservation(
                sequence=second + 1,
                boot_id="boot-a",
                monotonic_ns=second * 1_000_000_000,
                raw_status="OB DISCHRG",
                battery_voltage_v=13.5 - 0.0002 * second - 0.020 * load,
                voltage_token_quantum_v=0.01,
                load_percent=load,
            )
        )
    return tuple(observations)


@pytest.mark.parametrize("upward", (True, False))
def test_known_raw_step_recovers_positive_settled_k(upward):
    estimates = identify_load_steps(BLACKOUT_ID, SEGMENT_ID, _raw_step(upward=upward))
    assert len(estimates) == 1
    estimate = estimates[0]
    assert estimate.quality == StepQuality.QUALIFYING
    assert estimate.k_transition_v_per_pp == pytest.approx(0.020, abs=1e-9)
    assert estimate.k_settled_v_per_pp == pytest.approx(0.020, abs=1e-9)
    assert estimate.delta_load_pp == (20.0 if upward else -20.0)


def test_sliding_candidates_and_transition_bounce_choose_earliest_t0_once():
    observations = _two_step_event(
        second_transition=500,
        first_transition_loads={30: 30.0, 31: 42.0, 32: 38.0},
    )

    estimates = identify_load_steps(BLACKOUT_ID, SEGMENT_ID, observations)

    assert len(estimates) == 1
    assert estimates[0].transition_monotonic_ns == 30_000_000_000


def test_overlapping_windows_and_sub_180_second_transition_do_not_double_count():
    estimates = identify_load_steps(
        BLACKOUT_ID,
        SEGMENT_ID,
        _two_step_event(second_transition=160),
    )

    assert len(estimates) == 1
    assert estimates[0].transition_monotonic_ns == 30_000_000_000


@pytest.mark.parametrize(
    ("second_transition", "expected_count"),
    ((209, 1), (210, 2), (211, 2)),
)
def test_transition_separation_boundary_uses_earliest_sliding_candidate(
    second_transition,
    expected_count,
):
    estimates = identify_load_steps(
        BLACKOUT_ID,
        SEGMENT_ID,
        _two_step_event(second_transition=second_transition),
    )

    assert len(estimates) == expected_count


@pytest.mark.parametrize(
    ("unstable_until", "expected_count"),
    ((178, 2), (179, 1)),
)
def test_rearm_requires_full_30_stable_seconds_after_late_window(
    unstable_until,
    expected_count,
):
    estimates = identify_load_steps(
        BLACKOUT_ID,
        SEGMENT_ID,
        _two_step_event(unstable_until=unstable_until),
    )

    assert len(estimates) == expected_count


@pytest.mark.parametrize("second", (15, 30, 70, 150))
@pytest.mark.parametrize("status", ("OB DISCHRG LB", "OB DISCHRG CAL"))
def test_ineligible_status_anywhere_through_late_window_refuses_step_science(
    second,
    status,
):
    estimate = identify_load_steps(
        BLACKOUT_ID,
        SEGMENT_ID,
        _raw_step(ineligible_status_at=second, ineligible_status=status),
    )[0]
    assert estimate.quality == StepQuality.OBSERVED_ONLY
    assert estimate.k_settled_v_per_pp == pytest.approx(0.020, abs=1e-9)
    assert IdentificationReason.STEP_STATUS_NOT_ELIGIBLE in estimate.reasons.values


@pytest.mark.parametrize("second", (14, 151))
def test_status_gate_uses_exact_pre_through_late_boundaries(second):
    estimate = identify_load_steps(
        BLACKOUT_ID,
        SEGMENT_ID,
        _raw_step(ineligible_status_at=second),
    )[0]
    assert estimate.quality == StepQuality.QUALIFYING


@pytest.mark.parametrize(
    ("gap_s", "refused"),
    ((2.499, False), (2.5, False), (2.501, True)),
)
def test_step_timeline_gap_boundary(gap_s, refused):
    offset_ns = int((gap_s - 1.0) * 1_000_000_000)
    observations = tuple(
        replace(
            observation,
            monotonic_ns=observation.monotonic_ns
            + (offset_ns if observation.sequence >= 71 else 0),
        )
        for observation in _raw_step()
    )

    estimate = identify_load_steps(BLACKOUT_ID, SEGMENT_ID, observations)[0]

    assert (IdentificationReason.STEP_GAP_TOO_LARGE in estimate.reasons.values) is refused


def test_unsettled_polarization_is_recorded_and_refused():
    estimate = identify_load_steps(
        BLACKOUT_ID,
        SEGMENT_ID,
        _raw_step(early_k=0.015, settled_k=0.020),
    )[0]
    assert estimate.quality == StepQuality.OBSERVED_ONLY
    assert IdentificationReason.SAG_NOT_SETTLED in estimate.reasons.values


def test_step_crossing_boot_is_observed_only() -> None:
    observations = tuple(
        replace(item, boot_id="boot-b") if item.sequence == 71 else item for item in _custom_step()
    )

    estimate = identify_load_steps(BLACKOUT_ID, SEGMENT_ID, observations)[0]

    assert estimate.quality == StepQuality.OBSERVED_ONLY
    assert IdentificationReason.STEP_CROSSES_BOOT in estimate.reasons.values


def test_step_refuses_plateau_load_above_supported_range() -> None:
    estimate = identify_load_steps(
        BLACKOUT_ID,
        SEGMENT_ID,
        _custom_step(pre_load=40.0, post_load=60.0),
    )[0]

    assert IdentificationReason.LOAD_OUT_OF_RANGE in estimate.reasons.values


def test_step_refuses_unstable_pre_load_plateau() -> None:
    observations = tuple(
        replace(
            item,
            load_percent=15.0 if (item.sequence - 1) % 2 == 0 else 25.0,
            battery_voltage_v=(
                13.5
                - 0.0002 * (item.sequence - 1)
                - 0.020 * (15.0 if (item.sequence - 1) % 2 == 0 else 25.0)
            ),
        )
        if 15 <= item.sequence - 1 < 30
        else item
        for item in _custom_step()
    )

    estimate = identify_load_steps(BLACKOUT_ID, SEGMENT_ID, observations)[0]

    assert IdentificationReason.LOAD_PLATEAU_UNSTABLE in estimate.reasons.values


@pytest.mark.parametrize(
    ("drift_v_per_s", "refused"),
    ((0.002, False), (0.002001, True)),
)
def test_voltage_plateau_slope_boundary(drift_v_per_s: float, refused: bool) -> None:
    estimate = identify_load_steps(
        BLACKOUT_ID,
        SEGMENT_ID,
        _custom_step(drift_v_per_s=drift_v_per_s),
    )[0]

    assert (
        IdentificationReason.VOLTAGE_PLATEAU_SLOPE_TOO_LARGE in estimate.reasons.values
    ) is refused


@pytest.mark.parametrize(
    ("drift_v_per_s", "refused"),
    ((0.000333, False), (0.000334, True)),
)
def test_late_discharge_drift_boundary(drift_v_per_s: float, refused: bool) -> None:
    estimate = identify_load_steps(
        BLACKOUT_ID,
        SEGMENT_ID,
        _custom_step(drift_v_per_s=drift_v_per_s),
    )[0]

    assert (IdentificationReason.DISCHARGE_DRIFT_TOO_LARGE in estimate.reasons.values) is refused


@pytest.mark.parametrize(
    ("k_v_per_pp", "refused"),
    ((0.007499, True), (0.0075, False), (0.007501, False)),
)
def test_voltage_movement_floor_boundary(k_v_per_pp: float, refused: bool) -> None:
    estimate = identify_load_steps(
        BLACKOUT_ID,
        SEGMENT_ID,
        _custom_step(k_v_per_pp=k_v_per_pp, drift_v_per_s=0.0),
    )[0]

    assert (IdentificationReason.VOLTAGE_MOVEMENT_TOO_SMALL in estimate.reasons.values) is refused


def test_same_direction_voltage_and_load_is_refused() -> None:
    estimate = identify_load_steps(
        BLACKOUT_ID,
        SEGMENT_ID,
        _custom_step(k_v_per_pp=-0.020, drift_v_per_s=0.0),
    )[0]

    assert IdentificationReason.VOLTAGE_LOAD_DIRECTION_MISMATCH in estimate.reasons.values


def test_nonphysical_voltage_in_step_window_is_refused() -> None:
    observations = tuple(
        replace(item, battery_voltage_v=16.0) if item.sequence == 71 else item
        for item in _custom_step()
    )

    estimate = identify_load_steps(BLACKOUT_ID, SEGMENT_ID, observations)[0]

    assert IdentificationReason.INVALID_STEP_VOLTAGE in estimate.reasons.values


@pytest.mark.parametrize(
    ("k_v_per_pp", "refused"),
    (
        (DEFAULT_IR_LEARNING_POLICY.max_k_v_per_pp, False),
        (DEFAULT_IR_LEARNING_POLICY.max_k_v_per_pp + 0.000001, True),
    ),
)
def test_ir_estimate_upper_bound(k_v_per_pp: float, refused: bool) -> None:
    estimate = identify_load_steps(
        BLACKOUT_ID,
        SEGMENT_ID,
        _custom_step(k_v_per_pp=k_v_per_pp, drift_v_per_s=0.0),
    )[0]

    assert (IdentificationReason.IR_ESTIMATE_OUT_OF_RANGE in estimate.reasons.values) is refused


def test_ir_signature_cannot_accept_model_outputs():
    signature = inspect.signature(identify_load_steps)
    assert tuple(signature.parameters) == ("blackout_id", "segment_id", "observations")
    raw_fields = IrRawObservation.__dataclass_fields__
    forbidden = {"snapshot", "soc", "runtime", "lut", "residual", "ir_k"}
    assert forbidden.isdisjoint(raw_fields)


def _candidate(
    base,
    event_index,
    direction,
    *,
    revision="eval-1",
    transition_offset=0,
):
    event_id = chr(ord("c") + event_index) * 32
    estimate = replace(
        base,
        blackout_id=event_id,
        delta_load_pp=20.0 * direction,
        transition_monotonic_ns=base.transition_monotonic_ns + transition_offset,
    )
    return CohortStep(
        estimate=estimate,
        battery_epoch_id="epoch-a",
        evaluation_revision=revision,
        event_started_utc=START + timedelta(days=event_index),
        step_record_hash=chr(ord("1") + event_index) * 64,
    )


def _candidate_with_policy(base, event_index, direction, policy):
    return replace(
        _candidate(base, event_index, direction),
        learning_policy=policy,
    )


def test_cohort_requires_four_steps_two_events_and_both_directions():
    base = identify_load_steps(BLACKOUT_ID, SEGMENT_ID, _raw_step())[0]
    candidates = tuple(_candidate(base, index, 1 if index % 2 == 0 else -1) for index in range(4))
    current_id = candidates[-1].estimate.blackout_id
    selection = select_ir_cohort(
        candidates,
        IrCohortContext(current_id, "epoch-a", "eval-1", frozenset(), True, 0),
    )
    assert selection.estimate.median_k_v_per_pp == pytest.approx(0.020)
    assert selection.estimate.step_count == 4
    assert selection.estimate.up_step_count == 2
    assert selection.estimate.down_step_count == 2
    assert len(selection.consumed_step_hashes) == 4


def test_mixed_revision_or_unavailable_projection_refuses_exactly():
    base = identify_load_steps(BLACKOUT_ID, SEGMENT_ID, _raw_step())[0]
    candidates = tuple(
        _candidate(
            base,
            index,
            1 if index % 2 == 0 else -1,
            revision="old" if index == 0 else "eval-1",
        )
        for index in range(4)
    )
    mixed = select_ir_cohort(
        candidates,
        IrCohortContext(
            candidates[-1].estimate.blackout_id,
            "epoch-a",
            "eval-1",
            frozenset(),
            True,
            0,
        ),
    )
    assert IdentificationReason.MIXED_EVALUATION_REVISION in mixed.estimate.reasons.values
    assert mixed.estimate.median_k_v_per_pp is None
    unavailable = select_ir_cohort(
        candidates,
        IrCohortContext(
            candidates[-1].estimate.blackout_id,
            "epoch-a",
            "eval-1",
            frozenset(),
            False,
            0,
        ),
    )
    assert unavailable.estimate.reasons.values == (
        IdentificationReason.COHORT_PROJECTION_UNAVAILABLE,
    )


@pytest.mark.parametrize(
    ("case", "candidate_policy", "context_policy", "expected"),
    (
        ("old-known", DEFAULT_IR_LEARNING_POLICY, DEFAULT_IR_LEARNING_POLICY, "eligible"),
        (
            "unknown",
            replace(DEFAULT_IR_LEARNING_POLICY, revision="future-policy"),
            DEFAULT_IR_LEARNING_POLICY,
            "mixed",
        ),
        (
            "mixed",
            replace(DEFAULT_IR_LEARNING_POLICY, deadband_v_per_pp=0.002),
            DEFAULT_IR_LEARNING_POLICY,
            "mixed",
        ),
        (
            "mismatch",
            DEFAULT_IR_LEARNING_POLICY,
            replace(DEFAULT_IR_LEARNING_POLICY, deadband_v_per_pp=0.002),
            "context-refused",
        ),
        ("crash-replay", DEFAULT_IR_LEARNING_POLICY, DEFAULT_IR_LEARNING_POLICY, "replay"),
    ),
)
def test_learning_policy_revision_value_replay_matrix(
    case,
    candidate_policy,
    context_policy,
    expected,
):
    base = identify_load_steps(BLACKOUT_ID, SEGMENT_ID, _raw_step())[0]
    candidates = tuple(
        _candidate_with_policy(
            base,
            index,
            1 if index % 2 == 0 else -1,
            candidate_policy if index == 0 else DEFAULT_IR_LEARNING_POLICY,
        )
        for index in range(4)
    )
    current_id = candidates[-1].estimate.blackout_id

    if expected == "context-refused":
        with pytest.raises(ValueError, match="values do not match"):
            IrCohortContext(
                current_id,
                "epoch-a",
                "eval-1",
                frozenset(),
                True,
                0,
                learning_policy=context_policy,
            )
        return

    context = IrCohortContext(
        current_id,
        "epoch-a",
        "eval-1",
        frozenset(),
        True,
        0,
        learning_policy=context_policy,
    )
    selection = select_ir_cohort(candidates, context)

    if expected == "eligible":
        assert selection.estimate.median_k_v_per_pp == pytest.approx(0.020)
        return
    if expected == "mixed":
        assert IdentificationReason.MIXED_LEARNING_POLICY in selection.estimate.reasons.values
        assert selection.estimate.median_k_v_per_pp is None
        return

    replayed = select_ir_cohort(tuple(candidates), context)
    assert expected == "replay"
    assert replayed == selection


def test_third_current_event_position_is_not_counted_or_consumed():
    base = identify_load_steps(BLACKOUT_ID, SEGMENT_ID, _raw_step())[0]
    historical = tuple(_candidate(base, index, 1 if index % 2 == 0 else -1) for index in range(3))
    current_id = "f" * 32
    current = tuple(
        replace(
            _candidate(base, 10 + index, 1 if index % 2 == 0 else -1),
            estimate=replace(
                base,
                blackout_id=current_id,
                delta_load_pp=20.0 * (1 if index % 2 == 0 else -1),
                transition_monotonic_ns=base.transition_monotonic_ns + index,
            ),
            event_started_utc=START + timedelta(days=10),
        )
        for index in range(3)
    )
    candidates = (*historical, *current)
    context = IrCohortContext(current_id, "epoch-a", "eval-1", frozenset(), True, 0)
    selection = select_ir_cohort(candidates, context)

    assert selected_current_event_step_count(current, current_id, "epoch-a") == 2
    assert selection.estimate.step_count == 5
    assert len(selection.consumed_step_hashes) == 5


def test_third_step_does_not_replace_consumed_first_position():
    base = identify_load_steps(BLACKOUT_ID, SEGMENT_ID, _raw_step())[0]
    first = _candidate(base, 0, 1, transition_offset=0)
    second = replace(
        first,
        estimate=replace(first.estimate, transition_monotonic_ns=40_000_000_000),
        step_record_hash="8" * 64,
    )
    third = replace(
        first,
        estimate=replace(first.estimate, transition_monotonic_ns=50_000_000_000),
        step_record_hash="9" * 64,
    )
    others = tuple(_candidate(base, index, -1 if index == 1 else 1) for index in range(1, 3))
    candidates = (first, second, third, *others)
    current_id = others[-1].estimate.blackout_id
    selection = select_ir_cohort(
        candidates,
        IrCohortContext(
            current_id,
            "epoch-a",
            "eval-1",
            frozenset({first.step_record_hash}),
            True,
            0,
        ),
    )
    assert selection.estimate.median_k_v_per_pp is None
    assert IdentificationReason.INSUFFICIENT_UNCONSUMED_STEPS in selection.estimate.reasons.values
    assert third.step_record_hash not in selection.consumed_step_hashes


def test_cohort_universe_filters_epoch_before_taking_current_plus_31_events():
    base = identify_load_steps(BLACKOUT_ID, SEGMENT_ID, _raw_step())[0]
    same_epoch = tuple(_candidate(base, index, 1 if index % 2 == 0 else -1) for index in range(4))
    current = replace(same_epoch[-1], event_started_utc=START + timedelta(days=100))
    old_epoch = tuple(
        replace(
            _candidate(base, 0, 1 if index % 2 == 0 else -1),
            estimate=replace(
                same_epoch[0].estimate,
                blackout_id=f"{index + 100:032x}",
                delta_load_pp=20.0 if index % 2 == 0 else -20.0,
            ),
            battery_epoch_id="epoch-old",
            event_started_utc=START + timedelta(days=69 + index),
            step_record_hash=f"{index + 100:064x}",
        )
        for index in range(31)
    )

    selection = select_ir_cohort(
        (*same_epoch[:-1], *old_epoch, current),
        IrCohortContext(current.estimate.blackout_id, "epoch-a", "eval-1", frozenset(), True, 0),
    )

    assert selection.estimate.median_k_v_per_pp == pytest.approx(0.020)
    assert selection.estimate.step_count == 4
    assert IdentificationReason.MIXED_BATTERY_EPOCH not in selection.estimate.reasons.values


def test_cohort_requires_three_steps_other_than_the_current_blackout():
    base = identify_load_steps(BLACKOUT_ID, SEGMENT_ID, _raw_step())[0]
    current_first = _candidate(base, 0, 1, transition_offset=0)
    current_second = replace(
        current_first,
        estimate=replace(current_first.estimate, transition_monotonic_ns=40_000_000_000),
        step_record_hash="8" * 64,
    )
    other_first = _candidate(base, 1, -1)
    other_second = replace(
        other_first,
        estimate=replace(other_first.estimate, transition_monotonic_ns=40_000_000_000),
        step_record_hash="9" * 64,
    )

    selection = select_ir_cohort(
        (current_first, current_second, other_first, other_second),
        IrCohortContext(
            current_first.estimate.blackout_id,
            "epoch-a",
            "eval-1",
            frozenset(),
            True,
            0,
        ),
    )

    assert selection.estimate.median_k_v_per_pp is None
    assert IdentificationReason.INSUFFICIENT_UNCONSUMED_STEPS in selection.estimate.reasons.values
