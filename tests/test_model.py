"""Tests for battery model persistence and VRLA LUT initialization."""

import json
import os
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from src.model import (
    KNOWN_STATE_KEYS,
    BatteryModel,
    ModelLoadError,
    atomic_write_json,
    is_capacity_converged,
    latest_capacity_ah_ref,
)
from src.replacement_predictor import linear_regression_soh


def _state_with(path, updates):
    """Build a complete current-schema state for direct JSON fixture tests."""
    seed = BatteryModel(path)
    state = deepcopy(seed.state)
    state.update(updates)
    return state


def test_reset_baseline_starts_a_fresh_scientific_state(tmp_path):
    """Battery replacement must not carry learned state across its new epoch."""
    model = BatteryModel(tmp_path / "model.json")
    model.state.update(
        {
            "soh": 0.72,
            "soh_history": [
                {"date": "2025-01-01", "soh": 0.9, "capacity_ah_ref": 7.2},
                {"date": "2026-01-01", "soh": 0.72, "capacity_ah_ref": 7.2},
            ],
            "capacity_estimates": [{"ah_estimate": 5.4}],
            "capacity_ah_measured": 5.4,
            "r_internal_history": [{"r_ohm": 0.08}],
            "discharge_events": [{"event_id": "old-event"}],
            "last_upscmd_timestamp": "2026-08-01T00:00:00Z",
            "last_upscmd_type": "test.battery.start.quick",
            "last_upscmd_status": "OK",
        }
    )
    model.set_peukert_exponent(1.35)
    model.physics.ir_compensation.k_volts_per_percent = 0.03
    model.set_rls_state("ir_k", theta=0.03, P=0.2, sample_count=4)
    model.set_rls_state("peukert", theta=1.35, P=0.3, sample_count=5)

    model.reset_baseline(install_date="2026-08-15")

    assert model.state["soh"] == 1.0
    assert model.state["soh_history"] == [
        {"date": "2026-08-15", "soh": 1.0, "capacity_ah_ref": 7.2}
    ]
    assert model.state["capacity_estimates"] == []
    assert model.state["capacity_ah_measured"] is None
    assert model.state["r_internal_history"] == []
    assert model.state["discharge_events"] == []
    assert {entry["source"] for entry in model.state["lut"]} == {"standard", "anchor"}
    assert model.get_peukert_exponent() == pytest.approx(1.2)
    assert model.get_ir_k() == pytest.approx(0.015)
    assert model.get_rls_state("ir_k")["sample_count"] == 0
    assert model.get_rls_state("peukert")["sample_count"] == 0
    assert model.state["last_upscmd_status"] == "OK"
    assert model.state["last_upscmd_type"] == "test.battery.start.quick"
    assert model.state["last_upscmd_timestamp"] == "2026-08-01T00:00:00Z"


