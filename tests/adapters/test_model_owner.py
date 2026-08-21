"""Focused tests for strict target-model ownership."""

import ast
import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from src.adapters import model_state_persistence as model_files
from src.adapters.model_owner import (
    ModelCommitRefused,
    ModelOwner,
    ModelPersistenceError,
    ModelStateConflict,
    WorkRegistryNotEmpty,
)
from src.adapters.model_state_schema import (
    TargetModelStateError,
    canonical_json,
    fresh_target_state,
    validate_target_state,
)
from src.domain.values import DEFAULT_IR_LEARNING_POLICY, ModelChange


def _oracle(before, after):
    assert after.ir_reference_load_percent == 0.0
    return after.ir_k_v_per_pp <= before.ir_k_v_per_pp, "dense_no_later_lb"


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _change(before: float, measured: float, evidence: str) -> ModelChange:
    after = max(0.005, measured, 0.8 * before)
    return ModelChange(
        parameter="ir_k_v_per_pp",
        value_before=before,
        measured_estimate=measured,
        value_after=after,
        evidence_hashes=(_hash(evidence),),
        bound_applied=after != measured,
    )


def test_model_owner_can_hold_and_transfer_runtime_writer_lock(tmp_path: Path) -> None:
    lock_fd = model_files.acquire_writer_lock(tmp_path / "monitor.lock")
    owner = ModelOwner(
        tmp_path / "model.json",
        safety_oracle=_oracle,
        create_if_missing=True,
    )
    owner.adopt_writer_lock(lock_fd)
    try:
        assert owner.writer_lock_fd == lock_fd
        with pytest.raises(model_files.ModelStateLockHeld):
            model_files.acquire_writer_lock(tmp_path / "monitor.lock")
    finally:
        owner.close()

    released_fd = model_files.acquire_writer_lock(tmp_path / "monitor.lock")
    model_files.release_writer_lock(released_fd)


def test_runtime_factory_owns_lock_before_model_load(tmp_path: Path) -> None:
    owner = ModelOwner.open_runtime(
        tmp_path / "model.json",
        safety_oracle=_oracle,
        create_if_missing=True,
    )
    try:
        assert owner.writer_lock_fd is not None
        with pytest.raises(model_files.ModelStateLockHeld):
            model_files.acquire_writer_lock(tmp_path / "monitor.lock")
    finally:
        owner.close()

    released_fd = model_files.acquire_writer_lock(tmp_path / "monitor.lock")
    model_files.release_writer_lock(released_fd)


def _rich_target_state() -> dict:
    state = fresh_target_state(
        install_date="2025-01-01",
        epoch_id="00000000-0000-4000-8000-000000000001",
        last_upscmd=("2026-01-01T00:00:00Z", "test.battery.start.quick", "OK"),
    )
    state["soh"] = 0.9
    state["soh_history"] = [{"date": "2026-01-01", "soh": 0.9, "capacity_ah_ref": 7.2}]
    state["capacity_estimates"] = [
        {
            "timestamp": "2026-01-02T00:00:00Z",
            "ah_estimate": 6.5,
            "confidence": 0.8,
            "metadata": {"source": "blackout"},
        }
    ]
    state["capacity_ah_measured"] = 6.5
    state["r_internal_history"] = [
        {
            "date": "2026-01-02",
            "r_ohm": 0.08,
            "v_before": 12.7,
            "v_sag": 12.1,
            "load_percent": 42.0,
            "event": "blackout",
        }
    ]
    state["new_battery_detected_timestamp"] = "2025-01-01T00:00:00Z"
    state["discharge_events"] = [{"event_id": "legacy"}]
    state["ir_learning_policy"]["last_commit_utc"] = "2026-01-03T00:00:00.000000Z"
    state["lut"].insert(
        2,
        {"v": 13.0, "soc": 0.8, "source": "measured", "timestamp": 1234.0},
    )
    return state


def _set_path(state: dict, path: tuple[object, ...], value: object) -> None:
    target = state
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def test_rich_target_state_exercises_every_persisted_legacy_field():
    state = _rich_target_state()

    validate_target_state(state, source="rich.json")

    assert state["capacity_estimates"][0]["metadata"] == {"source": "blackout"}
    assert state["lut"][2]["source"] == "measured"


