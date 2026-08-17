"""Focused recovery integration against the real JSONL storage contract."""

import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.adapters.jsonl_event_store import JsonlEventStore
from src.application.assessment_codec import json_value
from src.application.assessment_worker import AssessmentWorker, CloseRequest
from src.application.close_blackout import close_blackout
from src.application.model_port import ModelPolicyProjection, PreparedModelCommit
from src.application.storage_values import EventRecord, EventRef, EventStart
from src.battery_math.lut import LutPoint
from src.domain.reasons import EvidenceReason, order_reasons
from src.domain.values import (
    ComparisonMode,
    EvidenceAssessment,
    EvidenceClass,
    ForwardComparison,
    FrozenModelSnapshot,
    IrCohortEstimate,
    LearningDecision,
    ModelChange,
    ModelCommitReceipt,
    NumericSummary,
    TerminalDisposition,
)

NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)


class RecoveryModel:
    def __init__(self, receipt: ModelCommitReceipt) -> None:
        self.receipt = receipt
        self.commit_calls = 0

    def commit_prepared(self, prepared):
        self.commit_calls += 1
        assert prepared.model_hash_before == self.receipt.model_hash_before
        return self.receipt

    def policy_projection(self):
        snapshot = FrozenModelSnapshot(
            "domain-jsonl-v2",
            "domain-jsonl-v2",
            uuid.UUID(int=30, version=4).hex,
            "d" * 64,
            7.0,
            12.0,
            900.0,
            1.0,
            1.2,
            0.015,
            0.0,
            (LutPoint(10.0, 0.0, "standard"), LutPoint(14.0, 1.0, "standard")),
        )
        return ModelPolicyProjection(
            snapshot,
            self.receipt.model_hash_before,
            0.015,
            None,
            frozenset(),
        )


class NoMutationModel:
    def __init__(self, snapshot: FrozenModelSnapshot) -> None:
        self.snapshot = snapshot
        self.prepare_calls = 0
        self.commit_calls = 0

    def current_snapshot(self):
        return self.snapshot

    def policy_projection(self):
        return ModelPolicyProjection(self.snapshot, "f" * 64, 0.015, None, frozenset())

    def prepare_commit(self, change, *, blackout_id, committed_at):
        self.prepare_calls += 1
        raise AssertionError("damaged capture cannot prepare a model commit")

    def commit_prepared(self, prepared):
        self.commit_calls += 1
        raise AssertionError("damaged capture cannot commit the model")


def _assessment() -> EvidenceAssessment:
    return EvidenceAssessment(
        EvidenceClass.QUALIFYING,
        300.0,
        301,
        1.0,
        1.0,
        NumericSummary(12.0, 13.0, 12.5, 0.1),
        NumericSummary(20.0, 40.0, 30.0, 10.0),
        order_reasons(()),
    )


def _comparison() -> ForwardComparison:
    return ForwardComparison(
        ComparisonMode.NONE,
        None,
        0.0,
        0,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        order_reasons(()),
    )


def _cohort() -> IrCohortEstimate:
    return IrCohortEstimate(
        "epoch-a",
        ("event-a", "event-b"),
        4,
        2,
        2,
        0.010,
        0.0,
        order_reasons(()),
    )


def _prepared(blackout_id: str) -> PreparedModelCommit:
    return PreparedModelCommit(
        blackout_id,
        ModelChange(
            "ir_k_v_per_pp",
            0.015,
            0.010,
            0.012,
            tuple(character * 64 for character in "1234"),
            True,
        ),
        NOW,
        "a" * 64,
        "b" * 64,
        "c" * 64,
    )


def _receipt(prepared: PreparedModelCommit) -> ModelCommitReceipt:
    change = prepared.change
    return ModelCommitReceipt(
        prepared.blackout_id,
        change.parameter,
        change.value_before,
        change.measured_estimate,
        change.value_after,
        prepared.model_hash_before,
        prepared.expected_model_hash_after,
        "d" * 64,
        prepared.expected_scientific_fingerprint_after,
        "e" * 64,
        change.evidence_hashes,
        False,
        "dense_no_later_lb",
    )


def _decision_payload(prepared: PreparedModelCommit):
    assessment = _assessment()
    comparison = _comparison()
    cohort = _cohort()
    return {
        "learning_decision": json_value(LearningDecision(True, False, False, True)),
        "learning_reasons": json_value(order_reasons(())),
        "observed_load_sag_increase": None,
        "prepared_commit": json_value(prepared),
        "outcome_basis": {
            "assessment": json_value(assessment),
            "comparison": json_value(comparison),
            "cohort_estimate": json_value(cohort),
            "reason_codes": [],
            "reason_overflow": 0,
        },
    }


