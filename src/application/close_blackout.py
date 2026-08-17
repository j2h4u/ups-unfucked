"""The single live-and-recovery blackout completion use case."""

from dataclasses import dataclass

from src.application.assessment_codec import json_value
from src.application.assessment_worker import PreparedClose
from src.application.model_port import ModelCommitPort, ModelPortConflict, ModelPortRefused
from src.application.ports import AssessmentCloseEventStorePort
from src.application.storage_values import EventRecord, TerminalOutcomeRecord
from src.domain.learning import (
    ObservedLoadSagIncrease,
    model_commit_refusal_reason,
    validate_observed_load_sag_outcome,
)
from src.domain.reasons import LearningReason, order_reasons
from src.domain.values import (
    ComparisonMode,
    EvidenceClass,
    ModelCommitReceipt,
    TerminalDisposition,
    TerminalOutcome,
)


@dataclass(frozen=True, slots=True)
class CloseResult:
    outcome: TerminalOutcome


def close_blackout(
    store: AssessmentCloseEventStorePort,
    model: ModelCommitPort,
    prepared: PreparedClose,
) -> CloseResult:
    """Apply one frozen plan; live close and recovery call this exact function."""
    handle = prepared.handle
    if prepared.projection.outcome is not None:
        outcome = _terminal_outcome(prepared, prepared.existing_receipt)
        store.seal(handle, _outcome_record(prepared, outcome))
        return CloseResult(outcome)

    for record in prepared.derived_records:
        handle = store.append(handle, record)
        store.checkpoint_processing(handle, f"{record.record_type}_durable")

    receipt = prepared.existing_receipt
    extra_reasons: tuple[LearningReason, ...] = ()
    if receipt is None and prepared.prepared_commit is not None:
        try:
            receipt = model.commit_prepared(prepared.prepared_commit)
        except ModelPortConflict:
            extra_reasons = (LearningReason.MODEL_STATE_CONFLICT,)
        except ModelPortRefused as exc:
            extra_reasons = (_refusal_reason(exc),)
        else:
            handle = store.append(handle, _receipt_record(prepared, receipt))
            store.checkpoint_processing(handle, "model_commit_durable")

    outcome = _terminal_outcome(prepared, receipt, extra_reasons=extra_reasons)
    store.seal(handle, _outcome_record(prepared, outcome))
    return CloseResult(outcome)


def _terminal_outcome(
    prepared: PreparedClose,
    receipt: ModelCommitReceipt | None,
    *,
    extra_reasons: tuple[LearningReason, ...] = (),
) -> TerminalOutcome:
    reasons = order_reasons((*prepared.outcome_reasons.values, *extra_reasons))
    if receipt is not None:
        disposition = TerminalDisposition.LEARNED
    elif prepared.assessment.evidence_class == EvidenceClass.REJECTED:
        disposition = TerminalDisposition.REJECTED
    else:
        disposition = TerminalDisposition.RECORDED_ONLY
    return TerminalOutcome(
        disposition=disposition,
        assessment=prepared.assessment,
        comparison=prepared.comparison,
        cohort_estimate=prepared.cohort_estimate,
        learning_decision=prepared.learning_decision,
        commit_receipt=receipt,
        reasons=reasons,
    )


def _receipt_record(
    prepared: PreparedClose,
    receipt: ModelCommitReceipt,
) -> EventRecord:
    final = _event_anchor(prepared)
    if final is None:
        raise RuntimeError("event_missing_physical_record")
    return EventRecord(
        record_type="model_commit",
        boot_id=final.boot_id,
        wall_time_utc=final.wall_time_utc,
        monotonic_ns=final.monotonic_ns,
        payload=json_value(receipt),
        provenance="derived",
    )


def _outcome_record(
    prepared: PreparedClose,
    outcome: TerminalOutcome,
) -> TerminalOutcomeRecord:
    final = _event_anchor(prepared)
    if final is None:
        raise RuntimeError("event_missing_physical_record")
    receipt = outcome.commit_receipt
    observed_increase = prepared.observed_load_sag_increase
    validate_observed_load_sag_outcome(observed_increase, outcome)
    comparison = outcome.comparison
    payload = {
        "disposition": outcome.disposition.value,
        "evidence_class": outcome.assessment.evidence_class.value,
        "duration_s": outcome.assessment.duration_s,
        "comparison_available": (comparison is not None and comparison.mode != ComparisonMode.NONE),
        "comparison_mode": (
            ComparisonMode.NONE.value if comparison is None else comparison.mode.value
        ),
        "ir_estimate_available": (
            outcome.cohort_estimate is not None
            and outcome.cohort_estimate.median_k_v_per_pp is not None
        ),
        "commit_receipt_id": None if receipt is None else receipt.evidence_set_id,
        "observed_load_sag_increase": json_value(observed_increase),
        "reasons": [reason.value for reason in outcome.reasons.values],
        "reason_overflow": outcome.reasons.overflow_count,
        "ordered_reasons": [reason.value for reason in outcome.reasons.values],
        "decline_evidence_eligible": outcome.learning_decision.record_decline_evidence,
        "evidence_identifiers": _evidence_identifiers(receipt, observed_increase),
        "model_change": (
            None
            if receipt is None
            else {
                "parameter": receipt.parameter,
                "value_before": receipt.value_before,
                "measured_estimate": receipt.measured_estimate,
                "value_after": receipt.value_after,
            }
        ),
        "terminal_outcome": json_value(outcome),
    }
    return TerminalOutcomeRecord(
        boot_id=final.boot_id,
        wall_time_utc=final.wall_time_utc,
        monotonic_ns=final.monotonic_ns,
        payload=payload,
    )


def _evidence_identifiers(
    receipt: ModelCommitReceipt | None,
    observed_increase: ObservedLoadSagIncrease | None,
) -> dict[str, object]:
    if receipt is not None and observed_increase is not None:
        raise RuntimeError("applied commit cannot also be an unapplied upward observation")
    return {
        "evidence_set_id": (
            receipt.evidence_set_id
            if receipt is not None
            else None
            if observed_increase is None
            else observed_increase.evidence_set_id
        ),
        "commit_receipt_id": None if receipt is None else receipt.evidence_set_id,
        "consumed_step_hashes": [] if receipt is None else list(receipt.consumed_step_hashes),
        "observed_step_hashes": (
            [] if observed_increase is None else list(observed_increase.evidence_hashes)
        ),
    }


def _refusal_reason(exc: ModelPortRefused) -> LearningReason:
    return model_commit_refusal_reason(str(exc))


def _event_anchor(prepared: PreparedClose):
    projection = prepared.projection
    if projection.end is not None:
        return projection.end
    if projection.start is not None:
        return projection.start
    return projection.records[-1] if projection.records else None
