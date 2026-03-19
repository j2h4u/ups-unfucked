"""Tests for discharge event logging and persistence (RPT-03).

Phase 16 integration test scaffold for discharge event logging layer.
Tests cover:
- Appending discharge events to model
- Schema validation (required fields)
- Event reason values (natural | test_initiated)
- Persistence to model.json
- Timestamp format (ISO8601)
- Pruning to keep last N events
- Filtering discharge events by reason
"""

import pytest
import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from src.model import BatteryModel


@pytest.fixture
def battery_model_with_discharge_events():
    """Create BatteryModel with pre-populated discharge_events array."""
    with tempfile.TemporaryDirectory() as tmpdir:
        model_path = Path(tmpdir) / "model.json"
        battery_model = BatteryModel(model_path=model_path)
        # Initialize discharge_events if not present
        if 'discharge_events' not in battery_model.data:
            battery_model.data['discharge_events'] = []
        yield battery_model


@pytest.fixture
def sample_discharge_event():
    """Return sample discharge event."""
    return {
        "timestamp": "2026-03-17T10:30:00Z",
        "event_reason": "natural",
        "duration_seconds": 1200,
        "depth_of_discharge": 0.75,
        "measured_capacity_ah": 6.8,
        "cycle_roi": 0.52
    }


@pytest.mark.integration
def test_append_discharge_event_to_model(battery_model_with_discharge_events, sample_discharge_event):
    """Verify append_discharge_event() method exists and works."""
    model = battery_model_with_discharge_events
    model.append_discharge_event(sample_discharge_event)
    assert 'discharge_events' in model.data
    assert len(model.data['discharge_events']) == 1
    assert model.data['discharge_events'][0] == sample_discharge_event


@pytest.mark.integration
def test_discharge_event_schema_required_fields(battery_model_with_discharge_events, sample_discharge_event):
    """Verify discharge_event has all required fields.

    Required fields:
    - timestamp (ISO8601 string)
    - event_reason ('natural' | 'test_initiated')
    - duration_seconds (int)
    - depth_of_discharge (float 0-1)
    - measured_capacity_ah (float or null)
    - cycle_roi (float)
    """
    model = battery_model_with_discharge_events
    event = sample_discharge_event.copy()
    model.append_discharge_event(event)
    stored_event = model.data['discharge_events'][0]

    required_fields = {'timestamp', 'event_reason', 'duration_seconds', 'depth_of_discharge', 'measured_capacity_ah', 'cycle_roi'}
    assert required_fields.issubset(set(stored_event.keys()))


@pytest.mark.integration
def test_discharge_event_reason_values(battery_model_with_discharge_events, sample_discharge_event):
    """Verify event_reason accepts only valid values ('natural' | 'test_initiated')."""
    model = battery_model_with_discharge_events

    # Test with 'natural'
    event_natural = sample_discharge_event.copy()
    event_natural['event_reason'] = 'natural'
    model.append_discharge_event(event_natural)

    # Test with 'test_initiated'
    event_test = sample_discharge_event.copy()
    event_test['timestamp'] = "2026-03-18T10:30:00Z"
    event_test['event_reason'] = 'test_initiated'
    model.append_discharge_event(event_test)

    assert model.data['discharge_events'][0]['event_reason'] == 'natural'
    assert model.data['discharge_events'][1]['event_reason'] == 'test_initiated'


@pytest.mark.integration
def test_discharge_event_persisted_in_model_json(battery_model_with_discharge_events, sample_discharge_event):
    """Verify discharge_events array persists after model.save()."""
    model = battery_model_with_discharge_events
    model.append_discharge_event(sample_discharge_event)
    model.save()

    # Reload from disk
    reloaded = BatteryModel(model_path=model.model_path)
    assert 'discharge_events' in reloaded.data
    assert len(reloaded.data['discharge_events']) == 1
    assert reloaded.data['discharge_events'][0] == sample_discharge_event


@pytest.mark.integration
def test_discharge_event_timestamp_format(battery_model_with_discharge_events, sample_discharge_event):
    """Verify timestamp is ISO8601 string format."""
    model = battery_model_with_discharge_events
    event = sample_discharge_event.copy()
    model.append_discharge_event(event)
    stored_event = model.data['discharge_events'][0]

    # Verify ISO8601 format: YYYY-MM-DDTHH:MM:SSZ
    timestamp = stored_event['timestamp']
    assert isinstance(timestamp, str)
    assert 'T' in timestamp
    assert timestamp.endswith('Z')
    # Basic parsing to verify format
    parts = timestamp.split('T')
    assert len(parts) == 2
    assert len(parts[0]) == 10  # YYYY-MM-DD


@pytest.mark.integration
def test_prune_discharge_events_keeps_last_30(battery_model_with_discharge_events, sample_discharge_event):
    """Verify _prune_discharge_events(keep_count=30) preserves only last 30."""
    model = battery_model_with_discharge_events
    # Append 50 events
    for i in range(50):
        event = sample_discharge_event.copy()
        event['timestamp'] = f"2026-03-{17+i:02d}T10:30:00Z"
        model.append_discharge_event(event)

    model._cap_history_entries('discharge_events', keep_count=30)
    assert len(model.data['discharge_events']) == 30


@pytest.mark.integration
def test_discharge_events_queryable_by_reason(battery_model_with_discharge_events, sample_discharge_event):
    """Verify discharge_events can be filtered by event_reason in model.data['discharge_events']."""
    model = battery_model_with_discharge_events

    # Add mix of natural and test_initiated events
    for i in range(3):
        event = sample_discharge_event.copy()
        event['timestamp'] = f"2026-03-{17+i:02d}T10:30:00Z"
        event['event_reason'] = 'natural'
        model.append_discharge_event(event)

    for i in range(2):
        event = sample_discharge_event.copy()
        event['timestamp'] = f"2026-03-{20+i:02d}T10:30:00Z"
        event['event_reason'] = 'test_initiated'
        model.append_discharge_event(event)

    # Filter by reason
    natural_events = [e for e in model.data['discharge_events'] if e['event_reason'] == 'natural']
    test_events = [e for e in model.data['discharge_events'] if e['event_reason'] == 'test_initiated']

    assert len(natural_events) == 3
    assert len(test_events) == 2
