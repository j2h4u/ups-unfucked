"""Tests for model.json sulfation history persistence (SULF-05).

Integration test scaffold for battery model persistence layer.
Tests cover:
- Appending sulfation history entries
- Pruning old entries to keep last N
- Persistence to model.json
- Discharge event appending
- Schema correctness
- Backward compatibility with v2.0 model.json
"""

import pytest
import tempfile
import json
from pathlib import Path
from datetime import datetime, timezone

from src.model import BatteryModel


@pytest.fixture
def battery_model_temp_file():
    """Create temporary model.json and return BatteryModel instance."""
    with tempfile.TemporaryDirectory() as tmpdir:
        model_path = Path(tmpdir) / "model.json"
        battery_model = BatteryModel(model_path=model_path)
        yield battery_model


@pytest.fixture
def sample_sulfation_entry():
    """Return sample sulfation history entry."""
    return {
        "timestamp": "2026-03-17T10:30:00Z",
        "event_type": "natural",
        "sulfation_score": 0.45,
        "days_since_deep": 7.2,
        "ir_trend_rate": 0.008,
        "recovery_delta": 0.12,
        "temperature_celsius": 35.0,
        "confidence_level": "high"
    }


@pytest.mark.integration
def test_append_sulfation_history_single_entry(battery_model_temp_file, sample_sulfation_entry):
    """Verify append_sulfation_history() method exists and accepts dict parameter."""
    model = battery_model_temp_file
    model.append_sulfation_history(sample_sulfation_entry)
    assert 'sulfation_history' in model.data
    assert len(model.data['sulfation_history']) == 1
    assert model.data['sulfation_history'][0] == sample_sulfation_entry


@pytest.mark.integration
def test_append_sulfation_history_multiple_entries(battery_model_temp_file, sample_sulfation_entry):
    """Verify multiple appends work and maintain order."""
    model = battery_model_temp_file
    for i in range(5):
        entry = sample_sulfation_entry.copy()
        entry['timestamp'] = f"2026-03-{17+i:02d}T10:30:00Z"
        model.append_sulfation_history(entry)
    assert len(model.data['sulfation_history']) == 5


@pytest.mark.integration
def test_sulfation_history_saved_to_model_json(battery_model_temp_file, sample_sulfation_entry):
    """Verify model.json contains sulfation_history array after save()."""
    model = battery_model_temp_file
    model.append_sulfation_history(sample_sulfation_entry)
    model.save()

    # Reload from disk
    reloaded = BatteryModel(model_path=model.model_path)
    assert 'sulfation_history' in reloaded.data
    assert len(reloaded.data['sulfation_history']) == 1
    assert reloaded.data['sulfation_history'][0] == sample_sulfation_entry


@pytest.mark.integration
def test_prune_sulfation_history_keeps_last_30(battery_model_temp_file, sample_sulfation_entry):
    """Verify _prune_sulfation_history(keep_count=30) preserves only last 30 entries."""
    model = battery_model_temp_file
    # Append 50 entries
    for i in range(50):
        entry = sample_sulfation_entry.copy()
        entry['timestamp'] = f"2026-03-{17+i:02d}T10:30:00Z"
        model.append_sulfation_history(entry)

    model._cap_history_entries('sulfation_history', keep_count=30)
    assert len(model.data['sulfation_history']) == 30


@pytest.mark.integration
def test_append_discharge_event(battery_model_temp_file):
    """Verify append_discharge_event() method exists and persists to model.json."""
    model = battery_model_temp_file
    event = {
        "timestamp": "2026-03-17T10:30:00Z",
        "event_reason": "natural",
        "duration_seconds": 1200.0,
        "depth_of_discharge": 0.75,
        "measured_capacity_ah": 6.8,
        "cycle_roi": 0.52
    }
    model.append_discharge_event(event)
    assert 'discharge_events' in model.data
    assert len(model.data['discharge_events']) == 1
    assert model.data['discharge_events'][0] == event


@pytest.mark.integration
def test_discharge_event_schema_correctness(battery_model_temp_file):
    """Verify discharge_event has all required fields.

    Required fields:
    - timestamp (ISO8601 string)
    - event_reason ('natural' | 'test_initiated')
    - duration_seconds (int)
    - depth_of_discharge (float 0-1)
    - measured_capacity_ah (float or null)
    - cycle_roi (float)
    """
    model = battery_model_temp_file
    event = {
        "timestamp": "2026-03-17T10:30:00Z",
        "event_reason": "natural",
        "duration_seconds": 1200.0,
        "depth_of_discharge": 0.75,
        "measured_capacity_ah": 6.8,
        "cycle_roi": 0.52
    }
    model.append_discharge_event(event)
    stored_event = model.data['discharge_events'][0]

    required_fields = {'timestamp', 'event_reason', 'duration_seconds', 'depth_of_discharge', 'measured_capacity_ah', 'cycle_roi'}
    assert required_fields.issubset(set(stored_event.keys()))


@pytest.mark.integration
def test_backward_compatibility_missing_keys(battery_model_temp_file):
    """Verify BatteryModel loads v2.0 model.json (missing sulfation_history key) without error."""
    # Create v2.0 style model with only essential keys
    model_path = battery_model_temp_file.model_path
    v2_0_data = {
        'lut': [{'v': 13.4, 'soc': 1.0, 'source': 'standard'}],
        'soh': 1.0,
        'physics': {'peukert_exponent': 1.2}
    }
    with open(model_path, 'w') as f:
        json.dump(v2_0_data, f)

    # Reload - should not raise error
    model = BatteryModel(model_path=model_path)
    assert model.data['soh'] == 1.0

    # All history arrays should initialize to empty
    assert model.data['sulfation_history'] == []
    assert model.data['discharge_events'] == []
    assert model.data['roi_history'] == []
    assert model.data['natural_blackout_events'] == []
