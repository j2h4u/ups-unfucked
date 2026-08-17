"""Immutable values shared by pure blackout-learning policies."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import Mapping

from src.battery_math.lut import FrozenLut
from src.domain.reasons import OrderedReasons


class BlackoutKind(StrEnum):
    ONLINE = "online"
    BLACKOUT_REAL = "blackout_real"
    BLACKOUT_TEST = "blackout_test"
    UNKNOWN = "unknown"


class TerminationFact(StrEnum):
    POWER_RESTORED = "power_restored"
    SERVICE_STOP = "service_stop"
    CLOSED_RESTART_GAP = "closed_restart_gap"
    CAPTURE_DAMAGED = "capture_damaged"


class EvidenceClass(StrEnum):
    QUALIFYING = "qualifying"
    OPERATIONAL_ONLY = "operational_only"
    REJECTED = "rejected"


class ComparisonMode(StrEnum):
    NONE = "none"
    SHORT_WINDOW = "short_window"
    FULL = "full"


class StepQuality(StrEnum):
    QUALIFYING = "qualifying"
    OBSERVED_ONLY = "observed_only"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class IrLearningPolicy:
    """Versioned, immutable numeric policy for IR learning.

    The policy is a domain value rather than a collection of constants spread
    across the application and model adapter.  Its serialized fields are
    intentionally primitive so the exact revision and values can be frozen in
    a durable assessment or prepared model commit.
    """

    revision: str
    deadband_v_per_pp: float
    min_k_v_per_pp: float
    max_k_v_per_pp: float
    max_single_commit_fraction: float
    max_epoch_decrease_fraction: float
    min_commit_interval_days: int
    max_consumed_step_hashes: int

    def __post_init__(self) -> None:
        if not isinstance(self.revision, str) or not self.revision.strip():
            raise ValueError("learning policy revision must be a non-empty string")
        values = (
            self.deadband_v_per_pp,
            self.min_k_v_per_pp,
            self.max_k_v_per_pp,
            self.max_single_commit_fraction,
            self.max_epoch_decrease_fraction,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(float(value))
            for value in values
        ):
            raise ValueError("learning policy numeric values must be finite")
        if self.deadband_v_per_pp < 0.0:
            raise ValueError("learning policy deadband must be nonnegative")
        if self.min_k_v_per_pp <= 0.0 or self.max_k_v_per_pp < self.min_k_v_per_pp:
            raise ValueError("learning policy IR bounds are invalid")
        if not 0.0 <= self.max_single_commit_fraction <= 1.0:
            raise ValueError("single commit fraction must be between zero and one")
        if not 0.0 <= self.max_epoch_decrease_fraction <= 1.0:
            raise ValueError("epoch decrease fraction must be between zero and one")
        if (
            isinstance(self.min_commit_interval_days, bool)
            or not isinstance(self.min_commit_interval_days, int)
            or self.min_commit_interval_days < 0
        ):
            raise ValueError("learning policy commit interval must be a nonnegative integer")
        if (
            isinstance(self.max_consumed_step_hashes, bool)
            or not isinstance(self.max_consumed_step_hashes, int)
            or self.max_consumed_step_hashes <= 0
        ):
            raise ValueError("learning policy evidence budget must be a positive integer")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "IrLearningPolicy":
        """Decode exactly one policy revision and its numeric values."""
        expected = {
            "revision",
            "deadband_v_per_pp",
            "min_k_v_per_pp",
            "max_k_v_per_pp",
            "max_single_commit_fraction",
            "max_epoch_decrease_fraction",
            "min_commit_interval_days",
            "max_consumed_step_hashes",
        }
        if set(value) != expected:
            raise ValueError("learning policy has invalid fields")
        return cls(
            revision=_required_policy_string(value["revision"]),
            deadband_v_per_pp=_required_policy_float(value["deadband_v_per_pp"]),
            min_k_v_per_pp=_required_policy_float(value["min_k_v_per_pp"]),
            max_k_v_per_pp=_required_policy_float(value["max_k_v_per_pp"]),
            max_single_commit_fraction=_required_policy_float(value["max_single_commit_fraction"]),
            max_epoch_decrease_fraction=_required_policy_float(
                value["max_epoch_decrease_fraction"]
            ),
            min_commit_interval_days=_required_policy_int(value["min_commit_interval_days"]),
            max_consumed_step_hashes=_required_policy_int(value["max_consumed_step_hashes"]),
        )

    @property
    def min_commit_interval(self) -> timedelta:
        """Return the rate-limit interval as a domain-friendly duration."""
        return timedelta(days=self.min_commit_interval_days)


def _required_policy_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("learning policy revision must be a string")
    return value


def _required_policy_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("learning policy value must be numeric")
    return float(value)


def _required_policy_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("learning policy count must be an integer")
    return value


DEFAULT_IR_LEARNING_POLICY = IrLearningPolicy(
    revision="ir-learning-v1",
    deadband_v_per_pp=0.001,
    min_k_v_per_pp=0.005,
    max_k_v_per_pp=0.040,
    max_single_commit_fraction=0.20,
    max_epoch_decrease_fraction=0.50,
    min_commit_interval_days=30,
    max_consumed_step_hashes=256,
)
SUPPORTED_IR_LEARNING_POLICY_REVISIONS = frozenset({DEFAULT_IR_LEARNING_POLICY.revision})


def ensure_supported_ir_learning_policy(policy: IrLearningPolicy) -> IrLearningPolicy:
    """Reject policy revisions that this binary cannot replay exactly."""
    if policy.revision not in SUPPORTED_IR_LEARNING_POLICY_REVISIONS:
        raise ValueError(f"unknown learning policy revision: {policy.revision}")
    if policy != DEFAULT_IR_LEARNING_POLICY:
        raise ValueError("learning policy values do not match its revision")
    return policy


class TerminalDisposition(StrEnum):
    LEARNED = "learned"
    RECORDED_ONLY = "recorded_only"
    REJECTED = "rejected"


class DeclineVerdict(StrEnum):
    INSUFFICIENT_COMPARABLE_EVIDENCE = "insufficient_comparable_evidence"
    POSSIBLE_DECLINE = "possible_decline"
    STABLE_WITHIN_OBSERVED_EVIDENCE = "stable_within_observed_evidence"


@dataclass(frozen=True, slots=True)
class PhysicalObservation:
    """One raw UPS reading; derived model outputs cannot be represented here."""

    boot_id: str
    monotonic_ns: int
    wall_time_utc: datetime
    raw_status: str
    battery_voltage_raw: str | None
    battery_voltage_v: float | None
    voltage_token_quantum_v: float | None
    load_percent: float | None
    input_voltage_v: float | None


@dataclass(frozen=True, slots=True)
class FrozenModelSnapshot:
    """Scientific model captured atomically at blackout start."""

    schema_revision: str
    evaluation_revision: str
    battery_epoch_id: str
    scientific_fingerprint: str
    rated_capacity_ah: float
    nominal_voltage_v: float
    nominal_power_watts: float
    soh: float
    peukert_exponent: float
    ir_k_v_per_pp: float
    ir_reference_load_percent: float
    lut: FrozenLut
    learning_policy: IrLearningPolicy = DEFAULT_IR_LEARNING_POLICY


@dataclass(frozen=True, slots=True)
class NumericSummary:
    minimum: float | None
    maximum: float | None
    mean: float | None
    population_stddev: float | None


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    evidence_class: EvidenceClass
    duration_s: float
    observation_count: int
    coverage_ratio: float
    max_gap_s: float
    voltage_summary: NumericSummary
    load_summary: NumericSummary
    reasons: OrderedReasons


@dataclass(frozen=True, slots=True)
class ForwardComparison:
    mode: ComparisonMode
    evaluation_origin_monotonic_ns: int | None
    evaluated_duration_s: float
    point_count: int
    start_residual_v: float | None
    end_residual_v: float | None
    mean_residual_v: float | None
    rmse_v: float | None
    observed_slope_v_per_s: float | None
    predicted_slope_v_per_s: float | None
    delivered_ah_proxy: float | None
    reasons: OrderedReasons


@dataclass(frozen=True, slots=True)
class LoadStepEstimate:
    step_id: str
    blackout_id: str
    segment_id: str
    pre_sequences: tuple[int, ...]
    post_sequences: tuple[int, ...]
    transition_monotonic_ns: int
    pre_slope_v_per_s: float
    early_post_slope_v_per_s: float
    late_post_slope_v_per_s: float
    delta_load_pp: float
    early_delta_voltage_at_transition_v: float
    settled_delta_voltage_at_transition_v: float
    voltage_quantum_v: float
    k_transition_v_per_pp: float
    k_settled_v_per_pp: float
    quality: StepQuality
    reasons: OrderedReasons


@dataclass(frozen=True, slots=True)
class IrCohortEstimate:
    battery_epoch_id: str
    blackout_ids: tuple[str, ...]
    step_count: int
    up_step_count: int
    down_step_count: int
    median_k_v_per_pp: float | None
    mad_ratio: float | None
    reasons: OrderedReasons


@dataclass(frozen=True, slots=True)
class LearningDecision:
    record_history: bool
    compare_forward_model: bool
    record_decline_evidence: bool
    commit_ir_k: bool


@dataclass(frozen=True, slots=True)
class ModelChange:
    parameter: str
    value_before: float
    measured_estimate: float
    value_after: float
    evidence_hashes: tuple[str, ...]
    bound_applied: bool


@dataclass(frozen=True, slots=True)
class ModelCommitReceipt:
    blackout_id: str
    parameter: str
    value_before: float
    measured_estimate: float
    value_after: float
    model_hash_before: str
    model_hash_after: str
    scientific_fingerprint_before: str
    scientific_fingerprint_after: str
    evidence_set_id: str
    consumed_step_hashes: tuple[str, ...]
    reference_reparameterization: bool
    safety_oracle: str


@dataclass(frozen=True, slots=True)
class TerminalOutcome:
    disposition: TerminalDisposition
    assessment: EvidenceAssessment
    comparison: ForwardComparison | None
    cohort_estimate: IrCohortEstimate | None
    learning_decision: LearningDecision
    commit_receipt: ModelCommitReceipt | None
    reasons: OrderedReasons


@dataclass(frozen=True, slots=True)
class ChargeReadiness:
    ready: bool
    continuous_online_s: float
    trailing_voltage_span_v: float | None
    reasons: OrderedReasons


@dataclass(frozen=True, slots=True)
class ReserveCohortStatus:
    metric: str
    verdict: DeclineVerdict
    baseline: float | None
    recent: float | None
    event_ids: tuple[str, ...]
    reasons: OrderedReasons


@dataclass(frozen=True, slots=True)
class PlainLanguageReport:
    blackout_id: str
    disposition: TerminalDisposition
    lines: tuple[str, ...]
    generated_utc: datetime
