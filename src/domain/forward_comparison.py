"""Frozen forward-model comparison over only the observed interval."""

from dataclasses import dataclass
from math import isclose, isfinite, sqrt
from statistics import fmean, median, pstdev

from src.battery_math.lut import inverse_lut_voltage, soc_from_voltage
from src.battery_math.peukert import peukert_runtime_hours
from src.domain.reasons import ComparisonReason, ReasonCode, order_reasons
from src.domain.timeline import summarize_timeline
from src.domain.values import (
    ComparisonMode,
    EvidenceAssessment,
    EvidenceClass,
    ForwardComparison,
    FrozenModelSnapshot,
    PhysicalObservation,
)

ORIGIN_DELAY_S = 60.0
ORIGIN_WINDOW_POINTS = 31
MAX_STABLE_LOAD_STDDEV_PP = 2.0
MAX_STABLE_GAP_S = 2.5
SHORT_MIN_DURATION_S = 180.0
SHORT_STABLE_SEGMENT_S = 120.0
FULL_MIN_DURATION_S = 300.0
FULL_MIN_MOVEMENT_V = 0.20
MAX_INTEGRATION_GAP_S = 5.0


@dataclass(frozen=True, slots=True)
class _EvaluationOrigin:
    index: int
    voltage_v: float
    load_percent: float


def compare_forward_model(
    observations: tuple[PhysicalObservation, ...],
    snapshot: FrozenModelSnapshot,
    assessment: EvidenceAssessment,
) -> ForwardComparison:
    """Compare a frozen model with raw observations; never identify parameters."""
    if assessment.evidence_class != EvidenceClass.QUALIFYING:
        return _refused(assessment.reasons.values)
    origin = _find_evaluation_origin(observations)
    if origin is None:
        if assessment.duration_s < SHORT_MIN_DURATION_S:
            return _refused((ComparisonReason.COMPARISON_NOT_ATTEMPTED,))
        return _refused((ComparisonReason.NO_STABLE_POST_TRANSFER_ORIGIN,))

    evaluated = observations[origin.index :]
    timeline = summarize_timeline(evaluated, 5.0)
    normalized_movement = abs(
        _normalized_voltage(evaluated[-1], snapshot) - _normalized_origin(origin, snapshot)
    )
    has_short_segment = _has_stable_segment(evaluated)
    mode, mode_reasons = _select_mode(
        timeline.duration_s,
        normalized_movement,
        has_short_segment,
    )
    if mode == ComparisonMode.NONE:
        return _refused(
            mode_reasons,
            evaluated_duration_s=timeline.duration_s,
            point_count=len(evaluated),
            origin_monotonic_ns=evaluated[0].monotonic_ns,
        )

    prediction = _predict(evaluated, snapshot, origin)
    if prediction is None:
        return _refused(
            (ComparisonReason.INVALID_EFFECTIVE_RUNTIME,),
            evaluated_duration_s=timeline.duration_s,
            point_count=len(evaluated),
            origin_monotonic_ns=evaluated[0].monotonic_ns,
        )
    predicted_voltages, predicted_socs, delivered_ah_proxy = prediction
    observed_voltages = (
        origin.voltage_v,
        *(tuple(_voltage(observation) for observation in evaluated[1:])),
    )
    residuals = tuple(
        observed - predicted
        for observed, predicted in zip(observed_voltages, predicted_voltages, strict=True)
    )
    uncertainty_reason = _uncertainty_reason(
        residuals[-1],
        predicted_socs[-1],
        snapshot,
    )
    reasons = order_reasons(tuple((*mode_reasons, uncertainty_reason)))
    return ForwardComparison(
        mode=mode,
        evaluation_origin_monotonic_ns=evaluated[0].monotonic_ns,
        evaluated_duration_s=timeline.duration_s,
        point_count=len(evaluated),
        start_residual_v=residuals[0],
        end_residual_v=residuals[-1],
        mean_residual_v=fmean(residuals),
        rmse_v=sqrt(fmean(tuple(residual * residual for residual in residuals))),
        observed_slope_v_per_s=_slope(observed_voltages, timeline.duration_s),
        predicted_slope_v_per_s=_slope(predicted_voltages, timeline.duration_s),
        delivered_ah_proxy=delivered_ah_proxy,
        reasons=reasons,
    )


