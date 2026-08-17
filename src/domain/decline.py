"""Conservative evidence-only battery decline trend policies."""

from dataclasses import dataclass
from datetime import datetime
from math import isclose
from statistics import median

from src.domain.reasons import DeclineReason, order_reasons
from src.domain.values import DeclineVerdict, ReserveCohortStatus


@dataclass(frozen=True, slots=True)
class LoadSagTrendSample:
    blackout_id: str
    event_started_utc: datetime
    battery_epoch_id: str
    transition_monotonic_ns: int
    k_settled_v_per_pp: float


@dataclass(frozen=True, slots=True)
class FirmwareReserveSample:
    blackout_id: str
    event_started_utc: datetime
    battery_epoch_id: str
    ready_at_start: bool
    start_voltage_v: float
    mean_load_percent: float
    load_stddev_percent: float
    coverage_ratio: float
    max_gap_s: float
    reserve_proxy_pp_s: float


@dataclass(frozen=True, slots=True)
class LongPartialSample:
    blackout_id: str
    event_started_utc: datetime
    battery_epoch_id: str
    ready_at_start: bool
    duration_s: float
    start_voltage_v: float
    mean_load_percent: float
    load_stddev_percent: float
    coverage_ratio: float
    max_gap_s: float
    voltage_at_600s_v: float | None


def assess_load_sag_trend(
    samples: tuple[LoadSagTrendSample, ...],
) -> ReserveCohortStatus:
    """Compare the latest six settled steps without result-based selection."""
    ordered = sorted(
        samples,
        key=lambda sample: (
            sample.event_started_utc,
            sample.blackout_id,
            sample.transition_monotonic_ns,
        ),
    )
    bounded: list[LoadSagTrendSample] = []
    per_event: dict[str, int] = {}
    for sample in ordered:
        count = per_event.get(sample.blackout_id, 0)
        if count < 2:
            bounded.append(sample)
            per_event[sample.blackout_id] = count + 1
    selected = tuple(bounded[-6:])
    if not _load_sag_group_comparable(selected):
        return _insufficient("load_sag_trend")
    baseline = median(tuple(sample.k_settled_v_per_pp for sample in selected[:3]))
    recent = median(tuple(sample.k_settled_v_per_pp for sample in selected[3:]))
    threshold = max(0.003, 0.20 * baseline)
    if _at_least(recent - baseline, threshold):
        return _possible_status(
            "load_sag_trend",
            baseline,
            recent,
            selected,
            DeclineReason.POSSIBLE_LOAD_SAG_DEGRADATION,
        )
    return _stable("load_sag_trend", baseline, recent, selected)


def assess_firmware_lb_reserve(
    samples: tuple[FirmwareReserveSample, ...],
) -> ReserveCohortStatus:
    """Assess the latest six raw-LB trusted-prefix reserve proxies."""
    selected = tuple(sorted(samples, key=_event_key)[-6:])
    if not _firmware_samples_comparable(selected):
        return _insufficient("firmware_lb_reserve_proxy")
    baseline = median(tuple(sample.reserve_proxy_pp_s for sample in selected[:3]))
    recent = median(tuple(sample.reserve_proxy_pp_s for sample in selected[3:]))
    if _at_most(recent, 0.80 * baseline):
        return _possible_status(
            "firmware_lb_reserve_proxy",
            baseline,
            recent,
            selected,
            DeclineReason.POSSIBLE_RESERVE_DECLINE,
        )
    return _stable("firmware_lb_reserve_proxy", baseline, recent, selected)


def assess_long_partial_curve(
    samples: tuple[LongPartialSample, ...],
) -> ReserveCohortStatus:
    """Compare raw interpolated voltage at 600 seconds across six events."""
    selected = tuple(sorted(samples, key=_event_key)[-6:])
    if not _long_partial_samples_comparable(selected):
        return _insufficient("long_partial_curve")
    voltages = tuple(sample.voltage_at_600s_v for sample in selected)
    if any(voltage is None for voltage in voltages):
        return _insufficient("long_partial_curve")
    numeric_voltages = tuple(float(voltage) for voltage in voltages if voltage is not None)
    baseline = median(numeric_voltages[:3])
    recent = median(numeric_voltages[3:])
    if _at_least(baseline - recent, 0.20):
        return _possible_status(
            "long_partial_curve",
            baseline,
            recent,
            selected,
            DeclineReason.POSSIBLE_RESERVE_DECLINE,
        )
    return _stable("long_partial_curve", baseline, recent, selected)


