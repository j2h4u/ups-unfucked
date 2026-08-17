"""Conservative IR learning eligibility and bounded change policy."""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from math import isfinite

from src.domain.reasons import (
    IdentificationReason,
    LearningReason,
    OrderedReasons,
    order_reasons,
)
from src.domain.values import (
    DEFAULT_IR_LEARNING_POLICY,
    ComparisonMode,
    EvidenceAssessment,
    EvidenceClass,
    ForwardComparison,
    IrCohortEstimate,
    IrLearningPolicy,
    LearningDecision,
    ModelChange,
    TerminalOutcome,
)

_ALLOWED_COHORT_REASONS = frozenset({IdentificationReason.CANDIDATE_EVENT_OVERFLOW})


def _canonical_step_hash(step_hash: str) -> bool:
    return len(step_hash) == 64 and all(character in "0123456789abcdef" for character in step_hash)


def evidence_set_id(step_hashes: tuple[str, ...]) -> str:
    """Return the canonical identity of one non-empty, unique physical cohort."""
    canonical = tuple(sorted(step_hashes))
    if not canonical:
        raise ValueError("evidence set must contain at least one step hash")
    if len(canonical) != len(set(canonical)):
        raise ValueError("evidence set contains duplicate step hashes")
    if any(not _canonical_step_hash(step_hash) for step_hash in canonical):
        raise ValueError("evidence set contains a non-canonical step hash")
    return sha256("".join(canonical).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class ObservedLoadSagIncrease:
    """Unapplied upward IR estimate with its exact independent evidence identity."""

    parameter: str
    value_before: float
    measured_estimate: float
    evidence_set_id: str
    evidence_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.parameter != "ir_k_v_per_pp":
            raise ValueError("observed load-sag increase parameter is unsupported")
        if not isfinite(self.value_before) or self.value_before <= 0.0:
            raise ValueError("observed load-sag increase before value must be positive and finite")
        if not isfinite(self.measured_estimate) or self.measured_estimate <= self.value_before:
            raise ValueError("observed load-sag estimate must be finite and greater than before")
        canonical = tuple(sorted(self.evidence_hashes))
        if self.evidence_hashes != canonical:
            raise ValueError("observed load-sag evidence hashes must be canonical")
        if self.evidence_set_id != evidence_set_id(canonical):
            raise ValueError("observed load-sag evidence-set identity is invalid")


@dataclass(frozen=True, slots=True)
class IrLearningResult:
    change: ModelChange | None
    evidence_set_id: str | None
    reasons: OrderedReasons
    observed_load_sag_increase: ObservedLoadSagIncrease | None = None


def validate_observed_load_sag_outcome(
    observation: ObservedLoadSagIncrease | None,
    outcome: TerminalOutcome,
) -> None:
    """Require an upward observation and its no-commit outcome to agree exactly."""
    has_upward_reason = LearningReason.UNSAFE_UPWARD_IR_CHANGE_NOT_APPLIED in outcome.reasons.values
    if observation is None:
        if has_upward_reason:
            raise ValueError("upward load-sag observation is missing")
        return
    cohort = outcome.cohort_estimate
    if (
        not has_upward_reason
        or outcome.commit_receipt is not None
        or outcome.learning_decision.commit_ir_k
        or cohort is None
        or cohort.median_k_v_per_pp != observation.measured_estimate
    ):
        raise ValueError("upward load-sag outcome is inconsistent")


@dataclass(frozen=True, slots=True)
class IrLearningContext:
    current_blackout_id: str
    current_blackout_step_count: int
    battery_epoch_id: str
    current_k_v_per_pp: float
    epoch_initial_k_v_per_pp: float
    reference_load_percent: float
    current_utc: datetime
    previous_commit_utc: datetime | None
    candidate_step_hashes: tuple[str, ...]
    consumed_step_hashes: frozenset[str]
    learning_policy: IrLearningPolicy = DEFAULT_IR_LEARNING_POLICY

    def __post_init__(self) -> None:
        from src.domain.values import ensure_supported_ir_learning_policy

        ensure_supported_ir_learning_policy(self.learning_policy)


_MODEL_COMMIT_REFUSAL_REASONS = {
    "overlapping_evidence_already_consumed": LearningReason.EVIDENCE_ALREADY_CONSUMED,
    "unsupported_model_parameter": LearningReason.UNSUPPORTED_MODEL_PARAMETER,
    "missing_commit_evidence": LearningReason.MISSING_COMMIT_EVIDENCE,
    "ir_commit_not_canonical": LearningReason.IR_COMMIT_NOT_CANONICAL,
    "ir_change_below_noise_floor": LearningReason.IR_CHANGE_BELOW_NOISE_FLOOR,
    "ir_bound_flag_mismatch": LearningReason.IR_BOUND_FLAG_MISMATCH,
    "invalid_evidence_hash": LearningReason.INVALID_EVIDENCE_HASH,
    "duplicate_evidence_hash": LearningReason.DUPLICATE_EVIDENCE_HASH,
    "value_before must be a finite number": LearningReason.INVALID_MODEL_CHANGE_VALUE,
    "value_before must be positive": LearningReason.INVALID_MODEL_CHANGE_VALUE,
    "measured_estimate must be a finite number": LearningReason.INVALID_MODEL_CHANGE_VALUE,
    "measured_estimate must be positive": LearningReason.INVALID_MODEL_CHANGE_VALUE,
    "value_after must be a finite number": LearningReason.INVALID_MODEL_CHANGE_VALUE,
    "value_after must be positive": LearningReason.INVALID_MODEL_CHANGE_VALUE,
    "commit time must be timezone-aware UTC": LearningReason.INVALID_COMMIT_TIME,
    "epoch_cumulative_decrease_exceeded": LearningReason.EPOCH_CUMULATIVE_DECREASE_LIMIT,
    "consumed_evidence_budget_exhausted": LearningReason.CONSUMED_EVIDENCE_BUDGET_EXHAUSTED,
    "commit_rate_window_indeterminate": LearningReason.COMMIT_RATE_WINDOW_INDETERMINATE,
    "commit_rate_limited": LearningReason.COMMIT_RATE_LIMITED,
    "safety_oracle_failed": LearningReason.SAFETY_ORACLE_FAILED,
    "learning_policy_mismatch": LearningReason.LEARNING_POLICY_MISMATCH,
}


def model_commit_refusal_reason(value: str) -> LearningReason:
    """Translate every known model-owner refusal; reject unknown wire values."""
    try:
        return _MODEL_COMMIT_REFUSAL_REASONS[value]
    except KeyError as exc:
        raise ValueError(f"unknown model commit refusal: {value!r}") from exc


def evaluate_ir_learning(
    cohort: IrCohortEstimate,
    context: IrLearningContext,
) -> IrLearningResult:
    """Return an intended conservative change or an exact zero-write reason."""
    measured_k = cohort.median_k_v_per_pp
    if measured_k is None or not _eligible_cohort(cohort, context):
        return _refused(LearningReason.COHORT_NOT_ELIGIBLE)
    prechange_refusal = _prechange_refusal(context)
    if prechange_refusal is not None:
        return _refused(prechange_refusal)
    policy = context.learning_policy
    if measured_k > context.current_k_v_per_pp:
        hashes = tuple(sorted(context.candidate_step_hashes))
        identity = evidence_set_id(hashes)
        return IrLearningResult(
            change=None,
            evidence_set_id=identity,
            reasons=order_reasons((LearningReason.UNSAFE_UPWARD_IR_CHANGE_NOT_APPLIED,)),
            observed_load_sag_increase=ObservedLoadSagIncrease(
                parameter="ir_k_v_per_pp",
                value_before=context.current_k_v_per_pp,
                measured_estimate=measured_k,
                evidence_set_id=identity,
                evidence_hashes=hashes,
            ),
        )
    if measured_k >= context.current_k_v_per_pp - policy.deadband_v_per_pp:
        return _refused(LearningReason.IR_CHANGE_BELOW_NOISE_FLOOR)

    fraction_floor = (1.0 - policy.max_single_commit_fraction) * context.current_k_v_per_pp
    value_after = max(policy.min_k_v_per_pp, measured_k, fraction_floor)
    bounded_refusal = _bounded_change_refusal(context, value_after)
    if bounded_refusal is not None:
        return _refused(bounded_refusal)

    hashes = tuple(sorted(context.candidate_step_hashes))
    change = ModelChange(
        parameter="ir_k_v_per_pp",
        value_before=context.current_k_v_per_pp,
        measured_estimate=measured_k,
        value_after=value_after,
        evidence_hashes=hashes,
        bound_applied=value_after != measured_k,
    )
    return IrLearningResult(change, evidence_set_id(hashes), order_reasons(()))


def _prechange_refusal(context: IrLearningContext) -> LearningReason | None:
    reason = None
    if context.reference_load_percent != 0.0:
        reason = LearningReason.IR_REFERENCE_FRAME_NOT_TRANSFORMED
    elif any(
        step_hash in context.consumed_step_hashes for step_hash in context.candidate_step_hashes
    ):
        reason = LearningReason.EVIDENCE_ALREADY_CONSUMED
    elif (
        len(context.consumed_step_hashes | set(context.candidate_step_hashes))
        > context.learning_policy.max_consumed_step_hashes
    ):
        reason = LearningReason.CONSUMED_EVIDENCE_BUDGET_EXHAUSTED
    elif (
        context.previous_commit_utc is not None
        and context.current_utc < context.previous_commit_utc
    ):
        reason = LearningReason.COMMIT_RATE_WINDOW_INDETERMINATE
    elif (
        context.previous_commit_utc is not None
        and context.current_utc - context.previous_commit_utc
        < context.learning_policy.min_commit_interval
    ):
        reason = LearningReason.COMMIT_RATE_LIMITED
    return reason


def _eligible_cohort(cohort: IrCohortEstimate, context: IrLearningContext) -> bool:
    hashes = context.candidate_step_hashes
    unique_hashes = frozenset(hashes)
    return (
        cohort.battery_epoch_id == context.battery_epoch_id
        and cohort.step_count >= 4
        and len(unique_hashes) == len(hashes) == cohort.step_count
        and all(_canonical_step_hash(step_hash) for step_hash in hashes)
        and context.current_blackout_id in cohort.blackout_ids
        and 1 <= context.current_blackout_step_count <= 2
        and cohort.step_count - context.current_blackout_step_count >= 3
        and len(set(cohort.blackout_ids)) >= 2
        and cohort.up_step_count > 0
        and cohort.down_step_count > 0
        and cohort.up_step_count + cohort.down_step_count == cohort.step_count
        and cohort.median_k_v_per_pp is not None
        and isfinite(cohort.median_k_v_per_pp)
        and context.learning_policy.min_k_v_per_pp
        <= cohort.median_k_v_per_pp
        <= context.learning_policy.max_k_v_per_pp
        and cohort.mad_ratio is not None
        and isfinite(cohort.mad_ratio)
        and 0.0 <= cohort.mad_ratio <= 0.25
        and all(reason in _ALLOWED_COHORT_REASONS for reason in cohort.reasons.values)
    )


def _bounded_change_refusal(
    context: IrLearningContext,
    value_after: float,
) -> LearningReason | None:
    policy = context.learning_policy
    if value_after > context.current_k_v_per_pp - policy.deadband_v_per_pp:
        return LearningReason.IR_CHANGE_BELOW_NOISE_FLOOR
    if value_after < (1.0 - policy.max_epoch_decrease_fraction) * context.epoch_initial_k_v_per_pp:
        return LearningReason.EPOCH_CUMULATIVE_DECREASE_LIMIT
    return None


def make_learning_decision(
    assessment: EvidenceAssessment,
    comparison: ForwardComparison,
    ir_result: IrLearningResult | None,
    *,
    decline_evidence_eligible: bool,
) -> LearningDecision:
    """Keep history, comparison, decline, and commit permissions explicit."""
    science_eligible = assessment.evidence_class == EvidenceClass.QUALIFYING
    return LearningDecision(
        record_history=True,
        compare_forward_model=science_eligible and comparison.mode != ComparisonMode.NONE,
        record_decline_evidence=science_eligible and decline_evidence_eligible,
        commit_ir_k=science_eligible and ir_result is not None and ir_result.change is not None,
    )


def _refused(reason: LearningReason) -> IrLearningResult:
    return IrLearningResult(None, None, order_reasons((reason,)))