def _find_evaluation_origin(
    observations: tuple[PhysicalObservation, ...],
) -> _EvaluationOrigin | None:
    if len(observations) < ORIGIN_WINDOW_POINTS:
        return None
    start_ns = observations[0].monotonic_ns
    last_start = len(observations) - ORIGIN_WINDOW_POINTS
    for index in range(last_start + 1):
        if (observations[index].monotonic_ns - start_ns) / 1_000_000_000 < ORIGIN_DELAY_S:
            continue
        window = observations[index : index + ORIGIN_WINDOW_POINTS]
        if _stable_window(window):
            return _EvaluationOrigin(
                index=index + ORIGIN_WINDOW_POINTS // 2,
                voltage_v=median(tuple(_voltage(observation) for observation in window)),
                load_percent=median(tuple(_load(observation) for observation in window)),
            )
    return None


def _stable_window(observations: tuple[PhysicalObservation, ...]) -> bool:
    if not observations or not all(_valid_point(observation) for observation in observations):
        return False
    loads = tuple(_load(observation) for observation in observations)
    timeline = summarize_timeline(observations, MAX_STABLE_GAP_S)
    return (
        not timeline.reboot_gap_observed
        and timeline.non_increasing_edge_count == 0
        and timeline.max_gap_s <= MAX_STABLE_GAP_S
        and pstdev(loads) <= MAX_STABLE_LOAD_STDDEV_PP
    )


def _has_stable_segment(observations: tuple[PhysicalObservation, ...]) -> bool:
    for start_index in range(len(observations)):
        for end_index in range(start_index + 1, len(observations)):
            duration_s = (
                observations[end_index].monotonic_ns - observations[start_index].monotonic_ns
            ) / 1_000_000_000
            if duration_s < SHORT_STABLE_SEGMENT_S:
                continue
            if _stable_window(observations[start_index : end_index + 1]):
                return True
            break
    return False


def _select_mode(
    duration_s: float,
    normalized_movement_v: float,
    has_short_segment: bool,
) -> tuple[ComparisonMode, tuple[ComparisonReason, ...]]:
    movement_qualifies = normalized_movement_v >= FULL_MIN_MOVEMENT_V or isclose(
        normalized_movement_v,
        FULL_MIN_MOVEMENT_V,
        rel_tol=0.0,
        abs_tol=1e-12,
    )
    if duration_s >= FULL_MIN_DURATION_S and movement_qualifies:
        return ComparisonMode.FULL, ()
    if duration_s >= SHORT_MIN_DURATION_S and has_short_segment:
        return ComparisonMode.SHORT_WINDOW, (ComparisonReason.SHORT_WINDOW_COMPARISON,)
    if duration_s < SHORT_MIN_DURATION_S:
        return ComparisonMode.NONE, (ComparisonReason.COMPARISON_NOT_ATTEMPTED,)
    reasons: list[ComparisonReason] = []
    if duration_s < FULL_MIN_DURATION_S:
        reasons.append(ComparisonReason.INSUFFICIENT_EVALUATED_DURATION)
    elif normalized_movement_v < FULL_MIN_MOVEMENT_V:
        reasons.append(ComparisonReason.INSUFFICIENT_NORMALIZED_MOVEMENT)
    if not has_short_segment:
        reasons.append(ComparisonReason.NO_QUALIFYING_SHORT_WINDOW)
    return ComparisonMode.NONE, tuple(reasons)