@pytest.mark.parametrize(
    ("path", "value", "error_fragment"),
    [
        (("soh",), 0.0, "soh must be > 0"),
        (("soh_history",), {}, "soh_history must be a list"),
        (("discharge_events",), [1], "discharge_events entries must be objects"),
        (("soh_history", 0), {"date": "x"}, "capacity_ah_ref only"),
        (("soh_history", 0, "date"), 1, "date must be a string"),
        (("soh_history", 0, "soh"), 0.0, "soh must be > 0"),
        (("soh_history", 0, "capacity_ah_ref"), 0.0, "must be positive"),
        (("capacity_estimates", 0), {"timestamp": "x"}, "invalid keys"),
        (("capacity_estimates", 0, "timestamp"), 1, "timestamp must be a string"),
        (("capacity_estimates", 0, "ah_estimate"), 0.0, "must be positive"),
        (("capacity_estimates", 0, "confidence"), 1.1, "must be <= 1.0"),
        (("capacity_estimates", 0, "metadata"), [], "metadata must be an object"),
        (("r_internal_history", 0), {"date": "x"}, "invalid keys"),
        (("r_internal_history", 0, "event"), 1, "date and event must be strings"),
        (("r_internal_history", 0, "r_ohm"), -0.1, "must be >= 0.0"),
        (("last_upscmd_status",), 1, "must be a string or null"),
        (("capacity_ah_measured",), 0.0, "must be positive"),
        (("cycle_count",), True, "must be a nonnegative integer"),
        (("cumulative_on_battery_sec",), -1.0, "must be >= 0.0"),
        (("new_battery_detected",), "no", "must be a boolean"),
        (("physics", "peukert_exponent"), 1.6, "must be <= 1.5"),
        (("lut",), [], "at least two entries"),
        (("lut", 0), 1, "must be an object"),
        (("lut", 0), {"soc": 1.0, "source": "standard"}, "must contain v"),
        (("lut", 0, "v"), float("nan"), "must be a finite number"),
        (("lut", 0, "soc"), 1.1, "must be <= 1.0"),
        (("lut", 0, "source"), "vendor", "source is invalid"),
        (
            ("lut", 1),
            {"v": 13.0, "soc": 0.9, "source": "measured"},
            "timestamp is required",
        ),
        (
            ("lut", 0),
            {"v": 13.7, "soc": 1.0, "source": "standard", "timestamp": 1.0},
            "invalid keys for source standard",
        ),
    ],
)
def test_target_validator_rejects_invalid_persisted_fields(
    path: tuple[object, ...],
    value: object,
    error_fragment: str,
):
    state = _rich_target_state()
    _set_path(state, path, value)

    with pytest.raises(TargetModelStateError) as error:
        validate_target_state(state, source="invalid.json")

    assert error_fragment in str(error.value)


@pytest.mark.parametrize(
    ("current_k", "initial_k", "error_fragment"),
    [
        (-0.1, 0.015, "current IR compensation is outside policy bounds"),
        (0.050, 0.015, "current IR compensation is outside policy bounds"),
        (0.015, 0.050, "epoch initial IR compensation is outside policy bounds"),
        (0.020, 0.015, "exceeds epoch initial"),
        (0.006, 0.015, "exceeds epoch decrease limit"),
    ],
)
def test_target_validator_rejects_unsafe_ir_policy_relationships(
    current_k: float,
    initial_k: float,
    error_fragment: str,
) -> None:
    state = _rich_target_state()
    state["physics"]["ir_compensation"]["k_volts_per_percent"] = current_k
    state["ir_learning_policy"]["epoch_initial_k_v_per_pp"] = initial_k

    with pytest.raises(TargetModelStateError, match=error_fragment):
        validate_target_state(state)


