"""Focused tests for conservative learning decisions and bounds."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest

from src.domain.learning import (
    IrLearningContext,
    evaluate_ir_learning,
    evidence_set_id,
    make_learning_decision,
    model_commit_refusal_reason,
)
from src.domain.reasons import IdentificationReason, LearningReason, order_reasons
from src.domain.values import (
    DEFAULT_IR_LEARNING_POLICY,
    ComparisonMode,
    EvidenceAssessment,
    EvidenceClass,
    ForwardComparison,
    IrCohortEstimate,
    IrLearningPolicy,
    NumericSummary,
    ensure_supported_ir_learning_policy,
)

NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)


def _cohort(measured_k):
    return IrCohortEstimate(
        battery_epoch_id="epoch-a",
        blackout_ids=("a", "b"),
        step_count=4,
        up_step_count=2,
        down_step_count=2,
        median_k_v_per_pp=measured_k,
        mad_ratio=0.1,
        reasons=order_reasons(()),
    )


def _evaluate(measured_k, **overrides):
    arguments = {
        "current_blackout_id": "a",
        "current_blackout_step_count": 1,
        "battery_epoch_id": "epoch-a",
        "current_k_v_per_pp": 0.020,
        "epoch_initial_k_v_per_pp": 0.030,
        "reference_load_percent": 0.0,
        "current_utc": NOW,
        "previous_commit_utc": None,
        "candidate_step_hashes": ("a" * 64, "b" * 64, "c" * 64, "d" * 64),
        "consumed_step_hashes": frozenset(),
    }
    arguments.update(overrides)
    return evaluate_ir_learning(_cohort(measured_k), IrLearningContext(**arguments))


def test_valid_decrease_is_bounded_to_twenty_percent():
    result = _evaluate(0.010)
    assert result.change is not None
    assert result.change.measured_estimate == 0.010
    assert result.change.value_after == pytest.approx(0.016)
    assert result.change.bound_applied is True


def test_evidence_set_identity_is_order_independent_and_strict():
    hashes = tuple(character * 64 for character in "abcd")

    expected = sha256("".join(sorted(hashes)).encode("ascii")).hexdigest()

    assert evidence_set_id(hashes) == expected
    assert evidence_set_id(tuple(reversed(hashes))) == expected
    assert len(evidence_set_id(hashes)) == 64
    with pytest.raises(ValueError, match="at least one"):
        evidence_set_id(())
    with pytest.raises(ValueError, match="duplicate"):
        evidence_set_id((hashes[0], hashes[0]))
    with pytest.raises(ValueError, match="non-canonical"):
        evidence_set_id(("not-a-hash",))


@pytest.mark.parametrize(
    ("measured_k", "reason"),
    (
        (0.019, LearningReason.IR_CHANGE_BELOW_NOISE_FLOOR),
        (0.021, LearningReason.UNSAFE_UPWARD_IR_CHANGE_NOT_APPLIED),
    ),
)
def test_noise_floor_and_upward_estimate_never_commit(measured_k, reason):
    result = _evaluate(measured_k)
    assert result.change is None
    assert result.reasons.values == (reason,)


def test_upward_estimate_retains_exact_before_measurement_and_unconsumed_evidence():
    hashes = ("a" * 64, "b" * 64, "c" * 64, "d" * 64)

    result = _evaluate(0.021, candidate_step_hashes=tuple(reversed(hashes)))

    assert result.change is None
    assert result.evidence_set_id == evidence_set_id(hashes)
    assert result.observed_load_sag_increase is not None
    assert result.observed_load_sag_increase.value_before == 0.020
    assert result.observed_load_sag_increase.measured_estimate == 0.021
    assert result.observed_load_sag_increase.evidence_set_id == evidence_set_id(hashes)
    assert result.observed_load_sag_increase.evidence_hashes == hashes


@pytest.mark.parametrize(
    ("elapsed_days", "commits"),
    ((29, False), (30, True), (31, True)),
)
def test_thirty_day_commit_boundary(elapsed_days, commits):
    result = _evaluate(0.015, previous_commit_utc=NOW - timedelta(days=elapsed_days))
    assert (result.change is not None) is commits


def test_backward_wall_time_fails_closed():
    result = _evaluate(0.015, previous_commit_utc=NOW + timedelta(seconds=1))
    assert result.reasons.values == (LearningReason.COMMIT_RATE_WINDOW_INDETERMINATE,)


def test_reference_frame_and_cumulative_bound_refuse():
    wrong_frame = _evaluate(0.015, reference_load_percent=20.0)
    assert wrong_frame.reasons.values == (LearningReason.IR_REFERENCE_FRAME_NOT_TRANSFORMED,)
    cumulative = _evaluate(
        0.005,
        current_k_v_per_pp=0.016,
        epoch_initial_k_v_per_pp=0.030,
    )
    assert cumulative.reasons.values == (LearningReason.EPOCH_CUMULATIVE_DECREASE_LIMIT,)


def test_consumed_evidence_is_never_reused():
    result = _evaluate(0.015, consumed_step_hashes=frozenset({"a" * 64}))
    assert result.reasons.values == (LearningReason.EVIDENCE_ALREADY_CONSUMED,)


@pytest.mark.parametrize(
    "candidate_step_hashes",
    (
        (),
        ("a" * 64, "b" * 64, "c" * 64),
        ("a" * 64, "a" * 64, "b" * 64, "c" * 64),
        ("a" * 64, "b" * 64, "c" * 64, "not-a-hash"),
    ),
)
def test_learning_refuses_missing_duplicate_or_noncanonical_evidence(candidate_step_hashes):
    result = _evaluate(0.015, candidate_step_hashes=candidate_step_hashes)
    assert result.change is None
    assert result.reasons.values == (LearningReason.COHORT_NOT_ELIGIBLE,)


def test_learning_refuses_blocked_or_current_event_inconsistent_cohort():
    blocked = _cohort(0.015)
    blocked = IrCohortEstimate(
        blocked.battery_epoch_id,
        blocked.blackout_ids,
        blocked.step_count,
        blocked.up_step_count,
        blocked.down_step_count,
        blocked.median_k_v_per_pp,
        blocked.mad_ratio,
        order_reasons((IdentificationReason.HIGH_COHORT_DISPERSION,)),
    )
    blocked_result = evaluate_ir_learning(
        blocked,
        IrLearningContext(
            current_blackout_id="a",
            current_blackout_step_count=1,
            battery_epoch_id="epoch-a",
            current_k_v_per_pp=0.020,
            epoch_initial_k_v_per_pp=0.030,
            reference_load_percent=0.0,
            current_utc=NOW,
            previous_commit_utc=None,
            candidate_step_hashes=("a" * 64, "b" * 64, "c" * 64, "d" * 64),
            consumed_step_hashes=frozenset(),
        ),
    )
    missing_current = _evaluate(0.015, current_blackout_id="missing")

    assert blocked_result.reasons.values == (LearningReason.COHORT_NOT_ELIGIBLE,)
    assert missing_current.reasons.values == (LearningReason.COHORT_NOT_ELIGIBLE,)


@pytest.mark.parametrize(
    ("value", "reason"),
    (
        ("overlapping_evidence_already_consumed", LearningReason.EVIDENCE_ALREADY_CONSUMED),
        ("unsupported_model_parameter", LearningReason.UNSUPPORTED_MODEL_PARAMETER),
        ("missing_commit_evidence", LearningReason.MISSING_COMMIT_EVIDENCE),
        ("ir_commit_not_canonical", LearningReason.IR_COMMIT_NOT_CANONICAL),
        ("ir_change_below_noise_floor", LearningReason.IR_CHANGE_BELOW_NOISE_FLOOR),
        ("ir_bound_flag_mismatch", LearningReason.IR_BOUND_FLAG_MISMATCH),
        ("invalid_evidence_hash", LearningReason.INVALID_EVIDENCE_HASH),
        ("duplicate_evidence_hash", LearningReason.DUPLICATE_EVIDENCE_HASH),
        ("value_before must be a finite number", LearningReason.INVALID_MODEL_CHANGE_VALUE),
        ("value_after must be positive", LearningReason.INVALID_MODEL_CHANGE_VALUE),
        ("commit time must be timezone-aware UTC", LearningReason.INVALID_COMMIT_TIME),
        ("epoch_cumulative_decrease_exceeded", LearningReason.EPOCH_CUMULATIVE_DECREASE_LIMIT),
        (
            "consumed_evidence_budget_exhausted",
            LearningReason.CONSUMED_EVIDENCE_BUDGET_EXHAUSTED,
        ),
        ("commit_rate_window_indeterminate", LearningReason.COMMIT_RATE_WINDOW_INDETERMINATE),
        ("commit_rate_limited", LearningReason.COMMIT_RATE_LIMITED),
        ("safety_oracle_failed", LearningReason.SAFETY_ORACLE_FAILED),
    ),
)
def test_model_commit_refusal_mapping_is_total_for_known_values(value, reason):
    assert model_commit_refusal_reason(value) == reason


def test_unknown_model_commit_refusal_fails_closed():
    with pytest.raises(ValueError, match="unknown model commit refusal"):
        model_commit_refusal_reason("new_unhandled_refusal")


def test_learning_policy_is_versioned_and_values_cannot_drift():
    assert DEFAULT_IR_LEARNING_POLICY.revision == "ir-learning-v1"
    assert DEFAULT_IR_LEARNING_POLICY.min_commit_interval.days == 30
    with pytest.raises(ValueError, match="unknown learning policy revision"):
        ensure_supported_ir_learning_policy(
            IrLearningPolicy(
                revision="future",
                deadband_v_per_pp=0.001,
                min_k_v_per_pp=0.005,
                max_k_v_per_pp=0.040,
                max_single_commit_fraction=0.20,
                max_epoch_decrease_fraction=0.50,
                min_commit_interval_days=30,
                max_consumed_step_hashes=256,
            )
        )
    with pytest.raises(ValueError, match="values do not match"):
        ensure_supported_ir_learning_policy(
            replace(DEFAULT_IR_LEARNING_POLICY, deadband_v_per_pp=0.002)
        )


def test_learning_decision_keeps_four_permissions_separate():
    summary = NumericSummary(1.0, 1.0, 1.0, 0.0)
    assessment = EvidenceAssessment(
        EvidenceClass.QUALIFYING,
        300.0,
        301,
        1.0,
        1.0,
        summary,
        summary,
        order_reasons(()),
    )
    comparison = ForwardComparison(
        ComparisonMode.FULL,
        1,
        300.0,
        301,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.1,
        order_reasons(()),
    )
    result = _evaluate(0.015)
    decision = make_learning_decision(
        assessment,
        comparison,
        result,
        decline_evidence_eligible=True,
    )
    assert decision.record_history is True
    assert decision.compare_forward_model is True
    assert decision.record_decline_evidence is True
    assert decision.commit_ir_k is True


def test_decline_permission_is_explicit_and_independent_of_ir_cohort():
    summary = NumericSummary(1.0, 1.0, 1.0, 0.0)
    assessment = EvidenceAssessment(
        EvidenceClass.QUALIFYING,
        650.0,
        651,
        1.0,
        1.0,
        summary,
        summary,
        order_reasons(()),
    )
    comparison = ForwardComparison(
        ComparisonMode.SHORT_WINDOW,
        1,
        180.0,
        181,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.1,
        order_reasons(()),
    )

    short_ineligible = make_learning_decision(
        assessment,
        comparison,
        _evaluate(0.015),
        decline_evidence_eligible=False,
    )
    independently_eligible = make_learning_decision(
        assessment,
        comparison,
        None,
        decline_evidence_eligible=True,
    )

    assert short_ineligible.record_decline_evidence is False
    assert short_ineligible.commit_ir_k is True
    assert independently_eligible.record_decline_evidence is True
    assert independently_eligible.commit_ir_k is False
