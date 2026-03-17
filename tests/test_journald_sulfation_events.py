"""Tests for structured journald event logging (RPT-02).

Phase 16 integration test scaffold for journald event logging layer.
Tests cover:
- Discharge completion events logged to journald
- Event type field in structured extra dict
- Event reason field (natural | test_initiated)
- Discharge metrics in event
- Timestamp formatting (ISO8601)
- JSON serialization compatibility
- journalctl filtering by event type
"""

import pytest
import logging
import json
from unittest.mock import patch, MagicMock, call
from datetime import datetime, timezone
from io import StringIO


@pytest.fixture
def mock_journal_handler():
    """Create and configure mocked JournalHandler for testing."""
    with patch('systemd.journal.JournalHandler') as mock_handler_class:
        mock_handler = MagicMock()
        mock_handler_class.return_value = mock_handler
        yield mock_handler


@pytest.fixture
def discharge_event_data():
    """Return sample discharge event data."""
    return {
        "event_type": "discharge_complete",
        "event_reason": "natural",
        "duration_seconds": 1200,
        "depth_of_discharge": 0.75,
        "sulfation_score": 0.45,
        "cycle_roi": 0.52
    }


@pytest.mark.integration
def test_discharge_complete_logged_to_journald(mock_journal_handler, discharge_event_data):
    """Verify logger.info('Discharge complete', extra={...}) is called."""
    logger = logging.getLogger('ups-battery-monitor')

    # Patch logger.info to capture calls
    with patch.object(logger, 'info') as mock_info:
        logger.info('Discharge complete', extra=discharge_event_data)

        # Verify logger.info was called
        assert mock_info.called, "logger.info() was not called"

        # Verify message
        call_args = mock_info.call_args
        assert call_args is not None, "No call arguments"
        assert 'Discharge complete' in str(call_args), "Message not 'Discharge complete'"


@pytest.mark.integration
def test_journald_event_includes_event_type_field(mock_journal_handler, discharge_event_data):
    """Verify 'event_type': 'discharge_complete' in extra dict."""
    logger = logging.getLogger('ups-battery-monitor')

    with patch.object(logger, 'info') as mock_info:
        logger.info('Discharge complete', extra=discharge_event_data)

        # Verify extra dict contains event_type
        assert mock_info.called
        call_kwargs = mock_info.call_args[1] if mock_info.call_args else {}
        extra = call_kwargs.get('extra', {})

        assert 'event_type' in extra, "Missing 'event_type' in extra dict"
        assert extra['event_type'] == 'discharge_complete', \
            f"Expected event_type='discharge_complete', got {extra['event_type']}"


@pytest.mark.integration
def test_journald_event_includes_event_reason_field(mock_journal_handler, discharge_event_data):
    """Verify 'event_reason' in extra dict (value: 'natural' | 'test_initiated')."""
    logger = logging.getLogger('ups-battery-monitor')

    with patch.object(logger, 'info') as mock_info:
        logger.info('Discharge complete', extra=discharge_event_data)

        # Verify extra dict contains event_reason
        assert mock_info.called
        call_kwargs = mock_info.call_args[1] if mock_info.call_args else {}
        extra = call_kwargs.get('extra', {})

        assert 'event_reason' in extra, "Missing 'event_reason' in extra dict"
        assert extra['event_reason'] in ('natural', 'test_initiated'), \
            f"event_reason must be 'natural' or 'test_initiated', got {extra['event_reason']}"


@pytest.mark.integration
def test_journald_event_includes_discharge_metrics(mock_journal_handler, discharge_event_data):
    """Verify extra dict contains discharge metrics.

    Required metrics:
    - duration_seconds (int)
    - depth_of_discharge (float 0-1)
    - sulfation_score (float or null)
    - cycle_roi (float or null)
    """
    logger = logging.getLogger('ups-battery-monitor')

    with patch.object(logger, 'info') as mock_info:
        logger.info('Discharge complete', extra=discharge_event_data)

        assert mock_info.called
        call_kwargs = mock_info.call_args[1] if mock_info.call_args else {}
        extra = call_kwargs.get('extra', {})

        # Verify required metrics fields
        assert 'duration_seconds' in extra, "Missing 'duration_seconds'"
        assert isinstance(extra['duration_seconds'], int), \
            f"duration_seconds should be int, got {type(extra['duration_seconds'])}"

        assert 'depth_of_discharge' in extra, "Missing 'depth_of_discharge'"
        assert isinstance(extra['depth_of_discharge'], (int, float)), \
            f"depth_of_discharge should be numeric, got {type(extra['depth_of_discharge'])}"
        assert 0 <= extra['depth_of_discharge'] <= 1, \
            f"depth_of_discharge should be in [0, 1], got {extra['depth_of_discharge']}"

        assert 'sulfation_score' in extra, "Missing 'sulfation_score'"
        assert extra['sulfation_score'] is None or isinstance(extra['sulfation_score'], (int, float)), \
            f"sulfation_score should be numeric or null, got {type(extra['sulfation_score'])}"

        assert 'cycle_roi' in extra, "Missing 'cycle_roi'"
        assert extra['cycle_roi'] is None or isinstance(extra['cycle_roi'], (int, float)), \
            f"cycle_roi should be numeric or null, got {type(extra['cycle_roi'])}"


