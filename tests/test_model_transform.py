"""Focused tests for the offline Release-A reference-frame transform."""

import fcntl
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from src.adapters import model_state_persistence as model_files
from src.adapters import model_transform
from src.adapters.model_owner import ModelOwner
from src.adapters.model_state_release_a import (
    ModelTransformError,
    build_target_state,
    decode_release_a_state,
    release_a_scientific_fingerprint,
)
from src.adapters.model_state_schema import scientific_fingerprint, validate_target_state
from src.adapters.model_transform import (
    TransformSettings,
    WriterLockHeld,
    dense_equivalence_oracle,
    load_release_a_source,
    transform_model_file,
)
from src.battery_math.lut import LutPoint, soc_from_voltage
from src.domain.values import DEFAULT_IR_LEARNING_POLICY

REGISTERED_RELEASE_A_FIXTURE_FINGERPRINT = (
    "97a021ad753b4dabb166ce7599a5398b7fce08b41ddc4f4f60df3fd8e03fa0d8"
)
TRANSFORM_SHUTDOWN_THRESHOLD_MINUTES = 17.0
TRANSFORM_SETTINGS = TransformSettings(TRANSFORM_SHUTDOWN_THRESHOLD_MINUTES)


def _oracle(before, after):
    return after.ir_k_v_per_pp <= before.ir_k_v_per_pp, "dense_no_later_lb"


def _release_a_state() -> dict:
    return {
        "soh": 0.91,
        "physics": {
            "peukert_exponent": 1.23,
            "ir_compensation": {
                "k_volts_per_percent": 0.017,
                "reference_load_percent": 20.0,
            },
            "rls_state": {
                "ir_k": {
                    "theta": 0.017,
                    "P": 0.25,
                    "sample_count": 7,
                    "forgetting_factor": 0.97,
                },
                "peukert": {
                    "theta": 1.23,
                    "P": 0.5,
                    "sample_count": 4,
                    "forgetting_factor": 0.97,
                },
            },
        },
        "lut": [
            {"v": 13.4, "soc": 1.0, "source": "standard"},
            {"v": 12.55, "soc": 0.7, "source": "measured", "timestamp": 1234.0},
            {"v": 10.5, "soc": 0.0, "source": "anchor"},
        ],
        "soh_history": [{"date": "2026-01-02", "soh": 0.91, "capacity_ah_ref": 7.2}],
        "capacity_estimates": [
            {
                "timestamp": "2026-01-02T03:04:05Z",
                "ah_estimate": 6.55,
                "confidence": 0.8,
                "metadata": {"note": "registered-α"},
            }
        ],
        "capacity_ah_measured": 6.55,
        "r_internal_history": [
            {
                "date": "2026-01-02",
                "r_ohm": 0.08,
                "v_before": 12.7,
                "v_sag": 11.9,
                "load_percent": 42.0,
                "event": "blackout",
            }
        ],
        "battery_install_date": "2025-01-01",
        "cycle_count": 3,
        "cumulative_on_battery_sec": 456.0,
        "battery_epoch_id": "00000000-0000-4000-8000-000000000001",
        "new_battery_detected": False,
        "new_battery_detected_timestamp": None,
        "discharge_events": [{"event_id": "legacy"}],
        "last_upscmd_timestamp": "2026-01-01T00:00:00Z",
        "last_upscmd_type": "test.battery.start.quick",
        "last_upscmd_status": "OK",
    }


def _release_a_duplicate_lut_state() -> dict:
    state = _release_a_state()
    state["lut"] = [
        {"v": 13.4, "soc": 1.0, "source": "standard"},
        {"v": 13.4, "soc": 1.0, "source": "measured", "timestamp": 2000.0},
        {"v": 12.55, "soc": 0.7, "source": "measured", "timestamp": 1234.0},
        {"v": 12.1, "soc": 0.4, "source": "standard"},
        {"v": 12.1, "soc": 0.4, "source": "measured", "timestamp": 1900.0},
        {"v": 11.6, "soc": 0.18, "source": "standard"},
        {"v": 11.6, "soc": 0.18, "source": "measured", "timestamp": 1800.0},
        {"v": 10.5, "soc": 0.0, "source": "anchor"},
    ]
    return state