def storage_corruption_status(metric: str) -> ReserveCohortStatus:
    """Expose corrupt durable evidence distinctly from an immature cohort."""
    return ReserveCohortStatus(
        metric,
        DeclineVerdict.INSUFFICIENT_COMPARABLE_EVIDENCE,
        None,
        None,
        (),
        order_reasons((DeclineReason.EVIDENCE_STORAGE_CORRUPT,)),
    )


def _load_sag_group_comparable(samples: tuple[LoadSagTrendSample, ...]) -> bool:
    if len(samples) != 6 or len({sample.blackout_id for sample in samples}) < 4:
        return False
    return (
        len({sample.battery_epoch_id for sample in samples}) == 1
        and len({sample.blackout_id for sample in samples[:3]}) >= 2
        and len({sample.blackout_id for sample in samples[3:]}) >= 2
    )


def _firmware_samples_comparable(samples: tuple[FirmwareReserveSample, ...]) -> bool:
    if len(samples) != 6 or not all(sample.ready_at_start for sample in samples):
        return False
    return (
        len({sample.battery_epoch_id for sample in samples}) == 1
        and _at_most(_range(tuple(sample.start_voltage_v for sample in samples)), 0.10)
        and _at_most(_range(tuple(sample.mean_load_percent for sample in samples)), 5.0)
        and all(_at_most(sample.load_stddev_percent, 2.0) for sample in samples)
        and all(_at_least(sample.coverage_ratio, 0.90) for sample in samples)
        and all(_at_most(sample.max_gap_s, 5.0) for sample in samples)
    )


def _long_partial_samples_comparable(samples: tuple[LongPartialSample, ...]) -> bool:
    if len(samples) != 6 or not all(sample.ready_at_start for sample in samples):
        return False
    return (
        len({sample.battery_epoch_id for sample in samples}) == 1
        and all(_at_least(sample.duration_s, 650.0) for sample in samples)
        and all(_at_least(sample.coverage_ratio, 0.90) for sample in samples)
        and all(_at_most(sample.max_gap_s, 5.0) for sample in samples)
        and _at_most(_range(tuple(sample.start_voltage_v for sample in samples)), 0.10)
        and _at_most(_range(tuple(sample.mean_load_percent for sample in samples)), 3.0)
        and all(_at_most(sample.load_stddev_percent, 2.0) for sample in samples)
    )


def _event_key(sample: FirmwareReserveSample | LongPartialSample) -> tuple[datetime, str]:
    return sample.event_started_utc, sample.blackout_id


def _range(values: tuple[float, ...]) -> float:
    return max(values) - min(values)


def _at_least(value: float, threshold: float) -> bool:
    return value > threshold or isclose(value, threshold, rel_tol=0.0, abs_tol=1e-12)


def _at_most(value: float, threshold: float) -> bool:
    return value < threshold or isclose(value, threshold, rel_tol=0.0, abs_tol=1e-12)


def _insufficient(metric: str) -> ReserveCohortStatus:
    return ReserveCohortStatus(
        metric,
        DeclineVerdict.INSUFFICIENT_COMPARABLE_EVIDENCE,
        None,
        None,
        (),
        order_reasons((DeclineReason.INSUFFICIENT_COMPARABLE_EVIDENCE,)),
    )


def _possible_status(
    metric: str,
    baseline: float,
    recent: float,
    samples: tuple[LoadSagTrendSample | FirmwareReserveSample | LongPartialSample, ...],
    reason: DeclineReason,
) -> ReserveCohortStatus:
    return ReserveCohortStatus(
        metric,
        DeclineVerdict.POSSIBLE_DECLINE,
        baseline,
        recent,
        tuple(sample.blackout_id for sample in samples),
        order_reasons((reason,)),
    )


def _stable(
    metric: str,
    baseline: float,
    recent: float,
    samples: tuple[LoadSagTrendSample | FirmwareReserveSample | LongPartialSample, ...],
) -> ReserveCohortStatus:
    return ReserveCohortStatus(
        metric=metric,
        verdict=DeclineVerdict.STABLE_WITHIN_OBSERVED_EVIDENCE,
        baseline=baseline,
        recent=recent,
        event_ids=tuple(sample.blackout_id for sample in samples),
        reasons=order_reasons((DeclineReason.STABLE_WITHIN_OBSERVED_EVIDENCE,)),
    )
