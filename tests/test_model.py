"""Tests for battery model persistence and VRLA LUT initialization."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, mock_open

import pytest

from src.model import BatteryModel, atomic_write_json


class TestAtomicWriteJson:
    """Test atomic_write_json() helper function."""

    def test_atomic_write_creates_file(self, tmp_path):
        """Verify file is created with JSON content."""
        model_file = tmp_path / "model.json"
        data = {'test': 'value', 'number': 42}

        atomic_write_json(model_file, data)

        assert model_file.exists()
        with open(model_file, 'r') as f:
            loaded = json.load(f)
        assert loaded == data

    def test_atomic_write_no_temp_files_left(self, tmp_path):
        """Verify no .tmp files remain after successful write."""
        model_file = tmp_path / "model.json"
        data = {'test': 'value'}

        atomic_write_json(model_file, data)

        # Check that no .tmp files exist in the directory
        tmp_files = list(tmp_path.glob('*.tmp'))
        assert len(tmp_files) == 0, f"Found leftover temp files: {tmp_files}"

    def test_atomic_write_creates_parent_dirs(self, tmp_path):
        """Verify parent directories are created automatically."""
        nested_file = tmp_path / "deep" / "nested" / "model.json"
        data = {'test': 'value'}

        atomic_write_json(nested_file, data)

        assert nested_file.exists()
        with open(nested_file, 'r') as f:
            loaded = json.load(f)
        assert loaded == data

    def test_atomic_write_handles_exception(self, tmp_path):
        """Verify temp file is cleaned up on write error."""
        model_file = tmp_path / "model.json"
        data = {'test': 'value'}

        # Mock os.fdatasync to raise an error (now using fdatasync instead of fsync)
        with patch('os.fdatasync', side_effect=OSError("Disk error")):
            with pytest.raises(IOError):
                atomic_write_json(model_file, data)

        # Verify model.json was not created
        assert not model_file.exists()


class TestBatteryModelLoad:
    """Test BatteryModel initialization and loading."""

    def test_model_loads_existing_file(self, tmp_path):
        """Verify model loads from existing JSON file."""
        model_file = tmp_path / "model.json"
        model_data = {
            'full_capacity_ah_ref': 7.2,
            'soh': 0.95,
            'lut': [{'v': 13.4, 'soc': 1.0, 'source': 'standard'}],
            'soh_history': [{'date': '2026-03-13', 'soh': 0.95}]
        }
        with open(model_file, 'w') as f:
            json.dump(model_data, f)

        model = BatteryModel(model_path=model_file)

        assert model.get_soh() == 0.95
        assert model.get_capacity_ah() == 7.2
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
        assert lut[0]['v'] == 13.4
        assert lut[-1]['v'] == 10.5

    def test_model_handles_malformed_json(self, tmp_path, caplog):
        """Verify malformed JSON triggers fallback to default curve."""
        model_file = tmp_path / "model.json"
        with open(model_file, 'w') as f:
            f.write("{invalid json content")

        model = BatteryModel(model_path=model_file)

        # Should have default VRLA curve, not crash
        assert model.get_soh() == 1.0
        assert model.get_capacity_ah() == 7.2
        # Verify error was logged
        assert "Malformed model.json" in caplog.text

    def test_model_initializes_with_default_path(self, tmp_path):
        """Verify model uses ~/.config path when no model_path given."""
        with patch('pathlib.Path.home') as mock_home:
            mock_home.return_value = tmp_path
            model_file = tmp_path / '.config' / 'ups-battery-monitor' / 'model.json'

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
        with open(model_file, 'r') as f:
            loaded = json.load(f)
        assert 'lut' in loaded
        assert 'soh' in loaded
        assert 'soh_history' in loaded

    def test_model_save_preserves_data(self, tmp_path):
        """Verify data is preserved across save/load cycle."""
        model_file = tmp_path / "model.json"
        model1 = BatteryModel(model_path=model_file)
        model1.add_soh_history_entry('2026-03-14', 0.95)

        model1.save()

        model2 = BatteryModel(model_path=model_file)
        assert len(model2.get_soh_history()) == 2
        assert model2.get_soh_history()[1] == {'date': '2026-03-14', 'soh': 0.95}


class TestVRLALUTInitialization:
    """Test standard VRLA curve initialization."""

    def test_default_lut_has_required_points(self, tmp_path):
        """Verify default LUT contains all required voltage points."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        lut = model.get_lut()
        voltages = [entry['v'] for entry in lut]

        assert 13.4 in voltages  # Full charge
        assert 12.4 in voltages  # Knee point
        assert 10.5 in voltages  # Anchor

    def test_default_lut_soc_monotonic(self, tmp_path):
        """Verify SoC values decrease monotonically with voltage."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        lut = model.get_lut()
        for i in range(len(lut) - 1):
            assert lut[i]['soc'] >= lut[i+1]['soc'], \
                f"SoC not monotonic: {lut[i]['soc']} > {lut[i+1]['soc']}"

    def test_default_lut_source_tracking(self, tmp_path):
        """Verify all LUT entries have source field."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        lut = model.get_lut()
        for entry in lut:
            assert 'source' in entry
            assert entry['source'] in ['standard', 'measured', 'anchor']

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
        assert 'date' in history[0]
        assert 'soh' in history[0]
        assert history[0]['soh'] == 1.0