def _processing_store(tmp_path: Path, *, with_receipt: bool):
    blackout_id = uuid.UUID(int=10, version=4).hex
    segment_id = uuid.UUID(int=20, version=4).hex
    store = JsonlEventStore(tmp_path)
    handle = store.open(
        EventStart(
            blackout_id,
            segment_id,
            "boot-a",
            "2026-08-16T00:00:00Z",
            0,
            {"battery_epoch_id": uuid.UUID(int=30, version=4).hex},
        )
    )
    handle = store.append(
        handle,
        EventRecord(
            "end",
            "boot-a",
            "2026-08-16T00:05:00Z",
            300_000_000_000,
            {"termination": "power_restored"},
            "physical",
        ),
    )
    prepared = _prepared(blackout_id)
    handle = store.append(
        handle,
        EventRecord(
            "learning_decision",
            "boot-a",
            "2026-08-16T00:05:00Z",
            300_000_000_000,
            _decision_payload(prepared),
            "derived",
        ),
    )
    receipt = _receipt(prepared)
    if with_receipt:
        handle = store.append(
            handle,
            EventRecord(
                "model_commit",
                "boot-a",
                "2026-08-16T00:05:00Z",
                300_000_000_000,
                json_value(receipt),
                "derived",
            ),
        )
    store.checkpoint_processing(
        handle, "model_commit_durable" if with_receipt else "decision_durable"
    )
    return store, prepared, receipt


def test_recovery_reuses_durable_candidate_without_recomputing(tmp_path: Path):
    store, _prepared_value, receipt = _processing_store(tmp_path, with_receipt=False)
    try:
        model = RecoveryModel(receipt)
        worker = AssessmentWorker(store, model)
        worker.after_first_safety_publication()
        request = CloseRequest(store.work_registry().pending_processing[0])

        result = close_blackout(store, model, worker.prepare(request))

        assert result.outcome.disposition == TerminalDisposition.LEARNED
        assert model.commit_calls == 1
        assert store.work_registry().pending_processing == ()
        assert store.index_tail(1)[0].commit_receipt_id == receipt.evidence_set_id
    finally:
        store.close()


def test_recovery_after_receipt_seals_without_model_call(tmp_path: Path):
    store, _prepared_value, receipt = _processing_store(tmp_path, with_receipt=True)
    try:
        model = RecoveryModel(receipt)
        worker = AssessmentWorker(store, model)
        worker.after_first_safety_publication()
        request = CloseRequest(store.work_registry().pending_processing[0])

        result = close_blackout(store, model, worker.prepare(request))

        assert result.outcome.disposition == TerminalDisposition.LEARNED
        assert model.commit_calls == 0
        assert store.work_registry().pending_processing == ()
    finally:
        store.close()


def _write_derived_prefix(
    event_dir: Path,
    prefix_count: int,
    epoch_id: str,
    snapshot: FrozenModelSnapshot,
    observation: dict[str, object],
) -> tuple[EventRecord, ...]:
    store = JsonlEventStore(event_dir)
    handle = store.open(
        EventStart(
            uuid.uuid4().hex,
            uuid.uuid4().hex,
            "boot-a",
            "2026-08-16T00:00:00Z",
            0,
            {
                "observation": observation,
                "frozen_model": json_value(snapshot),
                "battery_epoch_id": epoch_id,
                "evaluation_revision": snapshot.evaluation_revision,
            },
        )
    )
    store.append(
        handle,
        EventRecord(
            "end",
            "boot-a",
            "2026-08-16T00:00:02Z",
            2_000_000_000,
            {"termination": "power_restored"},
            "physical",
        ),
    )
    worker = AssessmentWorker(store, NoMutationModel(snapshot))
    worker.after_first_safety_publication()
    prepared = worker.prepare(CloseRequest(store.work_registry().pending_processing[0]))
    assert len(prepared.derived_records) == 4
    advanced = prepared.handle
    for record in prepared.derived_records[:prefix_count]:
        advanced = store.append(advanced, record)
        store.checkpoint_processing(advanced, f"{record.record_type}_durable")
    store.close()
    return prepared.derived_records


def _assert_derived_prefix_recovery(
    event_dir: Path,
    prefix_count: int,
    snapshot: FrozenModelSnapshot,
    expected: tuple[EventRecord, ...],
) -> None:
    recovered = JsonlEventStore(event_dir)
    try:
        model = NoMutationModel(snapshot)
        worker = AssessmentWorker(recovered, model)
        worker.after_first_safety_publication()
        resumed = worker.prepare(CloseRequest(recovered.work_registry().pending_processing[0]))
        assert resumed.derived_records == expected[prefix_count:]
        close_blackout(recovered, model, resumed)
        summary = recovered.index_tail(1)[0]
        projection = recovered.project(EventRef(summary.blackout_id, summary.segment_filename))
        assert tuple(
            (record.record_type, record.payload) for record in projection.derived_records
        ) == tuple((record.record_type, record.payload) for record in expected)
    finally:
        recovered.close()


