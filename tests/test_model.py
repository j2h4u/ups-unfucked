"""Tests for battery model persistence and VRLA LUT initialization."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.model import (
    KNOWN_STATE_KEYS,
    BatteryModel,
    ModelLoadError,
    atomic_write_json,
    latest_capacity_ah_ref,
)
from src.replacement_predictor import linear_regression_soh


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
        model_data = {
            "soh": 0.95,
            "lut": [{"v": 13.4, "soc": 1.0, "source": "standard"}],
            "soh_history": [{"date": "2026-03-13", "soh": 0.95}],
        }
        with open(model_file, "w") as f:
            json.dump(model_data, f)

        model = BatteryModel(model_path=model_file)

        assert model.get_soh() == 0.95
        assert model.get_capacity_ah() == 7.2  # default from RATED_CAPACITY_AH
        assert len(model.get_lut()) == 1

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

    def test_model_handles_malformed_json(self, tmp_path, caplog):
        """Verify malformed JSON triggers fallback to default curve."""
        model_file = tmp_path / "model.json"
        with open(model_file, "w") as f:
            f.write("{invalid json content")

        model = BatteryModel(model_path=model_file)

        # Should have default VRLA curve, not crash
        assert model.get_soh() == 1.0
        assert model.get_capacity_ah() == 7.2
        # Verify error was logged
        assert "Malformed model.json" in caplog.text

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
        model1.add_soh_history_entry("2026-03-14", 0.95)

        model1.save()

        model2 = BatteryModel(model_path=model_file)
        assert len(model2.get_soh_history()) == 2
        assert model2.get_soh_history()[1] == {"date": "2026-03-14", "soh": 0.95}


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
        model.add_soh_history_entry("2026-03-14", 0.90)

        history = model.get_soh_history()
        assert len(history) == initial_count + 1
        assert history[-1] == {"date": "2026-03-14", "soh": 0.90}
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
        model.add_capacity_estimate(8.8, 0.8, {}, "2026-06-01T00:00:00Z")

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

    def test_set_ir_k(self, tmp_path):
        """set_ir_k updates the IR compensation coefficient."""
        model = BatteryModel(model_path=tmp_path / "model.json")
        model.set_ir_k(0.020)
        assert model.get_ir_k() == 0.020


class TestRInternalHistory:
    """Test internal resistance tracking."""

    def test_add_r_internal_entry(self, tmp_path):
        model = BatteryModel(model_path=tmp_path / "model.json")
        model.add_r_internal_entry("2026-03-14", 0.0396, 13.50, 13.22, 16.5, "BLACKOUT_TEST")

        history = model.get_r_internal_history()
        assert len(history) == 1
        assert history[0] == {
            "date": "2026-03-14",
            "r_ohm": 0.0396,
            "v_before": 13.50,
            "v_sag": 13.22,
            "load_percent": 16.5,
            "event": "BLACKOUT_TEST",
        }

    def test_r_internal_history_empty_by_default(self, tmp_path):
        model = BatteryModel(model_path=tmp_path / "model.json")
        assert model.get_r_internal_history() == []

    def test_r_internal_history_persists(self, tmp_path):
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)
        model.add_r_internal_entry("2026-03-14", 0.0396, 13.50, 13.22, 16.5, "BLACKOUT_TEST")
        model.save()

        model2 = BatteryModel(model_path=model_file)
        assert len(model2.get_r_internal_history()) == 1

    def test_r_internal_multiple_entries(self, tmp_path):
        model = BatteryModel(model_path=tmp_path / "model.json")
        model.add_r_internal_entry("2026-03-14", 0.0396, 13.50, 13.22, 16.5, "BLACKOUT_TEST")
        model.add_r_internal_entry("2026-03-15", 0.0410, 13.48, 13.19, 17.0, "BLACKOUT_REAL")
        assert len(model.get_r_internal_history()) == 2

    def test_r_internal_rounding(self, tmp_path):
        model = BatteryModel(model_path=tmp_path / "model.json")
        model.add_r_internal_entry("2026-03-14", 0.03961111, 13.501, 13.219, 16.55, "BLACKOUT_TEST")
        entry = model.get_r_internal_history()[0]
        assert entry["r_ohm"] == 0.0396
        assert entry["v_before"] == 13.50
        assert entry["v_sag"] == 13.22
        assert entry["load_percent"] == 16.6


class TestCalibrationWrite:
    """Test BatteryModel.calibration_write() for real-time discharge data collection."""

    def test_calibration_write_adds_entry(self, tmp_path):
        """Verify calibration_write() appends new LUT entry with measured source."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        initial_count = len(model.get_lut())
        model.calibration_write(voltage=12.5, soc=0.65, timestamp=1234567890.0)

        lut = model.get_lut()
        assert len(lut) == initial_count + 1

        # Find the new entry
        new_entry = [e for e in lut if e["v"] == 12.5]
        assert len(new_entry) == 1
        assert new_entry[0]["soc"] == 0.65
        assert new_entry[0]["source"] == "measured"
        assert new_entry[0]["timestamp"] == 1234567890.0

    def test_calibration_write_duplicate_prevention(self, tmp_path):
        """Verify calibration_write() skips duplicates by timestamp."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Write first entry
        model.calibration_write(voltage=12.5, soc=0.65, timestamp=1000.0)
        count_after_first = len(model.get_lut())

        # Same timestamp = duplicate (e.g., retry after crash recovery)
        model.calibration_write(voltage=12.505, soc=0.64, timestamp=1000.0)
        count_after_second = len(model.get_lut())
        assert count_after_second == count_after_first

        # Different timestamp = new measurement (even if voltage similar)
        model.calibration_write(voltage=12.505, soc=0.64, timestamp=2000.0)
        count_after_third = len(model.get_lut())
        assert count_after_third == count_after_first + 1

    def test_calibration_write_fsync(self, tmp_path):
        """Verify calibration_batch_flush() persists accumulated calibration data."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Write calibration data (accumulates in memory, not persisted yet)
        model.calibration_write(voltage=12.5, soc=0.65, timestamp=1234567890.0)

        # Batch flush to persist
        model.calibration_batch_flush()

        # Verify file was written to disk
        assert model_file.exists()

        # Reload from disk to verify persistence
        model2 = BatteryModel(model_path=model_file)
        lut = model2.get_lut()
        new_entries = [e for e in lut if e["v"] == 12.5]
        assert len(new_entries) == 1

    def test_calibration_write_sorts_lut(self, tmp_path):
        """Verify LUT is sorted descending by voltage after each write."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Write entries in non-descending order
        model.calibration_write(voltage=11.5, soc=0.30, timestamp=1000.0)
        model.calibration_write(voltage=12.5, soc=0.65, timestamp=2000.0)
        model.calibration_write(voltage=12.0, soc=0.50, timestamp=3000.0)

        # Check LUT is sorted descending
        lut = model.get_lut()
        voltages = [e["v"] for e in lut]
        assert voltages == sorted(voltages, reverse=True)

    def test_calibration_write_multiple_calls(self, tmp_path):
        """Verify multiple calibration_write() calls accumulate entries."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        initial_count = len(model.get_lut())

        # Write 3 distinct entries
        model.calibration_write(voltage=13.0, soc=0.95, timestamp=1000.0)
        model.calibration_write(voltage=12.5, soc=0.65, timestamp=2000.0)
        model.calibration_write(voltage=11.5, soc=0.30, timestamp=3000.0)

        lut = model.get_lut()
        assert len(lut) == initial_count + 3

        # Check all entries are present
        voltages = [e["v"] for e in lut if e["source"] == "measured"]
        assert 13.0 in voltages
        assert 12.5 in voltages
        assert 11.5 in voltages