def _legacy_model(path: Path) -> tuple[bytes, str, dict]:
    state = _release_a_state()
    path.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")
    return path.read_bytes(), release_a_scientific_fingerprint(state), state


def _registrations(path: Path, source_fingerprint: str) -> tuple[str, dict]:
    source = load_release_a_source(
        path,
        registered_source_fingerprint=source_fingerprint,
    )
    target = build_target_state(source.state)
    return scientific_fingerprint(target), target


def test_release_a_codec_reproduces_registered_legacy_fingerprint_without_writes(
    tmp_path: Path,
):
    path = tmp_path / "model.json"
    before, fingerprint, state = _legacy_model(path)

    decoded = decode_release_a_state(before, source=str(path))
    loaded = load_release_a_source(
        path,
        registered_source_fingerprint=REGISTERED_RELEASE_A_FIXTURE_FINGERPRINT,
    )

    assert decoded == state
    assert fingerprint == REGISTERED_RELEASE_A_FIXTURE_FINGERPRINT
    assert loaded.scientific_fingerprint == REGISTERED_RELEASE_A_FIXTURE_FINGERPRINT
    assert path.read_bytes() == before


def test_transform_shifts_every_lut_row_drops_rls_and_keeps_exact_backup(tmp_path: Path):
    path = tmp_path / "model.json"
    backup = tmp_path / "model.pretransform.json"
    before_bytes, source_fingerprint, source_state = _legacy_model(path)
    target_fingerprint, _target = _registrations(path, source_fingerprint)

    receipt = transform_model_file(
        path,
        backup_path=backup,
        registered_source_fingerprint=source_fingerprint,
        registered_target_fingerprint=target_fingerprint,
        settings=TRANSFORM_SETTINGS,
    )

    transformed = json.loads(path.read_text(encoding="utf-8"))
    validate_target_state(transformed)
    assert backup.read_bytes() == before_bytes
    assert backup.stat().st_mode & 0o777 == 0o600
    assert "rls_state" not in transformed["physics"]
    assert transformed["physics"]["ir_compensation"]["reference_load_percent"] == 0.0
    policy = transformed["ir_learning_policy"]
    assert policy["revision"] == DEFAULT_IR_LEARNING_POLICY.revision
    assert policy["deadband_v_per_pp"] == DEFAULT_IR_LEARNING_POLICY.deadband_v_per_pp
    assert policy["min_k_v_per_pp"] == DEFAULT_IR_LEARNING_POLICY.min_k_v_per_pp
    assert policy["max_k_v_per_pp"] == DEFAULT_IR_LEARNING_POLICY.max_k_v_per_pp
    assert (
        policy["max_single_commit_fraction"]
        == DEFAULT_IR_LEARNING_POLICY.max_single_commit_fraction
    )
    assert (
        policy["max_epoch_decrease_fraction"]
        == DEFAULT_IR_LEARNING_POLICY.max_epoch_decrease_fraction
    )
    assert policy["min_commit_interval_days"] == DEFAULT_IR_LEARNING_POLICY.min_commit_interval_days
    assert policy["max_consumed_step_hashes"] == DEFAULT_IR_LEARNING_POLICY.max_consumed_step_hashes
    assert policy["epoch_initial_k_v_per_pp"] == pytest.approx(0.017)
    for old, new in zip(source_state["lut"], transformed["lut"], strict=True):
        assert new["v"] == pytest.approx(old["v"] + 0.017 * 20)
        assert {key: value for key, value in new.items() if key != "v"} == {
            key: value for key, value in old.items() if key != "v"
        }
    assert receipt.target_scientific_fingerprint == target_fingerprint
    assert receipt.equivalence.lb_transitions_identical
    assert receipt.equivalence.non_lb_status_identical
    assert receipt.equivalence.shutdown_threshold_minutes == TRANSFORM_SHUTDOWN_THRESHOLD_MINUTES
    assert receipt.equivalence.max_soc_delta <= 0.005
    assert ModelOwner(
        path, safety_oracle=_oracle
    ).current_snapshot().ir_k_v_per_pp == pytest.approx(0.017)


