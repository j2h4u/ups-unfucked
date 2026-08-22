"""Focused tests for the explicit model update boundary."""

from pathlib import Path

import pytest

from src.adapters import model_state_schema as schema
from src.adapters.model_owner import ModelOwner
from src.adapters.model_state_persistence import ModelStateFileError


def _owner(tmp_path: Path) -> ModelOwner:
    return ModelOwner.open_runtime(tmp_path / "model.json", create_if_missing=True)


def test_apply_feedback_persists_atomic_pair_and_refreshes_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    owner = _owner(tmp_path)
    try:
        writes = 0
        original_write = schema.files.atomic_write_model

        def count_write(*args, **kwargs):
            nonlocal writes
            writes += 1
            return original_write(*args, **kwargs)

        monkeypatch.setattr(schema.files, "atomic_write_model", count_write)
        changes = owner.apply_feedback(ir_k=0.012, soh=0.92)

        assert changes == {
            "physics.ir_compensation.k_volts_per_percent": (0.015, 0.012),
            "soh": (1.0, 0.92),
        }
        assert writes == 1
        snapshot = owner.current_snapshot()
        assert snapshot.ir_k_v_per_pp == 0.012
        assert snapshot.soh == 0.92
        persisted, _ = schema.load_target_state(tmp_path / "model.json")
        assert persisted["physics"]["ir_compensation"]["k_volts_per_percent"] == 0.012
        assert persisted["soh"] == 0.92
    finally:
        owner.close()


def test_apply_feedback_requires_lock_and_noop_does_not_write(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "model.json"
    owner = ModelOwner(path, create_if_missing=True)
    with pytest.raises(ModelStateFileError, match="writer lock"):
        owner.apply_feedback(ir_k=0.012)

    owner = _owner(tmp_path)
    try:
        writes = 0

        def count_write(*_args, **_kwargs):
            nonlocal writes
            writes += 1

        monkeypatch.setattr("src.adapters.model_owner.files.atomic_write_model", count_write)
        assert owner.apply_feedback() == {}
        assert owner.apply_feedback(ir_k=0.015, soh=1.0) == {}
        assert writes == 0
    finally:
        owner.close()


def test_apply_feedback_rejects_invalid_candidate_without_mutation(tmp_path: Path) -> None:
    owner = _owner(tmp_path)
    try:
        before = owner.current_snapshot()
        with pytest.raises(schema.TargetModelStateError):
            owner.apply_feedback(ir_k=float("nan"), soh=0.9)
        assert owner.current_snapshot() == before
        persisted, _ = schema.load_target_state(tmp_path / "model.json")
        assert persisted["physics"]["ir_compensation"]["k_volts_per_percent"] == 0.015
        assert persisted["soh"] == 1.0
    finally:
        owner.close()


def test_event_receipt_is_one_atomic_write_and_repeated_event_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "model.json"
    receipt = {
        "event_at": "2026-08-22T00:00:00.999Z",
        "evidence_at": "2026-08-22T00:00:30.123Z",
        "reason": "natural blackout",
        "field_metadata": {
            "soh": {
                "evidence_at": "2026-08-22T00:00:31.456Z",
                "reason": "curve evidence",
            }
        },
    }
    owner = ModelOwner.open_runtime(path, create_if_missing=True)
    try:
        assert owner.apply_feedback(soh=0.92, event_receipt=receipt) == {"soh": (1.0, 0.92)}
        assert owner.apply_feedback(soh=0.80, event_receipt=receipt) == {}
        persisted, _ = schema.load_target_state(path)
        assert persisted["last_feedback"] == {
            "event_at": "2026-08-22T00:00:00Z",
            "evidence_at": "2026-08-22T00:00:30Z",
            "reason": "natural blackout",
            "changes": {
                "soh": {
                    "from": 1.0,
                    "to": 0.92,
                    "delta": -0.08,
                    "evidence_at": "2026-08-22T00:00:31Z",
                    "reason": "curve evidence",
                }
            },
        }
    finally:
        owner.close()

    reopened = ModelOwner.open_runtime(path)
    try:
        assert reopened.apply_feedback(soh=0.80, event_receipt=receipt) == {}
    finally:
        reopened.close()
