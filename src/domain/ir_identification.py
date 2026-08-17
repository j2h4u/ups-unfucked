"""Independent raw load-step identification and deterministic cohort policy."""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from math import isfinite
from statistics import median, pstdev

from src.domain.reasons import IdentificationReason, order_reasons
from src.domain.values import (
    DEFAULT_IR_LEARNING_POLICY,
    IrCohortEstimate,
    IrLearningPolicy,
    LoadStepEstimate,
    StepQuality,
)

MIN_STEP_PP = 15.0
MIN_INITIAL_MOVEMENT_PP = 10.0
MAX_SETTLING_DISAGREEMENT = 0.15
MAX_PLATEAU_LOAD_PP = 50.0
MAX_LOAD_STDDEV_PP = 2.0
MAX_SLOPE_V_PER_S = 0.002
MAX_GAP_S = 2.5
MIN_TRANSITION_SEPARATION_S = 180.0
REARM_STABLE_SECONDS = 30.0
LATE_WINDOW_END_OFFSET = 120
MIN_NONOVERLAPPING_TRANSITION_OFFSET = LATE_WINDOW_END_OFFSET + 16


@dataclass(frozen=True, slots=True)
class IrRawObservation:
    """Raw-only estimator input; no model or derived value can be represented."""

    sequence: int
    boot_id: str
    monotonic_ns: int
    raw_status: str
    battery_voltage_v: float
    voltage_token_quantum_v: float
    load_percent: float


@dataclass(frozen=True, slots=True)
class CohortStep:
    estimate: LoadStepEstimate
    battery_epoch_id: str
    evaluation_revision: str
    event_started_utc: datetime
    step_record_hash: str
    learning_policy: IrLearningPolicy = DEFAULT_IR_LEARNING_POLICY


@dataclass(frozen=True, slots=True)
class IrCohortSelection:
    estimate: IrCohortEstimate
    consumed_step_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IrCohortContext:
    current_blackout_id: str
    battery_epoch_id: str
    evaluation_revision: str
    consumed_step_hashes: frozenset[str]
    projection_available: bool
    candidate_event_overflow: int
    learning_policy: IrLearningPolicy = DEFAULT_IR_LEARNING_POLICY

    def __post_init__(self) -> None:
        from src.domain.values import ensure_supported_ir_learning_policy

        ensure_supported_ir_learning_policy(self.learning_policy)


@dataclass(frozen=True, slots=True)
class _StepWindows:
    pre: tuple[IrRawObservation, ...]
    early: tuple[IrRawObservation, ...]
    late: tuple[IrRawObservation, ...]
    transition: IrRawObservation


@dataclass(frozen=True, slots=True)
class _StepCalculation:
    pre_slope: float
    early_slope: float
    late_slope: float
    delta_load: float
    early_delta_voltage: float
    settled_delta_voltage: float
    quantum: float
    k_transition: float
    k_settled: float


def identify_load_steps(
    blackout_id: str,
    segment_id: str,
    observations: tuple[IrRawObservation, ...],
) -> tuple[LoadStepEstimate, ...]:
    """Detect and estimate non-overlapping raw within-battery load steps."""
    estimates: list[LoadStepEstimate] = []
    index = 15
    next_transition_ns = 0
    next_nonoverlapping_index = 0
    while index + 120 < len(observations):
        candidate = _candidate_windows(observations, index)
        if candidate is None:
            index += 1
            continue
        run_end = _candidate_run_end(observations, index)
        if (
            observations[index].monotonic_ns < next_transition_ns
            or index < next_nonoverlapping_index
        ):
            index = run_end
            continue
        estimate = _estimate_step(blackout_id, segment_id, observations, index)
        estimates.append(estimate)
        next_transition_ns = observations[index].monotonic_ns + int(
            MIN_TRANSITION_SEPARATION_S * 1_000_000_000
        )
        next_nonoverlapping_index = index + MIN_NONOVERLAPPING_TRANSITION_OFFSET
        rearmed_index = _first_rearmed_index(
            observations,
            index + LATE_WINDOW_END_OFFSET,
        )
        if rearmed_index is None:
            break
        index = max(run_end, rearmed_index)
    return tuple(estimates)


