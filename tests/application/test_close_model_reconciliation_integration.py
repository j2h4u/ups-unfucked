"""Application proof for model publication reconciliation during close."""

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.adapters import model_state_persistence as model_files
from src.adapters.jsonl_event_store import JsonlEventStore
from src.adapters.model_owner import ModelOwner, ModelPersistenceError
from src.application.assessment_worker import CloseRequest, PreparedClose
from src.application.close_blackout import close_blackout
from src.application.storage_values import EventHandle, EventRecord, EventRef, EventStart
from src.domain.reasons import order_reasons
from src.domain.values import (
    ComparisonMode,
    EvidenceAssessment,
    EvidenceClass,
    ForwardComparison,
    IrCohortEstimate,
    LearningDecision,
    ModelChange,
    NumericSummary,
    TerminalDisposition,
)

NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)


def _oracle(before, after):
    return after.ir_k_v_per_pp <= before.ir_k_v_per_pp, "integration-test-oracle"


def _close_plan(tmp_path: Path):
    model_path = tmp_path / "model.json"
    owner = ModelOwner(model_path, safety_oracle=_oracle, create_if_missing=True)
    store = JsonlEventStore(tmp_path)
    blackout_id = uuid.uuid4().hex
    segment_id = uuid.uuid4().hex
    handle = store.open(
        EventStart(
            blackout_id,
            segment_id,
            "boot-a",
            "2026-08-16T00:00:00Z",
            0,
            {"battery_epoch_id": owner.current_snapshot().battery_epoch_id},
        )
    )
    store.append(
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
    processing = store.work_registry().pending_processing[0]
    projection = store.project(EventRef(blackout_id, processing.final_path_token))
    assert projection.end is not None
    change = ModelChange(
        "ir_k_v_per_pp",
        0.015,
        0.010,
        0.012,
        (hashlib.sha256(b"integration-reconciliation").hexdigest(),),
        True,
    )
    prepared_commit = owner.prepare_commit(
        change,
        blackout_id=blackout_id,
        committed_at=NOW,
    )
    assessment = EvidenceAssessment(
        EvidenceClass.QUALIFYING,
        300.0,
        301,
        1.0,
        1.0,
        NumericSummary(12.0, 13.0, 12.5, 0.1),
        NumericSummary(20.0, 40.0, 30.0, 10.0),
        order_reasons(()),
    )
    comparison = ForwardComparison(
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
    cohort = IrCohortEstimate(
        owner.current_snapshot().battery_epoch_id,
        (blackout_id,),
        2,
        1,
        1,
        0.010,
        0.0,
        order_reasons(()),
    )
    return (
        model_path,
        store,
        owner,
        PreparedClose(
            CloseRequest(processing),
            EventHandle(
                blackout_id,
                segment_id,
                processing.final_path_token,
                projection.records[-1].seq + 1,
                projection.records[-1].record_sha256,
            ),
            projection,
            assessment,
            comparison,
            cohort,
            LearningDecision(True, True, False, True),
            order_reasons(()),
            (),
            prepared_commit,
            None,
        ),
    )


def _fail_post_write_read_or_hash(
    model_path: Path,
    before: bytes,
    mode: str,
):
    real_read = model_files.read_model_file
    failed = False

    def read_with_fault(target: Path, *, error_type=Exception) -> bytes:
        nonlocal failed
        raw = real_read(target, error_type=error_type)
        if target == model_path and raw != before and not failed:
            failed = True
            if mode == "read":
                raise error_type("injected post-model-write read failure")
            return raw + b" "
        return raw

    return read_with_fault


@pytest.mark.parametrize("failure_mode", ("read", "hash"))
def test_close_propagates_reconciliation_failure_and_restart_commits_once(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    model_path, store, owner, prepared = _close_plan(tmp_path)
    before = model_path.read_bytes()
    before_snapshot = owner.current_snapshot()
    try:
        with (
            patch.object(
                model_files,
                "read_model_file",
                side_effect=_fail_post_write_read_or_hash(model_path, before, failure_mode),
            ),
            pytest.raises(ModelPersistenceError),
        ):
            close_blackout(store, owner, prepared)

        projection = store.project(
            EventRef(
                prepared.request.processing.blackout_id,
                prepared.request.processing.final_path_token,
            )
        )
        assert projection.outcome is None
        assert not any(
            record.record_type == "model_commit" for record in projection.derived_records
        )
        assert store.work_registry().pending_processing
        assert model_path.read_bytes() == before
        assert owner.current_snapshot() == before_snapshot
        assert owner.policy_projection().persisted_hash == model_files.persisted_hash(before)

        result = close_blackout(store, owner, prepared)
        assert result.outcome.disposition == TerminalDisposition.LEARNED
        assert result.outcome.commit_receipt is not None
        assert owner.current_snapshot().ir_k_v_per_pp == pytest.approx(0.012)
        final_projection = store.project(
            EventRef(
                prepared.request.processing.blackout_id,
                prepared.request.processing.final_path_token,
            )
        )
        assert (
            sum(record.record_type == "model_commit" for record in final_projection.derived_records)
            == 1
        )
        assert final_projection.outcome is not None
        assert store.work_registry().pending_processing == ()
    finally:
        store.close()


@pytest.mark.parametrize("failure_mode", ("read", "hash"))
def test_close_reconciles_pending_event_after_rollback_failure(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    model_path, store, owner, prepared = _close_plan(tmp_path)
    before = model_path.read_bytes()
    real_write = model_files.atomic_write_model
    model_writes = 0

    def fail_rollback_write(target: Path, content: str, *, mode: int = 0o600) -> str:
        nonlocal model_writes
        if target == model_path:
            model_writes += 1
            if model_writes == 2:
                raise OSError("injected rollback write failure")
        return real_write(target, content, mode=mode)

    try:
        with (
            patch.object(
                model_files,
                "read_model_file",
                side_effect=_fail_post_write_read_or_hash(model_path, before, failure_mode),
            ),
            patch.object(model_files, "atomic_write_model", side_effect=fail_rollback_write),
            pytest.raises(ModelPersistenceError, match="rollback failed"),
        ):
            close_blackout(store, owner, prepared)

        projection = store.project(
            EventRef(
                prepared.request.processing.blackout_id,
                prepared.request.processing.final_path_token,
            )
        )
        assert projection.outcome is None
        assert not any(
            record.record_type == "model_commit" for record in projection.derived_records
        )
        assert store.work_registry().pending_processing
        assert model_path.read_bytes() != before
        assert owner.current_snapshot().ir_k_v_per_pp == pytest.approx(0.015)
        assert owner.precommit_path.read_bytes() == before

        restarted = ModelOwner(model_path, safety_oracle=_oracle)
        result = close_blackout(store, restarted, prepared)
        assert result.outcome.disposition == TerminalDisposition.LEARNED
        assert result.outcome.commit_receipt is not None
        final_projection = store.project(
            EventRef(
                prepared.request.processing.blackout_id,
                prepared.request.processing.final_path_token,
            )
        )
        assert (
            sum(record.record_type == "model_commit" for record in final_projection.derived_records)
            == 1
        )
        assert final_projection.outcome is not None
        assert store.work_registry().pending_processing == ()
    finally:
        store.close()
