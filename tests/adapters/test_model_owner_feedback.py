"""Focused tests for the explicit model update boundary."""

from pathlib import Path

import pytest

from src.adapters import model_state_schema as schema
from src.adapters.model_owner import ModelOwner
from src.adapters.model_state_persistence import ModelStateFileError


def _owner(tmp_path: Path) -> ModelOwner:
    return ModelOwner.open_runtime(tmp_path / "model.json", create_if_missing=True)


def test_apply_ir_k_persists_audit_values_and_refreshes_snapshot(tmp_path: Path) -> None:
    owner = _owner(tmp_path)
    try:
        changes = owner.apply_ir_k(0.012)

        assert changes == (0.015, 0.012)
        snapshot = owner.current_snapshot()
        assert snapshot.ir_k_v_per_pp == 0.012
        persisted, _ = schema.load_target_state(tmp_path / "model.json")
        assert persisted["physics"]["ir_compensation"]["k_volts_per_percent"] == 0.012
    finally:
        owner.close()


def test_apply_ir_k_requires_lock_and_noop_does_not_write(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "model.json"
    owner = ModelOwner(path, create_if_missing=True)
    with pytest.raises(ModelStateFileError, match="writer lock"):
        owner.apply_ir_k(0.012)

    owner = _owner(tmp_path)
    try:
        writes = 0

        def count_write(*_args, **_kwargs):
            nonlocal writes
            writes += 1

        monkeypatch.setattr("src.adapters.model_owner.files.atomic_write_model", count_write)
        assert owner.apply_ir_k(0.015) is None
        assert writes == 0
    finally:
        owner.close()


def test_apply_ir_k_rejects_invalid_candidate_without_mutation(tmp_path: Path) -> None:
    owner = _owner(tmp_path)
    try:
        before = owner.current_snapshot()
        with pytest.raises(schema.TargetModelStateError):
            owner.apply_ir_k(float("nan"))
        assert owner.current_snapshot() == before
        persisted, _ = schema.load_target_state(tmp_path / "model.json")
        assert persisted["physics"]["ir_compensation"]["k_volts_per_percent"] == 0.015
    finally:
        owner.close()
