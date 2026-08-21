"""Application proof for model publication reconciliation during close."""

import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.adapters import model_state_persistence as model_files
from src.adapters.minimal_jsonl import MinimalJsonlEventStore
from src.adapters.model_owner import ModelOwner
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
    store = MinimalJsonlEventStore(tmp_path)
    blackout_id = uuid.uuid4().hex
    segment_id = uuid.uuid4().hex
    handle = store.open(
        EventStart(
            blackout_id,
            segment_id,
            "boot-a",
            "2026-08-16T00:00:00Z",
            0,
            {
                "battery_epoch_id": owner.current_snapshot().battery_epoch_id,
                "observation": {
                    "wall_time_utc": "2026-08-16T00:00:00Z",
                    "battery_voltage_v": 12.3,
                    "battery_pct": 50.0,
                    "runtime_s": 300.0,
                    "load_percent": 20.0,
                    "input_voltage_v": 0.0,
                    "output_v": 230.0,
                    "raw_status": "OB DISCHRG",
                },
            },
        )
    )
    store.append(
        handle,
        EventRecord(
            "end",
            "boot-a",
            "2026-08-16T00:05:00Z",
            300_000_000_000,
            {
                "termination": "power_restored",
                "observation": {
                    "wall_time_utc": "2026-08-16T00:05:00Z",
                    "battery_voltage_v": 12.3,
                    "battery_pct": 100.0,
                    "runtime_s": 300.0,
                    "load_percent": 20.0,
                    "input_voltage_v": 230.0,
                    "output_v": 230.0,
                    "raw_status": "OL",
                },
            },
            "physical",
        ),
    )
    processing = store.work_registry().pending_processing[0]
    blackout_id = processing.blackout_id
    projection = store.project(EventRef(blackout_id, processing.final_path_token))
    assert projection.end is not None
    change = ModelChange(
        "ir_k_v_per_pp",
        0.015,
        0.010,
        0.012,
        ("a" * 64,),
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


def _fail_post_write_read(
    model_path: Path,
    before: bytes,
):
    real_read = model_files.read_model_file
    failed = False

    def read_with_fault(target: Path, *, error_type=Exception) -> bytes:
        nonlocal failed
        raw = real_read(target, error_type=error_type)
        if target == model_path and raw != before and not failed:
            failed = True
            raise error_type("injected post-model-write read failure")
        return raw

    return read_with_fault


@pytest.mark.parametrize("failure_mode", ("read",))
def test_close_propagates_reconciliation_failure_and_restart_commits_once(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    model_path, store, owner, prepared = _close_plan(tmp_path)
    before = model_path.read_bytes()
    before_snapshot = owner.current_snapshot()
    try:
        with patch.object(
            model_files,
            "read_model_file",
            side_effect=_fail_post_write_read(model_path, before),
        ):
            result = close_blackout(store, owner, prepared)

        assert result.outcome.disposition == TerminalDisposition.RECORDED_ONLY
        assert model_path.read_bytes() == before
        assert owner.current_snapshot() == before_snapshot
    finally:
        store.close()


@pytest.mark.parametrize("failure_mode", ("read",))
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
                side_effect=_fail_post_write_read(model_path, before),
            ),
            patch.object(model_files, "atomic_write_model", side_effect=fail_rollback_write),
        ):
            result = close_blackout(store, owner, prepared)

        assert result.outcome.disposition == TerminalDisposition.RECORDED_ONLY
        assert model_path.read_bytes() == before
        assert owner.current_snapshot().ir_k_v_per_pp == pytest.approx(0.015)
        assert not owner.precommit_path.exists()
    finally:
        store.close()