class TestBatteryModelMethods:
    """Test BatteryModel helper methods."""

    def test_add_soh_history_entry(self, tmp_path):
        """Verify SoH history entry is added correctly."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        initial_count = len(model.get_soh_history())
        model.add_soh_history_entry('2026-03-14', 0.90)

        history = model.get_soh_history()
        assert len(history) == initial_count + 1
        assert history[-1] == {'date': '2026-03-14', 'soh': 0.90}
        assert model.get_soh() == 0.90

    def test_has_measured_data_false_by_default(self, tmp_path):
        """Verify has_measured_data() returns False for default LUT."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        assert model.has_measured_data() is False

    def test_has_measured_data_true_with_measured_entries(self, tmp_path):
        """Verify has_measured_data() detects measured entries."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Add a measured entry
        model.data['lut'].append({'v': 12.5, 'soc': 0.70, 'source': 'measured'})

        assert model.has_measured_data() is True

    def test_get_capacity_ah_default(self, tmp_path):
        """Verify default capacity is 7.2 Ah."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        assert model.get_capacity_ah() == 7.2

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
        model.add_r_internal_entry('2026-03-14', 0.0396, 13.50, 13.22, 16.5, 'BLACKOUT_TEST')

        history = model.get_r_internal_history()
        assert len(history) == 1
        assert history[0] == {
            'date': '2026-03-14', 'r_ohm': 0.0396,
            'v_before': 13.50, 'v_sag': 13.22,
            'load_percent': 16.5, 'event': 'BLACKOUT_TEST'
        }

    def test_r_internal_history_empty_by_default(self, tmp_path):
        model = BatteryModel(model_path=tmp_path / "model.json")
        assert model.get_r_internal_history() == []

    def test_r_internal_history_persists(self, tmp_path):
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)
        model.add_r_internal_entry('2026-03-14', 0.0396, 13.50, 13.22, 16.5, 'BLACKOUT_TEST')
        model.save()

        model2 = BatteryModel(model_path=model_file)
        assert len(model2.get_r_internal_history()) == 1

    def test_r_internal_multiple_entries(self, tmp_path):
        model = BatteryModel(model_path=tmp_path / "model.json")
        model.add_r_internal_entry('2026-03-14', 0.0396, 13.50, 13.22, 16.5, 'BLACKOUT_TEST')
        model.add_r_internal_entry('2026-03-15', 0.0410, 13.48, 13.19, 17.0, 'BLACKOUT_REAL')
        assert len(model.get_r_internal_history()) == 2

    def test_r_internal_rounding(self, tmp_path):
        model = BatteryModel(model_path=tmp_path / "model.json")
        model.add_r_internal_entry('2026-03-14', 0.03961111, 13.501, 13.219, 16.55, 'BLACKOUT_TEST')
        entry = model.get_r_internal_history()[0]
        assert entry['r_ohm'] == 0.0396
        assert entry['v_before'] == 13.50
        assert entry['v_sag'] == 13.22
        assert entry['load_percent'] == 16.6


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
        new_entry = [e for e in lut if e['v'] == 12.5]
        assert len(new_entry) == 1
        assert new_entry[0]['soc'] == 0.65
        assert new_entry[0]['source'] == 'measured'
        assert new_entry[0]['timestamp'] == 1234567890.0

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
        new_entries = [e for e in lut if e['v'] == 12.5]
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
        voltages = [e['v'] for e in lut]
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
        voltages = [e['v'] for e in lut if e['source'] == 'measured']
        assert 13.0 in voltages
        assert 12.5 in voltages
        assert 11.5 in voltages


