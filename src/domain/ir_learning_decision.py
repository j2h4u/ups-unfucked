"""Pure, bounded IR-learning decisions and commit receipt linkage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

from src.domain.learning import ObservedLoadSagIncrease
from src.domain.learning import evidence_set_id as make_evidence_set_id
from src.domain.reasons import LearningReason
from src.domain.values import ModelChange, ModelCommitReceipt

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_PARAMETER = "ir_k_v_per_pp"
_MAX_HASHES = 64


class IrLearningDisposition(StrEnum):
    """The only three scientific states plus an explicit refusal."""

    DOWNWARD_COMMIT = "downward_commit"
    UPWARD_OBSERVATION = "upward_observation"
    NO_CHANGE = "no_change"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class IrLearningDecision:
    """A pure decision; it grants no persistence or model-write authority."""

    disposition: IrLearningDisposition
    evidence_hashes: tuple[str, ...]
    evidence_set_id: str | None
    reasons: tuple[LearningReason, ...] = ()
    change: ModelChange | None = None
    observed_load_sag_increase: ObservedLoadSagIncrease | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, IrLearningDisposition):
            raise TypeError("IR learning disposition must be IrLearningDisposition")
        _validate_hashes(self.evidence_hashes)
        if self.evidence_hashes:
            if self.evidence_set_id != make_evidence_set_id(self.evidence_hashes):
                raise ValueError("evidence-set ID does not match evidence hashes")
        elif self.evidence_set_id is not None:
            raise ValueError("empty evidence cannot have an evidence-set ID")
        if any(not isinstance(reason, LearningReason) for reason in self.reasons):
            raise TypeError("IR learning reasons must be LearningReason values")
        _validate_change(self.change)
        _validate_observation(self.observed_load_sag_increase)
        _validate_disposition(self)


@dataclass(frozen=True, slots=True)
class IrModelCommitReceiptRecord:
    """Replayable wrapper binding a model-owner receipt to its pure decision."""

    decision: IrLearningDecision
    receipt: ModelCommitReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.decision, IrLearningDecision):
            raise TypeError("commit record decision must be IrLearningDecision")
        if not isinstance(self.receipt, ModelCommitReceipt):
            raise TypeError("commit record receipt must be ModelCommitReceipt")
        if self.decision.disposition is not IrLearningDisposition.DOWNWARD_COMMIT:
            raise ValueError("commit receipt requires a downward-commit decision")
        change = self.decision.change
        if change is None:
            raise ValueError("commit receipt decision has no model change")
        _validate_receipt(self.receipt, change, self.decision)


def _validate_disposition(value: IrLearningDecision) -> None:
    if value.disposition is IrLearningDisposition.DOWNWARD_COMMIT:
        _validate_downward(value)
    elif value.disposition is IrLearningDisposition.UPWARD_OBSERVATION:
        _validate_upward(value)
    elif value.disposition is IrLearningDisposition.NO_CHANGE:
        _validate_no_change(value)
    else:
        _validate_refusal(value)


def _validate_downward(value: IrLearningDecision) -> None:
    change = value.change
    if change is None or value.observed_load_sag_increase is not None:
        raise ValueError("downward commit requires exactly one downward ModelChange")
    if change.value_after >= change.value_before:
        raise ValueError("downward commit must lower ir_k")
    if not isinstance(change.bound_applied, bool):
        raise TypeError("downward commit bound flag must be bool")
    if not change.bound_applied and change.value_after != change.measured_estimate:
        raise ValueError("an unbound downward commit must retain the measured estimate exactly")
    if change.evidence_hashes != value.evidence_hashes:
        raise ValueError("decision evidence does not match model change evidence")


def _validate_upward(value: IrLearningDecision) -> None:
    observation = value.observed_load_sag_increase
    if observation is None or value.change is not None:
        raise ValueError("upward observation requires exactly one observed increase")
    if observation.evidence_hashes != value.evidence_hashes:
        raise ValueError("decision evidence does not match upward observation evidence")
    if observation.evidence_set_id != value.evidence_set_id:
        raise ValueError("decision evidence-set ID does not match upward observation")


def _validate_no_change(value: IrLearningDecision) -> None:
    if value.change is not None or value.observed_load_sag_increase is not None:
        raise ValueError("no-change decision cannot carry a change or observation")


def _validate_refusal(value: IrLearningDecision) -> None:
    if value.change is not None or value.observed_load_sag_increase is not None:
        raise ValueError("refusal cannot carry a change or observation")
    if not value.reasons:
        raise ValueError("refusal requires a typed learning reason")


def _validate_change(value: ModelChange | None) -> None:
    if value is None:
        return
    if value.parameter != _PARAMETER:
        raise ValueError("only ir_k_v_per_pp may be changed")
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float)) or not isfinite(float(item))
        for item in (value.value_before, value.measured_estimate, value.value_after)
    ):
        raise ValueError("model change values must be finite numbers")
    if min(value.value_before, value.measured_estimate, value.value_after) <= 0:
        raise ValueError("model change values must be positive")
    _validate_hashes(value.evidence_hashes)


def _validate_observation(value: ObservedLoadSagIncrease | None) -> None:
    if value is None:
        return
    if value.parameter != _PARAMETER:
        raise ValueError("only ir_k_v_per_pp may be observed")
    _validate_hashes(value.evidence_hashes)
    if not value.evidence_hashes or value.evidence_set_id != make_evidence_set_id(
        value.evidence_hashes
    ):
        raise ValueError("observed increase has invalid evidence linkage")
    if value.measured_estimate <= value.value_before:
        raise ValueError("observed increase must be upward")


def _validate_receipt(
    receipt: ModelCommitReceipt, change: ModelChange, decision: IrLearningDecision
) -> None:
    if receipt.parameter != _PARAMETER or receipt.reference_reparameterization:
        raise ValueError("receipt is not a canonical ir_k commit")
    if (
        receipt.value_before != change.value_before
        or receipt.measured_estimate != change.measured_estimate
    ):
        raise ValueError("receipt does not match decision change")
    if (
        receipt.value_after != change.value_after
        or receipt.consumed_step_hashes != change.evidence_hashes
    ):
        raise ValueError("receipt evidence/change does not match decision")
    if receipt.evidence_set_id != decision.evidence_set_id:
        raise ValueError("receipt evidence-set ID does not match decision")
    if not receipt.safety_oracle.strip():
        raise ValueError("receipt requires a non-empty safety oracle")
    for item in (
        receipt.model_hash_before,
        receipt.model_hash_after,
        receipt.scientific_fingerprint_before,
        receipt.scientific_fingerprint_after,
    ):
        if _HASH_RE.fullmatch(item) is None:
            raise ValueError("receipt hashes must be lowercase SHA-256")
    _validate_hashes(receipt.consumed_step_hashes)


def _validate_hashes(values: tuple[str, ...]) -> None:
    if not isinstance(values, tuple) or len(values) > _MAX_HASHES:
        raise ValueError("evidence hashes must be a tuple of at most 64 values")
    if any(_HASH_RE.fullmatch(item) is None for item in values):
        raise ValueError("evidence hashes must be lowercase SHA-256")
    if tuple(sorted(values)) != values or len(set(values)) != len(values):
        raise ValueError("evidence hashes must be sorted and unique")
