"""Small, honest domain policy for post-blackout recharge evidence.

Recharge is deliberately a diagnostic evidence surface.  This module never
reads or writes model state and never turns a stable float interval into a
claim that the battery reached full charge.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from math import isfinite

from src.domain.values import PhysicalObservation


class RechargeTermination(StrEnum):
    """Closed reasons for an unfinished or stabilized recharge episode."""

    CHARGE_STABILIZED = "charge_stabilized"
    SUPERSEDED_BY_BLACKOUT = "superseded_by_blackout"
    EPISODE_BUDGET_EXHAUSTED = "episode_budget_exhausted"
    SERVICE_STOP = "service_stop"
    GAP = "gap"
    CAPTURE_DAMAGED = "capture_damaged"


class RechargeAssessmentKind(StrEnum):
    """User-facing evidence result, intentionally not a model decision."""

    DIAGNOSTIC = "diagnostic"
    USABLE = "usable"
    REFUSED = "refused"


class RechargeSampleKind(StrEnum):
    """Selection provenance for unbiased backbone versus transition detail."""

    UNIFORM_BACKBONE = "uniform_backbone"
    ENRICHMENT = "enrichment"


@dataclass(frozen=True, slots=True)
class RechargeSamplingPolicy:
    """Bounded persistence policy applied after every one-second safety poll."""

    revision: str = "recharge-v1"
    backbone_interval_s: float = 60.0
    dense_enrichment_interval_s: float = 1.0
    sparse_enrichment_interval_s: float = 300.0
    dense_window_s: float = 120.0
    stable_voltage_band_quanta: int = 2
    required_consecutive_stable_windows: int = 3
    minimum_stabilization_duration_s: float = 1_800.0
    maximum_duration_s: float = 86_400.0
    maximum_samples: int = 4_096

    def __post_init__(self) -> None:
        if not self.revision.strip():
            raise ValueError("recharge policy revision must not be empty")
        durations = (
            self.backbone_interval_s,
            self.dense_enrichment_interval_s,
            self.sparse_enrichment_interval_s,
            self.dense_window_s,
            self.minimum_stabilization_duration_s,
            self.maximum_duration_s,
        )
        if any(
            isinstance(item, bool) or not isfinite(float(item)) or item <= 0.0 for item in durations
        ):
            raise ValueError("recharge policy durations must be positive and finite")
        if self.dense_enrichment_interval_s > self.backbone_interval_s:
            raise ValueError("dense recharge enrichment must not be sparser than the backbone")
        if self.sparse_enrichment_interval_s < self.dense_enrichment_interval_s:
            raise ValueError("sparse recharge enrichment must not be denser than dense enrichment")
        counts = (
            self.stable_voltage_band_quanta,
            self.required_consecutive_stable_windows,
            self.maximum_samples,
        )
        if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in counts):
            raise ValueError("recharge policy bounds must be positive integers")


@dataclass(frozen=True, slots=True)
class RechargeAssessment:
    """Explicit terminal statement for one recharge episode."""

    kind: RechargeAssessmentKind
    reason: str
    persisted_samples: int
    observed_samples: int


@dataclass(frozen=True, slots=True)
class RechargeTerminalContext:
    """Durable counters used to classify one terminal recharge evidence result."""

    persisted_samples: int
    observed_samples: int
    stable_windows: int
    policy: RechargeSamplingPolicy
    stabilization_duration_s: float = 0.0
    continuity_gap: bool = False


@dataclass(frozen=True, slots=True)
class RechargeObservationDecision:
    """Decision to persist one poll without changing safety polling cadence."""

    persist: bool
    sample_kind: RechargeSampleKind | None
    stable_windows: int


@dataclass(frozen=True, slots=True)
class RechargeObservationContext:
    """Inputs for one persistence-sampling decision."""

    policy: RechargeSamplingPolicy
    first_observation: PhysicalObservation
    previous_persisted: PhysicalObservation | None
    current: PhysicalObservation
    elapsed_s: float
    observed_samples: int
    stable_windows: int


def update_stable_since(
    policy: RechargeSamplingPolicy,
    previous: PhysicalObservation,
    current: PhysicalObservation,
    stable_since: datetime | None,
) -> datetime | None:
    """Track one continuous online voltage window across every poll."""
    if not _stable_window(policy, previous, current):
        return None
    return previous.wall_time_utc if stable_since is None else stable_since


def _stable_window(
    policy: RechargeSamplingPolicy,
    previous: PhysicalObservation,
    current: PhysicalObservation,
) -> bool:
    if "OL" not in current.raw_status.split() or "OL" not in previous.raw_status.split():
        return False
    previous_voltage = previous.battery_voltage_v
    current_voltage = current.battery_voltage_v
    if previous_voltage is None or current_voltage is None:
        return False
    quantum = current.voltage_token_quantum_v or previous.voltage_token_quantum_v or 0.001
    return abs(current_voltage - previous_voltage) <= quantum * policy.stable_voltage_band_quanta


def observation_identity(observation: PhysicalObservation) -> str:
    """Return a stable identity for idempotent restoration/restart linkage."""
    text = "|".join(
        (
            observation.boot_id,
            str(observation.monotonic_ns),
            observation.wall_time_utc.isoformat(),
            observation.raw_status,
            observation.battery_voltage_raw or "",
            "" if observation.battery_voltage_v is None else repr(observation.battery_voltage_v),
            "" if observation.load_percent is None else repr(observation.load_percent),
        )
    )
    return sha256(text.encode("utf-8")).hexdigest()


def decide_observation(context: RechargeObservationContext) -> RechargeObservationDecision:
    """Select a bounded sample while always retaining the uniform backbone."""
    policy = context.policy
    previous_persisted = context.previous_persisted
    current = context.current
    elapsed_s = context.elapsed_s
    observed_samples = context.observed_samples
    stable_windows = context.stable_windows
    if observed_samples <= 0 or previous_persisted is None:
        return RechargeObservationDecision(
            True, RechargeSampleKind.UNIFORM_BACKBONE, stable_windows
        )
    if elapsed_s < 0.0 or not isfinite(elapsed_s):
        raise ValueError("recharge elapsed time must be finite and nonnegative")
    next_stable = _stable_windows(policy, previous_persisted, current, stable_windows)
    since_persisted = max(
        0.0, current.wall_time_utc.timestamp() - previous_persisted.wall_time_utc.timestamp()
    )
    if since_persisted >= policy.backbone_interval_s:
        return RechargeObservationDecision(True, RechargeSampleKind.UNIFORM_BACKBONE, next_stable)
    if _changed_meaningfully(policy, previous_persisted, current):
        return RechargeObservationDecision(True, RechargeSampleKind.ENRICHMENT, next_stable)
    interval = (
        policy.dense_enrichment_interval_s
        if elapsed_s <= policy.dense_window_s
        else policy.sparse_enrichment_interval_s
    )
    if since_persisted >= interval:
        return RechargeObservationDecision(True, RechargeSampleKind.ENRICHMENT, next_stable)
    return RechargeObservationDecision(False, None, next_stable)


def _changed_meaningfully(
    policy: RechargeSamplingPolicy,
    previous: PhysicalObservation,
    current: PhysicalObservation,
) -> bool:
    if previous.raw_status != current.raw_status:
        return True
    previous_voltage = previous.battery_voltage_v
    current_voltage = current.battery_voltage_v
    if previous_voltage is None or current_voltage is None:
        return previous_voltage != current_voltage
    quantum = current.voltage_token_quantum_v or previous.voltage_token_quantum_v or 0.001
    return abs(current_voltage - previous_voltage) > quantum * policy.stable_voltage_band_quanta


def _stable_windows(
    policy: RechargeSamplingPolicy,
    previous: PhysicalObservation,
    current: PhysicalObservation,
    stable_windows: int,
) -> int:
    """Count consecutive online voltage windows in raw quantisation units."""
    if "OL" not in current.raw_status.split():
        return 0
    previous_voltage = previous.battery_voltage_v
    current_voltage = current.battery_voltage_v
    if previous_voltage is None or current_voltage is None:
        return 0
    quantum = current.voltage_token_quantum_v or previous.voltage_token_quantum_v or 0.001
    if abs(current_voltage - previous_voltage) <= quantum * policy.stable_voltage_band_quanta:
        return stable_windows + 1
    return 0


def terminal_assessment(
    termination: RechargeTermination,
    context: RechargeTerminalContext,
) -> RechargeAssessment:
    """Explain usefulness without treating stabilization as full charge."""
    persisted_samples = context.persisted_samples
    observed_samples = context.observed_samples
    stable_windows = context.stable_windows
    policy = context.policy
    stabilization_duration_s = context.stabilization_duration_s
    if termination in {RechargeTermination.CAPTURE_DAMAGED, RechargeTermination.GAP}:
        return RechargeAssessment(
            RechargeAssessmentKind.REFUSED,
            f"recharge evidence refused: {termination.value}",
            persisted_samples,
            observed_samples,
        )
    newly_observed_stability = (
        termination == RechargeTermination.CHARGE_STABILIZED
        and stable_windows >= policy.required_consecutive_stable_windows
        and stabilization_duration_s >= policy.minimum_stabilization_duration_s
    )
    if context.continuity_gap and not newly_observed_stability:
        return RechargeAssessment(
            RechargeAssessmentKind.REFUSED,
            "recharge evidence refused: restart gap requires newly observed stability",
            persisted_samples,
            observed_samples,
        )
    if persisted_samples < 2:
        return RechargeAssessment(
            RechargeAssessmentKind.DIAGNOSTIC,
            "recharge diagnostic only: fewer than two durable samples",
            persisted_samples,
            observed_samples,
        )
    if termination == RechargeTermination.EPISODE_BUDGET_EXHAUSTED:
        reason = "recharge diagnostic only: bounded observation budget exhausted"
        kind = RechargeAssessmentKind.DIAGNOSTIC
    elif termination == RechargeTermination.CHARGE_STABILIZED:
        reason = (
            "recharge usable for bounded stabilization evidence; full charge is not established"
            if (
                stable_windows >= policy.required_consecutive_stable_windows
                and stabilization_duration_s >= policy.minimum_stabilization_duration_s
            )
            else "recharge diagnostic only: stabilization evidence is incomplete"
        )
        kind = (
            RechargeAssessmentKind.USABLE
            if (
                stable_windows >= policy.required_consecutive_stable_windows
                and stabilization_duration_s >= policy.minimum_stabilization_duration_s
            )
            else RechargeAssessmentKind.DIAGNOSTIC
        )
    else:
        reason = f"recharge diagnostic only: episode ended by {termination.value}"
        kind = RechargeAssessmentKind.DIAGNOSTIC
    return RechargeAssessment(kind, reason, persisted_samples, observed_samples)
