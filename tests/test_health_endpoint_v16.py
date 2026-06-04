"""Tests for health.json export (RPT-01).

Integration test scaffold for health endpoint export layer.
Tests cover:
- File creation and basic structure
- Diagnostic fields (days_since_deep, ir_trend_rate, cycle_budget_remaining)
- Scheduling fields (scheduling_reason, next_test_timestamp)
- Discharge fields (last_discharge_timestamp)
- Null handling for optional fields
- Backward compatibility with v2.0 fields
- ISO8601 and unix timestamp formats
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from src.monitor_config import HealthSnapshot, write_health_endpoint


@pytest.fixture
def health_endpoint_temp_file():
    """Create temporary health.json file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        health_path = Path(tmpdir) / "health.json"
        yield health_path


@pytest.fixture
def baseline_health_params():
    """Return baseline health parameters for testing."""
    return {
        "soc_percent": 75.5,
        "is_online": True,
        "poll_latency_ms": 0.3,
        "capacity_ah_measured": 6.8,
        "capacity_ah_rated": 7.2,
        "capacity_confidence": 0.95,
        "capacity_samples_count": 5,
        "capacity_converged": True,
    }


@pytest.mark.integration
def test_write_health_endpoint_creates_file(health_endpoint_temp_file, baseline_health_params):
    """Verify write_health_endpoint() creates health.json file."""
    with patch("src.monitor_config.HEALTH_ENDPOINT_PATH", health_endpoint_temp_file):
        write_health_endpoint(HealthSnapshot(**baseline_health_params))
        assert health_endpoint_temp_file.exists(), "health.json file not created"

        # Verify valid JSON
        with open(health_endpoint_temp_file) as f:
            data = json.load(f)
        assert isinstance(data, dict), "health.json is not a valid JSON object"


@pytest.mark.integration
def test_health_endpoint_includes_shutdown_imminent(
    health_endpoint_temp_file, baseline_health_params
):
    """shutdown_imminent flag is serialized to health.json for operator visibility."""
    with patch("src.monitor_config.HEALTH_ENDPOINT_PATH", health_endpoint_temp_file):
        write_health_endpoint(
            HealthSnapshot(**{**baseline_health_params, "shutdown_imminent": True})
        )
        with open(health_endpoint_temp_file) as f:
            data = json.load(f)
        assert data["shutdown_imminent"] is True


def test_health_endpoint_includes_soh_alert_threshold(
    health_endpoint_temp_file, baseline_health_params
):
    """WR-01: the configured soh_alert_threshold is serialized so the read-only CLI/MOTD
    can reproduce the daemon's replacement date for any configured value."""
    with patch("src.monitor_config.HEALTH_ENDPOINT_PATH", health_endpoint_temp_file):
        write_health_endpoint(
            HealthSnapshot(**{**baseline_health_params, "soh_alert_threshold": 0.75})
        )
        with open(health_endpoint_temp_file) as f:
            data = json.load(f)
        assert data["soh_alert_threshold"] == 0.75


@pytest.mark.integration
def test_health_endpoint_includes_diagnostic_fields(
    health_endpoint_temp_file, baseline_health_params
):
    """Verify health.json contains diagnostic and scheduling fields.

    Required fields:
    - days_since_deep (float or null)
    - ir_trend_rate (float or null)
    - cycle_budget_remaining (int or null)
    - scheduling_reason (string)
    - next_test_timestamp (ISO8601 string or null)
    """
    with patch("src.monitor_config.HEALTH_ENDPOINT_PATH", health_endpoint_temp_file):
        write_health_endpoint(
            HealthSnapshot(
                **baseline_health_params,
                days_since_deep=7.2,
                ir_trend_rate=0.000008,
                cycle_budget_remaining=150,
                scheduling_reason="observing",
                next_test_timestamp="2024-03-19T11:30:00+00:00",
            )
        )

        with open(health_endpoint_temp_file) as f:
            data = json.load(f)

        assert "days_since_deep" in data, "Missing days_since_deep field"
        assert data["days_since_deep"] == 7.2, f"Expected 7.2, got {data['days_since_deep']}"

        assert "ir_trend_rate" in data, "Missing ir_trend_rate field"
        assert abs(data["ir_trend_rate"] - 0.000008) < 1e-9, "ir_trend_rate precision incorrect"

        assert "cycle_budget_remaining" in data, "Missing cycle_budget_remaining field"
        assert data["cycle_budget_remaining"] == 150

        assert "scheduling_reason" in data, "Missing scheduling_reason field"
        assert data["scheduling_reason"] == "observing"

        assert "next_test_timestamp" in data, "Missing next_test_timestamp field"
        assert data["next_test_timestamp"] == "2024-03-19T11:30:00+00:00"


@pytest.mark.integration
def test_health_endpoint_includes_v16_discharge_fields(
    health_endpoint_temp_file, baseline_health_params
):
    """Verify health.json contains discharge fields.

    Required fields:
    - last_discharge_timestamp (ISO8601 string or null)
    """
    with patch("src.monitor_config.HEALTH_ENDPOINT_PATH", health_endpoint_temp_file):
        write_health_endpoint(
            HealthSnapshot(
                **baseline_health_params,
                last_discharge_timestamp="2026-03-17T10:00:00Z",
            )
        )

        with open(health_endpoint_temp_file) as f:
            data = json.load(f)

        assert "last_discharge_timestamp" in data, "Missing last_discharge_timestamp field"
        assert data["last_discharge_timestamp"] == "2026-03-17T10:00:00Z"