def test_transform_canonicalizes_real_duplicate_coordinates_and_keeps_measured_provenance(
    tmp_path: Path,
):
    path = tmp_path / "model.json"
    backup = tmp_path / "model.pretransform.json"
    source_state = _release_a_duplicate_lut_state()
    path.write_text(json.dumps(source_state, ensure_ascii=True, indent=2), encoding="utf-8")
    before = path.read_bytes()
    source_fingerprint = release_a_scientific_fingerprint(source_state)
    source = load_release_a_source(path, registered_source_fingerprint=source_fingerprint)
    target_state = build_target_state(source.state)
    target_fingerprint = scientific_fingerprint(target_state)

    receipt = transform_model_file(
        path,
        backup_path=backup,
        registered_source_fingerprint=source_fingerprint,
        registered_target_fingerprint=target_fingerprint,
        settings=TRANSFORM_SETTINGS,
    )

    transformed = json.loads(path.read_text(encoding="utf-8"))
    validate_target_state(transformed)
    assert backup.read_bytes() == before
    assert receipt.backup_path == backup
    assert receipt.source_scientific_fingerprint == source_fingerprint
    assert receipt.target_scientific_fingerprint == target_fingerprint
    assert receipt.equivalence.lb_transitions_identical
    assert receipt.equivalence.non_lb_status_identical
    assert receipt.equivalence.max_soc_delta <= 0.005
    assert receipt.equivalence.max_runtime_delta_seconds <= 1.0
    assert transformed["lut"] == [
        {"v": 13.74, "soc": 1.0, "source": "measured", "timestamp": 2000.0},
        {"v": 12.89, "soc": 0.7, "source": "measured", "timestamp": 1234.0},
        {"v": 12.44, "soc": 0.4, "source": "measured", "timestamp": 1900.0},
        {"v": 11.94, "soc": 0.18, "source": "measured", "timestamp": 1800.0},
        {"v": 10.84, "soc": 0.0, "source": "anchor"},
    ]


def test_transform_refuses_conflicting_duplicate_voltage_without_writes(tmp_path: Path):
    path = tmp_path / "model.json"
    backup = tmp_path / "model.pretransform.json"
    source_state = _release_a_duplicate_lut_state()
    source_state["lut"][1]["soc"] = 0.99
    path.write_text(json.dumps(source_state, ensure_ascii=True, indent=2), encoding="utf-8")
    before = path.read_bytes()
    source_fingerprint = release_a_scientific_fingerprint(source_state)

    with pytest.raises(ModelTransformError, match="conflicting duplicate voltage 13.4"):
        transform_model_file(
            path,
            backup_path=backup,
            registered_source_fingerprint=source_fingerprint,
            registered_target_fingerprint="0" * 64,
            settings=TRANSFORM_SETTINGS,
        )

    assert path.read_bytes() == before
    assert not backup.exists()