@pytest.mark.parametrize(
    "lut",
    [
        [
            {"v": 10.0, "soc": 1.0, "source": "standard"},
            {"v": 14.0, "soc": 0.0, "source": "standard"},
        ],
        [
            {"v": 14.0, "soc": 1.0, "source": "standard"},
            {"v": 14.0, "soc": 0.5, "source": "standard"},
        ],
    ],
)
def test_target_validator_rejects_non_descending_lut(lut: list[dict[str, object]]) -> None:
    state = _rich_target_state()
    state["lut"] = lut
    with pytest.raises(TargetModelStateError, match="voltages must be strictly descending"):
        validate_target_state(state)


def test_target_validator_rejects_soc_increase_as_voltage_falls() -> None:
    state = _rich_target_state()
    state["lut"] = [
        {"v": 14.0, "soc": 0.5, "source": "standard"},
        {"v": 10.0, "soc": 1.0, "source": "standard"},
    ]
    with pytest.raises(TargetModelStateError, match="SoC must be non-increasing"):
        validate_target_state(state)


def test_model_owner_refuses_unsafe_lut_before_exposing_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "model.json"
    state = _rich_target_state()
    state["lut"] = [
        {"v": 10.0, "soc": 1.0, "source": "standard"},
        {"v": 14.0, "soc": 0.0, "source": "standard"},
    ]
    path.write_text(canonical_json(state), encoding="utf-8")

    with pytest.raises(TargetModelStateError, match="voltages must be strictly descending"):
        ModelOwner(path, safety_oracle=_oracle)


@pytest.mark.parametrize(
    ("path", "value", "error_fragment"),
    [
        (("physics",), {}, "physics must contain exactly"),
        (("physics", "ir_compensation"), {}, "ir_compensation has invalid keys"),
        (("physics", "ir_compensation", "reference_load_percent"), 20.0, "not_transformed"),
        (("ir_learning_policy",), {}, "ir_learning_policy has invalid keys"),
        (
            ("ir_learning_policy", "battery_epoch_id"),
            "00000000-0000-4000-8000-000000000002",
            "does not match model epoch",
        ),
        (("ir_learning_policy", "epoch_initial_k_v_per_pp"), 0.0, "must be positive"),
        (("ir_learning_policy", "last_commit_utc"), "yesterday", "canonical UTC"),
        (("ir_learning_policy", "consumed_step_hashes"), (), "must be a list"),
        (("ir_learning_policy", "consumed_step_hashes"), ["a"] * 257, "exceeds 256"),
        (
            ("ir_learning_policy", "consumed_step_hashes"),
            ["a" * 64, "a" * 64],
            "unique and canonical",
        ),
        (("ir_learning_policy", "consumed_step_hashes"), ["not-a-hash"], "invalid SHA-256"),
        (("battery_epoch_id",), "NOT-A-UUID", "must be a UUID string"),
        (
            ("battery_epoch_id",),
            "abcdefab-cdef-4abc-8def-abcdefabcdef".upper(),
            "canonical lowercase UUID text",
        ),
    ],
)
def test_target_validator_rejects_invalid_schema_and_policy(
    path: tuple[object, ...],
    value: object,
    error_fragment: str,
):
    state = _rich_target_state()
    _set_path(state, path, value)

    with pytest.raises(TargetModelStateError) as error:
        validate_target_state(state)

    assert error_fragment in str(error.value)


def test_target_validator_requires_mapping_and_exact_top_level_keys():
    with pytest.raises(TargetModelStateError, match="must contain a JSON object"):
        validate_target_state(cast(Any, []))

    state = _rich_target_state()
    del state["soh"]
    state["unexpected"] = True
    with pytest.raises(TargetModelStateError) as error:
        validate_target_state(state)

    assert "missing=['soh']" in str(error.value)
    assert "extra=['unexpected']" in str(error.value)