class TestHistoryPruning:
    """Test SoH and R_internal history list pruning to prevent unbounded growth."""

    def test_prune_soh_history_keeps_recent_entries(self, tmp_path):
        """Verify _prune_soh_history() keeps only last 30 entries when history > 30."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Create 50 history entries
        for i in range(50):
            model.add_soh_history_entry(f"2026-03-{(i % 28) + 1:02d}", 1.0 - (i * 0.001))

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
            model.add_soh_history_entry(f"2026-03-{(i % 28) + 1:02d}", 0.95)

        initial_history = model.get_soh_history().copy()

        # Prune (should have no effect)
        model._cap_history_entries("soh_history", keep_count=30)

        # Verify no change
        assert model.get_soh_history() == initial_history

    def test_prune_r_internal_history_keeps_recent_entries(self, tmp_path):
        """Verify _prune_r_internal_history() mirrors soh pruning for r_internal_history."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Create 40 r_internal entries
        for i in range(40):
            model.add_r_internal_entry(
                f"2026-03-{(i % 28) + 1:02d}",
                0.03 + (i * 0.0001),
                13.5 - (i * 0.01),
                13.0,
                15.0,
                "TEST",
            )

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
            model.add_soh_history_entry(f"2026-03-{(i % 28) + 1:02d}", 0.95)

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
            model.add_soh_history_entry(f"2026-03-{(i % 28) + 1:02d}", 1.0 - (i * 0.001))

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
            model.add_soh_history_entry(f"2026-03-{(i % 28) + 1:02d}", 0.95)
            model.add_r_internal_entry(
                f"2026-03-{(i % 28) + 1:02d}", 0.03, 13.5, 13.0, 15.0, "TEST"
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
            patch("os.open", wraps=os.open),
            patch("os.close", wraps=os.close),
        ):
            atomic_write_json(model_file, data)

            # fdatasync should be called
            assert mock_fdatasync.called, "os.fdatasync was not called"
            # fsync should NOT be called (replaced by fdatasync)
            assert not mock_fsync.called, "os.fsync should not be called; use fdatasync instead"

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

    def test_add_capacity_estimate_creates_array_if_missing(self, tmp_path):
        """Test 1: model.add_capacity_estimate() creates array if not present."""
        model = BatteryModel(model_path=tmp_path / "model.json")
        assert (
            "capacity_estimates" not in model.state
            or len(model.state.get("capacity_estimates", [])) == 0
        )

        model.add_capacity_estimate(
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
        model.add_capacity_estimate(7.4, 0.80, {"delta_soc_percent": 50.0}, "2026-03-15T10:00:00Z")
        model.add_capacity_estimate(7.5, 0.85, {"delta_soc_percent": 52.0}, "2026-03-15T11:00:00Z")

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
            model.add_capacity_estimate(
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

        model.add_capacity_estimate(7.5, 0.85, {"delta_soc_percent": 50.0}, "2026-03-15T12:34:56Z")
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
        model1.add_capacity_estimate(7.4, 0.80, {"delta_soc_percent": 50.0}, "2026-03-15T10:00:00Z")
        model1.add_capacity_estimate(7.5, 0.85, {"delta_soc_percent": 52.0}, "2026-03-15T11:00:00Z")
        model1.save()

        # Create new model instance, load from file
        model2 = BatteryModel(model_path=model_file)
        estimates = model2.get_capacity_estimates()
        assert len(estimates) == 2
        assert estimates[0]["ah_estimate"] == 7.5  # Latest first

    def test_capacity_estimates_schema_has_required_fields(self, tmp_path):
        """Verify capacity_estimates array elements have all required fields."""
        model = BatteryModel(model_path=tmp_path / "model.json")

        model.add_capacity_estimate(
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

        model.add_capacity_estimate(
            ah_estimate=7.0,
            confidence=0.0,
            metadata={"delta_soc_percent": 50.0, "duration_sec": 1234},
            timestamp="2026-03-16T10:00:00Z",
        )
        model.add_capacity_estimate(
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
        model.add_capacity_estimate(
            ah_estimate=7.0,
            confidence=0.0,
            metadata={"delta_soc_percent": 50.0, "duration_sec": 1234},
            timestamp="2026-03-16T10:00:00Z",
        )
        model.add_capacity_estimate(
            ah_estimate=7.2,
            confidence=0.0,
            metadata={"delta_soc_percent": 50.0, "duration_sec": 1234},
            timestamp="2026-03-16T11:00:00Z",
        )
        model.add_capacity_estimate(
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

    def test_soh_history_entry_backward_compat(self, tmp_path):
        """SOH-02: add_soh_history_entry() without capacity_ah_ref is backward compatible."""
        model_path = tmp_path / "model.json"
        model = BatteryModel(model_path)

        # Add entry without capacity baseline (old-style)
        model.add_soh_history_entry(date="2026-03-15", soh=0.95)

        # Verify entry exists but has no capacity_ah_ref field
        entry = model.state["soh_history"][-1]
        assert entry["date"] == "2026-03-15"
        assert entry["soh"] == 0.95
        assert "capacity_ah_ref" not in entry  # Not tagged (backward compat)

    def test_mixed_baseline_entries(self, tmp_path):
        """SOH-02: soh_history can contain entries with and without capacity_ah_ref field."""
        model_path = tmp_path / "model.json"
        model = BatteryModel(model_path)
        # Clear default entry
        model.state["soh_history"] = []

        # Add old-style entry (no baseline tag)
        model.add_soh_history_entry("2026-03-14", 0.97)

        # Add new-style entry with measured baseline
        model.add_soh_history_entry("2026-03-15", 0.95, capacity_ah_ref=6.8)

        # Add another new-style entry with different baseline (after battery replaced)
        model.add_soh_history_entry("2026-03-16", 0.92, capacity_ah_ref=6.9)

        # Verify history contains mixed entries
        history = model.state["soh_history"]
        assert len(history) == 3
        assert "capacity_ah_ref" not in history[0]  # Old entry
        assert history[1]["capacity_ah_ref"] == 6.8
        assert history[2]["capacity_ah_ref"] == 6.9


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
    """Field-level validation in _validate_and_clamp_fields(): scheduling strings and history lists."""

    def _base_model_data(self):
        """Return a valid base model dict that passes all validation."""
        return {
            "soh": 0.95,
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
            "soh_history": [{"date": "2026-03-01", "soh": 0.95}],
        }

    def test_validate_string_field_last_upscmd_type_non_string(self, temporary_model_path, caplog):
        """last_upscmd_type=123 (int) → reset to None, warning logged."""
        import logging

        data = self._base_model_data()
        data["last_upscmd_type"] = 123
        with open(temporary_model_path, "w") as f:
            json.dump(data, f)

        with caplog.at_level(logging.WARNING, logger="ups-battery-monitor"):
            model = BatteryModel(temporary_model_path)

        assert model.state["last_upscmd_type"] is None
        clamped_records = [
            r for r in caplog.records if getattr(r, "event_type", "") == "model_field_clamped"
        ]
        assert len(clamped_records) >= 1, (
            f"Expected model_field_clamped warning, got: {[r.message for r in caplog.records]}"
        )

    def test_validate_string_field_last_upscmd_status_non_string(
        self, temporary_model_path, caplog
    ):
        """last_upscmd_status=True (bool) → reset to None, warning logged."""
        import logging

        data = self._base_model_data()
        data["last_upscmd_status"] = True
        with open(temporary_model_path, "w") as f:
            json.dump(data, f)

        with caplog.at_level(logging.WARNING, logger="ups-battery-monitor"):
            model = BatteryModel(temporary_model_path)

        assert model.state["last_upscmd_status"] is None
        clamped_records = [
            r for r in caplog.records if getattr(r, "event_type", "") == "model_field_clamped"
        ]
        assert len(clamped_records) >= 1

    def test_validate_list_field_discharge_events_non_list(self, temporary_model_path, caplog):
        """discharge_events=123 (int) → reset to [], warning logged."""
        import logging

        data = self._base_model_data()
        data["discharge_events"] = 123
        with open(temporary_model_path, "w") as f:
            json.dump(data, f)

        with caplog.at_level(logging.WARNING, logger="ups-battery-monitor"):
            model = BatteryModel(temporary_model_path)

        assert model.state["discharge_events"] == []
        clamped_records = [
            r for r in caplog.records if getattr(r, "event_type", "") == "model_field_clamped"
        ]
        assert len(clamped_records) >= 1

    def test_validate_valid_fields_no_warnings(self, temporary_model_path, caplog):
        """Valid string and list fields → no model_field_clamped warnings, fields unchanged.

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

        clamped_records = [
            r for r in caplog.records if getattr(r, "event_type", "") == "model_field_clamped"
        ]
        assert len(clamped_records) == 0, (
            f"Expected no model_field_clamped warnings for valid data, got: {[r.message for r in clamped_records]}"
        )
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
        model.increment_cycle_count()
        model.add_on_battery_time(42.0)
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
        {"ah_estimate": 7.15, "timestamp": "2025-06-01T00:00:00Z", "confidence": 0.95, "metadata": {}},
        {"ah_estimate": 7.18, "timestamp": "2025-07-01T00:00:00Z", "confidence": 0.95, "metadata": {}},
        {"ah_estimate": 7.20, "timestamp": "2025-08-01T00:00:00Z", "confidence": 0.95, "metadata": {}},
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
        state = {
            "soh_history": soh_history,
            "capacity_estimates": _make_converged_capacity_estimates(),
        }
        with open(model_path, "w") as f:
            json.dump(state, f)

        model = BatteryModel(model_path, soh_threshold=threshold)

        # Fixture self-check: convergence must be True for the equivalence to be non-vacuous
        assert model.get_convergence_status().converged is True, (
            "Fixture self-check failed: capacity_estimates must be converged for this test to be meaningful"
        )

        expected = linear_regression_soh(soh_history, threshold_soh=threshold, capacity_ah_ref=7.2)
        assert expected is not None, "Fixture self-check: regression_soh returned None (check history quality)"

        assert model.compute_replacement_due() == expected[3]

    def test_short_soh_history_yields_none(self, tmp_path):
        """< 3 soh_history points → compute_replacement_due() is None."""
        model_path = tmp_path / "model.json"
        state = {
            "soh_history": [
                {"date": "2026-01-01", "soh": 0.98, "capacity_ah_ref": 7.2},
                {"date": "2026-03-01", "soh": 0.95, "capacity_ah_ref": 7.2},
            ],
            "capacity_estimates": _make_converged_capacity_estimates(),
        }
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
        state = {
            "soh_history": _make_regression_quality_soh_history(capacity_ah_ref=7.2),
            "capacity_estimates": [
                {"ah_estimate": 7.0, "timestamp": "2025-06-01T00:00:00Z", "confidence": 0.5, "metadata": {}},
                {"ah_estimate": 6.5, "timestamp": "2025-07-01T00:00:00Z", "confidence": 0.5, "metadata": {}},
            ],
        }
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
        state = {
            "soh_history": _make_regression_quality_soh_history(capacity_ah_ref=7.2),
            "capacity_estimates": _make_converged_capacity_estimates(),
        }
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
        state = {
            "soh_history": soh_history,
            "capacity_estimates": _make_converged_capacity_estimates(),
        }
        with open(model_path, "w") as f:
            json.dump(state, f)

        model = BatteryModel(model_path)

        # Fixture self-check: converged so the gate lets regression through
        assert model.get_convergence_status().converged is True

        # The shared helper must select the latest baseline
        assert latest_capacity_ah_ref(soh_history) == 7.2

        # compute_replacement_due() uses only entries matching the latest baseline (7.2)
        latest_only_result = linear_regression_soh(soh_history, threshold_soh=0.80, capacity_ah_ref=7.2)
        all_entries_result = linear_regression_soh(soh_history, threshold_soh=0.80, capacity_ah_ref=None)

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

    def test_latest_capacity_ah_ref_helper_no_tag(self):
        """Latest entry has no capacity_ah_ref field → None (backward compat)."""
        history = [{"date": "2026-01-01", "soh": 0.95}]
        assert latest_capacity_ah_ref(history) is None

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
        """A freshly generated model.json contains no removed keys and passes _reject_unknown_state_keys.

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
        for removed_key in ("replacement_due", "capacity_converged",
                            "scheduled_test_timestamp", "scheduled_test_reason",
                            "test_block_reason"):
            assert removed_key not in saved, f"Removed key '{removed_key}' found in freshly saved model.json"

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
                    "ir_k": {"theta": 0.016, "P": 0.9, "sample_count": 5, "forgetting_factor": 0.97},
                    "peukert": {"theta": 1.22, "P": 0.85, "sample_count": 5, "forgetting_factor": 0.97},
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
            "full_capacity_ah_ref": 7.2,           # wave-1 removed
            "replacement_due": "2027-06-01",        # wave-2 removed
            "capacity_converged": True,             # wave-2 removed
            "scheduled_test_timestamp": "2026-07-01T08:00:00Z",  # wave-2 removed
            "scheduled_test_reason": "diagnostic_cadence",        # wave-2 removed
            "test_block_reason": None,              # wave-2 removed
        }
        model_path.write_text(json.dumps(old_schema))

        # Step 1: strict loader must REJECT (documenting deploy-strip requirement)
        with pytest.raises(ModelLoadError) as exc_info:
            BatteryModel(model_path)
        error_msg = str(exc_info.value)
        # At least one of the removed top-level keys must appear in the error
        assert any(k in error_msg for k in (
            "full_capacity_ah_ref", "replacement_due", "capacity_converged",
            "scheduled_test_timestamp", "scheduled_test_reason", "test_block_reason",
        )), f"Expected removed key in error, got: {error_msg}"

    def test_strip_then_load_clean_and_learned_keys_survive(self, tmp_path):
        """After applying the documented strip, the file loads clean AND learned keys survive.

        NOTE: the strict loader only rejects TOP-LEVEL unknown keys.
        physics.nominal_voltage / physics.nominal_power_watts under the physics dict are
        silently ignored by _sync_physics_from_state (not rejected); the strip removes them
        for cleanliness, not to satisfy the loader.
        """
        model_path = tmp_path / "old.json"
        old_schema = {
            # Learned state (must survive)
            "soh": 0.92,
            "soh_history": [{"date": "2026-03-01", "soh": 0.92, "capacity_ah_ref": 7.2}],
            "capacity_estimates": _make_converged_capacity_estimates(),
            "physics": {
                "peukert_exponent": 1.22,
                "ir_compensation": {"k_volts_per_percent": 0.016, "reference_load_percent": 20.0},
                "rls_state": {
                    "ir_k": {"theta": 0.016, "P": 0.9, "sample_count": 5, "forgetting_factor": 0.97},
                    "peukert": {"theta": 1.22, "P": 0.85, "sample_count": 5, "forgetting_factor": 0.97},
                },
                "nominal_voltage": 12.0,          # wave-1 physics spec sub-key (silently ignored)
                "nominal_power_watts": 85.0,       # wave-1 physics spec sub-key (silently ignored)
            },
            "lut": [
                {"v": 13.4, "soc": 1.00, "source": "standard"},
                {"v": 10.5, "soc": 0.00, "source": "anchor"},
            ],
            "capacity_ah_measured": 7.15,
            "battery_install_date": "2024-01-15",
            "cycle_count": 12,
            "cumulative_on_battery_sec": 3600.0,
            # Removed top-level keys (all waves)
            "full_capacity_ah_ref": 7.2,
            "replacement_due": "2027-06-01",
            "capacity_converged": True,
            "scheduled_test_timestamp": "2026-07-01T08:00:00Z",
            "scheduled_test_reason": "diagnostic_cadence",
            "test_block_reason": None,
        }
        model_path.write_text(json.dumps(old_schema))

        # Apply the documented one-time deploy strip (stop → strip → start sequence)
        data = json.loads(model_path.read_text())
        for key in ("full_capacity_ah_ref", "replacement_due", "capacity_converged",
                    "scheduled_test_timestamp", "scheduled_test_reason", "test_block_reason"):
            data.pop(key, None)
        # Also remove physics spec sub-keys for cleanliness (silently ignored by loader but stale)
        physics = data.get("physics", {})
        physics.pop("nominal_voltage", None)
        physics.pop("nominal_power_watts", None)
        model_path.write_text(json.dumps(data))

        # Step 2: after strip, load must succeed
        model = BatteryModel(model_path)  # must not raise

        # Step 3: learned keys must have survived the strip
        assert model.get_soh() == 0.92, "soh must survive strip"
        assert len(model.get_soh_history()) == 1, "soh_history must survive strip"
        assert model.physics.peukert_exponent == pytest.approx(1.22, abs=1e-6), (
            "physics.peukert_exponent (learned) must survive strip"
        )