def test_dense_oracle_exercises_actual_source_and_target_float_frames(tmp_path: Path):
    path = tmp_path / "model.json"
    _before, source_fingerprint, _source_state = _legacy_model(path)
    source = load_release_a_source(
        path,
        registered_source_fingerprint=source_fingerprint,
    )
    target = build_target_state(source.state)
    source_lut = tuple(
        LutPoint(entry["v"], entry["soc"], entry["source"]) for entry in source.state["lut"]
    )
    target_lut = tuple(
        LutPoint(entry["v"], entry["soc"], entry["source"]) for entry in target["lut"]
    )

    source_soc = soc_from_voltage(9.65 + 0.017 * (100 - 20), source_lut)
    target_soc = soc_from_voltage(9.65 + 0.017 * 100, target_lut)
    source_thresholds: set[float] = set()
    target_thresholds: set[float] = set()
    release_a_status = model_transform._release_a_real_blackout_status
    current_status = model_transform.decide_unlatched_safety_status

    def record_source_threshold(runtime: float, threshold: float) -> str:
        source_thresholds.add(threshold)
        return release_a_status(runtime, threshold)

    def record_target_threshold(kind, runtime: float, threshold: float):
        target_thresholds.add(threshold)
        return current_status(kind, runtime, threshold)

    with (
        patch(
            "src.adapters.model_transform._release_a_real_blackout_status",
            side_effect=record_source_threshold,
        ),
        patch(
            "src.adapters.model_transform.decide_unlatched_safety_status",
            side_effect=record_target_threshold,
        ),
    ):
        report = dense_equivalence_oracle(
            source.state,
            target,
            shutdown_threshold_minutes=TRANSFORM_SHUTDOWN_THRESHOLD_MINUTES,
        )

    assert source_soc == pytest.approx(target_soc, abs=1e-12)
    assert source_thresholds == {TRANSFORM_SHUTDOWN_THRESHOLD_MINUTES}
    assert target_thresholds == {TRANSFORM_SHUTDOWN_THRESHOLD_MINUTES}
    assert report.shutdown_threshold_minutes == TRANSFORM_SHUTDOWN_THRESHOLD_MINUTES
    assert report.max_runtime_delta_seconds <= 1.0
    assert report.lb_transitions_identical
    assert report.non_lb_status_identical


def test_transform_and_dense_oracle_have_no_shutdown_threshold_default() -> None:
    transform_settings = inspect.signature(transform_model_file).parameters["settings"]
    oracle_threshold = inspect.signature(dense_equivalence_oracle).parameters[
        "shutdown_threshold_minutes"
    ]
    settings_threshold = inspect.signature(TransformSettings).parameters[
        "shutdown_threshold_minutes"
    ]

    assert transform_settings.default is inspect.Parameter.empty
    assert oracle_threshold.default is inspect.Parameter.empty
    assert settings_threshold.default is inspect.Parameter.empty


@pytest.mark.parametrize("threshold", (0.0, -1.0, float("inf"), float("nan")))
def test_invalid_shutdown_threshold_refuses_before_backup_or_write(
    tmp_path: Path,
    threshold: float,
) -> None:
    path = tmp_path / "model.json"
    backup = tmp_path / "backup.json"
    before, source_fingerprint, _state = _legacy_model(path)
    target_fingerprint, _target = _registrations(path, source_fingerprint)

    with pytest.raises(ModelTransformError, match="finite and positive"):
        transform_model_file(
            path,
            backup_path=backup,
            registered_source_fingerprint=source_fingerprint,
            registered_target_fingerprint=target_fingerprint,
            settings=TransformSettings(threshold),
        )

    assert path.read_bytes() == before
    assert not backup.exists()


def test_rerun_against_target_refuses_without_changing_model_or_backup(tmp_path: Path):
    path = tmp_path / "model.json"
    backup = tmp_path / "model.pretransform.json"
    _before, source_fingerprint, _state = _legacy_model(path)
    target_fingerprint, _target = _registrations(path, source_fingerprint)
    transform_model_file(
        path,
        backup_path=backup,
        registered_source_fingerprint=source_fingerprint,
        registered_target_fingerprint=target_fingerprint,
        settings=TRANSFORM_SETTINGS,
    )
    transformed_bytes = path.read_bytes()
    backup_bytes = backup.read_bytes()

    with pytest.raises(ModelTransformError, match="strict Release-A source validation failed"):
        transform_model_file(
            path,
            backup_path=backup,
            registered_source_fingerprint=source_fingerprint,
            registered_target_fingerprint=target_fingerprint,
            settings=TRANSFORM_SETTINGS,
        )

    assert path.read_bytes() == transformed_bytes
    assert backup.read_bytes() == backup_bytes