def select_ir_cohort(
    candidates: tuple[CohortStep, ...],
    context: IrCohortContext,
) -> IrCohortSelection:
    """Select the fixed current-plus-31 event universe without cherry-picking."""
    if not context.projection_available:
        estimate = _empty_cohort(
            context.battery_epoch_id,
            (IdentificationReason.COHORT_PROJECTION_UNAVAILABLE,),
        )
        return IrCohortSelection(estimate, ())

    first_two = _first_two_positions_in_universe(
        candidates,
        context.current_blackout_id,
        context.battery_epoch_id,
    )
    qualifying = tuple(
        candidate
        for candidate in first_two
        if candidate.estimate.quality == StepQuality.QUALIFYING
        and candidate.step_record_hash not in context.consumed_step_hashes
    )
    blackout_ids = tuple(sorted({candidate.estimate.blackout_id for candidate in qualifying}))
    up_count = sum(candidate.estimate.delta_load_pp > 0.0 for candidate in qualifying)
    down_count = sum(candidate.estimate.delta_load_pp < 0.0 for candidate in qualifying)
    values = tuple(candidate.estimate.k_settled_v_per_pp for candidate in qualifying)
    cohort_median = median(values) if values else None
    mad_ratio = _mad_ratio(values, cohort_median)
    blocking = _cohort_blocking_reasons(qualifying, context, up_count, down_count, mad_ratio)
    diagnostic = (
        (IdentificationReason.CANDIDATE_EVENT_OVERFLOW,)
        if context.candidate_event_overflow > 0
        else ()
    )
    reasons = order_reasons((*blocking, *diagnostic))
    estimate = IrCohortEstimate(
        battery_epoch_id=context.battery_epoch_id,
        blackout_ids=blackout_ids,
        step_count=len(qualifying),
        up_step_count=up_count,
        down_step_count=down_count,
        median_k_v_per_pp=cohort_median if not blocking else None,
        mad_ratio=mad_ratio,
        reasons=reasons,
    )
    hashes = tuple(sorted(candidate.step_record_hash for candidate in qualifying))
    return IrCohortSelection(estimate, hashes if not blocking else ())


def _mad_ratio(values: tuple[float, ...], cohort_median: float | None) -> float | None:
    if cohort_median is None or cohort_median <= 0.0:
        return None
    return median(tuple(abs(value - cohort_median) for value in values)) / cohort_median


def _cohort_blocking_reasons(
    qualifying: tuple[CohortStep, ...],
    context: IrCohortContext,
    up_count: int,
    down_count: int,
    mad_ratio: float | None,
) -> tuple[IdentificationReason, ...]:
    checks = (
        (
            len(qualifying) < 4
            or sum(
                candidate.estimate.blackout_id != context.current_blackout_id
                for candidate in qualifying
            )
            < 3,
            IdentificationReason.INSUFFICIENT_UNCONSUMED_STEPS,
        ),
        (
            not any(
                candidate.estimate.blackout_id == context.current_blackout_id
                for candidate in qualifying
            ),
            IdentificationReason.CURRENT_BLACKOUT_STEP_REQUIRED,
        ),
        (
            len({candidate.estimate.blackout_id for candidate in qualifying}) < 2,
            IdentificationReason.INSUFFICIENT_BLACKOUT_DIVERSITY,
        ),
        (
            {candidate.battery_epoch_id for candidate in qualifying} != {context.battery_epoch_id},
            IdentificationReason.MIXED_BATTERY_EPOCH,
        ),
        (
            {candidate.evaluation_revision for candidate in qualifying}
            != {context.evaluation_revision},
            IdentificationReason.MIXED_EVALUATION_REVISION,
        ),
        (
            {candidate.learning_policy for candidate in qualifying} != {context.learning_policy},
            IdentificationReason.MIXED_LEARNING_POLICY,
        ),
        (up_count == 0 or down_count == 0, IdentificationReason.BOTH_STEP_DIRECTIONS_REQUIRED),
        (
            mad_ratio is not None and mad_ratio > 0.25,
            IdentificationReason.HIGH_COHORT_DISPERSION,
        ),
    )
    return tuple(reason for failed, reason in checks if failed)


def _candidate_windows(
    observations: tuple[IrRawObservation, ...],
    index: int,
) -> tuple[tuple[IrRawObservation, ...], tuple[IrRawObservation, ...]] | None:
    pre = observations[index - 15 : index]
    post = observations[index + 10 : index + 26]
    pre_load = median(tuple(observation.load_percent for observation in pre))
    post_load = median(tuple(observation.load_percent for observation in post))
    if abs(post_load - pre_load) < MIN_STEP_PP:
        return None
    if abs(observations[index].load_percent - pre_load) < MIN_INITIAL_MOVEMENT_PP:
        return None
    reached_by_five = any(
        abs(observation.load_percent - post_load) <= 2.0
        for observation in observations[index : index + 6]
    )
    settled = all(
        abs(observation.load_percent - post_load) <= 2.0
        for observation in observations[index + 5 : index + 121]
    )
    return (pre, post) if reached_by_five and settled else None


