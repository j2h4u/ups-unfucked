"""Pure IR-learning decision and receipt linkage invariants."""

import pytest

from src.domain.ir_learning_decision import (
    IrLearningDecision,
    IrLearningDisposition,
    IrModelCommitReceiptRecord,
)
from src.domain.learning import evidence_set_id
from src.domain.reasons import LearningReason
from src.domain.values import ModelChange, ModelCommitReceipt

H1 = "a" * 64
H2 = "b" * 64


def change(*, after: float = 0.018) -> ModelChange:
    return ModelChange("ir_k_v_per_pp", 0.020, 0.018, after, (H1, H2), True)


def receipt(value_after: float = 0.018) -> ModelCommitReceipt:
    return ModelCommitReceipt(
        blackout_id="blackout-1",
        parameter="ir_k_v_per_pp",
        value_before=0.020,
        measured_estimate=0.018,
        value_after=value_after,
        model_hash_before="c" * 64,
        model_hash_after="d" * 64,
        scientific_fingerprint_before="e" * 64,
        scientific_fingerprint_after="f" * 64,
        evidence_set_id=evidence_set_id((H1, H2)),
        consumed_step_hashes=(H1, H2),
        reference_reparameterization=False,
        safety_oracle="sampled_safety_regression_grid:1",
    )


def test_downward_decision_is_pure_and_bounded() -> None:
    value = IrLearningDecision(
        IrLearningDisposition.DOWNWARD_COMMIT,
        (H1, H2),
        evidence_set_id((H1, H2)),
        change=change(),
    )
    assert value.change is not None


def test_unbound_downward_change_is_valid_only_at_measured_estimate() -> None:
    value = IrLearningDecision(
        IrLearningDisposition.DOWNWARD_COMMIT,
        (H1, H2),
        evidence_set_id((H1, H2)),
        change=change(after=0.018),
    )
    assert value.change is not None
    value = IrLearningDecision(
        IrLearningDisposition.DOWNWARD_COMMIT,
        (H1, H2),
        evidence_set_id((H1, H2)),
        change=ModelChange("ir_k_v_per_pp", 0.020, 0.018, 0.018, (H1, H2), False),
    )
    assert value.change is not None
    with pytest.raises(ValueError, match="measured estimate"):
        IrLearningDecision(
            IrLearningDisposition.DOWNWARD_COMMIT,
            (H1, H2),
            evidence_set_id((H1, H2)),
            change=ModelChange("ir_k_v_per_pp", 0.020, 0.018, 0.017, (H1, H2), False),
        )


def test_upward_observation_cannot_carry_model_change() -> None:
    with pytest.raises(ValueError):
        IrLearningDecision(
            IrLearningDisposition.UPWARD_OBSERVATION,
            (H1, H2),
            evidence_set_id((H1, H2)),
            change=change(),
        )


def test_no_change_and_refusal_shapes() -> None:
    assert IrLearningDecision(IrLearningDisposition.NO_CHANGE, (), None).change is None
    with pytest.raises(ValueError):
        IrLearningDecision(IrLearningDisposition.REFUSED, (), None)
    with pytest.raises(ValueError):
        IrLearningDecision(IrLearningDisposition.NO_CHANGE, (H2, H1), evidence_set_id((H1, H2)))


def test_commit_receipt_replays_exact_decision() -> None:
    decision = IrLearningDecision(
        IrLearningDisposition.DOWNWARD_COMMIT,
        (H1, H2),
        evidence_set_id((H1, H2)),
        change=change(),
    )
    record = IrModelCommitReceiptRecord(decision, receipt())
    assert record.receipt.parameter == "ir_k_v_per_pp"
    with pytest.raises(ValueError):
        IrModelCommitReceiptRecord(decision, receipt(0.019))


def test_only_ir_k_and_lowercase_sorted_hashes_are_accepted() -> None:
    bad = ModelChange("soh", 0.9, 0.8, 0.8, (H1,), True)
    with pytest.raises(ValueError):
        IrLearningDecision(
            IrLearningDisposition.DOWNWARD_COMMIT,
            (H1,),
            evidence_set_id((H1,)),
            change=bad,
        )
    with pytest.raises(ValueError):
        IrLearningDecision(
            IrLearningDisposition.REFUSED,
            ("A" * 64,),
            "A" * 64,
            reasons=(LearningReason.COHORT_NOT_ELIGIBLE,),
        )