@pytest.mark.integration
def test_health_endpoint_nulls_when_optional_fields_not_provided(
    health_endpoint_temp_file, baseline_health_params
):
    """Verify health.json serializes None optional fields without error."""
    with patch("src.monitor_config.HEALTH_ENDPOINT_PATH", health_endpoint_temp_file):
        # Call without optional parameters (all default to None)
        write_health_endpoint(HealthSnapshot(**baseline_health_params))

        with open(health_endpoint_temp_file) as f:
            data = json.load(f)

        # Verify diagnostic fields exist but are null when not provided
        assert "days_since_deep" in data, "days_since_deep field missing"
        assert data["days_since_deep"] is None

        assert "ir_trend_rate" in data, "ir_trend_rate field missing"
        assert data["ir_trend_rate"] is None

        assert "last_discharge_timestamp" in data, "last_discharge_timestamp field missing"
        assert data["last_discharge_timestamp"] is None


@pytest.mark.integration
def test_health_endpoint_preserves_v20_fields(health_endpoint_temp_file, baseline_health_params):
    """Verify health.json includes all v2.0 fields.

    v2.0 fields:
    - last_poll (ISO8601)
    - current_soc_percent (float)
    - online (bool)
    - daemon_version (string)
    - poll_latency_ms (float)
    - capacity_ah_measured (float)
    - capacity_ah_rated (float)
    - capacity_confidence (float)
    - capacity_samples_count (int)
    - capacity_converged (bool)
    """
    with patch("src.monitor_config.HEALTH_ENDPOINT_PATH", health_endpoint_temp_file):
        write_health_endpoint(HealthSnapshot(**baseline_health_params))

        with open(health_endpoint_temp_file) as f:
            data = json.load(f)

        # V2.0 fields
        assert "last_poll" in data, "Missing last_poll field"
        assert isinstance(data["last_poll"], str), "last_poll should be string (ISO8601)"

        assert "last_poll_unix" in data, "Missing last_poll_unix field"
        assert isinstance(data["last_poll_unix"], int), "last_poll_unix should be int"

        assert "current_soc_percent" in data, "Missing current_soc_percent field"
        assert data["current_soc_percent"] == 75.5

        assert "online" in data, "Missing online field"
        assert data["online"] is True

        assert "daemon_version" in data, "Missing daemon_version field"
        assert isinstance(data["daemon_version"], str)

        assert "poll_latency_ms" in data, "Missing poll_latency_ms field"
        assert data["poll_latency_ms"] == 0.3

        assert "capacity_ah_measured" in data, "Missing capacity_ah_measured field"
        assert data["capacity_ah_measured"] == 6.8

        assert "capacity_ah_rated" in data, "Missing capacity_ah_rated field"
        assert data["capacity_ah_rated"] == 7.2

        assert "capacity_confidence" in data, "Missing capacity_confidence field"
        assert data["capacity_confidence"] == 0.95

        assert "capacity_samples_count" in data, "Missing capacity_samples_count field"
        assert data["capacity_samples_count"] == 5

        assert "capacity_converged" in data, "Missing capacity_converged field"
        assert data["capacity_converged"] is True


@pytest.mark.integration
def test_health_endpoint_iso8601_timestamps(health_endpoint_temp_file, baseline_health_params):
    """Verify last_poll and last_discharge_timestamp use ISO8601 format."""
    with patch("src.monitor_config.HEALTH_ENDPOINT_PATH", health_endpoint_temp_file):
        write_health_endpoint(
            HealthSnapshot(
                **baseline_health_params,
                last_discharge_timestamp="2026-03-17T10:30:00Z",
            )
        )

        with open(health_endpoint_temp_file) as f:
            data = json.load(f)

        # Verify ISO8601 format: YYYY-MM-DDTHH:MM:SS[.microseconds](Z|±HH:MM)
        import re

        iso8601_pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"

        assert re.match(iso8601_pattern, data["last_poll"]), (
            f"last_poll not in ISO8601 format: {data['last_poll']}"
        )

        assert re.match(iso8601_pattern, data["last_discharge_timestamp"]), (
            f"last_discharge_timestamp not in ISO8601 format: {data['last_discharge_timestamp']}"
        )


@pytest.mark.integration
def test_health_endpoint_unix_timestamp(health_endpoint_temp_file, baseline_health_params):
    """Verify last_poll_unix field contains integer unix timestamp."""
    fixed_time = 1710800000.0
    with (
        patch("src.monitor_config.HEALTH_ENDPOINT_PATH", health_endpoint_temp_file),
        patch("src.monitor_config.time.time", return_value=fixed_time),
    ):
        write_health_endpoint(HealthSnapshot(**baseline_health_params))

        with open(health_endpoint_temp_file) as f:
            data = json.load(f)

        assert "last_poll_unix" in data, "Missing last_poll_unix field"
        assert isinstance(data["last_poll_unix"], int), "last_poll_unix should be integer"
        assert data["last_poll_unix"] == int(fixed_time)