def _candidate_run_end(
    observations: tuple[IrRawObservation, ...],
    first_index: int,
) -> int:
    """Return the first index after one contiguous run of candidate instants."""
    index = first_index + 1
    while index + LATE_WINDOW_END_OFFSET < len(observations):
        if _candidate_windows(observations, index) is None:
            break
        index += 1
    return index


def _first_rearmed_index(
    observations: tuple[IrRawObservation, ...],
    late_window_end_index: int,
) -> int | None:
    """Return the first scan index after 30 stable post-window seconds."""
    for start_index in range(late_window_end_index, len(observations) - 1):
        for end_index in range(start_index + 1, len(observations)):
            left = observations[end_index - 1]
            right = observations[end_index]
            delta_s = (right.monotonic_ns - left.monotonic_ns) / 1_000_000_000
            if left.boot_id != right.boot_id or not 0.0 < delta_s <= MAX_GAP_S:
                break
            duration_s = (
                observations[end_index].monotonic_ns - observations[start_index].monotonic_ns
            ) / 1_000_000_000
            if duration_s < REARM_STABLE_SECONDS:
                continue
            window = observations[start_index : end_index + 1]
            if _stable_rearm_plateau(window):
                return end_index + 1
            break
    return None


def _stable_rearm_plateau(observations: tuple[IrRawObservation, ...]) -> bool:
    loads = tuple(observation.load_percent for observation in observations)
    if not loads or any(not isfinite(load) for load in loads):
        return False
    plateau_load = median(loads)
    return pstdev(loads) <= MAX_LOAD_STDDEV_PP and all(
        abs(load - plateau_load) <= 2.0 for load in loads
    )


def _estimate_step(
    blackout_id: str,
    segment_id: str,
    observations: tuple[IrRawObservation, ...],
    index: int,
) -> LoadStepEstimate:
    windows = _step_windows(observations, index)
    calculation = _calculate_step(windows)
    reasons = _step_reasons(
        observations[index - 15 : index + 121],
        windows,
        calculation,
    )
    return LoadStepEstimate(
        step_id=_stable_step_id(
            blackout_id,
            segment_id,
            windows.pre[-1].sequence,
            windows.transition.sequence,
        ),
        blackout_id=blackout_id,
        segment_id=segment_id,
        pre_sequences=tuple(observation.sequence for observation in windows.pre),
        post_sequences=tuple(
            observation.sequence for observation in (*windows.early, *windows.late)
        ),
        transition_monotonic_ns=windows.transition.monotonic_ns,
        pre_slope_v_per_s=calculation.pre_slope,
        early_post_slope_v_per_s=calculation.early_slope,
        late_post_slope_v_per_s=calculation.late_slope,
        delta_load_pp=calculation.delta_load,
        early_delta_voltage_at_transition_v=calculation.early_delta_voltage,
        settled_delta_voltage_at_transition_v=calculation.settled_delta_voltage,
        voltage_quantum_v=calculation.quantum,
        k_transition_v_per_pp=calculation.k_transition,
        k_settled_v_per_pp=calculation.k_settled,
        quality=StepQuality.QUALIFYING if not reasons else StepQuality.OBSERVED_ONLY,
        reasons=order_reasons(reasons),
    )


def _step_windows(
    observations: tuple[IrRawObservation, ...],
    index: int,
) -> _StepWindows:
    return _StepWindows(
        pre=observations[index - 15 : index],
        early=observations[index + 10 : index + 26],
        late=observations[index + 60 : index + 121],
        transition=observations[index],
    )


def _calculate_step(windows: _StepWindows) -> _StepCalculation:
    transition_ns = windows.transition.monotonic_ns
    pre_intercept, pre_slope = _linear_fit_at_transition(windows.pre, transition_ns)
    early_intercept, early_slope = _linear_fit_at_transition(windows.early, transition_ns)
    late_intercept, late_slope = _linear_fit_at_transition(windows.late, transition_ns)
    delta_load = median(tuple(item.load_percent for item in windows.early)) - median(
        tuple(item.load_percent for item in windows.pre)
    )
    early_delta_voltage = early_intercept - pre_intercept
    settled_delta_voltage = late_intercept - pre_intercept
    return _StepCalculation(
        pre_slope=pre_slope,
        early_slope=early_slope,
        late_slope=late_slope,
        delta_load=delta_load,
        early_delta_voltage=early_delta_voltage,
        settled_delta_voltage=settled_delta_voltage,
        quantum=max(
            observation.voltage_token_quantum_v
            for observation in (*windows.pre, *windows.early, *windows.late)
        ),
        k_transition=-early_delta_voltage / delta_load,
        k_settled=-settled_delta_voltage / delta_load,
    )