@pytest.mark.parametrize(
    ("change", "committed_at", "error_fragment"),
    [
        (
            replace(_change(0.015, 0.010, "a"), parameter="soh"),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            "unsupported_model_parameter",
        ),
        (
            replace(_change(0.015, 0.010, "a"), evidence_hashes=()),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            "missing_commit_evidence",
        ),
        (
            replace(_change(0.015, 0.010, "a"), value_before=0.014),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            "model_state_conflict",
        ),
        (
            replace(_change(0.015, 0.010, "a"), value_after=0.011),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            "ir_commit_not_canonical",
        ),
        (
            ModelChange("ir_k_v_per_pp", 0.015, 0.0145, 0.0145, (_hash("a"),), False),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            "ir_change_below_noise_floor",
        ),
        (
            replace(_change(0.015, 0.010, "a"), bound_applied=False),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            "ir_bound_flag_mismatch",
        ),
        (
            replace(_change(0.015, 0.010, "a"), evidence_hashes=("invalid",)),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            "invalid_evidence_hash",
        ),
        (
            replace(
                _change(0.015, 0.010, "a"),
                evidence_hashes=(_hash("a"), _hash("a")),
            ),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            "duplicate_evidence_hash",
        ),
        (
            _change(0.015, 0.010, "a"),
            datetime(2026, 1, 1),
            "timezone-aware UTC",
        ),
    ],
)
def test_model_owner_refuses_noncanonical_commit_intent_without_writes(
    tmp_path: Path,
    change: ModelChange,
    committed_at: datetime,
    error_fragment: str,
):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    before = path.read_bytes()

    with pytest.raises((ModelCommitRefused, ModelStateConflict)) as error:
        owner.commit(change, blackout_id="blackout-a", committed_at=committed_at)

    assert error_fragment in str(error.value)
    assert path.read_bytes() == before
    assert not owner.precommit_path.exists()