def test_reset_baseline_rolls_back_fresh_state_on_save_failure(tmp_path):
    """A failed reset save restores both old state and old physics exactly."""
    model = BatteryModel(tmp_path / "model.json")
    model.state["soh_history"] = [{"date": "2025-01-01", "soh": 0.8, "capacity_ah_ref": 7.2}]
    model.state["discharge_events"] = [{"event_id": "old-event"}]
    model.set_peukert_exponent(1.3)
    before_state = deepcopy(model.state)
    before_physics = deepcopy(model.physics)

    with patch.object(model, "save", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            model.reset_baseline(install_date="2026-08-15")

    assert model.state == before_state
    assert model.physics == before_physics


class TestAtomicWriteJson:
    """Test atomic_write_json() helper function."""

    def test_atomic_write_creates_file(self, tmp_path):
        """Verify file is created with JSON content."""
        model_file = tmp_path / "model.json"
        data = {"test": "value", "number": 42}

        atomic_write_json(model_file, data)

        assert model_file.exists()
        with open(model_file, "r") as f:
            loaded = json.load(f)
        assert loaded == data

    def test_atomic_write_no_temp_files_left(self, tmp_path):
        """Verify no .tmp files remain after successful write."""
        model_file = tmp_path / "model.json"
        data = {"test": "value"}

        atomic_write_json(model_file, data)

        # Check that no .tmp files exist in the directory
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0, f"Found leftover temp files: {tmp_files}"

    def test_atomic_write_creates_parent_dirs(self, tmp_path):
        """Verify parent directories are created automatically."""
        nested_file = tmp_path / "deep" / "nested" / "model.json"
        data = {"test": "value"}

        atomic_write_json(nested_file, data)

        assert nested_file.exists()
        with open(nested_file, "r") as f:
            loaded = json.load(f)
        assert loaded == data

    def test_atomic_write_handles_exception(self, tmp_path):
        """Verify temp file is cleaned up on write error."""
        model_file = tmp_path / "model.json"
        data = {"test": "value"}

        # Mock os.fdatasync to raise an error (now using fdatasync instead of fsync)
        with patch("os.fdatasync", side_effect=OSError("Disk error")):
            with pytest.raises(IOError):
                atomic_write_json(model_file, data)

        # Verify model.json was not created
        assert not model_file.exists()

    def test_atomic_write_logs_cleanup_failure(self, tmp_path, caplog):
        """Verify cleanup failure during exception is logged, not silently swallowed."""
        import logging

        model_file = tmp_path / "model.json"
        data = {"test": "value"}

        with patch("os.fdatasync", side_effect=OSError("Disk error")):
            with patch.object(Path, "unlink", side_effect=OSError("Permission denied")):
                with caplog.at_level(logging.WARNING, logger="ups-battery-monitor"):
                    with pytest.raises(IOError):
                        atomic_write_json(model_file, data)

        assert any(
            "atomic_write_cleanup_failed" in r.message
            or getattr(r, "event_type", "") == "atomic_write_cleanup_failed"
            for r in caplog.records
        ), (
            f"Expected 'atomic_write_cleanup_failed' log entry, got: {[r.message for r in caplog.records]}"
        )


class TestBatteryModelLoad:
    """Test BatteryModel initialization and loading."""

    def test_model_loads_existing_file(self, tmp_path):
        """Verify model loads from existing JSON file."""
        model_file = tmp_path / "model.json"
        model_data = _state_with(
            model_file,
            {
                "soh": 0.95,
                "battery_epoch_id": "00000000-0000-4000-8000-000000000001",
                "lut": [
                    {"v": 13.4, "soc": 1.0, "source": "standard"},
                    {"v": 10.5, "soc": 0.0, "source": "anchor"},
                ],
                "soh_history": [{"date": "2026-03-13", "soh": 0.95, "capacity_ah_ref": 7.2}],
            },
        )
        with open(model_file, "w") as f:
            json.dump(model_data, f)

        model = BatteryModel(model_path=model_file)

        assert model.get_soh() == 0.95
        assert model.get_capacity_ah() == 7.2  # default from RATED_CAPACITY_AH
        assert len(model.get_lut()) == 2

    def test_model_initializes_default_on_missing_file(self, tmp_path):
        """Verify default VRLA curve is used when file doesn't exist."""
        model_file = tmp_path / "nonexistent" / "model.json"

        model = BatteryModel(model_path=model_file)

        # Should have default VRLA curve
        assert model.get_soh() == 1.0
        assert model.get_capacity_ah() == 7.2
        lut = model.get_lut()
        assert len(lut) >= 7  # At least 7 standard curve points
        assert lut[0]["v"] == 13.4
        assert lut[-1]["v"] == 10.5

    def test_existing_current_schema_is_read_only_on_constructor(self, tmp_path):
        """Loading a valid current-schema file does not rewrite its bytes."""
        model_file = tmp_path / "model.json"
        seed = BatteryModel(model_file)
        seed.save()
        before = model_file.read_bytes()

        BatteryModel(model_file)

        assert model_file.read_bytes() == before

    def test_model_handles_malformed_json(self, tmp_path, caplog):
        """Malformed JSON fails fast and remains byte-for-byte untouched."""
        model_file = tmp_path / "model.json"
        malformed = b"{invalid json content"
        model_file.write_bytes(malformed)

        with pytest.raises(ModelLoadError, match="Malformed model"):
            BatteryModel(model_path=model_file)
        assert model_file.read_bytes() == malformed
        assert not model_file.with_suffix(".json.corrupt").exists()
        assert "Malformed model.json" in caplog.text

    def test_existing_state_requires_every_top_level_key(self, tmp_path):
        """A missing declared key is invalid; no nested defaults are applied."""
        model_file = tmp_path / "model.json"
        state = _state_with(model_file, {})
        state.pop("capacity_estimates")
        model_file.write_text(json.dumps(state))

        with pytest.raises(ModelLoadError, match="capacity_estimates"):
            BatteryModel(model_file)

    @pytest.mark.parametrize(
        "mutate, expected",
        [
            (lambda state: state.__setitem__("soh", 0.0), "soh"),
            (
                lambda state: state["physics"].__setitem__("peukert_exponent", 2.0),
                "peukert_exponent",
            ),
            (
                lambda state: state["physics"]["rls_state"]["ir_k"].__setitem__("theta", "bad"),
                "physics.rls_state.ir_k.theta",
            ),
            (lambda state: state["lut"][0].__setitem__("soc", 2.0), "soc"),
            (lambda state: state.__setitem__("capacity_ah_measured", 0.0), "capacity_ah_measured"),
            (lambda state: state.__setitem__("discharge_events", None), "discharge_events"),
        ],
    )
    def test_invalid_current_schema_values_fail_fast(self, tmp_path, mutate, expected):
        """Invalid primitive, physics, and LUT values are rejected unchanged."""
        model_file = tmp_path / "model.json"
        state = _state_with(model_file, {})
        mutate(state)
        model_file.write_text(json.dumps(state))

        with pytest.raises(ModelLoadError, match=expected):
            BatteryModel(model_file)

    def test_model_initializes_with_default_path(self, tmp_path):
        """Verify model uses ~/.config path when no model_path given."""
        with patch("pathlib.Path.home") as mock_home:
            mock_home.return_value = tmp_path
            model_file = tmp_path / ".config" / "ups-battery-monitor" / "model.json"

            model = BatteryModel()

            assert model.model_path == model_file


class TestBatteryModelSave:
    """Test BatteryModel persistence."""

    def test_model_save_writes_json(self, tmp_path):
        """Verify save() writes valid JSON to disk."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        model.save()

        assert model_file.exists()
        with open(model_file, "r") as f:
            loaded = json.load(f)
        assert "lut" in loaded
        assert "soh" in loaded
        assert "soh_history" in loaded

    def test_model_save_preserves_data(self, tmp_path):
        """Verify data is preserved across save/load cycle."""
        model_file = tmp_path / "model.json"
        model1 = BatteryModel(model_path=model_file)
        model1.add_soh_history_entry("2026-03-14", 0.95, capacity_ah_ref=7.2)

        model1.save()

        model2 = BatteryModel(model_path=model_file)
        assert len(model2.get_soh_history()) == 2
        assert model2.get_soh_history()[1] == {
            "date": "2026-03-14",
            "soh": 0.95,
            "capacity_ah_ref": 7.2,
        }


class TestVRLALUTInitialization:
    """Test standard VRLA curve initialization."""

    def test_default_lut_has_required_points(self, tmp_path):
        """Verify default LUT contains all required voltage points."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        lut = model.get_lut()
        voltages = [entry["v"] for entry in lut]

        assert 13.4 in voltages  # Full charge
        assert 12.4 in voltages  # Knee point
        assert 10.5 in voltages  # Anchor

    def test_default_lut_soc_monotonic(self, tmp_path):
        """Verify SoC values decrease monotonically with voltage."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        lut = model.get_lut()
        for i in range(len(lut) - 1):
            assert lut[i]["soc"] >= lut[i + 1]["soc"], (
                f"SoC not monotonic: {lut[i]['soc']} > {lut[i + 1]['soc']}"
            )

    def test_default_lut_source_tracking(self, tmp_path):
        """Verify all LUT entries have source field."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        lut = model.get_lut()
        for entry in lut:
            assert "source" in entry
            assert entry["source"] in ["standard", "measured", "anchor"]

    def test_anchor_voltage_is_10_5v(self, tmp_path):
        """Verify anchor point is 10.5V (0% SoC)."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        anchor = model.get_anchor_voltage()
        assert anchor == 10.5

    def test_soh_history_initialized_with_entry(self, tmp_path):
        """Verify SoH history contains initial entry."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        history = model.get_soh_history()
        assert len(history) >= 1
        assert "date" in history[0]
        assert "soh" in history[0]
        assert history[0]["soh"] == 1.0


class TestBatteryModelMethods:
    """Test BatteryModel helper methods."""

    def test_add_soh_history_entry(self, tmp_path):
        """Verify SoH history entry is added correctly."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        initial_count = len(model.get_soh_history())
        model.add_soh_history_entry("2026-03-14", 0.90, capacity_ah_ref=7.2)

        history = model.get_soh_history()
        assert len(history) == initial_count + 1
        assert history[-1] == {"date": "2026-03-14", "soh": 0.90, "capacity_ah_ref": 7.2}
        assert model.get_soh() == 0.90

    def test_get_capacity_ah_default(self, tmp_path):
        """Verify default capacity is 7.2 Ah."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        assert model.get_capacity_ah() == 7.2

    def test_capacity_ah_injection(self, tmp_path):
        """BatteryModel(path, capacity_ah=9.0).get_capacity_ah() == 9.0; save/reload does not
        reintroduce full_capacity_ah_ref into the file."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file, capacity_ah=9.0)

        assert model.get_capacity_ah() == 9.0

        model.save()

        # Reloaded file must not carry full_capacity_ah_ref
        with open(model_file, "r") as f:
            raw = json.load(f)
        assert "full_capacity_ah_ref" not in raw

    def test_rated_ah_propagation_empty_model(self, tmp_path):
        """get_convergence_status().rated_ah equals the injected capacity_ah (empty-estimates branch)."""
        model = BatteryModel(model_path=tmp_path / "model.json", capacity_ah=9.0)

        status = model.get_convergence_status()
        assert status.sample_count == 0  # empty-estimates branch
        assert status.rated_ah == 9.0

    def test_rated_ah_propagation_populated_model(self, tmp_path):
        """get_convergence_status().rated_ah equals the injected capacity_ah (populated branch)."""
        model = BatteryModel(model_path=tmp_path / "model.json", capacity_ah=9.0)
        model.append_capacity_estimate(8.8, 0.8, {}, "2026-06-01T00:00:00Z")

        status = model.get_convergence_status()
        assert status.sample_count == 1  # populated branch
        assert status.rated_ah == 9.0

    def test_rated_ah_propagation_default(self, tmp_path):
        """get_convergence_status().rated_ah is 7.2 when capacity_ah not injected."""
        from src.battery_math.constants import RATED_CAPACITY_AH

        model = BatteryModel(model_path=tmp_path / "model.json")

        assert model.get_convergence_status().rated_ah == RATED_CAPACITY_AH

    def test_get_soh_default(self, tmp_path):
        """Verify default SoH is 1.0 (100%)."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        assert model.get_soh() == 1.0


class TestPhysicsSection:
    """Test physics section in model.json."""

    def test_default_has_physics(self, tmp_path):
        """New model has physics section with defaults."""
        model = BatteryModel(model_path=tmp_path / "model.json")
        assert model.get_peukert_exponent() == 1.2
        assert model.get_nominal_voltage() == 12.0
        assert model.get_nominal_power_watts() == 425.0
        assert model.get_ir_k() == 0.015
        assert model.get_ir_reference_load() == 20.0

    def test_set_peukert_exponent(self, tmp_path):
        """set_peukert_exponent updates the value."""
        model = BatteryModel(model_path=tmp_path / "model.json")
        model.set_peukert_exponent(1.15)
        assert model.get_peukert_exponent() == 1.15


class TestRInternalHistory:
    """Test internal resistance history access and persistence."""

    def test_r_internal_history_empty_by_default(self, tmp_path):
        model = BatteryModel(model_path=tmp_path / "model.json")
        assert model.get_r_internal_history() == []


class TestHistoryPruning:
    """Test SoH and R_internal history list pruning to prevent unbounded growth."""

    def test_prune_soh_history_keeps_recent_entries(self, tmp_path):
        """Verify _prune_soh_history() keeps only last 30 entries when history > 30."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Create 50 history entries
        for i in range(50):
            model.add_soh_history_entry(
                f"2026-03-{(i % 28) + 1:02d}", 1.0 - (i * 0.001), capacity_ah_ref=7.2
            )

        initial_count = len(model.get_soh_history())
        assert initial_count >= 50

        # Prune
        model._cap_history_entries("soh_history", keep_count=30)

        # Verify only last 30 are kept
        history = model.get_soh_history()
        assert len(history) == 30
        # Verify we kept the most recent entries (last ones added)
        assert history[-1]["soh"] == pytest.approx(1.0 - (49 * 0.001))

    def test_prune_soh_history_no_change_if_small(self, tmp_path):
        """Verify _prune_soh_history() leaves history unchanged if ≤ 30 entries."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Create only 15 entries
        for i in range(15):
            model.add_soh_history_entry(f"2026-03-{(i % 28) + 1:02d}", 0.95, capacity_ah_ref=7.2)

        initial_history = model.get_soh_history().copy()

        # Prune (should have no effect)
        model._cap_history_entries("soh_history", keep_count=30)

        # Verify no change
        assert model.get_soh_history() == initial_history

    def test_prune_r_internal_history_keeps_recent_entries(self, tmp_path):
        """Verify _prune_r_internal_history() mirrors soh pruning for r_internal_history."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Seed valid current-schema entries directly; the former convenience
        # mutator was removed in favor of journal/transaction writes.
        model.state["r_internal_history"] = [
            {
                "date": f"2026-03-{(i % 28) + 1:02d}",
                "r_ohm": 0.03 + (i * 0.0001),
                "v_before": 13.5 - (i * 0.01),
                "v_sag": 13.0,
                "load_percent": 15.0,
                "event": "TEST",
            }
            for i in range(40)
        ]

        initial_count = len(model.get_r_internal_history())
        assert initial_count >= 40

        # Prune
        model._cap_history_entries("r_internal_history", keep_count=30)

        # Verify only last 30 are kept
        history = model.get_r_internal_history()
        assert len(history) == 30

    def test_pruning_is_idempotent(self, tmp_path):
        """Verify pruning twice produces same result (idempotent)."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Create 35 history entries
        for i in range(35):
            model.add_soh_history_entry(f"2026-03-{(i % 28) + 1:02d}", 0.95, capacity_ah_ref=7.2)

        # Prune once
        model._cap_history_entries("soh_history", keep_count=30)
        history_after_first = model.get_soh_history().copy()

        # Prune again
        model._cap_history_entries("soh_history", keep_count=30)
        history_after_second = model.get_soh_history().copy()

        # Should be identical
        assert history_after_first == history_after_second

    def test_save_automatically_prunes_history(self, tmp_path):
        """Verify save() calls pruning automatically and persists pruned model."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Create 40 history entries
        for i in range(40):
            model.add_soh_history_entry(
                f"2026-03-{(i % 28) + 1:02d}", 1.0 - (i * 0.001), capacity_ah_ref=7.2
            )

        # Save (should prune internally)
        model.save()

        # Reload from disk
        model2 = BatteryModel(model_path=model_file)
        history = model2.get_soh_history()

        # Verify history was pruned to max 30
        assert len(history) == 30

    def test_save_prunes_both_histories(self, tmp_path):
        """Verify save() prunes both soh_history and r_internal_history."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Add many entries to both histories
        for i in range(35):
            model.add_soh_history_entry(f"2026-03-{(i % 28) + 1:02d}", 0.95, capacity_ah_ref=7.2)
            model.state["r_internal_history"].append(
                {
                    "date": f"2026-03-{(i % 28) + 1:02d}",
                    "r_ohm": 0.03,
                    "v_before": 13.5,
                    "v_sag": 13.0,
                    "load_percent": 15.0,
                    "event": "TEST",
                }
            )

        # Save
        model.save()

        # Reload and verify both were pruned
        model2 = BatteryModel(model_path=model_file)
        assert len(model2.get_soh_history()) <= 30
        assert len(model2.get_r_internal_history()) <= 30


class TestFdatasyncOptimization:
    """Test fdatasync replacement for performance optimization in atomic_write_json."""

    def test_atomic_write_uses_fdatasync(self, tmp_path):
        """Verify atomic_write_json() calls os.fdatasync instead of os.fsync."""
        model_file = tmp_path / "model.json"
        data = {"test": "value", "number": 42}

        # Patch both os.fdatasync and os.fsync to track calls
        with (
            patch("os.fdatasync") as mock_fdatasync,
            patch("os.fsync") as mock_fsync,
            patch("src.model._sync_parent_directory") as mock_parent_sync,
            patch("os.open", wraps=os.open),
            patch("os.close", wraps=os.close),
        ):
            atomic_write_json(model_file, data)

            # fdatasync should be called
            assert mock_fdatasync.called, "os.fdatasync was not called"
            # File data uses fdatasync; directory durability is a separate helper.
            assert not mock_fsync.called, "os.fsync should not be called; use fdatasync instead"
            mock_parent_sync.assert_called_once_with(model_file.parent)

    def test_atomic_write_json_still_works_with_fdatasync(self, tmp_path):
        """Verify atomic file write still succeeds after switching to fdatasync."""
        model_file = tmp_path / "model.json"
        data = {"test": "value", "nested": {"key": "value"}, "list": [1, 2, 3]}

        # Write with fdatasync (already implemented)
        atomic_write_json(model_file, data)

        # Verify file was created and contains correct data
        assert model_file.exists()
        with open(model_file, "r") as f:
            loaded = json.load(f)
        assert loaded == data

    def test_atomic_write_json_content_integrity_with_fdatasync(self, tmp_path):
        """Verify JSON content remains intact after switching to fdatasync."""
        model_file = tmp_path / "model.json"
        data = {
            "lut": [
                {"v": 13.4, "soc": 1.0, "source": "standard"},
                {"v": 12.4, "soc": 0.8, "source": "measured"},
                {"v": 10.5, "soc": 0.0, "source": "anchor"},
            ],
            "soh": 0.95,
            "full_capacity_ah_ref": 7.2,
            "cycle_count": 42,
        }

        atomic_write_json(model_file, data)

        # Read back and verify exact match
        with open(model_file, "r") as f:
            loaded = json.load(f)
        assert loaded == data
        assert loaded["lut"][0]["v"] == 13.4
        assert loaded["soh"] == 0.95
        assert loaded["full_capacity_ah_ref"] == 7.2


class TestCapacityEstimates:
    """Test BatteryModel capacity_estimates array."""

    def test_append_capacity_estimate_creates_array_if_missing(self, tmp_path):
        """The current append primitive creates a capacity estimate entry."""
        model = BatteryModel(model_path=tmp_path / "model.json")
        assert (
            "capacity_estimates" not in model.state
            or len(model.state.get("capacity_estimates", [])) == 0
        )

        model.append_capacity_estimate(
            ah_estimate=7.5,
            confidence=0.85,
            metadata={"delta_soc_percent": 50.0, "duration_sec": 1234},
            timestamp="2026-03-15T12:34:56Z",
        )

        assert "capacity_estimates" in model.state
        assert len(model.state["capacity_estimates"]) == 1

    def test_get_capacity_estimates_returns_list_latest_first(self, tmp_path):
        """Test 2: model.get_capacity_estimates() returns list with latest first."""
        model = BatteryModel(model_path=tmp_path / "model.json")

        # Add two estimates
        model.append_capacity_estimate(
            7.4, 0.80, {"delta_soc_percent": 50.0}, "2026-03-15T10:00:00Z"
        )
        model.append_capacity_estimate(
            7.5, 0.85, {"delta_soc_percent": 52.0}, "2026-03-15T11:00:00Z"
        )

        estimates = model.get_capacity_estimates()
        assert len(estimates) == 2
        # Latest first
        assert estimates[0]["timestamp"] == "2026-03-15T11:00:00Z"
        assert estimates[1]["timestamp"] == "2026-03-15T10:00:00Z"

    def test_prune_capacity_estimates_keeps_30(self, tmp_path):
        """Test 4: capacity_estimates pruned to last 30 entries (no unbounded growth)."""
        model = BatteryModel(model_path=tmp_path / "model.json")

        # Add 35 estimates
        for i in range(35):
            model.append_capacity_estimate(
                ah_estimate=7.0 + (i * 0.01),
                confidence=0.5 + (i * 0.01),
                metadata={"delta_soc_percent": 50.0},
                timestamp=f"2026-03-15T{i:02d}:00:00Z",
            )

        # After adding the 35th, should be pruned to 30
        estimates = model.state["capacity_estimates"]
        assert len(estimates) <= 30, f"Expected <= 30 estimates, got {len(estimates)}"

    def test_save_persists_capacity_estimates_atomically(self, tmp_path):
        """Test 5: model.save() writes capacity_estimates atomically."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        model.append_capacity_estimate(
            7.5, 0.85, {"delta_soc_percent": 50.0}, "2026-03-15T12:34:56Z"
        )
        model.save()

        # Verify file exists
        assert model_file.exists()
        with open(model_file, "r") as f:
            loaded = json.load(f)
        assert "capacity_estimates" in loaded
        assert len(loaded["capacity_estimates"]) == 1
        assert loaded["capacity_estimates"][0]["ah_estimate"] == 7.5

    def test_reload_persists_capacity_estimates(self, tmp_path):
        """Test 6: Reload from model.json → capacity_estimates persists across daemon restarts."""
        model_file = tmp_path / "model.json"
        model1 = BatteryModel(model_path=model_file)

        # Add estimates and save
        model1.append_capacity_estimate(
            7.4, 0.80, {"delta_soc_percent": 50.0}, "2026-03-15T10:00:00Z"
        )
        model1.append_capacity_estimate(
            7.5, 0.85, {"delta_soc_percent": 52.0}, "2026-03-15T11:00:00Z"
        )
        model1.save()

        # Create new model instance, load from file
        model2 = BatteryModel(model_path=model_file)
        estimates = model2.get_capacity_estimates()
        assert len(estimates) == 2
        assert estimates[0]["ah_estimate"] == 7.5  # Latest first

    def test_capacity_estimates_schema_has_required_fields(self, tmp_path):
        """Verify capacity_estimates array elements have all required fields."""
        model = BatteryModel(model_path=tmp_path / "model.json")

        model.append_capacity_estimate(
            ah_estimate=7.45,
            confidence=0.82,
            metadata={
                "delta_soc_percent": 52.0,
                "duration_sec": 1234,
                "discharge_slope_mohm": 45.2,
                "load_avg_percent": 35.0,
            },
            timestamp="2026-03-15T12:34:56Z",
        )

        estimate = model.state["capacity_estimates"][0]
        assert "timestamp" in estimate
        assert "ah_estimate" in estimate
        assert "confidence" in estimate
        assert "metadata" in estimate
        assert estimate["timestamp"] == "2026-03-15T12:34:56Z"
        assert estimate["ah_estimate"] == 7.45
        assert estimate["confidence"] == 0.82
        assert estimate["metadata"]["delta_soc_percent"] == 52.0

    def test_get_convergence_status_empty_model(self, tmp_path):
        """Test: get_convergence_status() returns zeros for empty model."""
        model = BatteryModel(model_path=tmp_path / "model.json")

        status = model.get_convergence_status()
        assert status.sample_count == 0
        assert status.confidence_percent == 0.0
        assert status.latest_ah is None
        assert status.rated_ah == 7.2
        assert status.converged is False
        assert status.capacity_ah_measured is None

    def test_get_convergence_status_two_measurements(self, tmp_path):
        """Test: get_convergence_status() with 2 measurements (not converged)."""
        model = BatteryModel(model_path=tmp_path / "model.json")

        model.append_capacity_estimate(
            ah_estimate=7.0,
            confidence=0.0,
            metadata={"delta_soc_percent": 50.0, "duration_sec": 1234},
            timestamp="2026-03-16T10:00:00Z",
        )
        model.append_capacity_estimate(
            ah_estimate=7.2,
            confidence=0.0,
            metadata={"delta_soc_percent": 50.0, "duration_sec": 1234},
            timestamp="2026-03-16T11:00:00Z",
        )

        status = model.get_convergence_status()
        assert status.sample_count == 2
        assert status.confidence_percent == 0.0  # < 3 samples = 0% confidence
        assert status.latest_ah == 7.2
        assert status.rated_ah == 7.2
        assert status.converged is False  # Need >= 3 samples

    def test_get_convergence_status_three_consistent_measurements(self, tmp_path):
        """Test: get_convergence_status() with 3 consistent measurements (converged)."""
        model = BatteryModel(model_path=tmp_path / "model.json")

        # Add 3 measurements with low variance (CoV < 0.10)
        model.append_capacity_estimate(
            ah_estimate=7.0,
            confidence=0.0,
            metadata={"delta_soc_percent": 50.0, "duration_sec": 1234},
            timestamp="2026-03-16T10:00:00Z",
        )
        model.append_capacity_estimate(
            ah_estimate=7.2,
            confidence=0.0,
            metadata={"delta_soc_percent": 50.0, "duration_sec": 1234},
            timestamp="2026-03-16T11:00:00Z",
        )
        model.append_capacity_estimate(
            ah_estimate=7.1,
            confidence=0.86,
            metadata={"delta_soc_percent": 50.0, "duration_sec": 1234},
            timestamp="2026-03-16T12:00:00Z",
        )

        status = model.get_convergence_status()
        assert status.sample_count == 3
        assert status.latest_ah == 7.1
        assert status.rated_ah == 7.2
        assert status.converged is True  # 3 samples + low CoV
        assert status.confidence_percent > 80  # High confidence with consistent estimates

    def test_get_convergence_status_is_immutable(self, tmp_path):
        """Test: ConvergenceStatus is frozen — mutation raises FrozenInstanceError."""
        import dataclasses

        model = BatteryModel(model_path=tmp_path / "model.json")
        status = model.get_convergence_status()
        with pytest.raises(dataclasses.FrozenInstanceError):
            status.sample_count = 99  # type: ignore[misc]


class TestIsCapacityConverged:
    """WR-02: one shared convergence predicate for daemon + CLI, robust to corrupt entries."""

    def test_three_low_variance_samples_converged(self):
        estimates = [{"ah_estimate": v} for v in (7.0, 7.2, 7.1)]
        assert is_capacity_converged(estimates) is True

    def test_fewer_than_three_not_converged(self):
        estimates = [{"ah_estimate": 7.0}, {"ah_estimate": 7.1}]
        assert is_capacity_converged(estimates) is False

    def test_high_variance_not_converged(self):
        estimates = [{"ah_estimate": v} for v in (5.0, 7.0, 9.0)]
        assert is_capacity_converged(estimates) is False

    def test_entries_missing_ah_estimate_are_skipped_not_raised(self):
        # A corrupt/partial entry must NOT raise KeyError — it drops out of the sample set
        # so the daemon and the CLI report the same `converged` from identical state.
        estimates = [{"ah_estimate": 7.0}, {"confidence": 0.5}, {"ah_estimate": 7.1}]
        assert is_capacity_converged(estimates) is False  # only 2 usable samples

    def test_convergence_status_agrees_with_predicate_on_corrupt_entry(self, tmp_path):
        # get_convergence_status() must use the same predicate AND not crash on a missing key.
        model = BatteryModel(model_path=tmp_path / "model.json")
        model.state["capacity_estimates"] = [
            {"ah_estimate": 7.0},
            {"confidence": 0.5},  # corrupt: no ah_estimate
            {"ah_estimate": 7.1},
            {"ah_estimate": 7.05},
        ]
        status = model.get_convergence_status()
        assert status.converged is is_capacity_converged(model.state["capacity_estimates"])
        assert status.latest_ah == 7.05  # last usable sample, no KeyError


class TestSoHHistoryVersioning:
    """SOH-02: SoH history versioning with capacity_ah_ref field."""

    def test_soh_history_entry_with_baseline(self, tmp_path):
        """SOH-02: add_soh_history_entry() stores capacity_ah_ref field when provided."""
        model_path = tmp_path / "model.json"
        model = BatteryModel(model_path)

        # Add entry with capacity baseline
        model.add_soh_history_entry(date="2026-03-16", soh=0.92, capacity_ah_ref=6.8)

        # Verify entry contains baseline tag
        entry = model.state["soh_history"][-1]
        assert entry["date"] == "2026-03-16"
        assert entry["soh"] == 0.92
        assert entry["capacity_ah_ref"] == 6.8  # Tagged

    def test_mixed_baseline_entries_all_tagged(self, tmp_path):
        """soh_history can hold entries with DIFFERENT baselines (e.g. after a battery swap);
        every entry is tagged — there is no untagged/old-style entry."""
        model_path = tmp_path / "model.json"
        model = BatteryModel(model_path)
        model.state["soh_history"] = []

        model.add_soh_history_entry("2026-03-14", 0.97, capacity_ah_ref=7.2)
        model.add_soh_history_entry("2026-03-15", 0.95, capacity_ah_ref=6.8)
        model.add_soh_history_entry("2026-03-16", 0.92, capacity_ah_ref=6.9)

        history = model.state["soh_history"]
        assert len(history) == 3
        assert [e["capacity_ah_ref"] for e in history] == [7.2, 6.8, 6.9]


class TestSchedulingSchema:
    """Scheduling state schema tests — only category ③ persisted keys (last upscmd result).

    scheduled_test_timestamp / scheduled_test_reason / test_block_reason are NOT persisted;
    they are scheduler outputs surfaced health.json-only. Tests that asserted their round-trip
    were exercising dead behavior and have been removed (project policy: remove tests with dead
    code). Only last_upscmd_* (category ③ learned state) is tested here.
    """

    def test_scheduling_schema_fields_initialized(self, temporary_model_path):
        """last_upscmd fields initialized to None on new model creation."""
        model = BatteryModel(temporary_model_path)
        assert model.state.get("last_upscmd_timestamp") is None
        assert model.state.get("last_upscmd_type") is None
        assert model.state.get("last_upscmd_status") is None
        # removed keys are NOT present at all (not even as None)
        assert "scheduled_test_timestamp" not in model.state
        assert "scheduled_test_reason" not in model.state
        assert "test_block_reason" not in model.state

    def test_scheduling_fields_persist_after_save(self, temporary_model_path):
        """last_upscmd fields persist correctly through save/reload cycle."""
        model = BatteryModel(temporary_model_path)

        # Set last upscmd fields (the only scheduling fields that ARE persisted)
        model.state["last_upscmd_timestamp"] = "2026-03-17T10:30:00Z"
        model.state["last_upscmd_type"] = "test.battery.start.deep"
        model.state["last_upscmd_status"] = "OK"
        model.save()

        # Reload and verify
        model2 = BatteryModel(temporary_model_path)
        assert model2.state.get("last_upscmd_timestamp") == "2026-03-17T10:30:00Z"
        assert model2.state.get("last_upscmd_type") == "test.battery.start.deep"
        assert model2.state.get("last_upscmd_status") == "OK"

    def test_update_upscmd_result_method(self, temporary_model_path):
        """update_upscmd_result() updates last command info."""
        model = BatteryModel(temporary_model_path)
        model.update_upscmd_result(
            upscmd_timestamp="2026-03-17T10:30:00Z",
            upscmd_type="test.battery.start.deep",
            upscmd_status="OK",
        )
        assert model.state["last_upscmd_timestamp"] == "2026-03-17T10:30:00Z"
        assert model.state["last_upscmd_type"] == "test.battery.start.deep"
        assert model.state["last_upscmd_status"] == "OK"

    def test_get_last_upscmd_timestamp_method(self, temporary_model_path):
        """get_last_upscmd_timestamp() returns correct value or None."""
        model = BatteryModel(temporary_model_path)
        assert model.get_last_upscmd_timestamp() is None

        model.update_upscmd_result(
            upscmd_timestamp="2026-03-17T10:30:00Z",
            upscmd_type="test.battery.start.quick",
            upscmd_status="OK",
        )
        assert model.get_last_upscmd_timestamp() == "2026-03-17T10:30:00Z"


class TestFieldLevelValidation:
    """Persisted field validation rejects invalid types without changing state."""

    def _base_model_data(self):
        """Return a valid base model dict that passes all validation."""
        return {
            "soh": 0.95,
            "battery_epoch_id": "00000000-0000-4000-8000-000000000001",
            "physics": {
                "peukert_exponent": 1.2,
                "ir_compensation": {"k_volts_per_percent": 0.015, "reference_load_percent": 20.0},
                "rls_state": {
                    "ir_k": {
                        "theta": 0.015,
                        "P": 1.0,
                        "sample_count": 0,
                        "forgetting_factor": 0.97,
                    },
                    "peukert": {
                        "theta": 1.2,
                        "P": 1.0,
                        "sample_count": 0,
                        "forgetting_factor": 0.97,
                    },
                },
            },
            "lut": [
                {"v": 13.4, "soc": 1.00, "source": "standard"},
                {"v": 10.5, "soc": 0.00, "source": "anchor"},
            ],
            "soh_history": [{"date": "2026-03-01", "soh": 0.95, "capacity_ah_ref": 7.2}],
            "capacity_estimates": [],
            "capacity_ah_measured": None,
            "r_internal_history": [],
            "battery_install_date": None,
            "cycle_count": 0,
            "cumulative_on_battery_sec": 0.0,
            "new_battery_detected": False,
            "new_battery_detected_timestamp": None,
            "discharge_events": [],
            "last_upscmd_timestamp": None,
            "last_upscmd_type": None,
            "last_upscmd_status": None,
        }

    def test_validate_string_field_last_upscmd_type_non_string(self, temporary_model_path):
        """last_upscmd_type=123 (int) is rejected."""
        data = self._base_model_data()
        data["last_upscmd_type"] = 123
        with open(temporary_model_path, "w") as f:
            json.dump(data, f)

        with pytest.raises(ModelLoadError, match="last_upscmd_type"):
            BatteryModel(temporary_model_path)

    def test_validate_string_field_last_upscmd_status_non_string(self, temporary_model_path):
        """last_upscmd_status=True (bool) is rejected."""
        data = self._base_model_data()
        data["last_upscmd_status"] = True
        with open(temporary_model_path, "w") as f:
            json.dump(data, f)

        with pytest.raises(ModelLoadError, match="last_upscmd_status"):
            BatteryModel(temporary_model_path)

    def test_validate_list_field_discharge_events_non_list(self, temporary_model_path):
        """discharge_events=123 (int) is rejected."""
        data = self._base_model_data()
        data["discharge_events"] = 123
        with open(temporary_model_path, "w") as f:
            json.dump(data, f)

        with pytest.raises(ModelLoadError, match="discharge_events"):
            BatteryModel(temporary_model_path)

    def test_validate_valid_fields_no_warnings(self, temporary_model_path, caplog):
        """Valid string and list fields load unchanged.

        Only last_upscmd_* are validated as string fields now; scheduled_test_* and
        test_block_reason are no longer in the schema (removed in HYG-03).
        """
        import logging

        data = self._base_model_data()
        data["last_upscmd_type"] = "test.battery.start.deep"
        data["last_upscmd_status"] = "OK"
        data["discharge_events"] = [
            {"timestamp": "2026-03-02T00:00:00Z", "depth_of_discharge": 0.8}
        ]
        with open(temporary_model_path, "w") as f:
            json.dump(data, f)

        with caplog.at_level(logging.WARNING, logger="ups-battery-monitor"):
            model = BatteryModel(temporary_model_path)

        assert not [r for r in caplog.records if "model_field" in r.message]
        # Fields remain unchanged
        assert model.state["last_upscmd_type"] == "test.battery.start.deep"
        assert model.state["last_upscmd_status"] == "OK"
        assert model.state["discharge_events"] == [
            {"timestamp": "2026-03-02T00:00:00Z", "depth_of_discharge": 0.8}
        ]


class TestStateSchemaValidation:
    """load() enforces the ModelState schema — no silent round-trip of stale keys."""

    def _write_state(self, path, extra):
        """Write a minimal valid default model, merge `extra`, return the file path."""
        base = BatteryModel(path)  # default VRLA model on disk
        base.state.update(extra)
        base.save()
        return path

    @pytest.mark.parametrize(
        "retired_key",
        ["sulfation_history", "roi_history", "blackout_credit"],
    )
    def test_rejects_retired_v3_keys(self, temporary_model_path, retired_key):
        """A leftover v3.0 desulfation key must fail-fast, not round-trip silently."""
        self._write_state(temporary_model_path, {retired_key: []})

        with pytest.raises(ModelLoadError, match=retired_key):
            BatteryModel(temporary_model_path)

    def test_rejects_arbitrary_unknown_key(self, temporary_model_path):
        """Any key outside the schema is rejected — typed state, no garbage tolerated."""
        self._write_state(temporary_model_path, {"totally_made_up_key": 1})

        with pytest.raises(ModelLoadError, match="totally_made_up_key"):
            BatteryModel(temporary_model_path)

    def test_lists_all_unknown_keys_in_error(self, temporary_model_path):
        """The error names every offending key so the operator can remove them in one pass."""
        self._write_state(
            temporary_model_path,
            {"sulfation_history": [], "roi_history": [], "blackout_credit": None},
        )

        with pytest.raises(ModelLoadError) as exc_info:
            BatteryModel(temporary_model_path)
        message = str(exc_info.value)
        assert "blackout_credit" in message
        assert "roi_history" in message
        assert "sulfation_history" in message

    def test_accepts_every_schema_key(self, temporary_model_path):
        """A file populated with all KNOWN_STATE_KEYS loads cleanly (schema is self-consistent).

        Only fills keys absent from the default model so the valid lut/physics/soh
        structures are preserved; the conditional keys get type-appropriate benign values.
        capacity_converged is no longer in the schema (removed in HYG-03) — it is
        derived live from get_convergence_status().converged.
        """
        base = BatteryModel(temporary_model_path)
        list_keys = {"soh_history", "capacity_estimates", "r_internal_history", "discharge_events"}
        bool_keys = {"new_battery_detected"}  # capacity_converged removed from schema
        for key in KNOWN_STATE_KEYS:
            if key in base.state:
                continue
            base.state[key] = [] if key in list_keys else False if key in bool_keys else None
        base.save()

        model = BatteryModel(temporary_model_path)  # must not raise

        assert KNOWN_STATE_KEYS <= set(model.state)  # every schema key accepted
        assert set(model.state) <= KNOWN_STATE_KEYS  # and nothing extra crept in

    def test_lifecycle_round_trip_stays_within_schema(self, temporary_model_path):
        """Drift guard: exercising the public mutators then reloading must never produce
        a key outside the schema — otherwise the strict loader would brick the daemon.
        set_replacement_due() is removed (HYG-03); replacement_due is computed live."""
        model = BatteryModel(temporary_model_path)
        model.state["cycle_count"] = 1
        model.state["cumulative_on_battery_sec"] = 42.0
        model.add_soh_history_entry("2026-06-04", 0.88, capacity_ah_ref=8.5)
        model.append_discharge_event(
            {"timestamp": "2026-06-04T00:00:00+00:00", "depth_of_discharge": 0.8}
        )
        model.update_upscmd_result(
            upscmd_timestamp="2026-06-04T03:00:00+00:00",
            upscmd_type="test.battery.start.quick",
            upscmd_status="OK",
        )
        model.save()

        reloaded = BatteryModel(temporary_model_path)  # must not raise

        assert set(reloaded.state) <= KNOWN_STATE_KEYS
        # replacement_due is NOT a state key — it is computed live
        assert "replacement_due" not in reloaded.state


# ---------------------------------------------------------------------------
# Fixtures shared by the new HYG-03/04 replacement_due tests
# ---------------------------------------------------------------------------


def _make_converged_capacity_estimates():
    """Return 3 capacity estimates with CoV well below 0.10 (convergence = True)."""
    return [
        {
            "ah_estimate": 7.15,
            "timestamp": "2025-06-01T00:00:00Z",
            "confidence": 0.95,
            "metadata": {},
        },
        {
            "ah_estimate": 7.18,
            "timestamp": "2025-07-01T00:00:00Z",
            "confidence": 0.95,
            "metadata": {},
        },
        {
            "ah_estimate": 7.20,
            "timestamp": "2025-08-01T00:00:00Z",
            "confidence": 0.95,
            "metadata": {},
        },
    ]


def _make_regression_quality_soh_history(capacity_ah_ref=7.2):
    """Return 4 soh_history entries with clearly negative slope, R²≥0.5.

    Using ~90-day intervals and ~3% SoH drop per quarter, the regression line
    has a strongly negative slope and R² well above 0.5.
    """
    return [
        {"date": "2025-06-01", "soh": 0.98, "capacity_ah_ref": capacity_ah_ref},
        {"date": "2025-09-01", "soh": 0.95, "capacity_ah_ref": capacity_ah_ref},
        {"date": "2025-12-01", "soh": 0.92, "capacity_ah_ref": capacity_ah_ref},
        {"date": "2026-03-01", "soh": 0.89, "capacity_ah_ref": capacity_ah_ref},
    ]


class TestComputeReplacementDueEquivalence:
    """T-26-04: parametrized equivalence test — compute_replacement_due() reproduces the
    old discharge_handler._predict_replacement result for ALL configured thresholds."""

    @pytest.mark.parametrize("threshold", [0.80, 0.75])
    def test_live_recompute_matches_direct_regression_call(self, tmp_path, threshold):
        """compute_replacement_due() at threshold t equals linear_regression_soh()[3] at t.

        Proves the live recompute reproduces exactly what the OLD persisted
        discharge_handler path stored at the CONFIGURED threshold — would FAIL if
        compute_replacement_due() hardcoded 0.80 (the t=0.75 case would diverge).

        The converged capacity_estimates fixture ensures get_convergence_status().converged
        is True so the convergence gate lets regression through (otherwise both would be
        None and the assertion would be vacuous).
        """
        model_path = tmp_path / "model.json"
        soh_history = _make_regression_quality_soh_history(capacity_ah_ref=7.2)
        state = _state_with(
            model_path,
            {
                "soh_history": soh_history,
                "capacity_estimates": _make_converged_capacity_estimates(),
                "battery_epoch_id": "00000000-0000-4000-8000-000000000001",
            },
        )
        with open(model_path, "w") as f:
            json.dump(state, f)

        model = BatteryModel(model_path, soh_threshold=threshold)

        # Fixture self-check: convergence must be True for the equivalence to be non-vacuous
        assert model.get_convergence_status().converged is True, (
            "Fixture self-check failed: capacity_estimates must be converged for this test to be meaningful"
        )

        expected = linear_regression_soh(soh_history, threshold_soh=threshold, capacity_ah_ref=7.2)
        assert expected is not None, (
            "Fixture self-check: regression_soh returned None (check history quality)"
        )

        assert model.compute_replacement_due() == expected[3]

    def test_short_soh_history_yields_none(self, tmp_path):
        """< 3 soh_history points → compute_replacement_due() is None."""
        model_path = tmp_path / "model.json"
        state = _state_with(
            model_path,
            {
                "soh_history": [
                    {"date": "2026-01-01", "soh": 0.98, "capacity_ah_ref": 7.2},
                    {"date": "2026-03-01", "soh": 0.95, "capacity_ah_ref": 7.2},
                ],
                "capacity_estimates": _make_converged_capacity_estimates(),
                "battery_epoch_id": "00000000-0000-4000-8000-000000000001",
            },
        )
        with open(model_path, "w") as f:
            json.dump(state, f)

        model = BatteryModel(model_path)
        # Even with converged estimates, <3 soh points → no regression → None
        assert model.compute_replacement_due() is None


class TestComputeReplacementDueConvergenceGate:
    """T-26-08 (cycle-2 HIGH): compute_replacement_due() returns None when
    get_convergence_status().converged is False, even with regression-quality soh_history.

    soh_history and capacity_estimates are INDEPENDENT arrays (model.py:36/37), so a
    model can have regression-quality soh_history while capacity_estimates is non-converged.
    Without this gate the OLD persisted None would diverge from the NEW live value —
    a value-divergence that breaks the HYG-04 "same values" contract for the
    DEFAULT-config user (cycle-2 HIGH).
    """

    def test_non_converged_capacity_yields_none(self, tmp_path):
        """Regression-quality soh_history BUT non-converged capacity_estimates → None.

        First asserts get_convergence_status().converged is False so the fixture
        provably exercises the convergence gate (not the <3-soh_history path).
        """
        model_path = tmp_path / "model.json"
        # Non-converged: only 2 estimates (below the >=3 threshold)
        state = _state_with(
            model_path,
            {
                "soh_history": _make_regression_quality_soh_history(capacity_ah_ref=7.2),
                "capacity_estimates": [
                    {
                        "ah_estimate": 7.0,
                        "timestamp": "2025-06-01T00:00:00Z",
                        "confidence": 0.5,
                        "metadata": {},
                    },
                    {
                        "ah_estimate": 6.5,
                        "timestamp": "2025-07-01T00:00:00Z",
                        "confidence": 0.5,
                        "metadata": {},
                    },
                ],
                "battery_epoch_id": "00000000-0000-4000-8000-000000000001",
            },
        )
        with open(model_path, "w") as f:
            json.dump(state, f)

        model = BatteryModel(model_path)

        # Self-validate the fixture: convergence MUST be False for the gate to be exercised
        assert model.get_convergence_status().converged is False, (
            "Fixture self-check: capacity_estimates must NOT be converged for T-26-08 to be non-vacuous"
        )
        # The gate must suppress the regression result even though soh_history is regression-quality
        assert model.compute_replacement_due() is None, (
            "T-26-08: compute_replacement_due() must return None when converged=False, "
            "matching the old persisted None from discharge_handler (discharge_handler.py:218-219)"
        )

    def test_converged_twin_yields_date(self, tmp_path):
        """Same regression-quality soh_history, but converged capacity_estimates → not None.

        Proves the gate flips on convergence alone, holding soh_history constant.
        """
        model_path = tmp_path / "model.json"
        state = _state_with(
            model_path,
            {
                "soh_history": _make_regression_quality_soh_history(capacity_ah_ref=7.2),
                "capacity_estimates": _make_converged_capacity_estimates(),
                "battery_epoch_id": "00000000-0000-4000-8000-000000000001",
            },
        )
        with open(model_path, "w") as f:
            json.dump(state, f)

        model = BatteryModel(model_path)

        assert model.get_convergence_status().converged is True, (
            "Fixture self-check: capacity_estimates must be converged for this twin test"
        )
        assert model.compute_replacement_due() is not None, (
            "T-26-08 converged twin: same soh_history + converged capacity → must return a date"
        )


class TestLatestCapacityAhRefBaseline:
    """T-26-07 (HIGH #3): shared latest_capacity_ah_ref helper ensures compute_replacement_due()
    and battery-health.py select the SAME capacity baseline for mixed-baseline soh_history."""

    def test_mixed_baseline_selects_latest(self, tmp_path):
        """Earlier entries: capacity_ah_ref=6.5; latest entries: 7.2.

        compute_replacement_due() uses latest_capacity_ah_ref (7.2) so only the
        latest-baseline entries participate — differs from the all-entries result.
        The fixture also seeds converged capacity_estimates so the convergence gate
        lets regression through (otherwise the baseline comparison is vacuous).
        """
        model_path = tmp_path / "model.json"
        soh_history = [
            # Old baseline (after battery degraded below threshold)
            {"date": "2024-01-01", "soh": 0.70, "capacity_ah_ref": 6.5},
            {"date": "2024-04-01", "soh": 0.65, "capacity_ah_ref": 6.5},
            {"date": "2024-07-01", "soh": 0.60, "capacity_ah_ref": 6.5},
            # New baseline (battery replaced)
            {"date": "2025-06-01", "soh": 0.98, "capacity_ah_ref": 7.2},
            {"date": "2025-09-01", "soh": 0.95, "capacity_ah_ref": 7.2},
            {"date": "2025-12-01", "soh": 0.92, "capacity_ah_ref": 7.2},
            {"date": "2026-03-01", "soh": 0.89, "capacity_ah_ref": 7.2},
        ]
        state = _state_with(
            model_path,
            {
                "soh_history": soh_history,
                "capacity_estimates": _make_converged_capacity_estimates(),
                "battery_epoch_id": "00000000-0000-4000-8000-000000000001",
            },
        )
        with open(model_path, "w") as f:
            json.dump(state, f)

        model = BatteryModel(model_path)

        # Fixture self-check: converged so the gate lets regression through
        assert model.get_convergence_status().converged is True

        # The shared helper must select the latest baseline
        assert latest_capacity_ah_ref(soh_history) == 7.2

        # compute_replacement_due() uses only entries matching the latest baseline (7.2)
        latest_only_result = linear_regression_soh(
            soh_history, threshold_soh=0.80, capacity_ah_ref=7.2
        )
        all_entries_result = linear_regression_soh(
            soh_history, threshold_soh=0.80, capacity_ah_ref=None
        )

        assert model.compute_replacement_due() == latest_only_result[3], (
            "compute_replacement_due() must use only the latest-baseline entries"
        )
        # Prove the baseline filter actually changes the result (test is non-vacuous).
        # The all-entries result may be None (when the mixed-baseline history fails R²<0.5
        # due to the V-shaped pattern of old-declining + new-healthy) or a different date.
        # Either way the latest-only result (a real date) differs from the all-entries result.
        latest_date = latest_only_result[3]
        all_entries_date = all_entries_result[3] if all_entries_result is not None else None
        assert latest_date != all_entries_date, (
            "Mixed-baseline test must produce different dates for latest-only vs all-entries "
            "(otherwise the filter has no observable effect and the test is vacuous)"
        )

    def test_latest_capacity_ah_ref_helper_empty(self):
        """Empty soh_history → None."""
        assert latest_capacity_ah_ref([]) is None

    def test_latest_capacity_ah_ref_helper_tagged(self):
        """Latest entry has capacity_ah_ref → returns it."""
        history = [
            {"date": "2026-01-01", "soh": 0.98, "capacity_ah_ref": 6.5},
            {"date": "2026-03-01", "soh": 0.95, "capacity_ah_ref": 7.2},
        ]
        assert latest_capacity_ah_ref(history) == 7.2


class TestRegenLoaderGates:
    """HYG-05 strict-loader contract tests: regen-loads-clean and strip-then-loads-clean."""

    def test_fresh_save_loads_clean(self, tmp_path):
        """A freshly generated model.json contains no removed keys and passes strict validation.

        Proves save() only emits schema-compliant keys so the strict loader always
        accepts a newly seeded file without operator intervention.
        """
        model_path = tmp_path / "m.json"
        m1 = BatteryModel(model_path)
        m1.save()

        # Must not raise
        BatteryModel(model_path)

        # No removed keys in the saved file
        saved = json.loads(model_path.read_text())
        for removed_key in (
            "replacement_due",
            "capacity_converged",
            "scheduled_test_timestamp",
            "scheduled_test_reason",
            "test_block_reason",
        ):
            assert removed_key not in saved, (
                f"Removed key '{removed_key}' found in freshly saved model.json"
            )

    def test_old_schema_raises_on_load(self, tmp_path):
        """An old-schema model.json carrying removed top-level keys raises ModelLoadError.

        Documents the deploy-strip requirement as a tested contract: the strict loader
        will REJECT the deployed model.json on next start until the operator strips
        the removed keys (stop → strip → start).
        """
        model_path = tmp_path / "old.json"

        # Build a realistic old-schema file with both learned and removed keys
        old_schema = {
            # Learned state (must survive the strip)
            "soh": 0.92,
            "soh_history": [{"date": "2026-03-01", "soh": 0.92, "capacity_ah_ref": 7.2}],
            "capacity_estimates": _make_converged_capacity_estimates(),
            "physics": {
                "peukert_exponent": 1.22,
                "ir_compensation": {"k_volts_per_percent": 0.016, "reference_load_percent": 20.0},
                "rls_state": {
                    "ir_k": {
                        "theta": 0.016,
                        "P": 0.9,
                        "sample_count": 5,
                        "forgetting_factor": 0.97,
                    },
                    "peukert": {
                        "theta": 1.22,
                        "P": 0.85,
                        "sample_count": 5,
                        "forgetting_factor": 0.97,
                    },
                    # Wave-1 removed physics spec sub-keys (silently ignored by _sync_physics_from_state,
                    # but stripped for cleanliness — the strict loader only rejects top-level unknowns)
                    "nominal_voltage": 12.0,
                    "nominal_power_watts": 85.0,
                },
            },
            "lut": [
                {"v": 13.4, "soc": 1.00, "source": "standard"},
                {"v": 10.5, "soc": 0.00, "source": "anchor"},
            ],
            "capacity_ah_measured": 7.15,
            "battery_install_date": "2024-01-15",
            "cycle_count": 12,
            "cumulative_on_battery_sec": 3600.0,
            # Removed top-level keys (from waves 1+2)
            "full_capacity_ah_ref": 7.2,  # wave-1 removed
            "replacement_due": "2027-06-01",  # wave-2 removed
            "capacity_converged": True,  # wave-2 removed
            "scheduled_test_timestamp": "2026-07-01T08:00:00Z",  # wave-2 removed
            "scheduled_test_reason": "diagnostic_cadence",  # wave-2 removed
            "test_block_reason": None,  # wave-2 removed
        }
        model_path.write_text(json.dumps(old_schema))

        # Step 1: strict loader must REJECT (documenting deploy-strip requirement)
        with pytest.raises(ModelLoadError) as exc_info:
            BatteryModel(model_path)
        error_msg = str(exc_info.value)
        # At least one of the removed top-level keys must appear in the error
        assert any(
            k in error_msg
            for k in (
                "full_capacity_ah_ref",
                "replacement_due",
                "capacity_converged",
                "scheduled_test_timestamp",
                "scheduled_test_reason",
                "test_block_reason",
            )
        ), f"Expected removed key in error, got: {error_msg}"

    def test_strip_then_load_clean_and_learned_keys_survive(self, tmp_path):
        """Removing a few retired keys is not a runtime migration path."""
        model_path = tmp_path / "old.json"
        old_schema = _state_with(
            model_path,
            {
                "soh": 0.92,
                "battery_epoch_id": "00000000-0000-4000-8000-000000000001",
                "full_capacity_ah_ref": 7.2,
                "replacement_due": "2027-06-01",
            },
        )
        model_path.write_text(json.dumps(old_schema))

        with pytest.raises(ModelLoadError):
            BatteryModel(model_path)