def _step_reasons(
    all_points: tuple[IrRawObservation, ...],
    windows: _StepWindows,
    calculation: _StepCalculation,
) -> tuple[IdentificationReason, ...]:
    """Evaluate independent gates in their canonical serialized order."""
    return (
        *_raw_window_reasons(all_points),
        *_plateau_reasons(windows, calculation),
        *_voltage_change_reasons(all_points, calculation),
        *_estimate_reasons(calculation),
    )


def _raw_window_reasons(
    all_points: tuple[IrRawObservation, ...],
) -> tuple[IdentificationReason, ...]:
    reasons: list[IdentificationReason] = []
    if any(
        "OB" not in point.raw_status.split()
        or "CAL" in point.raw_status.split()
        or "LB" in point.raw_status.split()
        for point in all_points
    ):
        reasons.append(IdentificationReason.STEP_STATUS_NOT_ELIGIBLE)
    if len({point.boot_id for point in all_points}) != 1:
        reasons.append(IdentificationReason.STEP_CROSSES_BOOT)
    if _has_invalid_timeline_edge(all_points):
        reasons.append(IdentificationReason.STEP_GAP_TOO_LARGE)
    return tuple(reasons)


def _plateau_reasons(
    windows: _StepWindows,
    calculation: _StepCalculation,
) -> tuple[IdentificationReason, ...]:
    reasons: list[IdentificationReason] = []
    plateau_loads = tuple(
        tuple(point.load_percent for point in part)
        for part in (windows.pre, windows.early, windows.late)
    )
    plateau_medians = tuple(median(loads) for loads in plateau_loads)
    if any(not 0.0 < load <= MAX_PLATEAU_LOAD_PP for load in plateau_medians):
        reasons.append(IdentificationReason.LOAD_OUT_OF_RANGE)
    if any(pstdev(loads) > MAX_LOAD_STDDEV_PP for loads in plateau_loads):
        reasons.append(IdentificationReason.LOAD_PLATEAU_UNSTABLE)
    slopes = (calculation.pre_slope, calculation.early_slope, calculation.late_slope)
    if any(abs(slope) > MAX_SLOPE_V_PER_S for slope in slopes):
        reasons.append(IdentificationReason.VOLTAGE_PLATEAU_SLOPE_TOO_LARGE)
    if _drift_too_large(
        slopes,
        calculation.early_delta_voltage,
        calculation.settled_delta_voltage,
    ):
        reasons.append(IdentificationReason.DISCHARGE_DRIFT_TOO_LARGE)
    return tuple(reasons)


def _voltage_change_reasons(
    all_points: tuple[IrRawObservation, ...],
    calculation: _StepCalculation,
) -> tuple[IdentificationReason, ...]:
    reasons: list[IdentificationReason] = []
    movement_floor = max(0.15, 3.0 * calculation.quantum)
    if (
        abs(calculation.early_delta_voltage) < movement_floor
        or abs(calculation.settled_delta_voltage) < movement_floor
    ):
        reasons.append(IdentificationReason.VOLTAGE_MOVEMENT_TOO_SMALL)
    if (
        calculation.delta_load * calculation.early_delta_voltage >= 0.0
        or calculation.delta_load * calculation.settled_delta_voltage >= 0.0
    ):
        reasons.append(IdentificationReason.VOLTAGE_LOAD_DIRECTION_MISMATCH)
    if any(
        not isfinite(point.battery_voltage_v) or not 8.0 <= point.battery_voltage_v <= 15.0
        for point in all_points
    ):
        reasons.append(IdentificationReason.INVALID_STEP_VOLTAGE)
    return tuple(reasons)


def _estimate_reasons(calculation: _StepCalculation) -> tuple[IdentificationReason, ...]:
    estimates = (calculation.k_transition, calculation.k_settled)
    if not all(
        isfinite(value)
        and DEFAULT_IR_LEARNING_POLICY.min_k_v_per_pp
        <= value
        <= DEFAULT_IR_LEARNING_POLICY.max_k_v_per_pp
        for value in estimates
    ):
        return (IdentificationReason.IR_ESTIMATE_OUT_OF_RANGE,)
    if (
        abs(calculation.k_settled - calculation.k_transition) / calculation.k_transition
        > MAX_SETTLING_DISAGREEMENT
    ):
        return (IdentificationReason.SAG_NOT_SETTLED,)
    return ()