@pytest.mark.parametrize(
    "mutation",
    ["missing", "extra", "wrong_reference", "missing_rls_parameter", "extra_lut_field"],
)
def test_invalid_source_preconditions_are_byte_identical(tmp_path: Path, mutation: str):
    path = tmp_path / "model.json"
    _before, source_fingerprint, _state = _legacy_model(path)
    state = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "missing":
        del state["soh"]
    elif mutation == "extra":
        state["unexpected"] = True
    elif mutation == "wrong_reference":
        state["physics"]["ir_compensation"]["reference_load_percent"] = 19.0
    elif mutation == "missing_rls_parameter":
        del state["physics"]["rls_state"]["ir_k"]["P"]
    else:
        state["lut"][0]["unexpected"] = True
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    invalid_bytes = path.read_bytes()

    with pytest.raises(ModelTransformError):
        transform_model_file(
            path,
            backup_path=tmp_path / "backup.json",
            registered_source_fingerprint=source_fingerprint,
            registered_target_fingerprint="0" * 64,
            settings=TRANSFORM_SETTINGS,
        )

    assert path.read_bytes() == invalid_bytes
    assert not (tmp_path / "backup.json").exists()


def test_unregistered_target_fingerprint_refuses_before_backup_or_write(tmp_path: Path):
    path = tmp_path / "model.json"
    before, source_fingerprint, _state = _legacy_model(path)

    with pytest.raises(ModelTransformError, match="registered target fingerprint mismatch"):
        transform_model_file(
            path,
            backup_path=tmp_path / "backup.json",
            registered_source_fingerprint=source_fingerprint,
            registered_target_fingerprint="0" * 64,
            settings=TRANSFORM_SETTINGS,
        )

    assert path.read_bytes() == before
    assert not (tmp_path / "backup.json").exists()


def test_unregistered_source_fingerprint_refuses_before_backup_or_write(tmp_path: Path):
    path = tmp_path / "model.json"
    before, _source_fingerprint, _state = _legacy_model(path)

    with pytest.raises(ModelTransformError, match="registered Release-A fingerprint mismatch"):
        transform_model_file(
            path,
            backup_path=tmp_path / "backup.json",
            registered_source_fingerprint="f" * 64,
            registered_target_fingerprint="0" * 64,
            settings=TRANSFORM_SETTINGS,
        )

    assert path.read_bytes() == before
    assert not (tmp_path / "backup.json").exists()


def test_conflicting_existing_backup_is_never_overwritten(tmp_path: Path):
    path = tmp_path / "model.json"
    backup = tmp_path / "backup.json"
    before, source_fingerprint, _state = _legacy_model(path)
    target_fingerprint, _target = _registrations(path, source_fingerprint)
    conflicting_backup = b'{"different":"rollback"}'
    backup.write_bytes(conflicting_backup)

    with pytest.raises(ModelTransformError, match="backup conflicts"):
        transform_model_file(
            path,
            backup_path=backup,
            registered_source_fingerprint=source_fingerprint,
            registered_target_fingerprint=target_fingerprint,
            settings=TRANSFORM_SETTINGS,
        )

    assert path.read_bytes() == before
    assert backup.read_bytes() == conflicting_backup


def test_identical_existing_backup_is_verified_and_reused(tmp_path: Path):
    path = tmp_path / "model.json"
    backup = tmp_path / "backup.json"
    before, source_fingerprint, _state = _legacy_model(path)
    target_fingerprint, _target = _registrations(path, source_fingerprint)
    backup.write_bytes(before)
    backup.chmod(0o600)
    original_backup_inode = backup.stat().st_ino

    transform_model_file(
        path,
        backup_path=backup,
        registered_source_fingerprint=source_fingerprint,
        registered_target_fingerprint=target_fingerprint,
        settings=TRANSFORM_SETTINGS,
    )

    assert backup.read_bytes() == before
    assert backup.stat().st_ino == original_backup_inode