def _predict(
    observations: tuple[PhysicalObservation, ...],
    snapshot: FrozenModelSnapshot,
    origin: _EvaluationOrigin,
) -> tuple[tuple[float, ...], tuple[float, ...], float] | None:
    start_normalized = _normalized_origin(origin, snapshot)
    soc = soc_from_voltage(start_normalized, snapshot.lut)
    predicted = inverse_lut_voltage(soc, snapshot.lut) - snapshot.ir_k_v_per_pp * (
        origin.load_percent - snapshot.ir_reference_load_percent
    )
    predicted_voltages = [predicted]
    predicted_socs = [soc]
    delivered_ah_proxy = 0.0
    for edge_index, (left, right) in enumerate(zip(observations, observations[1:], strict=False)):
        delta_s = (right.monotonic_ns - left.monotonic_ns) / 1_000_000_000
        if not _accepted_edge(left, right, delta_s):
            return None
        left_load = origin.load_percent if edge_index == 0 else _load(left)
        load_mid = (left_load + _load(right)) / 2.0
        healthy_runtime_h = peukert_runtime_hours(
            load_mid,
            snapshot.rated_capacity_ah,
            snapshot.peukert_exponent,
            snapshot.nominal_voltage_v,
            snapshot.nominal_power_watts,
        )
        effective_runtime_s = 3600.0 * healthy_runtime_h * snapshot.soh
        if not isfinite(effective_runtime_s) or effective_runtime_s <= 0.0:
            return None
        soc = max(0.0, min(1.0, soc - delta_s / effective_runtime_s))
        normalized_prediction = inverse_lut_voltage(soc, snapshot.lut)
        predicted_voltages.append(
            normalized_prediction
            - snapshot.ir_k_v_per_pp * (_load(right) - snapshot.ir_reference_load_percent)
        )
        predicted_socs.append(soc)
        current_a = load_mid / 100.0 * snapshot.nominal_power_watts / snapshot.nominal_voltage_v
        delivered_ah_proxy += current_a * delta_s / 3600.0
    return tuple(predicted_voltages), tuple(predicted_socs), delivered_ah_proxy


def _uncertainty_reason(
    residual_v: float,
    predicted_soc: float,
    snapshot: FrozenModelSnapshot,
) -> ComparisonReason:
    center_voltage = inverse_lut_voltage(predicted_soc, snapshot.lut)
    low_voltage = inverse_lut_voltage(max(0.0, predicted_soc - 0.05), snapshot.lut)
    high_voltage = inverse_lut_voltage(min(1.0, predicted_soc + 0.05), snapshot.lut)
    uncertainty_v = max(abs(low_voltage - center_voltage), abs(high_voltage - center_voltage))
    if abs(residual_v) <= uncertainty_v:
        return ComparisonReason.WITHIN_KNOWN_LUT_FRAME_UNCERTAINTY
    return ComparisonReason.BEYOND_KNOWN_LUT_FRAME_UNCERTAINTY


def _normalized_voltage(
    observation: PhysicalObservation,
    snapshot: FrozenModelSnapshot,
) -> float:
    return _voltage(observation) + snapshot.ir_k_v_per_pp * (
        _load(observation) - snapshot.ir_reference_load_percent
    )


def _normalized_origin(origin: _EvaluationOrigin, snapshot: FrozenModelSnapshot) -> float:
    return origin.voltage_v + snapshot.ir_k_v_per_pp * (
        origin.load_percent - snapshot.ir_reference_load_percent
    )


def _accepted_edge(
    left: PhysicalObservation,
    right: PhysicalObservation,
    delta_s: float,
) -> bool:
    return (
        left.boot_id == right.boot_id
        and isfinite(delta_s)
        and 0.0 < delta_s <= MAX_INTEGRATION_GAP_S
    )


def _valid_point(observation: PhysicalObservation) -> bool:
    return (
        observation.battery_voltage_v is not None
        and isfinite(observation.battery_voltage_v)
        and 8.0 <= observation.battery_voltage_v <= 15.0
        and observation.load_percent is not None
        and isfinite(observation.load_percent)
        and 0.0 <= observation.load_percent <= 100.0
    )


def _voltage(observation: PhysicalObservation) -> float:
    assert observation.battery_voltage_v is not None
    return observation.battery_voltage_v


def _load(observation: PhysicalObservation) -> float:
    assert observation.load_percent is not None
    return observation.load_percent


def _slope(values: tuple[float, ...], duration_s: float) -> float:
    return (values[-1] - values[0]) / duration_s if duration_s > 0.0 else 0.0


def _refused(
    reasons: tuple[ReasonCode, ...],
    *,
    evaluated_duration_s: float = 0.0,
    point_count: int = 0,
    origin_monotonic_ns: int | None = None,
) -> ForwardComparison:
    return ForwardComparison(
        mode=ComparisonMode.NONE,
        evaluation_origin_monotonic_ns=origin_monotonic_ns,
        evaluated_duration_s=evaluated_duration_s,
        point_count=point_count,
        start_residual_v=None,
        end_residual_v=None,
        mean_residual_v=None,
        rmse_v=None,
        observed_slope_v_per_s=None,
        predicted_slope_v_per_s=None,
        delivered_ah_proxy=None,
        reasons=order_reasons(reasons),
    )
