"""Pure provenance and raw data-quality assessment."""

from dataclasses import dataclass
from math import isfinite
from statistics import fmean, pstdev

from src.domain.reasons import EvidenceReason, order_reasons
from src.domain.timeline import summarize_timeline
from src.domain.values import (
    BlackoutKind,
    EvidenceAssessment,
    EvidenceClass,
    NumericSummary,
    PhysicalObservation,
    TerminationFact,
)

MIN_COVERAGE_RATIO = 0.90
MAX_RAW_GAP_S = 5.0
MIN_BATTERY_VOLTAGE_V = 8.0
MAX_BATTERY_VOLTAGE_V = 15.0


@dataclass(frozen=True, slots=True)
class TerminalSciencePolicy:
    """Closed authorization boundary for scientific use of a terminal event."""

    termination: TerminationFact | None

    @property
    def authorizes_science(self) -> bool:
        """Return whether this exact terminal fact permits scientific use."""
        return self.termination is TerminationFact.POWER_RESTORED


def terminal_science_policy(value: object) -> TerminalSciencePolicy:
    """Classify one raw terminal fact without guessing unknown or missing data."""
    if isinstance(value, TerminationFact):
        termination = value
    elif type(value) is str:
        try:
            termination = TerminationFact(value)
        except ValueError:
            termination = None
    else:
        termination = None
    return TerminalSciencePolicy(termination=termination)


@dataclass(frozen=True, slots=True)
class EvidenceContext:
    blackout_kind: BlackoutKind
    frozen_snapshot_supported: bool
    current_battery_epoch_id: str
    frozen_battery_epoch_id: str
    capture_damaged: bool
    snapshot_within_budget: bool
    terminal_policy: TerminalSciencePolicy | None = None


def assess_evidence(
    observations: tuple[PhysicalObservation, ...],
    context: EvidenceContext,
) -> EvidenceAssessment:
    """Judge provenance and shared comparison gates without model outputs."""
    timeline = summarize_timeline(observations, MAX_RAW_GAP_S)
    blocking = _blocking_reasons(observations, context, timeline)
    diagnostic = (
        (EvidenceReason.INPUT_VOLTAGE_UNAVAILABLE,)
        if any(observation.input_voltage_v is None for observation in observations)
        else ()
    )

    if context.capture_damaged:
        evidence_class = EvidenceClass.REJECTED
    elif context.terminal_policy is not None and not context.terminal_policy.authorizes_science:
        evidence_class = EvidenceClass.OPERATIONAL_ONLY
    elif blocking:
        evidence_class = EvidenceClass.OPERATIONAL_ONLY
    else:
        evidence_class = EvidenceClass.QUALIFYING
    voltages = tuple(
        observation.battery_voltage_v
        for observation in observations
        if observation.battery_voltage_v is not None and isfinite(observation.battery_voltage_v)
    )
    loads = tuple(
        observation.load_percent
        for observation in observations
        if observation.load_percent is not None and isfinite(observation.load_percent)
    )
    return EvidenceAssessment(
        evidence_class=evidence_class,
        duration_s=timeline.duration_s,
        observation_count=len(observations),
        coverage_ratio=timeline.coverage_ratio,
        max_gap_s=timeline.max_gap_s,
        voltage_summary=_numeric_summary(voltages),
        load_summary=_numeric_summary(loads),
        reasons=order_reasons((*blocking, *diagnostic)),
    )


def _blocking_reasons(
    observations: tuple[PhysicalObservation, ...],
    context: EvidenceContext,
    timeline,
) -> tuple[EvidenceReason, ...]:
    checks = (
        (
            context.blackout_kind != BlackoutKind.BLACKOUT_REAL
            or any(
                not ({"OB", "CAL"} & set(observation.raw_status.split()))
                for observation in observations
            ),
            EvidenceReason.NOT_NATURAL_PHYSICAL_BLACKOUT,
        ),
        (
            any("CAL" in observation.raw_status.split() for observation in observations),
            EvidenceReason.CALIBRATION_OBSERVED,
        ),
        (not context.frozen_snapshot_supported, EvidenceReason.UNSUPPORTED_FROZEN_SNAPSHOT),
        (
            context.current_battery_epoch_id != context.frozen_battery_epoch_id,
            EvidenceReason.BATTERY_EPOCH_MISMATCH,
        ),
        (timeline.coverage_ratio < MIN_COVERAGE_RATIO, EvidenceReason.INSUFFICIENT_COVERAGE),
        (
            timeline.non_increasing_edge_count > 0,
            EvidenceReason.INSUFFICIENT_COVERAGE,
        ),
        (timeline.max_gap_s > MAX_RAW_GAP_S, EvidenceReason.RAW_GAP_TOO_LARGE),
        (timeline.reboot_gap_observed, EvidenceReason.REBOOT_GAP),
        (not _valid_voltages(observations), EvidenceReason.INVALID_BATTERY_VOLTAGE),
        (not _valid_loads(observations), EvidenceReason.INVALID_LOAD_PERCENT),
        (
            context.capture_damaged
            or (
                context.terminal_policy is not None
                and context.terminal_policy.termination is TerminationFact.CAPTURE_DAMAGED
            ),
            EvidenceReason.CAPTURE_DAMAGED,
        ),
        (
            context.terminal_policy is not None
            and not context.terminal_policy.authorizes_science
            and context.terminal_policy.termination is not TerminationFact.CAPTURE_DAMAGED,
            EvidenceReason.EVENT_NOT_NATURALLY_COMPLETED,
        ),
        (not context.snapshot_within_budget, EvidenceReason.SNAPSHOT_BUDGET_EXCEEDED),
    )
    return tuple(reason for failed, reason in checks if failed)


def _valid_voltages(observations: tuple[PhysicalObservation, ...]) -> bool:
    return bool(observations) and all(
        observation.battery_voltage_v is not None
        and isfinite(observation.battery_voltage_v)
        and MIN_BATTERY_VOLTAGE_V <= observation.battery_voltage_v <= MAX_BATTERY_VOLTAGE_V
        for observation in observations
    )


def _valid_loads(observations: tuple[PhysicalObservation, ...]) -> bool:
    return bool(observations) and all(
        observation.load_percent is not None
        and isfinite(observation.load_percent)
        and 0.0 <= observation.load_percent <= 100.0
        for observation in observations
    )


def _numeric_summary(values: tuple[float, ...]) -> NumericSummary:
    if not values:
        return NumericSummary(None, None, None, None)
    return NumericSummary(min(values), max(values), fmean(values), pstdev(values))