def test_post_write_verification_failure_restores_exact_source(tmp_path: Path):
    path = tmp_path / "model.json"
    backup = tmp_path / "backup.json"
    before, source_fingerprint, _state = _legacy_model(path)
    target_fingerprint, _target = _registrations(path, source_fingerprint)

    with (
        patch(
            "src.adapters.model_transform._verify_written_target",
            side_effect=ModelTransformError("injected verification failure"),
        ),
        pytest.raises(ModelTransformError, match="exact source restored"),
    ):
        transform_model_file(
            path,
            backup_path=backup,
            registered_source_fingerprint=source_fingerprint,
            registered_target_fingerprint=target_fingerprint,
            settings=TRANSFORM_SETTINGS,
        )

    assert path.read_bytes() == before
    assert backup.read_bytes() == before


def test_rollback_failure_retains_verified_exact_backup(tmp_path: Path):
    path = tmp_path / "model.json"
    backup = tmp_path / "backup.json"
    before, source_fingerprint, _state = _legacy_model(path)
    target_fingerprint, _target = _registrations(path, source_fingerprint)
    real_atomic_write = model_files.atomic_write_model
    model_write_count = 0

    def fail_second_model_write(target, content, *, mode=0o600):
        nonlocal model_write_count
        if Path(target) == path:
            model_write_count += 1
            if model_write_count == 2:
                raise OSError("injected rollback failure")
        return real_atomic_write(target, content, mode=mode)

    with (
        patch(
            "src.adapters.model_transform._verify_written_target",
            side_effect=ModelTransformError("injected verification failure"),
        ),
        patch(
            "src.adapters.model_state_persistence.atomic_write_model",
            side_effect=fail_second_model_write,
        ),
        pytest.raises(ModelTransformError, match="verified source backup retained"),
    ):
        transform_model_file(
            path,
            backup_path=backup,
            registered_source_fingerprint=source_fingerprint,
            registered_target_fingerprint=target_fingerprint,
            settings=TRANSFORM_SETTINGS,
        )

    assert path.read_bytes() != before
    assert backup.read_bytes() == before


def test_nonblocking_writer_lock_refuses_byte_identically(tmp_path: Path):
    path = tmp_path / "model.json"
    before, source_fingerprint, _state = _legacy_model(path)
    target_fingerprint, _target = _registrations(path, source_fingerprint)
    lock_path = tmp_path / "monitor.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(WriterLockHeld):
            transform_model_file(
                path,
                backup_path=tmp_path / "backup.json",
                registered_source_fingerprint=source_fingerprint,
                registered_target_fingerprint=target_fingerprint,
                settings=TRANSFORM_SETTINGS,
            )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert path.read_bytes() == before
    assert not (tmp_path / "backup.json").exists()


def test_cli_refuses_missing_shutdown_threshold_without_writes(tmp_path: Path) -> None:
    path = tmp_path / "model.json"
    backup = tmp_path / "backup.json"
    before, source_fingerprint, _state = _legacy_model(path)
    target_fingerprint, _target = _registrations(path, source_fingerprint)
    script = Path(__file__).parents[1] / "scripts" / "reparameterize-ir-reference"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--model",
            str(path),
            "--backup",
            str(backup),
            "--source-fingerprint",
            source_fingerprint,
            "--target-fingerprint",
            target_fingerprint,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--shutdown-minutes" in result.stderr
    assert path.read_bytes() == before
    assert not backup.exists()


def test_cli_requires_threshold_and_registered_fingerprints_and_emits_receipt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "model.json"
    backup = tmp_path / "backup.json"
    _before, source_fingerprint, _state = _legacy_model(path)
    target_fingerprint, _target = _registrations(path, source_fingerprint)
    script = Path(__file__).parents[1] / "scripts" / "reparameterize-ir-reference"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--model",
            str(path),
            "--backup",
            str(backup),
            "--source-fingerprint",
            source_fingerprint,
            "--target-fingerprint",
            target_fingerprint,
            "--shutdown-minutes",
            str(TRANSFORM_SHUTDOWN_THRESHOLD_MINUTES),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["source_scientific_fingerprint"] == source_fingerprint
    assert payload["target_scientific_fingerprint"] == target_fingerprint
    assert payload["backup_path"] == str(backup)
    assert (
        payload["equivalence"]["shutdown_threshold_minutes"] == TRANSFORM_SHUTDOWN_THRESHOLD_MINUTES
    )