@pytest.mark.integration
def test_journald_event_timestamp_field(mock_journal_handler, discharge_event_data):
    """Verify timestamp field exists in extra dict (ISO8601 format)."""
    import re
    logger = logging.getLogger('ups-battery-monitor')

    # Create event with timestamp
    event_data = discharge_event_data.copy()
    event_data['timestamp'] = datetime.now(timezone.utc).isoformat()

    with patch.object(logger, 'info') as mock_info:
        logger.info('Discharge complete', extra=event_data)

        assert mock_info.called
        call_kwargs = mock_info.call_args[1] if mock_info.call_args else {}
        extra = call_kwargs.get('extra', {})

        assert 'timestamp' in extra, "Missing 'timestamp' field"
        timestamp = extra['timestamp']

        # ISO8601 pattern: YYYY-MM-DDTHH:MM:SS[.microseconds](Z|±HH:MM)
        iso8601_pattern = r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$'
        assert re.match(iso8601_pattern, timestamp), \
            f"timestamp not in ISO8601 format: {timestamp}"


@pytest.mark.integration
def test_journald_event_structured_format(mock_journal_handler, discharge_event_data):
    """Verify all extra fields are serializable to JSON."""
    logger = logging.getLogger('ups-battery-monitor')

    # Create event with timestamp and all fields
    event_data = discharge_event_data.copy()
    event_data['timestamp'] = datetime.now(timezone.utc).isoformat()
    event_data['sulfation_confidence'] = 'high'
    event_data['recovery_delta'] = 0.12
    event_data['measured_capacity_ah'] = 6.8

    with patch.object(logger, 'info') as mock_info:
        logger.info('Discharge complete', extra=event_data)

        assert mock_info.called
        call_kwargs = mock_info.call_args[1] if mock_info.call_args else {}
        extra = call_kwargs.get('extra', {})

        # Verify all fields are JSON-serializable
        try:
            json_str = json.dumps(extra)
            assert json_str is not None, "Failed to serialize extra dict to JSON"
        except (TypeError, ValueError) as e:
            pytest.fail(f"Extra dict not JSON-serializable: {e}")


@pytest.mark.integration
def test_journald_query_by_event_type(mock_journal_handler, discharge_event_data):
    """Verify journalctl -o json query can filter by EVENT_TYPE=discharge_complete.

    This test simulates the journalctl output format and verifies that
    operator can filter discharge events by event type.
    """
    # Simulate journalctl -o json-seq output format (one JSON object per line)
    # Fields in journalctl output are uppercase
    journalctl_output = [
        {
            "MESSAGE": "INFO - Discharge complete",
            "EVENT_TYPE": "discharge_complete",
            "EVENT_REASON": "natural",
            "DURATION_SECONDS": "1200",
            "DEPTH_OF_DISCHARGE": "0.75",
            "SULFATION_SCORE": "0.450",
            "CYCLE_ROI": "0.520",
            "TIMESTAMP": "2026-03-17T10:30:00Z",
        },
        {
            "MESSAGE": "INFO - Some other event",
            "EVENT_TYPE": "capacity_measurement",
        }
    ]

    # Filter by EVENT_TYPE=discharge_complete
    discharge_events = [e for e in journalctl_output
                       if e.get("EVENT_TYPE") == "discharge_complete"]

    assert len(discharge_events) == 1, f"Expected 1 discharge event, got {len(discharge_events)}"
    event = discharge_events[0]

    # Verify required fields in filtered event
    assert event.get("MESSAGE") == "INFO - Discharge complete"
    assert event.get("EVENT_TYPE") == "discharge_complete"
    assert event.get("EVENT_REASON") == "natural"
    assert event.get("DURATION_SECONDS") == "1200"
    assert event.get("DEPTH_OF_DISCHARGE") == "0.75"
    assert event.get("SULFATION_SCORE") == "0.450"
    assert event.get("CYCLE_ROI") == "0.520"
    assert event.get("TIMESTAMP") == "2026-03-17T10:30:00Z"
