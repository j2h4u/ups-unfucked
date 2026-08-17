"""Focused transactional ordering and crash-recovery tests for close_blackout."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.application.assessment_worker import CloseRequest, PreparedClose
from src.application.close_blackout import close_blackout
from src.application.model_port import PreparedModelCommit
from src.application.storage_values import (
    EventHandle,
    EventProjection,
    EventRecord,
    ProcessingRef,
    ProjectedEventRecord,
)
from src.domain.learning import ObservedLoadSagIncrease, evidence_set_id
from src.domain.reasons import LearningReason, order_reasons
from src.domain.values import (
    ComparisonMode,
    EvidenceAssessment,
    EvidenceClass,
    ForwardComparison,
    IrCohortEstimate,
    LearningDecision,
    ModelChange,
    ModelCommitReceipt,
    NumericSummary,
    TerminalDisposition,
)

NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)
BLACKOUT_ID = "a" * 32
SEGMENT_ID = "b" * 32
PATH = f"evt-20260816T000000.000Z-{BLACKOUT_ID}.jsonl"


class TransactionStore:
    def __init__(self, *, fail_after_receipt: bool = False) -> None:
        self.operations: list[str] = []
        self.fail_after_receipt = fail_after_receipt
        self.receipt_payload = None
        self.outcome = None
        self.capture_active = None

    def append(self, handle, record):
        self.operations.append(f"append:{record.record_type}")
        advanced = replace(
            handle,
            next_seq=handle.next_seq + 1,
            last_record_sha256=f"{handle.next_seq:064x}",
        )
        if record.record_type == "learning_decision":
            self.capture_active = "blackout-b"
        if record.record_type == "model_commit":
            self.receipt_payload = record.payload
            if self.fail_after_receipt:
                self.fail_after_receipt = False
                raise RuntimeError("crash_after_durable_receipt")
        return advanced

    def checkpoint_processing(self, handle, frozen_stage):
        self.operations.append(f"checkpoint:{frozen_stage}")

    def seal(self, handle, outcome):
        assert self.capture_active in {None, "blackout-b"}
        self.operations.append("seal")
        self.outcome = outcome
        return object()


class TransactionModel:
    def __init__(self, store: TransactionStore, *, crash_after_apply: bool = False) -> None:
        self.store = store
        self.crash_after_apply = crash_after_apply
        self.calls = 0
        self.applies = 0

    def commit_prepared(self, prepared):
        self.calls += 1
        self.store.operations.append("model_commit")
        if self.applies == 0:
            self.applies += 1
            if self.crash_after_apply:
                self.crash_after_apply = False
                raise RuntimeError("crash_after_model_save_before_receipt")
        return _receipt()


def _physical_record(seq: int, record_type: str) -> ProjectedEventRecord:
    return ProjectedEventRecord(
        schema_version=2,
        record_type=record_type,
        provenance="physical",
        blackout_id=BLACKOUT_ID,
        segment_id=SEGMENT_ID,
        seq=seq,
        boot_id="boot-a",
        wall_time_utc="2026-08-16T00:00:00Z",
        monotonic_ns=seq,
        prev_record_sha256=None if seq == 0 else "a" * 64,
        payload={"termination": "power_restored"} if record_type == "end" else {},
        record_sha256=chr(98 + seq) * 64,
    )


def _projection() -> EventProjection:
    start = _physical_record(0, "start")
    end = _physical_record(1, "end")
    return EventProjection(
        start,
        (),
        (),
        end,
        (),
        None,
        ((start, end),),
        (),
        0,
        (start, end),
    )


def _request() -> CloseRequest:
    processing = ProcessingRef(BLACKOUT_ID, (SEGMENT_ID,), PATH, "end_durable", "c" * 64)
    return CloseRequest(processing)


def _assessment() -> EvidenceAssessment:
    summary = NumericSummary(12.0, 12.5, 12.25, 0.1)
    return EvidenceAssessment(
        EvidenceClass.QUALIFYING,
        300.0,
        301,
        1.0,
        1.0,
        summary,
        NumericSummary(20.0, 40.0, 30.0, 10.0),
        order_reasons(()),
    )


def _comparison() -> ForwardComparison:
    return ForwardComparison(
        ComparisonMode.FULL,
        60_000_000_000,
        240.0,
        241,
        0.0,
        0.0,
        0.0,
        0.0,
        -0.001,
        -0.001,
        1.0,
        order_reasons(()),
    )


def _cohort() -> IrCohortEstimate:
    return IrCohortEstimate(
        "epoch-a",
        (BLACKOUT_ID, "c" * 32),
        4,
        2,
        2,
        0.010,
        0.1,
        order_reasons(()),
    )


def _prepared_commit() -> PreparedModelCommit:
    change = ModelChange(
        "ir_k_v_per_pp",
        0.015,
        0.010,
        0.012,
        tuple(character * 64 for character in "1234"),
        True,
    )
    return PreparedModelCommit(
        BLACKOUT_ID,
        change,
        NOW,
        "d" * 64,
        "e" * 64,
        "f" * 64,
    )


def _receipt() -> ModelCommitReceipt:
    prepared = _prepared_commit()
    return ModelCommitReceipt(
        BLACKOUT_ID,
        prepared.change.parameter,
        prepared.change.value_before,
        prepared.change.measured_estimate,
        prepared.change.value_after,
        prepared.model_hash_before,
        prepared.expected_model_hash_after,
        "0" * 64,
        prepared.expected_scientific_fingerprint_after,
        "9" * 64,
        prepared.change.evidence_hashes,
        False,
        "dense_no_later_lb",
    )


def _upward_observation() -> ObservedLoadSagIncrease:
    evidence_hashes = tuple(character * 64 for character in "1234")
    return ObservedLoadSagIncrease(
        parameter="ir_k_v_per_pp",
        value_before=0.009,
        measured_estimate=0.010,
        evidence_set_id=evidence_set_id(evidence_hashes),
        evidence_hashes=evidence_hashes,
    )


def _plan(
    *,
    derived: bool,
    prepared: bool,
    receipt: ModelCommitReceipt | None = None,
) -> PreparedClose:
    projection = _projection()
    records = (
        (
            EventRecord(
                "learning_decision",
                "boot-a",
                "2026-08-16T00:00:00Z",
                1,
                {"prepared_commit": "exact"},
                "derived",
            ),
        )
        if derived
        else ()
    )
    return PreparedClose(
        request=_request(),
        handle=EventHandle(BLACKOUT_ID, SEGMENT_ID, PATH, 2, "c" * 64),
        projection=projection,
        assessment=_assessment(),
        comparison=_comparison(),
        cohort_estimate=_cohort(),
        learning_decision=LearningDecision(True, True, False, prepared),
        outcome_reasons=order_reasons(()),
        derived_records=records,
        prepared_commit=_prepared_commit() if prepared else None,
        existing_receipt=receipt,
    )


def test_safe_commit_persists_decision_before_model_receipt_and_outcome():
    store = TransactionStore()
    model = TransactionModel(store)

    result = close_blackout(store, model, _plan(derived=True, prepared=True))

    assert result.outcome.disposition == TerminalDisposition.LEARNED
    assert store.operations == [
        "append:learning_decision",
        "checkpoint:learning_decision_durable",
        "model_commit",
        "append:model_commit",
        "checkpoint:model_commit_durable",
        "seal",
    ]


def test_missing_learning_gate_makes_zero_model_writes():
    store = TransactionStore()
    model = TransactionModel(store)

    result = close_blackout(
        store,
        model,
        _plan(derived=True, prepared=False),
    )

    assert result.outcome.disposition == TerminalDisposition.RECORDED_ONLY
    assert model.calls == model.applies == 0


def test_upward_ir_observation_is_sealed_exactly_without_consuming_evidence():
    store = TransactionStore()
    model = TransactionModel(store)
    observation = _upward_observation()
    plan = replace(
        _plan(derived=True, prepared=False),
        learning_decision=LearningDecision(True, True, False, False),
        outcome_reasons=order_reasons((LearningReason.UNSAFE_UPWARD_IR_CHANGE_NOT_APPLIED,)),
        observed_load_sag_increase=observation,
    )

    result = close_blackout(store, model, plan)

    assert result.outcome.disposition == TerminalDisposition.RECORDED_ONLY
    assert result.outcome.commit_receipt is None
    assert model.calls == model.applies == 0
    assert store.outcome is not None
    payload = store.outcome.payload
    assert payload["observed_load_sag_increase"] == {
        "parameter": "ir_k_v_per_pp",
        "value_before": 0.009,
        "measured_estimate": 0.010,
        "evidence_set_id": observation.evidence_set_id,
        "evidence_hashes": list(observation.evidence_hashes),
    }
    assert payload["evidence_identifiers"] == {
        "evidence_set_id": observation.evidence_set_id,
        "commit_receipt_id": None,
        "consumed_step_hashes": [],
        "observed_step_hashes": list(observation.evidence_hashes),
    }
    assert payload["model_change"] is None
    assert payload["ordered_reasons"] == [LearningReason.UNSAFE_UPWARD_IR_CHANGE_NOT_APPLIED.value]


def test_recovery_after_model_save_before_receipt_reconstructs_once():
    store = TransactionStore()
    model = TransactionModel(store, crash_after_apply=True)
    with pytest.raises(RuntimeError, match="crash_after_model_save"):
        close_blackout(store, model, _plan(derived=True, prepared=True))
    result = close_blackout(store, model, _plan(derived=False, prepared=True))

    assert result.outcome.disposition == TerminalDisposition.LEARNED
    assert model.calls == 2
    assert model.applies == 1
    assert store.operations.count("append:model_commit") == 1
    assert store.operations.count("seal") == 1


def test_recovery_after_durable_receipt_seals_without_second_model_call():
    store = TransactionStore(fail_after_receipt=True)
    model = TransactionModel(store)
    receipt = _receipt()
    with pytest.raises(RuntimeError, match="crash_after_durable_receipt"):
        close_blackout(store, model, _plan(derived=True, prepared=True))
    result = close_blackout(
        store,
        model,
        _plan(derived=False, prepared=True, receipt=receipt),
    )

    assert result.outcome.disposition == TerminalDisposition.LEARNED
    assert model.calls == 1
    assert store.operations.count("append:model_commit") == 1
    assert store.operations.count("seal") == 1


def test_blackout_b_can_start_while_blackout_a_is_processing():
    store = TransactionStore()
    model = TransactionModel(store)

    close_blackout(store, model, _plan(derived=True, prepared=False))

    assert store.capture_active == "blackout-b"
    assert store.operations[-1] == "seal"
