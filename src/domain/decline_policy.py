"""Pure construction and eligibility policy for independent decline evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from statistics import fmean, pstdev

from src.domain.decline import (
    FirmwareReserveSample,
    LoadSagTrendSample,
    LongPartialSample,
)
from src.domain.forward_comparison import (
    MAX_STABLE_GAP_S,
    MAX_STABLE_LOAD_STDDEV_PP,
    ORIGIN_DELAY_S,
    ORIGIN_WINDOW_POINTS,
)
from src.domain.ir_identification import CohortStep, IrRawObservation, identify_load_steps
from src.domain.lifecycle import classify_physical_observation
from src.domain.timeline import summarize_timeline
from src.domain.values import (
    BlackoutKind,
    EvidenceAssessment,
    EvidenceClass,
    PhysicalObservation,
)

LONG_PARTIAL_HORIZON_S = 600.0


@dataclass(frozen=True, slots=True)
class DeclineEventContext:
    blackout_id: str
    segment_id: str
    started_utc: datetime
    battery_epoch_id: str


@dataclass(frozen=True, slots=True)
class _TrustedLbInterval:
    metric_prefix: tuple[PhysicalObservation, ...]
    provenance: tuple[PhysicalObservation, ...]
    lb_observation_index: int


def load_sag_samples(
    context: DeclineEventContext,
    observations: tuple[PhysicalObservation, ...],
) -> tuple[LoadSagTrendSample, ...]:
    raw = tuple(
        IrRawObservation(
            sequence=index,
            boot_id=item.boot_id,
            monotonic_ns=item.monotonic_ns,
            raw_status=item.raw_status,
            battery_voltage_v=required_number(item.battery_voltage_v),
            voltage_token_quantum_v=required_number(item.voltage_token_quantum_v),
            load_percent=required_number(item.load_percent),
        )
        for index, item in enumerate(observations)
    )
    estimates = identify_load_steps(context.blackout_id, context.segment_id, raw)
    return tuple(
        LoadSagTrendSample(
            blackout_id=context.blackout_id,
            event_started_utc=context.started_utc,
            battery_epoch_id=context.battery_epoch_id,
            transition_monotonic_ns=estimate.transition_monotonic_ns,
            k_settled_v_per_pp=estimate.k_settled_v_per_pp,
        )
        for estimate in estimates
        if estimate.quality.value == "qualifying"
    )


def firmware_sample(
    context: DeclineEventContext,
    ready_at_start: bool,
    observations: tuple[PhysicalObservation, ...],
) -> FirmwareReserveSample | None:
    """Build the trusted raw-LB prefix after a stable transfer origin."""
    interval = _trusted_lb_interval(observations)
    if interval is None:
        return None
    prefix = interval.metric_prefix
    loads = tuple(
        float(item.load_percent)
        for item in prefix
        if item.load_percent is not None and isfinite(item.load_percent)
    )
    first_voltage = prefix[0].battery_voltage_v
    if len(loads) != len(prefix) or first_voltage is None or not isfinite(first_voltage):
        return None
    timeline = summarize_timeline(prefix, 5.0)
    return FirmwareReserveSample(
        blackout_id=context.blackout_id,
        event_started_utc=context.started_utc,
        battery_epoch_id=context.battery_epoch_id,
        ready_at_start=ready_at_start,
        start_voltage_v=float(first_voltage),
        mean_load_percent=fmean(loads),
        load_stddev_percent=pstdev(loads),
        coverage_ratio=timeline.coverage_ratio,
        max_gap_s=timeline.max_gap_s,
        reserve_proxy_pp_s=integrated_load(prefix),
    )


def long_partial_sample(
    context: DeclineEventContext,
    ready_at_start: bool,
    observations: tuple[PhysicalObservation, ...],
) -> LongPartialSample:
    timeline = summarize_timeline(observations, 5.0)
    loads = tuple(required_number(item.load_percent) for item in observations)
    voltages = tuple(required_number(item.battery_voltage_v) for item in observations)
    return LongPartialSample(
        blackout_id=context.blackout_id,
        event_started_utc=context.started_utc,
        battery_epoch_id=context.battery_epoch_id,
        ready_at_start=ready_at_start,
        duration_s=timeline.duration_s,
        start_voltage_v=voltages[0],
        mean_load_percent=fmean(loads),
        load_stddev_percent=pstdev(loads),
        coverage_ratio=timeline.coverage_ratio,
        max_gap_s=timeline.max_gap_s,
        voltage_at_600s_v=voltage_at_horizon(observations),
    )


def decline_evidence_eligible(
    assessment: EvidenceAssessment,
    observations: tuple[PhysicalObservation, ...],
    steps: tuple[CohortStep, ...],
    ready_at_start: bool,
) -> bool:
    """Apply the independent raw-evidence eligibility thresholds."""
    if assessment.evidence_class != EvidenceClass.QUALIFYING:
        return False
    if any(step.estimate.quality.value == "qualifying" for step in steps):
        return True
    raw_lb = any("LB" in observation.raw_status.split() for observation in observations)
    return ready_at_start and (raw_lb or assessment.duration_s >= 650.0)


def integrated_load(observations: tuple[PhysicalObservation, ...]) -> float:
    total = 0.0
    for left, right in zip(observations, observations[1:], strict=False):
        if left.boot_id != right.boot_id:
            continue
        delta_s = (right.monotonic_ns - left.monotonic_ns) / 1_000_000_000
        if delta_s <= 0.0 or not isfinite(delta_s):
            continue
        if left.load_percent is None or right.load_percent is None:
            continue
        total += ((left.load_percent + right.load_percent) / 2.0) * delta_s
    return total


def voltage_at_horizon(
    observations: tuple[PhysicalObservation, ...],
    horizon_s: float = LONG_PARTIAL_HORIZON_S,
) -> float | None:
    if not observations:
        return None
    origin = observations[0].monotonic_ns
    target = origin + round(horizon_s * 1_000_000_000)
    left: PhysicalObservation | None = None
    right: PhysicalObservation | None = None
    for item in observations:
        if item.boot_id != observations[0].boot_id or item.battery_voltage_v is None:
            continue
        if item.monotonic_ns <= target:
            left = item
        if item.monotonic_ns >= target:
            right = item
            break
    if left is None or right is None:
        return None
    left_distance = (target - left.monotonic_ns) / 1_000_000_000
    right_distance = (right.monotonic_ns - target) / 1_000_000_000
    if left_distance > 2.0 or right_distance > 2.0:
        return None
    assert left.battery_voltage_v is not None and right.battery_voltage_v is not None
    if left.monotonic_ns == right.monotonic_ns:
        return float(left.battery_voltage_v)
    fraction = (target - left.monotonic_ns) / (right.monotonic_ns - left.monotonic_ns)
    return float(left.battery_voltage_v) + fraction * (
        float(right.battery_voltage_v) - float(left.battery_voltage_v)
    )


def _evaluation_origin_index(observations: tuple[PhysicalObservation, ...]) -> int | None:
    if len(observations) < ORIGIN_WINDOW_POINTS:
        return None
    start_ns = observations[0].monotonic_ns
    last_start = len(observations) - ORIGIN_WINDOW_POINTS
    for index in range(last_start + 1):
        if (observations[index].monotonic_ns - start_ns) / 1_000_000_000 < ORIGIN_DELAY_S:
            continue
        window = observations[index : index + ORIGIN_WINDOW_POINTS]
        if stable_origin_window(window):
            return index + ORIGIN_WINDOW_POINTS // 2
    return None


def stable_origin_window(observations: tuple[PhysicalObservation, ...]) -> bool:
    if not observations:
        return False
    try:
        loads = tuple(required_number(item.load_percent) for item in observations)
        tuple(required_number(item.battery_voltage_v) for item in observations)
    except ValueError:
        return False
    timeline = summarize_timeline(observations, MAX_STABLE_GAP_S)
    return (
        not timeline.reboot_gap_observed
        and timeline.non_increasing_edge_count == 0
        and timeline.max_gap_s <= MAX_STABLE_GAP_S
        and pstdev(loads) <= MAX_STABLE_LOAD_STDDEV_PP
    )


def required_number(value: float | None) -> float:
    if value is None or not isfinite(value):
        raise ValueError("expected finite number")
    return float(value)


def natural_event(observations: tuple[PhysicalObservation, ...]) -> bool:
    """Return whether the complete raw interval is a natural blackout."""
    return (
        bool(observations)
        and len({item.boot_id for item in observations}) == 1
        and all(natural_observation(item) for item in observations)
    )


def natural_prefix(
    observations: tuple[PhysicalObservation, ...],
    *,
    provenance_gap_observed: bool = False,
) -> bool:
    """Return whether the trusted raw prefix through first LB is natural."""
    interval = _trusted_lb_interval(observations)
    return (
        interval is not None and not provenance_gap_observed and natural_event(interval.provenance)
    )


def trusted_lb_observation_index(
    observations: tuple[PhysicalObservation, ...],
) -> int | None:
    """Return the record-order position of the policy-selected firmware LB."""
    interval = _trusted_lb_interval(observations)
    return None if interval is None else interval.lb_observation_index


def _trusted_lb_interval(
    observations: tuple[PhysicalObservation, ...],
) -> _TrustedLbInterval | None:
    origin_index = _evaluation_origin_index(observations)
    if origin_index is None:
        return None
    lb_index = next(
        (
            index
            for index in range(origin_index, len(observations))
            if "LB" in observations[index].raw_status.split()
        ),
        None,
    )
    if lb_index is None:
        return None
    return _TrustedLbInterval(
        observations[origin_index : lb_index + 1],
        observations[: lb_index + 1],
        lb_index,
    )


def natural_observation(observation: PhysicalObservation) -> bool:
    flags = set(observation.raw_status.split())
    return (
        "OB" in flags
        and "CAL" not in flags
        and classify_physical_observation(observation) == BlackoutKind.BLACKOUT_REAL
    )


def load_sag_observations(
    observations: tuple[PhysicalObservation, ...],
) -> tuple[PhysicalObservation, ...]:
    """Keep a valid load-sag prefix before unrelated suffix damage."""
    if not observations or any(
        "CAL" in item.raw_status.split()
        or classify_physical_observation(item) == BlackoutKind.BLACKOUT_TEST
        for item in observations
    ):
        return ()
    if not natural_observation(observations[0]):
        return ()
    for index, (left, right) in enumerate(zip(observations, observations[1:], strict=False), 1):
        delta_s = (right.monotonic_ns - left.monotonic_ns) / 1_000_000_000
        if not _load_sag_edge_valid(left, right, delta_s):
            return observations[:index]
    return observations


def _load_sag_edge_valid(
    left: PhysicalObservation,
    right: PhysicalObservation,
    delta_s: float,
) -> bool:
    return (
        natural_observation(right)
        and right.load_percent is not None
        and right.battery_voltage_v is not None
        and right.voltage_token_quantum_v is not None
        and right.boot_id == left.boot_id
        and 0.0 < delta_s <= 5.0
    )