class TestUpdateLutFromCalibration:
    """Test BatteryModel.update_lut_from_calibration() method."""

    def test_update_lut_from_calibration_replaces(self, tmp_path):
        """Verify update_lut_from_calibration() replaces LUT in memory."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Original LUT
        original_count = len(model.get_lut())

        # Create new LUT (interpolated)
        new_lut = [
            {'v': 13.4, 'soc': 1.00, 'source': 'standard'},
            {'v': 11.0, 'soc': 0.50, 'source': 'measured'},
            {'v': 10.8, 'soc': 0.40, 'source': 'interpolated'},
            {'v': 10.6, 'soc': 0.20, 'source': 'interpolated'},
            {'v': 10.5, 'soc': 0.00, 'source': 'measured'},
        ]

        # Update
        model.update_lut_from_calibration(new_lut)

        # Verify replacement
        assert model.get_lut() == new_lut
        assert len(model.get_lut()) == 5

    def test_update_lut_from_calibration_persists(self, tmp_path):
        """Verify update_lut_from_calibration() persists to disk."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # New LUT
        new_lut = [
            {'v': 13.4, 'soc': 1.00, 'source': 'standard'},
            {'v': 11.0, 'soc': 0.50, 'source': 'measured'},
            {'v': 10.5, 'soc': 0.00, 'source': 'measured'},
        ]

        # Update
        model.update_lut_from_calibration(new_lut)

        # Reload from disk
        model2 = BatteryModel(model_path=model_file)
        assert model2.get_lut() == new_lut

    def test_update_lut_from_calibration_logging(self, tmp_path, caplog):
        """Verify update_lut_from_calibration() logs with entry count."""
        import logging
        caplog.set_level(logging.INFO)

        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        new_lut = [
            {'v': 13.4, 'soc': 1.00, 'source': 'standard'},
            {'v': 11.0, 'soc': 0.50, 'source': 'measured'},
            {'v': 10.5, 'soc': 0.00, 'source': 'measured'},
        ]

        model.update_lut_from_calibration(new_lut)

        # Check log message
        assert 'LUT updated from calibration' in caplog.text
        assert '3 entries' in caplog.text
        assert 'cliff region interpolated' in caplog.text

    def test_update_lut_from_calibration_with_mixed_sources(self, tmp_path):
        """Verify update preserves source field values (measured, interpolated, standard)."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        new_lut = [
            {'v': 13.4, 'soc': 1.00, 'source': 'standard'},
            {'v': 11.0, 'soc': 0.50, 'source': 'measured'},
            {'v': 10.8, 'soc': 0.40, 'source': 'interpolated'},
            {'v': 10.6, 'soc': 0.20, 'source': 'interpolated'},
            {'v': 10.5, 'soc': 0.00, 'source': 'measured'},
        ]

        model.update_lut_from_calibration(new_lut)

        lut = model.get_lut()
        assert lut[0]['source'] == 'standard'
        assert lut[1]['source'] == 'measured'
        assert lut[2]['source'] == 'interpolated'
        assert lut[3]['source'] == 'interpolated'
        assert lut[4]['source'] == 'measured'


class TestHistoryPruning:
    """Test SoH and R_internal history list pruning to prevent unbounded growth."""

    def test_prune_soh_history_keeps_recent_entries(self, tmp_path):
        """Verify _prune_soh_history() keeps only last 30 entries when history > 30."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Create 50 history entries
        for i in range(50):
            model.add_soh_history_entry(f'2026-03-{(i % 28) + 1:02d}', 1.0 - (i * 0.001))

        initial_count = len(model.get_soh_history())
        assert initial_count >= 50

        # Prune
        model._prune_soh_history(keep_count=30)

        # Verify only last 30 are kept
        history = model.get_soh_history()
        assert len(history) == 30
        # Verify we kept the most recent entries (last ones added)
        assert history[-1]['soh'] == pytest.approx(1.0 - (49 * 0.001))

    def test_prune_soh_history_no_change_if_small(self, tmp_path):
        """Verify _prune_soh_history() leaves history unchanged if ≤ 30 entries."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Create only 15 entries
        for i in range(15):
            model.add_soh_history_entry(f'2026-03-{(i % 28) + 1:02d}', 0.95)

        initial_history = model.get_soh_history().copy()

        # Prune (should have no effect)
        model._prune_soh_history(keep_count=30)

        # Verify no change
        assert model.get_soh_history() == initial_history

    def test_prune_r_internal_history_keeps_recent_entries(self, tmp_path):
        """Verify _prune_r_internal_history() mirrors soh pruning for r_internal_history."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Create 40 r_internal entries
        for i in range(40):
            model.add_r_internal_entry(
                f'2026-03-{(i % 28) + 1:02d}',
                0.03 + (i * 0.0001),
                13.5 - (i * 0.01),
                13.0,
                15.0,
                'TEST'
            )

        initial_count = len(model.get_r_internal_history())
        assert initial_count >= 40

        # Prune
        model._prune_r_internal_history(keep_count=30)

        # Verify only last 30 are kept
        history = model.get_r_internal_history()
        assert len(history) == 30

    def test_pruning_is_idempotent(self, tmp_path):
        """Verify pruning twice produces same result (idempotent)."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Create 35 history entries
        for i in range(35):
            model.add_soh_history_entry(f'2026-03-{(i % 28) + 1:02d}', 0.95)

        # Prune once
        model._prune_soh_history(keep_count=30)
        history_after_first = model.get_soh_history().copy()

        # Prune again
        model._prune_soh_history(keep_count=30)
        history_after_second = model.get_soh_history().copy()

        # Should be identical
        assert history_after_first == history_after_second

    def test_save_automatically_prunes_history(self, tmp_path):
        """Verify save() calls pruning automatically and persists pruned model."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        # Create 40 history entries
        for i in range(40):
            model.add_soh_history_entry(f'2026-03-{(i % 28) + 1:02d}', 1.0 - (i * 0.001))

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
            model.add_soh_history_entry(f'2026-03-{(i % 28) + 1:02d}', 0.95)
            model.add_r_internal_entry(f'2026-03-{(i % 28) + 1:02d}', 0.03, 13.5, 13.0, 15.0, 'TEST')

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
        data = {'test': 'value', 'number': 42}

        # Patch both os.fdatasync and os.fsync to track calls
        with patch('os.fdatasync') as mock_fdatasync, \
             patch('os.fsync') as mock_fsync, \
             patch('os.open', wraps=os.open), \
             patch('os.close', wraps=os.close):
            atomic_write_json(model_file, data)

            # fdatasync should be called
            assert mock_fdatasync.called, "os.fdatasync was not called"
            # fsync should NOT be called (replaced by fdatasync)
            assert not mock_fsync.called, "os.fsync should not be called; use fdatasync instead"

    def test_atomic_write_json_still_works_with_fdatasync(self, tmp_path):
        """Verify atomic file write still succeeds after switching to fdatasync."""
        model_file = tmp_path / "model.json"
        data = {'test': 'value', 'nested': {'key': 'value'}, 'list': [1, 2, 3]}

        # Write with fdatasync (already implemented)
        atomic_write_json(model_file, data)

        # Verify file was created and contains correct data
        assert model_file.exists()
        with open(model_file, 'r') as f:
            loaded = json.load(f)
        assert loaded == data

    def test_atomic_write_json_content_integrity_with_fdatasync(self, tmp_path):
        """Verify JSON content remains intact after switching to fdatasync."""
        model_file = tmp_path / "model.json"
        data = {
            'lut': [
                {'v': 13.4, 'soc': 1.0, 'source': 'standard'},
                {'v': 12.4, 'soc': 0.8, 'source': 'measured'},
                {'v': 10.5, 'soc': 0.0, 'source': 'anchor'}
            ],
            'soh': 0.95,
            'full_capacity_ah_ref': 7.2,
            'cycle_count': 42
        }

        atomic_write_json(model_file, data)

        # Read back and verify exact match
        with open(model_file, 'r') as f:
            loaded = json.load(f)
        assert loaded == data
        assert loaded['lut'][0]['v'] == 13.4
        assert loaded['soh'] == 0.95
        assert loaded['full_capacity_ah_ref'] == 7.2