def _linear_fit_at_transition(
    observations: tuple[IrRawObservation, ...],
    transition_ns: int,
) -> tuple[float, float]:
    times = tuple(
        (observation.monotonic_ns - transition_ns) / 1_000_000_000 for observation in observations
    )
    voltages = tuple(observation.battery_voltage_v for observation in observations)
    mean_time = sum(times) / len(times)
    mean_voltage = sum(voltages) / len(voltages)
    denominator = sum((time - mean_time) ** 2 for time in times)
    slope = (
        sum(
            (time - mean_time) * (voltage - mean_voltage)
            for time, voltage in zip(times, voltages, strict=True)
        )
        / denominator
        if denominator > 0.0
        else 0.0
    )
    return mean_voltage - slope * mean_time, slope


def _has_invalid_timeline_edge(observations: tuple[IrRawObservation, ...]) -> bool:
    for left, right in zip(observations, observations[1:], strict=False):
        if right.boot_id != left.boot_id:
            continue
        delta_s = (right.monotonic_ns - left.monotonic_ns) / 1_000_000_000
        if not 0.0 < delta_s <= MAX_GAP_S:
            return True
    return False


def _drift_too_large(
    slopes: tuple[float, float, float],
    early_delta_voltage: float,
    settled_delta_voltage: float,
) -> bool:
    if early_delta_voltage == 0.0 or settled_delta_voltage == 0.0:
        return True
    early_drift = max(abs(slopes[0]), abs(slopes[1])) * 25.0
    late_drift = max(abs(slopes[0]), abs(slopes[2])) * 120.0
    return early_drift > 0.10 * abs(early_delta_voltage) or late_drift > 0.10 * abs(
        settled_delta_voltage
    )


def _stable_step_id(
    blackout_id: str,
    segment_id: str,
    last_pre_sequence: int,
    first_post_sequence: int,
) -> str:
    if not blackout_id.isascii() or not blackout_id.isalnum():
        raise ValueError("blackout_id must be canonical lowercase UUID hex")
    if not segment_id.isascii() or not segment_id.isalnum():
        raise ValueError("segment_id must be canonical lowercase UUID hex")
    canonical_array = f'["{blackout_id}","{segment_id}",{last_pre_sequence},{first_post_sequence}]'
    return sha256(canonical_array.encode("utf-8")).hexdigest()


def _first_two_positions_in_universe(
    candidates: tuple[CohortStep, ...],
    current_blackout_id: str,
    battery_epoch_id: str,
) -> tuple[CohortStep, ...]:
    same_epoch = tuple(
        candidate for candidate in candidates if candidate.battery_epoch_id == battery_epoch_id
    )
    event_keys = sorted(
        {(candidate.event_started_utc, candidate.estimate.blackout_id) for candidate in same_epoch}
    )
    current_keys = tuple(key for key in event_keys if key[1] == current_blackout_id)
    if not current_keys:
        return ()
    current_key = current_keys[-1]
    eligible_event_keys = tuple(key for key in event_keys if key <= current_key)[-32:]
    selected: list[CohortStep] = []
    for event_key in eligible_event_keys:
        event_steps = sorted(
            (
                candidate
                for candidate in same_epoch
                if (candidate.event_started_utc, candidate.estimate.blackout_id) == event_key
            ),
            key=lambda candidate: candidate.estimate.transition_monotonic_ns,
        )
        selected.extend(event_steps[:2])
    return tuple(selected)


def selected_current_event_step_count(
    candidates: tuple[CohortStep, ...],
    current_blackout_id: str,
    battery_epoch_id: str,
) -> int:
    """Count only the positions the cohort selector can consume for this event."""
    current = tuple(
        candidate
        for candidate in candidates
        if candidate.battery_epoch_id == battery_epoch_id
        and candidate.estimate.blackout_id == current_blackout_id
    )
    return min(2, len(current))


def _empty_cohort(
    battery_epoch_id: str,
    reasons: tuple[IdentificationReason, ...],
) -> IrCohortEstimate:
    return IrCohortEstimate(
        battery_epoch_id=battery_epoch_id,
        blackout_ids=(),
        step_count=0,
        up_step_count=0,
        down_step_count=0,
        median_k_v_per_pp=None,
        mad_ratio=None,
        reasons=order_reasons(reasons),
    )
