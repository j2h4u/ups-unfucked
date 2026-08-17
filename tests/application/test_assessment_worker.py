"""Focused tests for read-only, deferred blackout assessment."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.application.assessment_codec import json_value
from src.application.assessment_replay import observed_load_sag_increase_from_json
from src.application.assessment_worker import (
    AssessmentWorker,
    CloseRequest,
    ProjectionInputError,
)
from src.application.close_blackout import close_blackout
from src.application.model_port import (
    ModelPolicyProjection,
    ModelPortConflict,
    ModelPortRefused,
    PreparedModelCommit,
)
from src.application.startup_recovery import (
    defer_processing_after_first_publication,
    recover_startup_metadata,
)
from src.application.storage_values import (
    EpochIndexTail,
    EventProjection,
    EventRecord,
    EventSummary,
    ProcessingRef,
    ProjectedEventRecord,
    WorkRegistry,
)
from src.battery_math.lut import LutPoint
from src.domain.reasons import EvidenceReason, IdentificationReason, LearningReason
from src.domain.values import (
    EvidenceClass,
    FrozenModelSnapshot,
    ModelCommitReceipt,
    PhysicalObservation,
    TerminalDisposition,
)

NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)
BLACKOUT_ID = "a" * 32
SEGMENT_ID = "b" * 32
PATH = f"evt-20260816T000000.000Z-{BLACKOUT_ID}.jsonl"


class FakeModel:
    def __init__(self, snapshot: FrozenModelSnapshot) -> None:
        self.snapshot = snapshot
        self.prepare_calls = 0

    def current_snapshot(self):
        return self.snapshot

    def policy_projection(self):
        return ModelPolicyProjection(
            self.snapshot,
            self.snapshot.scientific_fingerprint,
            0.015,
            None,
            frozenset(),
        )

    def prepare_commit(self, change, *, blackout_id, committed_at):
        self.prepare_calls += 1
        raise AssertionError("short/refused evidence must not prepare a model commit")


class FakeStore:
    def __init__(self, projection: EventProjection) -> None:
        self.projection = projection
        self.project_calls = 0
        self.append_calls = 0
        self.processing = _processing()
        self.outcomes = []

    def project(self, event_ref):
        self.project_calls += 1
        assert event_ref.blackout_id == BLACKOUT_ID
        return self.projection

    def index_tail(self, limit):
        assert limit == 32
        return ()

    def index_tail_for_epoch(self, battery_epoch_id, limit):
        assert battery_epoch_id == "epoch-a"
        assert limit == 31
        return EpochIndexTail((), 0, True)

    def recover_startup(self):
        return None

    def work_registry(self):
        return WorkRegistry(None, (self.processing,))

    def append(self, handle, record):
        self.append_calls += 1
        return replace(
            handle,
            next_seq=handle.next_seq + 1,
            last_record_sha256=f"{handle.next_seq:064x}",
        )

    def checkpoint_processing(self, handle, frozen_stage):
        return None

    def seal(self, handle, outcome):
        self.outcomes.append(outcome)
        return object()


def _snapshot(*, k: float = 0.015, fingerprint: str = "f" * 64) -> FrozenModelSnapshot:
    return FrozenModelSnapshot(
        schema_revision="domain-jsonl-v2",
        evaluation_revision="domain-jsonl-v2",
        battery_epoch_id="epoch-a",
        scientific_fingerprint=fingerprint,
        rated_capacity_ah=7.0,
        nominal_voltage_v=12.0,
        nominal_power_watts=900.0,
        soh=1.0,
        peukert_exponent=1.2,
        ir_k_v_per_pp=k,
        ir_reference_load_percent=0.0,
        lut=(LutPoint(10.0, 0.0, "standard"), LutPoint(14.0, 1.0, "standard")),
    )


def _observation(
    second: int,
    *,
    status: str = "OB DISCHRG",
    input_voltage_v: float | None = None,
) -> PhysicalObservation:
    return PhysicalObservation(
        boot_id="boot-a",
        monotonic_ns=second * 1_000_000_000,
        wall_time_utc=NOW + timedelta(seconds=second),
        raw_status=status,
        battery_voltage_raw="12.50",
        battery_voltage_v=12.5 - second * 0.001,
        voltage_token_quantum_v=0.01,
        load_percent=20.0,
        input_voltage_v=input_voltage_v,
    )


def _record(seq: int, record_type: str, payload, *, second: int) -> ProjectedEventRecord:
    return ProjectedEventRecord(
        schema_version=2,
        record_type=record_type,
        provenance="physical",
        blackout_id=BLACKOUT_ID,
        segment_id=SEGMENT_ID,
        seq=seq,
        boot_id="boot-a",
        wall_time_utc=(NOW + timedelta(seconds=second)).isoformat().replace("+00:00", "Z"),
        monotonic_ns=second * 1_000_000_000,
        prev_record_sha256=None if seq == 0 else chr(96 + seq) * 64,
        payload=payload,
        record_sha256=chr(97 + seq) * 64,
    )


def _projection(
    *,
    status: str = "OB DISCHRG",
    input_voltage_v: float | None = None,
    second: int = 1,
    damaged: bool = False,
    termination: str = "power_restored",
) -> EventProjection:
    snapshot = _snapshot()
    start_observation = _observation(0, status=status, input_voltage_v=input_voltage_v)
    later = _observation(second, status=status, input_voltage_v=input_voltage_v)
    start = _record(
        0,
        "start",
        {
            "observation": json_value(start_observation),
            "frozen_model": json_value(snapshot),
            "battery_epoch_id": snapshot.battery_epoch_id,
            "evaluation_revision": snapshot.evaluation_revision,
        },
        second=0,
    )
    observation = _record(1, "observation", json_value(later), second=second)
    end = _record(2, "end", {"termination": termination}, second=second + 1)
    return EventProjection(
        start=start,
        observations=(observation,),
        gaps=(),
        end=end,
        derived_records=(),
        outcome=None,
        trusted_prefixes=((start, observation, end),),
        damaged_segment_hashes=(("d" * 64,) if damaged else ()),
        damaged_segment_overflow=0,
        records=(start, observation, end),
    )


def _processing() -> ProcessingRef:
    return ProcessingRef(BLACKOUT_ID, (SEGMENT_ID,), PATH, "end_durable", "c" * 64)


def _prepare(projection: EventProjection, model: FakeModel | None = None):
    store = FakeStore(projection)
    selected_model = model or FakeModel(_snapshot())
    worker = AssessmentWorker(store, selected_model)
    worker.after_first_safety_publication()
    return worker.prepare(CloseRequest(_processing())), store, selected_model


def _with_derived_records(
    projection: EventProjection,
    records: tuple[EventRecord, ...],
) -> EventProjection:
    projected: list[ProjectedEventRecord] = []
    anchor = projection.records[-1]
    previous_hash = anchor.record_sha256
    for offset, record in enumerate(records, start=1):
        seq = anchor.seq + offset
        record_hash = f"{seq:064x}"
        projected.append(
            ProjectedEventRecord(
                schema_version=2,
                record_type=record.record_type,
                provenance=record.provenance,
                blackout_id=anchor.blackout_id,
                segment_id=anchor.segment_id,
                seq=seq,
                boot_id=record.boot_id,
                wall_time_utc=record.wall_time_utc,
                monotonic_ns=record.monotonic_ns,
                prev_record_sha256=previous_hash,
                payload=record.payload,
                record_sha256=record_hash,
            )
        )
        previous_hash = record_hash
    return replace(
        projection,
        derived_records=tuple(projected),
        records=(*projection.records, *projected),
    )


def test_short_blackout_is_recorded_only_without_model_or_store_writes():
    prepared, store, model = _prepare(_projection())

    assert prepared.assessment.evidence_class == EvidenceClass.QUALIFYING
    assert prepared.comparison.mode.value == "none"
    assert prepared.prepared_commit is None
    assert model.prepare_calls == 0
    assert store.append_calls == 0


def test_cal_with_missing_input_is_real_but_still_operational_only():
    missing, _, _ = _prepare(_projection(status="OB DISCHRG CAL", input_voltage_v=None))
    high, _, _ = _prepare(_projection(status="OB DISCHRG CAL", input_voltage_v=230.0))

    assert EvidenceReason.CALIBRATION_OBSERVED in missing.assessment.reasons.values
    assert EvidenceReason.NOT_NATURAL_PHYSICAL_BLACKOUT not in missing.assessment.reasons.values
    assert EvidenceReason.NOT_NATURAL_PHYSICAL_BLACKOUT in high.assessment.reasons.values
    assert missing.assessment.evidence_class == EvidenceClass.OPERATIONAL_ONLY
    assert missing.prepared_commit is None


def test_any_positive_self_test_makes_mixed_event_non_natural():
    projection = _projection(status="OB DISCHRG", input_voltage_v=None)
    synthetic_test = _observation(
        1,
        status="OB DISCHRG CAL",
        input_voltage_v=230.0,
    )
    test_record = replace(
        projection.observations[0],
        payload=json_value(synthetic_test),
    )
    mixed = replace(
        projection,
        observations=(test_record,),
        trusted_prefixes=((projection.start, test_record, projection.end),),
        records=(projection.start, test_record, projection.end),
    )

    prepared, _, model = _prepare(mixed)

    assert EvidenceReason.NOT_NATURAL_PHYSICAL_BLACKOUT in prepared.assessment.reasons.values
    assert prepared.assessment.evidence_class == EvidenceClass.OPERATIONAL_ONLY
    assert prepared.learning_decision.commit_ir_k is False
    assert prepared.prepared_commit is None
    assert model.prepare_calls == 0


def test_raw_lb_is_diagnostic_only_for_event_evidence():
    prepared, _, _ = _prepare(_projection(status="OB DISCHRG LB"))

    assert prepared.assessment.evidence_class == EvidenceClass.QUALIFYING
    assert EvidenceReason.NOT_NATURAL_PHYSICAL_BLACKOUT not in prepared.assessment.reasons.values


def test_ready_raw_lb_event_is_marked_for_later_decline_recomputation():
    projection = _projection(status="OB DISCHRG LB")
    assert projection.start is not None
    start = replace(
        projection.start,
        payload={**projection.start.payload, "charge_readiness": {"ready": True}},
    )
    projection = replace(
        projection,
        start=start,
        records=(start, *projection.records[1:]),
    )

    prepared, _, _ = _prepare(projection)

    assert prepared.learning_decision.record_decline_evidence is True


def test_gap_and_corruption_refuse_scientific_use_with_exact_classes():
    gap, _, _ = _prepare(_projection(second=10))
    corrupt, _, _ = _prepare(_projection(damaged=True))

    assert EvidenceReason.RAW_GAP_TOO_LARGE in gap.assessment.reasons.values
    assert gap.assessment.evidence_class == EvidenceClass.OPERATIONAL_ONLY
    assert corrupt.assessment.evidence_class == EvidenceClass.REJECTED
    assert EvidenceReason.CAPTURE_DAMAGED in corrupt.assessment.reasons.values
    assert EvidenceReason.EVENT_NOT_NATURALLY_COMPLETED not in corrupt.assessment.reasons.values
    assert gap.prepared_commit is corrupt.prepared_commit is None


def test_power_restored_with_stored_gap_remains_rejected_capture_damage():
    projection = _projection()
    assert projection.end is not None
    gap = replace(
        projection.end,
        record_type="gap",
        payload={"reason": "boot_changed"},
    )
    gapped = replace(projection, gaps=(gap,))

    prepared, _, _ = _prepare(gapped)

    assert prepared.assessment.evidence_class == EvidenceClass.REJECTED
    assert EvidenceReason.CAPTURE_DAMAGED in prepared.assessment.reasons.values
    assert EvidenceReason.EVENT_NOT_NATURALLY_COMPLETED not in prepared.assessment.reasons.values


def test_malformed_raw_projection_is_rejected_without_model_prepare():
    projection = _projection()
    assert projection.start is not None
    payload = dict(projection.start.payload)
    malformed = dict(payload["observation"])
    malformed.pop("battery_voltage_raw")
    payload["observation"] = malformed
    start = replace(projection.start, payload=payload)
    damaged_projection = replace(
        projection,
        start=start,
        records=(start, *projection.records[1:]),
    )

    prepared, _, model = _prepare(damaged_projection)

    assert prepared.assessment.evidence_class == EvidenceClass.REJECTED
    assert EvidenceReason.CAPTURE_DAMAGED in prepared.assessment.reasons.values
    assert prepared.prepared_commit is None
    assert model.prepare_calls == 0


def test_missing_start_and_end_uses_stable_fail_closed_record_time():
    normal = _projection()
    assert normal.end is not None
    anchor = replace(
        normal.end,
        record_type="gap",
        provenance="system",
        payload={"reason": "observation_queue_overflow"},
    )
    malformed = replace(
        normal,
        start=None,
        observations=(),
        gaps=(anchor,),
        end=None,
        trusted_prefixes=((anchor,),),
        records=(anchor,),
    )
    first_store = FakeStore(malformed)
    second_store = FakeStore(malformed)
    model = FakeModel(_snapshot())
    first_worker = AssessmentWorker(first_store, model)
    second_worker = AssessmentWorker(second_store, model)
    first_worker.after_first_safety_publication()
    second_worker.after_first_safety_publication()

    first = close_blackout(
        first_store,
        model,
        first_worker.prepare(CloseRequest(_processing())),
    )
    second = close_blackout(
        second_store,
        model,
        second_worker.prepare(CloseRequest(_processing())),
    )

    assert first.outcome == second.outcome
    assert first.outcome.disposition == TerminalDisposition.REJECTED
    assert model.prepare_calls == 0
    assert len(first_store.outcomes) == len(second_store.outcomes) == 1
    assert json.dumps(first_store.outcomes[0].payload, sort_keys=True) == json.dumps(
        second_store.outcomes[0].payload,
        sort_keys=True,
    )


def test_frozen_model_makes_assessment_deterministic_if_live_model_changes():
    projection = _projection()
    first, _, _ = _prepare(projection, FakeModel(_snapshot(k=0.015, fingerprint="1" * 64)))
    second, _, _ = _prepare(projection, FakeModel(_snapshot(k=0.009, fingerprint="2" * 64)))

    assert first.assessment == second.assessment
    assert first.comparison == second.comparison
    assert first.cohort_estimate == second.cohort_estimate
    assert tuple(record.payload for record in first.derived_records) == tuple(
        record.payload for record in second.derived_records
    )


def test_each_durable_derived_prefix_resumes_without_duplicate_records():
    projection = _projection()
    baseline, _, _ = _prepare(projection)
    assert len(baseline.derived_records) == 4

    for prefix_count in range(1, len(baseline.derived_records) + 1):
        partial = _with_derived_records(
            projection,
            baseline.derived_records[:prefix_count],
        )

        resumed, _, _ = _prepare(partial)

        assert resumed.assessment == baseline.assessment
        assert resumed.comparison == baseline.comparison
        assert resumed.cohort_estimate == baseline.cohort_estimate
        assert resumed.learning_decision == baseline.learning_decision
        assert resumed.derived_records == baseline.derived_records[prefix_count:]


def test_startup_recovery_opens_no_pending_event_before_first_publication():
    store = FakeStore(_projection())
    worker = AssessmentWorker(store, FakeModel(_snapshot()))

    recovery = recover_startup_metadata(store)
    assert store.project_calls == 0
    assert worker.peek_pending() is None

    assert defer_processing_after_first_publication(recovery, worker) == 1
    assert store.project_calls == 0
    request = worker.peek_pending()
    assert request is not None
    assert worker.prepare(request) is not None
    worker.discard_pending(request)
    assert store.project_calls == 1


def test_transient_prepare_failure_keeps_request_for_retry():
    class FailOnceStore(FakeStore):
        def project(self, event_ref):
            if self.project_calls == 0:
                self.project_calls += 1
                raise OSError("transient projection read failure")
            return super().project(event_ref)

    store = FailOnceStore(_projection())
    worker = AssessmentWorker(store, FakeModel(_snapshot()))
    worker.after_first_safety_publication()
    assert worker.defer(CloseRequest(_processing()))

    request = worker.peek_pending()
    assert request is not None
    with pytest.raises(OSError, match="transient projection read failure"):
        worker.prepare(request)

    assert worker.peek_pending() is request
    assert worker.prepare(request) is not None
    assert store.project_calls == 2


class VerticalModel(FakeModel):
    def __init__(self, snapshot: FrozenModelSnapshot) -> None:
        super().__init__(snapshot)
        self.operations: list[str] = []

    def prepare_commit(self, change, *, blackout_id, committed_at):
        self.prepare_calls += 1
        return PreparedModelCommit(
            blackout_id,
            change,
            committed_at,
            "1" * 64,
            "2" * 64,
            "3" * 64,
        )

    def commit_prepared(self, prepared):
        self.operations.append("model_commit")
        change = prepared.change
        return ModelCommitReceipt(
            prepared.blackout_id,
            change.parameter,
            change.value_before,
            change.measured_estimate,
            change.value_after,
            prepared.model_hash_before,
            prepared.expected_model_hash_after,
            self.snapshot.scientific_fingerprint,
            prepared.expected_scientific_fingerprint_after,
            "4" * 64,
            change.evidence_hashes,
            False,
            "dense_no_later_lb",
        )


class VerticalStore:
    def __init__(self, projections, summaries) -> None:
        self.projections = projections
        self.summaries = summaries
        self.projected_ids: list[str] = []
        self.operations: list[str] = []
        self.outcome = None

    def project(self, event_ref):
        self.projected_ids.append(event_ref.blackout_id)
        return self.projections[event_ref.blackout_id]

    def index_tail(self, limit):
        assert limit == 32
        return self.summaries

    def index_tail_for_epoch(self, battery_epoch_id, limit):
        assert battery_epoch_id == "epoch-a"
        assert limit == 31
        matching = tuple(
            summary for summary in self.summaries if summary.battery_epoch_id == battery_epoch_id
        )
        return EpochIndexTail(matching[-limit:], max(0, len(matching) - limit), True)

    def append(self, handle, record):
        self.operations.append(f"append:{record.record_type}")
        return replace(
            handle,
            next_seq=handle.next_seq + 1,
            last_record_sha256=f"{handle.next_seq:064x}",
        )

    def checkpoint_processing(self, handle, frozen_stage):
        self.operations.append(f"checkpoint:{frozen_stage}")

    def seal(self, handle, outcome):
        self.operations.append("seal")
        self.outcome = outcome
        return object()


def _step_projection(
    blackout_id: str,
    *,
    upward: bool,
    day: int,
    termination: str = "power_restored",
) -> EventProjection:
    snapshot = _snapshot()
    observations = []
    transition = 30
    event_start = NOW - timedelta(days=day)
    for second in range(240):
        pre_load, post_load = (20.0, 40.0) if upward else (40.0, 20.0)
        load = pre_load if second < transition else post_load
        voltage = 13.5 - 0.010 * load
        observations.append(
            PhysicalObservation(
                boot_id=f"boot-{blackout_id[0]}",
                monotonic_ns=second * 1_000_000_000,
                wall_time_utc=event_start + timedelta(seconds=second),
                raw_status="OB DISCHRG",
                battery_voltage_raw=f"{voltage:.3f}",
                battery_voltage_v=voltage,
                voltage_token_quantum_v=0.001,
                load_percent=load,
                input_voltage_v=None,
            )
        )

    def record(seq, record_type, payload, second):
        return ProjectedEventRecord(
            2,
            record_type,
            "physical",
            blackout_id,
            blackout_id,
            seq,
            observations[min(second, 239)].boot_id,
            (event_start + timedelta(seconds=second)).isoformat().replace("+00:00", "Z"),
            second * 1_000_000_000,
            None if seq == 0 else f"{seq - 1:064x}",
            payload,
            f"{seq:064x}",
        )

    start = record(
        0,
        "start",
        {
            "observation": json_value(observations[0]),
            "frozen_model": json_value(snapshot),
            "battery_epoch_id": snapshot.battery_epoch_id,
            "evaluation_revision": snapshot.evaluation_revision,
        },
        0,
    )
    raw_records = tuple(
        record(index, "observation", json_value(item), index)
        for index, item in enumerate(observations[1:], start=1)
    )
    end = record(240, "end", {"termination": termination}, 240)
    records = (start, *raw_records, end)
    return EventProjection(start, raw_records, (), end, (), None, (records,), (), 0, records)


def _summary(blackout_id: str, day: int) -> EventSummary:
    return EventSummary(
        schema_version=2,
        blackout_id=blackout_id,
        segment_filename=f"path-{blackout_id}",
        started_utc=(NOW - timedelta(days=day)).isoformat().replace("+00:00", "Z"),
        ended_utc=None,
        termination="power_restored",
        evidence_class="qualifying",
        disposition="recorded_only",
        duration_s=240.0,
        observation_count=240,
        battery_epoch_id="epoch-a",
        comparison_available=False,
        comparison_mode="none",
        ir_estimate_available=True,
        commit_receipt_id=None,
        damaged_segment_hashes=(),
        damaged_segment_overflow=0,
        outcome_record_sha256="5" * 64,
        event_file_sha256="6" * 64,
    )


def test_fixed_history_window_filters_epoch_before_the_31_event_slice():
    historical_ids = tuple(f"{index + 100:032x}" for index in range(32))
    summaries = tuple(
        _summary(blackout_id, index + 1) for index, blackout_id in enumerate(historical_ids)
    )

    class WindowStore(FakeStore):
        def __init__(self):
            super().__init__(_projection())
            self.projected_ids = []

        def project(self, event_ref):
            self.projected_ids.append(event_ref.blackout_id)
            return self.projection

        def index_tail(self, limit):
            raise AssertionError("global tail must not precede epoch filtering")

        def index_tail_for_epoch(self, battery_epoch_id, limit):
            assert battery_epoch_id == "epoch-a"
            assert limit == 31
            return EpochIndexTail(summaries[-31:], 1, True)

    store = WindowStore()
    worker = AssessmentWorker(store, FakeModel(_snapshot()))
    worker.after_first_safety_publication()

    prepared = worker.prepare(CloseRequest(_processing()))

    assert store.projected_ids[0] == BLACKOUT_ID
    assert store.projected_ids[1:] == list(historical_ids[-31:])
    assert historical_ids[0] not in store.projected_ids
    assert IdentificationReason.CANDIDATE_EVENT_OVERFLOW in prepared.cohort_estimate.reasons.values


def test_incomplete_epoch_scan_fails_closed_without_projecting_partial_history():
    historical_id = "b" * 32

    class IncompleteStore(FakeStore):
        def __init__(self):
            super().__init__(_projection())
            self.projected_ids = []

        def project(self, event_ref):
            self.projected_ids.append(event_ref.blackout_id)
            return self.projection

        def index_tail_for_epoch(self, battery_epoch_id, limit):
            assert battery_epoch_id == "epoch-a"
            assert limit == 31
            return EpochIndexTail((_summary(historical_id, 1),), 0, False)

    store = IncompleteStore()
    worker = AssessmentWorker(store, FakeModel(_snapshot()))
    worker.after_first_safety_publication()

    prepared = worker.prepare(CloseRequest(_processing()))

    assert store.projected_ids == [BLACKOUT_ID]
    assert (
        IdentificationReason.COHORT_PROJECTION_UNAVAILABLE
        in prepared.cohort_estimate.reasons.values
    )


def test_epoch_index_tail_retains_exact_bounded_overflow_count():
    tail = EpochIndexTail((), 7, True)

    assert tail.overflow_count == 7


def test_epoch_index_tail_rejects_boolean_overflow_count():
    legacy_count = bool(1)
    with pytest.raises(ValueError, match="overflow_count must be a nonnegative integer"):
        EpochIndexTail((), legacy_count, True)


def test_unknown_durable_learning_policy_fails_closed():
    baseline, _, _ = _prepare(_projection())
    assessment = replace(baseline.derived_records[0])
    payload = dict(assessment.payload)
    policy = dict(payload["learning_policy"])
    policy["revision"] = "future-policy"
    payload["learning_policy"] = policy
    corrupted = replace(assessment, payload=payload)
    projection = replace(
        baseline.projection,
        derived_records=(corrupted, *baseline.derived_records[1:]),
    )
    store = FakeStore(projection)
    worker = AssessmentWorker(store, FakeModel(_snapshot()))
    worker.after_first_safety_publication()

    with pytest.raises(ProjectionInputError, match="unknown learning policy revision"):
        worker.prepare(CloseRequest(_processing()))


def test_durable_non_restored_terminal_adds_censor_reason():
    baseline, _, _ = _prepare(_projection())
    assert baseline.projection.end is not None
    end = replace(baseline.projection.end, payload={"termination": "service_stop"})
    records = tuple(
        end if record.record_type == "end" else record for record in baseline.projection.records
    )
    projection = _with_derived_records(
        replace(baseline.projection, end=end, records=records),
        baseline.derived_records,
    )
    prepared, _, _ = _prepare(projection)

    assert EvidenceReason.EVENT_NOT_NATURALLY_COMPLETED in prepared.assessment.reasons.values
    assert EvidenceReason.EVENT_NOT_NATURALLY_COMPLETED in prepared.outcome_reasons.values
    assert prepared.learning_decision.commit_ir_k is False
    assert prepared.prepared_commit is None


def test_real_current_plus_three_history_pipeline_commits_one_safe_decrease():
    history_ids = ("b" * 32, "c" * 32, "d" * 32)
    projections = {
        BLACKOUT_ID: _step_projection(BLACKOUT_ID, upward=True, day=0),
        history_ids[0]: _step_projection(history_ids[0], upward=False, day=3),
        history_ids[1]: _step_projection(history_ids[1], upward=True, day=2),
        history_ids[2]: _step_projection(history_ids[2], upward=False, day=1),
    }
    store = VerticalStore(
        projections,
        tuple(_summary(blackout_id, day) for day, blackout_id in enumerate(history_ids, 1)),
    )
    model = VerticalModel(_snapshot())
    worker = AssessmentWorker(store, model)
    worker.after_first_safety_publication()
    request = CloseRequest(
        ProcessingRef(
            BLACKOUT_ID,
            (BLACKOUT_ID,),
            PATH,
            "end_durable",
            projections[BLACKOUT_ID].records[-1].record_sha256,
        )
    )

    result = close_blackout(store, model, worker.prepare(request))

    assert result.outcome.disposition == TerminalDisposition.LEARNED
    assert result.outcome.cohort_estimate is not None
    assert result.outcome.cohort_estimate.step_count == 4
    assert result.outcome.commit_receipt is not None
    assert result.outcome.commit_receipt.value_after == pytest.approx(0.012)
    assert result.outcome.learning_decision.record_decline_evidence is True
    assert model.prepare_calls == 1
    assert model.operations == ["model_commit"]
    assert store.operations.index("append:learning_decision") < store.operations.index(
        "append:model_commit"
    )
    assert store.operations[-1] == "seal"


@pytest.mark.parametrize("termination", ("service_stop", "closed_restart_gap"))
def test_non_restored_terminal_is_operational_and_cannot_prepare_or_commit_learning(
    termination: str,
):
    history_ids = ("b" * 32, "c" * 32, "d" * 32)
    projections = {
        BLACKOUT_ID: _step_projection(
            BLACKOUT_ID,
            upward=True,
            day=0,
            termination=termination,
        ),
        history_ids[0]: _step_projection(history_ids[0], upward=False, day=3),
        history_ids[1]: _step_projection(history_ids[1], upward=True, day=2),
        history_ids[2]: _step_projection(history_ids[2], upward=False, day=1),
    }
    store = VerticalStore(
        projections,
        tuple(_summary(blackout_id, day) for day, blackout_id in enumerate(history_ids, 1)),
    )
    model = VerticalModel(_snapshot())
    worker = AssessmentWorker(store, model)
    worker.after_first_safety_publication()
    request = CloseRequest(
        ProcessingRef(
            BLACKOUT_ID,
            (BLACKOUT_ID,),
            PATH,
            "end_durable",
            projections[BLACKOUT_ID].records[-1].record_sha256,
        )
    )

    prepared = worker.prepare(request)
    result = close_blackout(store, model, prepared)

    assert prepared.assessment.evidence_class == EvidenceClass.OPERATIONAL_ONLY
    assert EvidenceReason.EVENT_NOT_NATURALLY_COMPLETED in prepared.assessment.reasons.values
    assert prepared.learning_decision.commit_ir_k is False
    assert prepared.learning_decision.record_decline_evidence is False
    assert prepared.prepared_commit is None
    assert result.outcome.disposition == TerminalDisposition.RECORDED_ONLY
    assert EvidenceReason.EVENT_NOT_NATURALLY_COMPLETED in result.outcome.reasons.values
    assert result.outcome.commit_receipt is None
    assert model.prepare_calls == 0
    assert model.operations == []


def test_upward_cohort_durably_observes_exact_assessment_time_ir_without_commit():
    history_ids = ("b" * 32, "c" * 32, "d" * 32)
    projections = {
        BLACKOUT_ID: _step_projection(BLACKOUT_ID, upward=True, day=0),
        history_ids[0]: _step_projection(history_ids[0], upward=False, day=3),
        history_ids[1]: _step_projection(history_ids[1], upward=True, day=2),
        history_ids[2]: _step_projection(history_ids[2], upward=False, day=1),
    }
    store = VerticalStore(
        projections,
        tuple(_summary(blackout_id, day) for day, blackout_id in enumerate(history_ids, 1)),
    )
    model = VerticalModel(_snapshot(k=0.009))
    worker = AssessmentWorker(store, model)
    worker.after_first_safety_publication()
    prepared = worker.prepare(
        CloseRequest(
            ProcessingRef(
                BLACKOUT_ID,
                (BLACKOUT_ID,),
                PATH,
                "end_durable",
                projections[BLACKOUT_ID].records[-1].record_sha256,
            )
        )
    )

    observation = prepared.observed_load_sag_increase
    assert observation is not None
    assert observation.value_before == 0.009
    assert observation.measured_estimate == pytest.approx(0.010)
    assert observation.evidence_hashes == tuple(sorted(observation.evidence_hashes))
    assert prepared.prepared_commit is None
    assert prepared.learning_decision.commit_ir_k is False
    assert LearningReason.UNSAFE_UPWARD_IR_CHANGE_NOT_APPLIED in prepared.outcome_reasons.values
    assert model.prepare_calls == 0
    decision_record = next(
        record for record in prepared.derived_records if record.record_type == "learning_decision"
    )
    assert (
        observed_load_sag_increase_from_json(decision_record.payload["observed_load_sag_increase"])
        == observation
    )
    store.projections[BLACKOUT_ID] = _with_derived_records(
        projections[BLACKOUT_ID],
        prepared.derived_records,
    )
    restarted_worker = AssessmentWorker(store, model)
    restarted_worker.after_first_safety_publication()

    resumed = restarted_worker.prepare(prepared.request)

    assert resumed.observed_load_sag_increase == observation
    assert resumed.derived_records == ()
    assert resumed.prepared_commit is None
    assert model.prepare_calls == 0


@pytest.mark.parametrize(
    ("evidence_class", "damaged_segment_hashes", "damaged_segment_overflow"),
    (
        pytest.param("operational_only", (), 0, id="partial-history"),
        pytest.param("qualifying", ("d" * 64,), 1, id="capture-damaged-history"),
    ),
)
def test_ineligible_historical_provenance_cannot_teach_or_commit(
    evidence_class: str,
    damaged_segment_hashes: tuple[str, ...],
    damaged_segment_overflow: int,
):
    history_ids = ("b" * 32, "c" * 32, "d" * 32)
    projections = {
        BLACKOUT_ID: _step_projection(BLACKOUT_ID, upward=True, day=0),
        history_ids[0]: _step_projection(history_ids[0], upward=False, day=3),
        history_ids[1]: _step_projection(history_ids[1], upward=True, day=2),
        history_ids[2]: _step_projection(history_ids[2], upward=False, day=1),
    }
    summaries = tuple(
        replace(
            _summary(blackout_id, day),
            evidence_class=evidence_class,
            damaged_segment_hashes=damaged_segment_hashes,
            damaged_segment_overflow=damaged_segment_overflow,
        )
        for day, blackout_id in enumerate(history_ids, 1)
    )
    store = VerticalStore(projections, summaries)
    model = VerticalModel(_snapshot())
    worker = AssessmentWorker(store, model)
    worker.after_first_safety_publication()
    request = CloseRequest(
        ProcessingRef(
            BLACKOUT_ID,
            (BLACKOUT_ID,),
            PATH,
            "end_durable",
            projections[BLACKOUT_ID].records[-1].record_sha256,
        )
    )

    result = close_blackout(store, model, worker.prepare(request))

    assert store.projected_ids == [BLACKOUT_ID]
    assert result.outcome.cohort_estimate is not None
    assert result.outcome.cohort_estimate.blackout_ids == (BLACKOUT_ID,)
    assert result.outcome.disposition == TerminalDisposition.RECORDED_ONLY
    assert result.outcome.commit_receipt is None
    assert model.prepare_calls == 0
    assert model.operations == []


@pytest.mark.parametrize(
    ("failure", "reason"),
    (
        (ModelPortConflict("model_state_conflict"), LearningReason.MODEL_STATE_CONFLICT),
        (ModelPortRefused("commit_rate_limited"), LearningReason.COMMIT_RATE_LIMITED),
    ),
)
def test_prepare_race_or_refusal_becomes_deterministic_recorded_only(failure, reason):
    history_ids = ("b" * 32, "c" * 32, "d" * 32)
    projections = {
        BLACKOUT_ID: _step_projection(BLACKOUT_ID, upward=True, day=0),
        history_ids[0]: _step_projection(history_ids[0], upward=False, day=3),
        history_ids[1]: _step_projection(history_ids[1], upward=True, day=2),
        history_ids[2]: _step_projection(history_ids[2], upward=False, day=1),
    }
    store = VerticalStore(
        projections,
        tuple(_summary(blackout_id, day) for day, blackout_id in enumerate(history_ids, 1)),
    )

    class RefusingModel(VerticalModel):
        def prepare_commit(self, change, *, blackout_id, committed_at):
            self.prepare_calls += 1
            raise failure

    model = RefusingModel(_snapshot())
    worker = AssessmentWorker(store, model)
    worker.after_first_safety_publication()
    request = CloseRequest(
        ProcessingRef(
            BLACKOUT_ID,
            (BLACKOUT_ID,),
            PATH,
            "end_durable",
            projections[BLACKOUT_ID].records[-1].record_sha256,
        )
    )

    prepared = worker.prepare(request)

    assert prepared.prepared_commit is None
    assert prepared.learning_decision.commit_ir_k is False
    assert reason in prepared.outcome_reasons.values
    assert model.prepare_calls == 1
    assert model.operations == []