def test_recovery_after_each_derived_checkpoint_appends_only_missing_suffix(
    tmp_path: Path,
) -> None:
    epoch_id = uuid.UUID(int=90, version=4).hex
    snapshot = FrozenModelSnapshot(
        "domain-jsonl-v2",
        "domain-jsonl-v2",
        epoch_id,
        "a" * 64,
        7.0,
        12.0,
        900.0,
        1.0,
        1.2,
        0.015,
        0.0,
        (LutPoint(10.0, 0.0, "standard"), LutPoint(14.0, 1.0, "standard")),
    )
    observation = {
        "boot_id": "boot-a",
        "monotonic_ns": 0,
        "wall_time_utc": "2026-08-16T00:00:00Z",
        "raw_status": "OB DISCHRG",
        "battery_voltage_raw": "12.5",
        "battery_voltage_v": 12.5,
        "voltage_token_quantum_v": 0.1,
        "load_percent": 20.0,
        "input_voltage_v": None,
    }

    for prefix_count in range(1, 5):
        event_dir = tmp_path / f"stage-{prefix_count}"
        expected = _write_derived_prefix(
            event_dir,
            prefix_count,
            epoch_id,
            snapshot,
            observation,
        )
        _assert_derived_prefix_recovery(event_dir, prefix_count, snapshot, expected)


def test_new_blackout_capture_survives_older_event_close(tmp_path: Path):
    store, _prepared_value, receipt = _processing_store(tmp_path, with_receipt=True)
    try:
        second_blackout_id = uuid.UUID(int=40, version=4).hex
        second_segment_id = uuid.UUID(int=50, version=4).hex
        store.open(
            EventStart(
                second_blackout_id,
                second_segment_id,
                "boot-a",
                "2026-08-16T00:06:00Z",
                360_000_000_000,
                {"battery_epoch_id": uuid.UUID(int=30, version=4).hex},
            )
        )
        model = RecoveryModel(receipt)
        worker = AssessmentWorker(store, model)
        worker.after_first_safety_publication()
        first_processing = store.work_registry().pending_processing[0]

        close_blackout(store, model, worker.prepare(CloseRequest(first_processing)))

        registry = store.work_registry()
        assert registry.capture is not None
        assert registry.capture.blackout_id == second_blackout_id
        assert registry.pending_processing == ()
    finally:
        store.close()


def test_gap_and_capture_damaged_end_refuse_science_even_at_end_durable_stage(
    tmp_path: Path,
) -> None:
    blackout_id = uuid.UUID(int=60, version=4).hex
    segment_id = uuid.UUID(int=70, version=4).hex
    epoch_id = uuid.UUID(int=80, version=4).hex
    snapshot = FrozenModelSnapshot(
        "domain-jsonl-v2",
        "domain-jsonl-v2",
        epoch_id,
        "a" * 64,
        7.0,
        12.0,
        900.0,
        1.0,
        1.2,
        0.015,
        0.0,
        (LutPoint(10.0, 0.0, "standard"), LutPoint(14.0, 1.0, "standard")),
    )
    raw_observation = {
        "boot_id": "boot-a",
        "monotonic_ns": 0,
        "wall_time_utc": "2026-08-16T00:00:00Z",
        "raw_status": "OB DISCHRG",
        "battery_voltage_raw": "12.5",
        "battery_voltage_v": 12.5,
        "voltage_token_quantum_v": 0.1,
        "load_percent": 20.0,
        "input_voltage_v": None,
    }
    store = JsonlEventStore(tmp_path)
    try:
        handle = store.open(
            EventStart(
                blackout_id,
                segment_id,
                "boot-a",
                "2026-08-16T00:00:00Z",
                0,
                {
                    "observation": raw_observation,
                    "frozen_model": json_value(snapshot),
                    "battery_epoch_id": epoch_id,
                    "evaluation_revision": snapshot.evaluation_revision,
                },
            )
        )
        handle = store.append(
            handle,
            EventRecord(
                "gap",
                "boot-a",
                "2026-08-16T00:00:01Z",
                1_000_000_000,
                {"reason": "observation_queue_overflow"},
                "system",
            ),
        )
        store.append(
            handle,
            EventRecord(
                "end",
                "boot-a",
                "2026-08-16T00:00:02Z",
                2_000_000_000,
                {"termination": "capture_damaged"},
                "physical",
            ),
        )
        processing = store.work_registry().pending_processing[0]
        assert processing.frozen_stage == "end_durable"
        model = NoMutationModel(snapshot)
        worker = AssessmentWorker(store, model)
        worker.after_first_safety_publication()

        result = close_blackout(
            store,
            model,
            worker.prepare(CloseRequest(processing)),
        )

        assert result.outcome.disposition == TerminalDisposition.REJECTED
        assert EvidenceReason.CAPTURE_DAMAGED in result.outcome.reasons.values
        assert EvidenceReason.RAW_GAP_TOO_LARGE in result.outcome.reasons.values
        assert model.prepare_calls == model.commit_calls == 0
    finally:
        store.close()