def test_prepare_and_reset_detect_external_state_changes(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    path.write_bytes(path.read_bytes() + b" ")
    externally_changed = path.read_bytes()

    with pytest.raises(ModelStateConflict, match="model_state_conflict"):
        owner.prepare_commit(
            _change(0.015, 0.010, "prepare"),
            blackout_id="blackout-a",
            committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    with pytest.raises(ModelStateConflict, match="model_state_conflict"):
        owner.reset_baseline(work_is_empty=lambda: True)

    assert path.read_bytes() == externally_changed


def test_prepare_rejects_evidence_already_consumed_by_prior_commit(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    change = _change(0.015, 0.010, "same-step")
    owner.commit(
        change,
        blackout_id="blackout-a",
        committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    committed = path.read_bytes()

    with pytest.raises(ModelCommitRefused, match="overlapping_evidence_already_consumed"):
        owner.prepare_commit(
            change,
            blackout_id="blackout-a",
            committed_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )

    assert path.read_bytes() == committed


def test_commit_refuses_wrong_expected_hash_before_precommit_write(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    before = path.read_bytes()

    with pytest.raises(ModelStateConflict, match="model_state_conflict"):
        owner.commit(
            _change(0.015, 0.010, "a"),
            blackout_id="blackout-a",
            expected_model_hash="f" * 64,
            committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    assert path.read_bytes() == before
    assert not owner.precommit_path.exists()


def test_safety_oracle_refusal_is_byte_identical(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(
        path,
        safety_oracle=lambda _before, _after: (False, "unsafe"),
        create_if_missing=True,
    )
    before = path.read_bytes()

    with pytest.raises(ModelCommitRefused, match="safety_oracle_failed"):
        owner.commit(
            _change(0.015, 0.010, "a"),
            blackout_id="blackout-a",
            committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    assert path.read_bytes() == before
    assert not owner.precommit_path.exists()


def test_precommit_backup_verification_failure_prevents_model_write(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    before = path.read_bytes()
    real_read = model_files.read_model_file

    def corrupt_only_backup(target: Path, *, error_type=Exception) -> bytes:
        if target == owner.precommit_path:
            return b"corrupt-backup"
        return real_read(target, error_type=error_type)

    with (
        patch.object(model_files, "read_model_file", side_effect=corrupt_only_backup),
        pytest.raises(ModelStateConflict, match="precommit_backup_verification_failed"),
    ):
        owner.commit(
            _change(0.015, 0.010, "a"),
            blackout_id="blackout-a",
            committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    assert path.read_bytes() == before


def test_post_commit_read_failure_restores_before_bytes_and_keeps_memory_unchanged(
    tmp_path: Path,
):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    before = path.read_bytes()
    change = _change(0.015, 0.010, "post-read-failure")
    real_read = model_files.read_model_file
    model_reads = 0

    def fail_once_after_publication(target: Path, *, error_type=Exception) -> bytes:
        nonlocal model_reads
        if target == path:
            model_reads += 1
            if model_reads == 2:
                raise error_type("injected post-commit read failure")
        return real_read(target, error_type=error_type)

    with (
        patch.object(model_files, "read_model_file", side_effect=fail_once_after_publication),
        pytest.raises(ModelPersistenceError, match="exact source restored"),
    ):
        owner.commit(
            change,
            blackout_id="blackout-a",
            committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    assert path.read_bytes() == before
    assert owner.policy_projection().persisted_hash == model_files.persisted_hash(before)
    assert owner.current_snapshot().ir_k_v_per_pp == pytest.approx(0.015)
    assert owner.precommit_path.read_bytes() == before

    restarted = ModelOwner(path, safety_oracle=_oracle)
    receipt = restarted.commit(
        change,
        blackout_id="blackout-a",
        expected_model_hash=model_files.persisted_hash(before),
        committed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    assert receipt.model_hash_before == model_files.persisted_hash(before)
    assert path.read_bytes() != before


def test_post_commit_hash_mismatch_restores_before_bytes_and_keeps_memory_unchanged(
    tmp_path: Path,
):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    before = path.read_bytes()
    real_read = model_files.read_model_file
    model_reads = 0

    def return_corrupt_post_write_bytes(target: Path, *, error_type=Exception) -> bytes:
        nonlocal model_reads
        raw = real_read(target, error_type=error_type)
        if target == path:
            model_reads += 1
            if model_reads == 2:
                return raw + b" "
        return raw

    with (
        patch.object(model_files, "read_model_file", side_effect=return_corrupt_post_write_bytes),
        pytest.raises(ModelPersistenceError, match="exact source restored"),
    ):
        owner.commit(
            _change(0.015, 0.010, "post-hash-mismatch"),
            blackout_id="blackout-a",
            committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    assert path.read_bytes() == before
    assert owner.policy_projection().persisted_hash == model_files.persisted_hash(before)
    assert owner.current_snapshot().ir_k_v_per_pp == pytest.approx(0.015)
    assert owner.precommit_path.read_bytes() == before


def test_post_commit_failure_retains_backup_when_rollback_fails(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    before = path.read_bytes()
    change = _change(0.015, 0.010, "rollback-failure")
    real_read = model_files.read_model_file
    real_write = model_files.atomic_write_model
    model_reads = 0
    target_writes = 0

    def fail_once_after_publication(target: Path, *, error_type=Exception) -> bytes:
        nonlocal model_reads
        if target == path:
            model_reads += 1
            if model_reads == 2:
                raise error_type("injected post-commit read failure")
        return real_read(target, error_type=error_type)

    def fail_rollback_write(target: Path, content: str, *, mode: int = 0o600) -> str:
        nonlocal target_writes
        if Path(target) == path:
            target_writes += 1
            if target_writes == 2:
                raise OSError("injected rollback write failure")
        return real_write(target, content, mode=mode)

    with (
        patch.object(model_files, "read_model_file", side_effect=fail_once_after_publication),
        patch.object(model_files, "atomic_write_model", side_effect=fail_rollback_write),
        pytest.raises(ModelPersistenceError, match="rollback failed") as error,
    ):
        owner.commit(
            change,
            blackout_id="blackout-a",
            committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    assert "verified source backup retained" in str(error.value)
    assert path.read_bytes() != before
    assert owner.policy_projection().persisted_hash == model_files.persisted_hash(before)
    assert owner.current_snapshot().ir_k_v_per_pp == pytest.approx(0.015)
    assert owner.precommit_path.read_bytes() == before

    restarted = ModelOwner(path, safety_oracle=_oracle)
    receipt = restarted.commit(
        change,
        blackout_id="blackout-a",
        expected_model_hash=model_files.persisted_hash(before),
        committed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    assert receipt.model_hash_before == model_files.persisted_hash(before)
    assert receipt.model_hash_after == model_files.persisted_hash(path.read_bytes())


def test_idempotent_receipt_rejects_partial_or_inconsistent_replay(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    original = _change(0.015, 0.010, "a")
    receipt = owner.commit(
        original,
        blackout_id="blackout-a",
        committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    committed = path.read_bytes()

    partial_overlap = replace(
        original,
        evidence_hashes=(*original.evidence_hashes, _hash("new")),
    )
    with pytest.raises(ModelCommitRefused, match="overlapping_evidence_already_consumed"):
        owner.commit(partial_overlap, blackout_id="blackout-a")
    with pytest.raises(ModelCommitRefused, match="overlapping_evidence_already_consumed"):
        owner.commit(replace(original, value_after=0.011), blackout_id="blackout-a")
    with pytest.raises(ModelStateConflict, match="model_state_conflict"):
        owner.commit(
            original,
            blackout_id="blackout-a",
            expected_model_hash="f" * 64,
        )

    assert owner.policy_projection().persisted_hash == receipt.model_hash_after
    assert path.read_bytes() == committed


def test_missing_model_creates_strict_reference_zero_snapshot(tmp_path: Path):
    owner = ModelOwner(
        tmp_path / "model.json",
        safety_oracle=_oracle,
        create_if_missing=True,
    )

    snapshot = owner.current_snapshot()
    state = json.loads((tmp_path / "model.json").read_text(encoding="utf-8"))
    validate_target_state(state)
    assert snapshot.ir_reference_load_percent == 0.0
    assert snapshot.battery_epoch_id == state["ir_learning_policy"]["battery_epoch_id"]
    assert snapshot.lut[0].voltage_v == pytest.approx(13.7)
    with pytest.raises(FrozenInstanceError):
        setattr(snapshot.lut[0], "soc", 0.0)


def test_normal_load_refuses_missing_target_without_creating_parent(tmp_path: Path):
    path = tmp_path / "missing" / "model.json"

    with pytest.raises(TargetModelStateError, match="target model does not exist"):
        ModelOwner(path, safety_oracle=_oracle)

    assert not path.exists()
    assert not path.parent.exists()


def test_normal_owner_rejects_release_a_schema_without_changing_bytes(tmp_path: Path):
    path = tmp_path / "model.json"
    legacy = fresh_target_state(epoch_id="00000000-0000-4000-8000-000000000001")
    del legacy["ir_learning_policy"]
    legacy["physics"]["ir_compensation"]["reference_load_percent"] = 20.0
    legacy["physics"]["rls_state"] = {
        "ir_k": {"theta": 0.015, "P": 1.0, "sample_count": 0, "forgetting_factor": 0.97},
        "peukert": {"theta": 1.2, "P": 1.0, "sample_count": 0, "forgetting_factor": 0.97},
    }
    for entry in legacy["lut"]:
        entry["v"] -= 0.015 * 20.0
    path.write_text(canonical_json(legacy), encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(TargetModelStateError, match="invalid target keys"):
        ModelOwner(path, safety_oracle=_oracle)

    assert path.read_bytes() == before


def test_commit_backs_up_before_state_and_atomically_swaps_snapshot(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    before_bytes = path.read_bytes()
    before_snapshot = owner.current_snapshot()
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    change = _change(before_snapshot.ir_k_v_per_pp, 0.010, "step-a")

    receipt = owner.commit(
        change,
        blackout_id="blackout-a",
        expected_model_hash=owner.policy_projection().persisted_hash,
        committed_at=when,
    )

    assert (tmp_path / "model.precommit.json").read_bytes() == before_bytes
    assert (tmp_path / "model.precommit.json").stat().st_mode & 0o777 == 0o600
    assert receipt.value_after == pytest.approx(0.012)
    assert receipt.model_hash_before != receipt.model_hash_after
    assert receipt.safety_oracle == "dense_no_later_lb"
    assert owner.current_snapshot().ir_k_v_per_pp == pytest.approx(0.012)
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["ir_learning_policy"]["last_commit_utc"].startswith("2026-01-01T")
    assert state["ir_learning_policy"]["consumed_step_hashes"] == [_hash("step-a")]


def test_prepare_commit_is_read_only_and_freezes_exact_expected_hash(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    before = path.read_bytes()
    policy = owner.policy_projection()

    prepared = owner.prepare_commit(
        _change(policy.snapshot.ir_k_v_per_pp, 0.010, "step-a"),
        blackout_id="blackout-a",
        committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert path.read_bytes() == before
    assert not (tmp_path / "model.precommit.json").exists()
    assert prepared.model_hash_before == policy.persisted_hash
    assert prepared.expected_model_hash_after != prepared.model_hash_before
    assert policy.consumed_step_hashes == frozenset()


def test_prepared_commit_applies_then_reconstructs_after_restart(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    prepared = owner.prepare_commit(
        _change(0.015, 0.010, "step-a"),
        blackout_id="blackout-a",
        committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    first = owner.commit_prepared(prepared)
    committed = path.read_bytes()
    replayed = ModelOwner(path, safety_oracle=_oracle).commit_prepared(prepared)

    assert replayed == first
    assert path.read_bytes() == committed
    assert first.model_hash_after == prepared.expected_model_hash_after


def test_prepared_commit_rejects_unexpected_hash_without_writes(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    prepared = owner.prepare_commit(
        _change(0.015, 0.010, "step-a"),
        blackout_id="blackout-a",
        committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    before = path.read_bytes()

    with pytest.raises(ModelStateConflict, match="model_state_conflict"):
        owner.commit_prepared(replace(prepared, model_hash_before="f" * 64))

    assert path.read_bytes() == before
    assert not (tmp_path / "model.precommit.json").exists()


def test_prepared_commit_policy_mismatch_refuses_without_changing_model_bytes(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    prepared = owner.prepare_commit(
        _change(0.015, 0.010, "step-policy"),
        blackout_id="blackout-policy",
        committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    mismatched = replace(
        prepared,
        learning_policy=replace(prepared.learning_policy, deadband_v_per_pp=0.002),
    )
    before = path.read_bytes()

    with pytest.raises(ModelCommitRefused, match="learning_policy_mismatch"):
        owner.commit_prepared(mismatched)

    assert path.read_bytes() == before


def test_retry_after_restart_reconstructs_receipt_without_second_write(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    expected_before = owner.policy_projection().persisted_hash
    change = _change(0.015, 0.010, "step-a")
    first = owner.commit(
        change,
        blackout_id="blackout-a",
        expected_model_hash=expected_before,
        committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    committed_bytes = path.read_bytes()
    restarted = ModelOwner(path, safety_oracle=_oracle)

    replayed = restarted.commit(
        change,
        blackout_id="blackout-a",
        expected_model_hash=expected_before,
        committed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    assert replayed == first
    assert path.read_bytes() == committed_bytes


def test_external_model_write_is_a_conflict_and_refusal_is_byte_identical(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    path.write_bytes(path.read_bytes() + b" ")
    externally_changed = path.read_bytes()

    with pytest.raises(ModelStateConflict, match="model_state_conflict"):
        owner.commit(
            _change(0.015, 0.010, "step-a"),
            blackout_id="blackout-a",
        )

    assert path.read_bytes() == externally_changed
    assert not (tmp_path / "model.precommit.json").exists()


def test_rate_limit_and_backward_time_refuse_without_writes(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    first_time = datetime(2026, 2, 1, tzinfo=timezone.utc)
    owner.commit(_change(0.015, 0.010, "a"), blackout_id="a", committed_at=first_time)
    committed = path.read_bytes()

    with pytest.raises(ModelCommitRefused, match="commit_rate_limited"):
        owner.commit(
            _change(0.012, 0.009, "b"),
            blackout_id="b",
            committed_at=first_time + timedelta(days=29),
        )
    assert path.read_bytes() == committed

    with pytest.raises(ModelCommitRefused, match="commit_rate_window_indeterminate"):
        owner.commit(
            _change(0.012, 0.009, "c"),
            blackout_id="c",
            committed_at=first_time - timedelta(seconds=1),
        )
    assert path.read_bytes() == committed


def test_epoch_cumulative_bound_refuses_fourth_decrease(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    current = 0.015
    for index in range(3):
        change = _change(current, current * 0.5, f"step-{index}")
        owner.commit(
            change,
            blackout_id=f"event-{index}",
            committed_at=when + timedelta(days=31 * index),
        )
        current = change.value_after
    committed = path.read_bytes()

    with pytest.raises(ModelCommitRefused, match="epoch_cumulative_decrease_exceeded"):
        owner.commit(
            _change(current, current * 0.5, "step-4"),
            blackout_id="event-4",
            committed_at=when + timedelta(days=93),
        )

    assert path.read_bytes() == committed


def test_consumed_hash_budget_refuses_without_writes(tmp_path: Path):
    path = tmp_path / "model.json"
    state = fresh_target_state()
    state["ir_learning_policy"]["consumed_step_hashes"] = sorted(
        _hash(f"existing-{index}")
        for index in range(DEFAULT_IR_LEARNING_POLICY.max_consumed_step_hashes)
    )
    validate_target_state(state)
    path.write_text(canonical_json(state), encoding="utf-8")
    owner = ModelOwner(path, safety_oracle=_oracle)
    before = path.read_bytes()

    with pytest.raises(ModelCommitRefused, match="consumed_evidence_budget_exhausted"):
        owner.commit(
            _change(0.015, 0.010, "new-step"),
            blackout_id="event",
            committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    assert path.read_bytes() == before


def test_reset_requires_explicit_empty_work_and_creates_new_epoch(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    old_epoch = owner.current_snapshot().battery_epoch_id
    before = path.read_bytes()

    with pytest.raises(WorkRegistryNotEmpty):
        owner.reset_baseline(work_is_empty=lambda: False, install_date="2026-08-16")
    assert path.read_bytes() == before

    snapshot = owner.reset_baseline(work_is_empty=lambda: True, install_date="2026-08-16")
    assert snapshot.battery_epoch_id != old_epoch
    assert snapshot.ir_reference_load_percent == 0.0
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["ir_learning_policy"]["consumed_step_hashes"] == []


def test_reset_post_write_read_failure_restores_exact_previous_model(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    before = path.read_bytes()
    before_snapshot = owner.current_snapshot()
    real_read = model_files.read_model_file
    model_reads = 0

    def fail_once_after_publication(target: Path, *, error_type=Exception) -> bytes:
        nonlocal model_reads
        if target == path:
            model_reads += 1
            if model_reads == 2:
                raise error_type("injected post-reset read failure")
        return real_read(target, error_type=error_type)

    with (
        patch.object(model_files, "read_model_file", side_effect=fail_once_after_publication),
        pytest.raises(ModelPersistenceError, match="exact source restored"),
    ):
        owner.reset_baseline(work_is_empty=lambda: True, install_date="2026-08-16")

    assert path.read_bytes() == before
    assert owner.current_snapshot() == before_snapshot
    assert owner.precommit_path.read_bytes() == before


def test_reset_post_write_hash_mismatch_restores_exact_previous_model(tmp_path: Path):
    path = tmp_path / "model.json"
    owner = ModelOwner(path, safety_oracle=_oracle, create_if_missing=True)
    before = path.read_bytes()
    real_read = model_files.read_model_file
    model_reads = 0

    def corrupt_once_after_publication(target: Path, *, error_type=Exception) -> bytes:
        nonlocal model_reads
        raw = real_read(target, error_type=error_type)
        if target == path:
            model_reads += 1
            if model_reads == 2:
                return raw + b" "
        return raw

    with (
        patch.object(model_files, "read_model_file", side_effect=corrupt_once_after_publication),
        pytest.raises(ModelPersistenceError, match="exact source restored"),
    ):
        owner.reset_baseline(work_is_empty=lambda: True)

    assert path.read_bytes() == before
    assert owner.precommit_path.read_bytes() == before


def test_production_has_no_legacy_model_import_edge():
    repository = Path(__file__).parents[2]
    production_paths = [
        *(repository / "src").rglob("*.py"),
        *(repository / "scripts").rglob("*.py"),
    ]
    legacy_importers = []
    for path in production_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "src.model":
                legacy_importers.append(path.relative_to(repository).as_posix())
            if isinstance(node, ast.Import) and any(
                alias.name == "src.model" for alias in node.names
            ):
                legacy_importers.append(path.relative_to(repository).as_posix())

    assert legacy_importers == []
