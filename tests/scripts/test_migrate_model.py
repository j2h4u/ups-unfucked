import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[2] / "scripts" / "migrate_model.py"
    spec = importlib.util.spec_from_file_location("migrate_model", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> dict[str, object]:
    return {
        "soh": 0.91,
        "soh_history": [],
        "capacity_estimates": [],
        "capacity_ah_measured": None,
        "physics": {
            "peukert_exponent": 1.2,
            "ir_compensation": {"k_volts_per_percent": 0.015, "reference_load_percent": 0.0},
        },
        "lut": [
            {
                "v": 13.7,
                "soc": 1.0,
                "source": "measured",
                "timestamp": 1787356800.0,
            },
            {"v": 12.7, "soc": 0.7, "source": "standard"},
            {"v": 10.8, "soc": 0.0, "source": "anchor"},
        ],
        "r_internal_history": [
            {
                "date": "2026-08-20",
                "r_ohm": 0.1,
                "v_before": 13.2,
                "v_sag": 0.2,
                "load_percent": 20.0,
                "event": "blackout",
            },
            {
                "date": "2026-08-20",
                "r_ohm": 0.1,
                "v_before": 13.2,
                "v_sag": 0.2,
                "load_percent": 20.0,
                "event": "blackout",
            },
        ],
        "battery_install_date": "2026-08-01",
        "battery_epoch_id": "epoch-is-not-migrated",
        "cycle_count": 4,
        "cumulative_on_battery_sec": 12.0,
        "new_battery_detected": False,
        "new_battery_detected_timestamp": None,
        "discharge_events": [],
        "last_upscmd_timestamp": None,
        "last_upscmd_type": None,
        "last_upscmd_status": None,
        "ir_learning_policy": {
            "revision": "ir-learning-v1",
            "deadband_v_per_pp": 0.001,
            "min_k_v_per_pp": 0.005,
            "max_k_v_per_pp": 0.04,
            "max_single_commit_fraction": 0.2,
            "max_epoch_decrease_fraction": 0.5,
            "min_commit_interval_days": 30,
            "max_consumed_step_hashes": 256,
            "battery_epoch_id": "epoch-is-not-migrated",
            "epoch_initial_k_v_per_pp": 0.015,
            "last_commit_utc": None,
            "consumed_step_hashes": [],
        },
    }


def _write_source(root: Path) -> bytes:
    raw = json.dumps(_source(), sort_keys=True, indent=2).encode()
    (root / "model.json").write_bytes(raw)
    return raw


def test_default_dry_run_writes_nothing(tmp_path: Path, capsys) -> None:
    module = _module()
    original = _write_source(tmp_path)

    module.main(["--state-dir", str(tmp_path)])

    assert (tmp_path / "model.json").read_bytes() == original
    assert not (tmp_path / "events").exists()
    assert not (tmp_path / "monitor.lock").exists()
    assert "dry-run" in capsys.readouterr().out


def test_apply_writes_exact_target_and_extracts_unique_history(tmp_path: Path) -> None:
    module = _module()
    _write_source(tmp_path)

    module.main(["--state-dir", str(tmp_path), "--apply"])

    model = json.loads((tmp_path / "model.json").read_text())
    assert set(model) == {"soh", "physics", "lut"}
    assert set(model["physics"]["ir_compensation"]) == {"k_volts_per_percent"}
    assert all(set(point) == {"v", "soc"} for point in model["lut"])
    history = [
        json.loads(line)
        for line in (tmp_path / "events" / "history.jsonl").read_text().splitlines()
    ]
    assert history == [
        {"kind": "battery_installed", "at": "2026-08-01T00:00:00Z"},
        {
            "kind": "ir_observation",
            "at": "2026-08-20T00:00:00Z",
            "r_ohm": 0.1,
            "v_before": 13.2,
            "v_sag": 0.2,
            "load_pct": 20.0,
            "event": "blackout",
        },
        {
            "kind": "lut_observation",
            "at": "2026-08-22T00:00:00Z",
            "v": 13.7,
            "soc": 1.0,
        },
    ]


def test_equivalence_rejects_changed_predictor(tmp_path: Path) -> None:
    module = _module()
    source = _source()
    migrated = module._target_state(source)
    migrated["lut"][0]["soc"] = 0.99

    with pytest.raises(RuntimeError, match="equivalence"):
        module.prove_prediction_equivalence(source, migrated)


def test_source_shape_must_be_exact(tmp_path: Path) -> None:
    module = _module()
    source = _source()
    source["retired_extra"] = True
    (tmp_path / "model.json").write_text(json.dumps(source))

    with pytest.raises(SystemExit, match="2"):
        module.main(["--state-dir", str(tmp_path)])


def test_symlink_model_is_refused(tmp_path: Path) -> None:
    module = _module()
    target = tmp_path / "real-model.json"
    target.write_text(json.dumps(_source()))
    (tmp_path / "model.json").symlink_to(target)

    with pytest.raises(SystemExit, match="2"):
        module.main(["--state-dir", str(tmp_path)])


def test_history_append_failure_leaves_model_untouched(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    original = _write_source(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("injected history failure")

    monkeypatch.setattr(module, "_append_history", fail)
    with pytest.raises(SystemExit, match="2"):
        module.main(["--state-dir", str(tmp_path), "--apply"])

    assert (tmp_path / "model.json").read_bytes() == original