class TestCapacityEstimates:
    """Test BatteryModel capacity_estimates array for Phase 12 Plan 02."""

    def test_add_capacity_estimate_creates_array_if_missing(self, tmp_path):
        """Test 1: model.add_capacity_estimate() creates array if not present."""
        model = BatteryModel(model_path=tmp_path / "model.json")
        assert 'capacity_estimates' not in model.data or len(model.data.get('capacity_estimates', [])) == 0

        model.add_capacity_estimate(
            ah_estimate=7.5,
            confidence=0.85,
            metadata={'delta_soc_percent': 50.0, 'duration_sec': 1234},
            timestamp='2026-03-15T12:34:56Z'
        )

        assert 'capacity_estimates' in model.data
        assert len(model.data['capacity_estimates']) == 1

    def test_get_capacity_estimates_returns_list_latest_first(self, tmp_path):
        """Test 2: model.get_capacity_estimates() returns list with latest first."""
        model = BatteryModel(model_path=tmp_path / "model.json")

        # Add two estimates
        model.add_capacity_estimate(7.4, 0.80, {'delta_soc_percent': 50.0}, '2026-03-15T10:00:00Z')
        model.add_capacity_estimate(7.5, 0.85, {'delta_soc_percent': 52.0}, '2026-03-15T11:00:00Z')

        estimates = model.get_capacity_estimates()
        assert len(estimates) == 2
        # Latest first
        assert estimates[0]['timestamp'] == '2026-03-15T11:00:00Z'
        assert estimates[1]['timestamp'] == '2026-03-15T10:00:00Z'

    def test_get_latest_capacity_returns_float_or_none(self, tmp_path):
        """Test 3: model.get_latest_capacity() returns float (latest Ah) or None."""
        model = BatteryModel(model_path=tmp_path / "model.json")

        # Empty case
        latest = model.get_latest_capacity()
        assert latest is None

        # After adding estimate
        model.add_capacity_estimate(7.5, 0.85, {'delta_soc_percent': 50.0}, '2026-03-15T12:34:56Z')
        latest = model.get_latest_capacity()
        assert isinstance(latest, float)
        assert latest == 7.5

    def test_prune_capacity_estimates_keeps_30(self, tmp_path):
        """Test 4: capacity_estimates pruned to last 30 entries (no unbounded growth)."""
        model = BatteryModel(model_path=tmp_path / "model.json")

        # Add 35 estimates
        for i in range(35):
            model.add_capacity_estimate(
                ah_estimate=7.0 + (i * 0.01),
                confidence=0.5 + (i * 0.01),
                metadata={'delta_soc_percent': 50.0},
                timestamp=f'2026-03-15T{i:02d}:00:00Z'
            )

        # After adding the 35th, should be pruned to 30
        estimates = model.data['capacity_estimates']
        assert len(estimates) <= 30, f"Expected <= 30 estimates, got {len(estimates)}"

    def test_save_persists_capacity_estimates_atomically(self, tmp_path):
        """Test 5: model.save() writes capacity_estimates atomically."""
        model_file = tmp_path / "model.json"
        model = BatteryModel(model_path=model_file)

        model.add_capacity_estimate(7.5, 0.85, {'delta_soc_percent': 50.0}, '2026-03-15T12:34:56Z')
        model.save()

        # Verify file exists
        assert model_file.exists()
        with open(model_file, 'r') as f:
            loaded = json.load(f)
        assert 'capacity_estimates' in loaded
        assert len(loaded['capacity_estimates']) == 1
        assert loaded['capacity_estimates'][0]['ah_estimate'] == 7.5

    def test_reload_persists_capacity_estimates(self, tmp_path):
        """Test 6: Reload from model.json → capacity_estimates persists across daemon restarts."""
        model_file = tmp_path / "model.json"
        model1 = BatteryModel(model_path=model_file)

        # Add estimates and save
        model1.add_capacity_estimate(7.4, 0.80, {'delta_soc_percent': 50.0}, '2026-03-15T10:00:00Z')
        model1.add_capacity_estimate(7.5, 0.85, {'delta_soc_percent': 52.0}, '2026-03-15T11:00:00Z')
        model1.save()

        # Create new model instance, load from file
        model2 = BatteryModel(model_path=model_file)
        estimates = model2.get_capacity_estimates()
        assert len(estimates) == 2
        assert estimates[0]['ah_estimate'] == 7.5  # Latest first

    def test_capacity_estimates_schema_has_required_fields(self, tmp_path):
        """Verify capacity_estimates array elements have all required fields."""
        model = BatteryModel(model_path=tmp_path / "model.json")

        model.add_capacity_estimate(
            ah_estimate=7.45,
            confidence=0.82,
            metadata={
                'delta_soc_percent': 52.0,
                'duration_sec': 1234,
                'ir_mohms': 45.2,
                'load_avg_percent': 35.0
            },
            timestamp='2026-03-15T12:34:56Z'
        )

        estimate = model.data['capacity_estimates'][0]
        assert 'timestamp' in estimate
        assert 'ah_estimate' in estimate
        assert 'confidence' in estimate
        assert 'metadata' in estimate
        assert estimate['timestamp'] == '2026-03-15T12:34:56Z'
        assert estimate['ah_estimate'] == 7.45
        assert estimate['confidence'] == 0.82
        assert estimate['metadata']['delta_soc_percent'] == 52.0

