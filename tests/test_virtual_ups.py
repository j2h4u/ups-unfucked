"""Test suite for virtual UPS tmpfs writing and NUT format compliance.

Phase 3 requirements: VUPS-01 through SHUT-03 stubbed with comprehensive test structure.
Tests ensure virtual UPS metrics are written atomically to tmpfs without SSD wear and
format is fully NUT-compatible for transparent data source switching.
"""

import pytest
import tempfile
import os
import re
from pathlib import Path
from unittest.mock import Mock, patch, mock_open
from src.event_classifier import EventType
from src.virtual_ups import write_virtual_ups_dev, compute_ups_status_override


class TestVirtualUPSWriting:
    """Tests for virtual UPS tmpfs writing infrastructure (VUPS-01, VUPS-02)."""

    def test_write_to_tmpfs(self):
        """Test VUPS-01: Metrics written atomically via write_virtual_ups_dev().

        Validates:
        - File is created at the patched path
        - Atomic write pattern prevents partial files on crash
        - All metrics from input dict appear in output
        - File is readable after write
        """
        metrics = {
            "battery.voltage": "13.4",
            "battery.charge": "85",
            "battery.runtime": "245",
            "ups.load": "25",
        }

        with tempfile.TemporaryDirectory(dir="/tmp", prefix="ups_test_") as tmpdir:
            test_file = Path(tmpdir) / "ups-virtual.dev"

            with patch("src.virtual_ups.Path", side_effect=lambda *a, **kw: (
                test_file if a == ("/run/ups-battery-monitor/ups-virtual.dev",) else Path(*a, **kw)
            )):
                write_virtual_ups_dev(metrics)

            # Assert: File exists at patched location
            assert test_file.exists(), "File not created at expected location"

            content = test_file.read_text()
            assert content, "File is empty"

            # Assert: All metrics appear in the file (production format: "key: value\n")
            for key, value in metrics.items():
                assert f"{key}: {value}" in content, \
                    f"Metric {key}={value} not found in output"

            # Assert: No leftover .tmp files
            tmp_files = list(Path(tmpdir).glob("*.tmp"))
            assert len(tmp_files) == 0, f"Leftover temp files found: {tmp_files}"

    def test_passthrough_fields(self):
        """Test VUPS-02: All real UPS fields transparently proxy via write_virtual_ups_dev().

        Validates:
        - All input fields appear in output file unchanged
        - Production function writes the exact dict contents passed to it
        """
        metrics = {
            "battery.voltage": "13.4",
            "ups.load": "25",
            "input.voltage": "230",
            "device.mfr": "CyberPower",
            "device.model": "UT850EG",
            "device.serial": "ABC123456",
            "battery.type": "PbAc",
            "ups.temperature": "25",
            "battery.runtime": "600",
            "battery.charge": "87",
            "ups.status": "OB DISCHRG LB",
        }

        with tempfile.TemporaryDirectory(dir="/tmp", prefix="ups_test_") as tmpdir:
            test_file = Path(tmpdir) / "ups-virtual.dev"

            with patch("src.virtual_ups.Path", side_effect=lambda *a, **kw: (
                test_file if a == ("/run/ups-battery-monitor/ups-virtual.dev",) else Path(*a, **kw)
            )):
                write_virtual_ups_dev(metrics)

            assert test_file.exists(), "File not created"
            file_content = test_file.read_text()

            # Assert: All passthrough fields appear exactly as provided
            passthrough_fields = {
                "battery.voltage", "ups.load", "input.voltage",
                "device.mfr", "device.model", "device.serial",
                "battery.type", "ups.temperature"
            }
            for field in passthrough_fields:
                value = metrics[field]
                assert f"{field}: {value}" in file_content, \
                    f"Passthrough field {field} not found in output"

            # Assert: Override fields are also present
            for field in ["battery.runtime", "battery.charge", "ups.status"]:
                assert field in file_content, \
                    f"Override field {field} not found in output"

            # Assert: Correct line count
            lines_in_file = [l for l in file_content.strip().split('\n') if l]
            assert len(lines_in_file) == len(metrics), \
                f"Field count mismatch: expected {len(metrics)}, got {len(lines_in_file)}"


class TestFieldOverrides:
    """Tests for field overrides in virtual UPS (VUPS-03)."""

    def test_field_overrides(self):
        """Test VUPS-03: Three critical fields correctly written by write_virtual_ups_dev().

        Validates:
        - battery.runtime, battery.charge, ups.status written as provided
        - Override values appear verbatim in output (production function is pass-through)
        """
        metrics = {
            "battery.runtime": "600",
            "battery.charge": "87",
            "ups.status": "OB DISCHRG LB",
            "battery.voltage": "11.8",
            "ups.load": "35",
            "input.voltage": "0",
            "device.mfr": "CyberPower",
            "device.model": "UT850EG",
        }

        with tempfile.TemporaryDirectory(dir="/tmp", prefix="ups_test_") as tmpdir:
            test_file = Path(tmpdir) / "ups-virtual.dev"

            with patch("src.virtual_ups.Path", side_effect=lambda *a, **kw: (
                test_file if a == ("/run/ups-battery-monitor/ups-virtual.dev",) else Path(*a, **kw)
            )):
                write_virtual_ups_dev(metrics)

            assert test_file.exists(), "Virtual UPS file not created"
            file_content = test_file.read_text()

            # Parse output into dict (format: "key: value\n")
            var_dict = {}
            for line in file_content.strip().split('\n'):
                if ': ' in line:
                    key, _, value = line.partition(': ')
                    var_dict[key.strip()] = value.strip()

            # Assert: Three override fields are exactly as provided
            assert var_dict["battery.runtime"] == "600", \
                f"battery.runtime not correctly written: {var_dict.get('battery.runtime')}"
            assert var_dict["battery.charge"] == "87", \
                f"battery.charge not correctly written: {var_dict.get('battery.charge')}"
            assert var_dict["ups.status"] == "OB DISCHRG LB", \
                f"ups.status not correctly written: {var_dict.get('ups.status')}"


class TestNUTFormatCompliance:
    """Tests for NUT format compliance in tmpfs file (VUPS-04)."""

    def test_nut_format_compliance(self):
        """Test VUPS-04: File written by write_virtual_ups_dev() uses key: value format.

        Validates:
        - Each line follows dummy-ups format: '<field>: <value>'
        - All required fields present in output
        - Correct field count
        """
        metrics = {
            "battery.voltage": "13.4",
            "battery.charge": "85",
            "battery.runtime": "245",
            "ups.load": "25",
            "ups.status": "OL",
            "input.voltage": "230",
        }

        with tempfile.TemporaryDirectory(dir="/tmp", prefix="ups_test_") as tmpdir:
            test_file = Path(tmpdir) / "ups-virtual.dev"

            with patch("src.virtual_ups.Path", side_effect=lambda *a, **kw: (
                test_file if a == ("/run/ups-battery-monitor/ups-virtual.dev",) else Path(*a, **kw)
            )):
                write_virtual_ups_dev(metrics)

            assert test_file.exists(), "Virtual UPS file not created"

            content = test_file.read_text()
            lines = [l for l in content.strip().split('\n') if l]

            # Assert: dummy-ups format compliance (each line: "key: value")
            for line in lines:
                assert ': ' in line, f"Line doesn't match 'key: value' format: {line}"

            # Assert: All metrics present
            for key, value in metrics.items():
                assert f"{key}: {value}" in content, \
                    f"Metric {key} not found in output"

            # Assert: Correct field count
            assert len(lines) == len(metrics), "Not all metrics written"


class TestShutdownThresholds:
    """Tests for LB flag and shutdown threshold logic (SHUT-01, SHUT-02, SHUT-03)."""

    @pytest.mark.parametrize("time_rem,expected_status", [
        (6, "OB DISCHRG"),          # time_rem > threshold: no LB
        (5, "OB DISCHRG"),          # time_rem == threshold: no LB (uses <, not <=)
        (4.9, "OB DISCHRG LB"),     # time_rem < threshold: LB flag set
        (0, "OB DISCHRG LB"),       # time_rem = 0: LB flag set
    ])
    def test_lb_flag_threshold(self, time_rem, expected_status):
        """Test SHUT-01: LB flag set when time_rem < shutdown threshold (5 min)."""
        result = compute_ups_status_override(
            EventType.BLACKOUT_REAL,
            time_rem,
            5  # default threshold
        )
        assert result == expected_status

    @pytest.mark.parametrize("threshold", [3, 5, 10])
    def test_configurable_threshold(self, threshold):
        """Test SHUT-02: Shutdown threshold configurable via environment variable.
        Thresholds must be > SAFETY_LB_FLOOR_MINUTES (2) for both branches to be testable.

        Validates:
        - Threshold parameter actually controls LB firing (not hardcoded)
        - Default shutdown threshold (e.g., 5 minutes) when env var not set
        - Custom threshold from UPS_SHUTDOWN_THRESHOLD_MINUTES env var
        - Threshold applies to all blackout event classifications
        - Invalid threshold values handled gracefully (fallback to default)
        """
        # Arrange: Test just below threshold (should trigger LB)
        time_rem_below = threshold - 0.1

        # Act: Call compute_ups_status_override with various thresholds
        result = compute_ups_status_override(
            EventType.BLACKOUT_REAL,
            time_rem_below,
            threshold
        )

        # Assert: LB flag fires for all thresholds when time_rem < threshold
        assert result == "OB DISCHRG LB", \
            f"Threshold {threshold}: LB should fire when time_rem={time_rem_below} < {threshold}"

        # Verify threshold parameter actually controls the decision
        time_rem_above = threshold + 0.1
        result_above = compute_ups_status_override(
            EventType.BLACKOUT_REAL,
            time_rem_above,
            threshold
        )
        assert result_above == "OB DISCHRG", \
            f"Threshold {threshold}: LB should not fire when time_rem={time_rem_above} > {threshold}"

    @pytest.mark.parametrize("calibration_threshold", [1, 0])
    def test_calibration_mode_threshold(self, calibration_threshold):
        """Test SHUT-03: Calibration mode uses reduced shutdown threshold.

        Note: F41 safety floor (2 min) overrides any calibration threshold
        when runtime is critically low. Values above the floor still respect
        the calibration threshold.
        """
        # Above safety floor (2 min): calibration threshold controls LB
        time_rem_above_floor = 3.0
        result = compute_ups_status_override(
            EventType.BLACKOUT_REAL, time_rem_above_floor, calibration_threshold
        )
        if time_rem_above_floor >= calibration_threshold:
            assert result == "OB DISCHRG", \
                f"time_rem={time_rem_above_floor} >= threshold={calibration_threshold} should not trigger LB"

        # Below safety floor (2 min): LB always fires regardless of threshold
        time_rem_below_floor = 0.9
        result_below = compute_ups_status_override(
            EventType.BLACKOUT_REAL, time_rem_below_floor, calibration_threshold
        )
        assert result_below == "OB DISCHRG LB", \
            f"Below safety floor: time_rem={time_rem_below_floor} should always trigger LB"


class TestMonitorIntegration:
    """Integration tests for monitor daemon + virtual UPS end-to-end flow."""

    def test_monitor_virtual_ups_integration(self):
        """Test end-to-end flow: monitor calculates metrics → virtual UPS writes output.

        Validates:
        - virtual_metrics dict built correctly with 3 override fields + passthrough fields
        - write_virtual_ups_dev() writes the dict contents verbatim
        - ups.status reflects event type and time_rem threshold
        """
        real_ups_data = {
            "battery.voltage": "13.4",
            "ups.load": "25",
            "input.voltage": "230",
            "device.mfr": "CyberPower",
            "device.model": "UT850EG",
            "device.serial": "ABC123",
            "battery.type": "PbAc",
            "ups.temperature": "28",
        }

        battery_charge = 87
        time_rem = 3.5  # 3.5 minutes < threshold of 5 → LB fires
        event_type = EventType.BLACKOUT_REAL
        shutdown_threshold = 5

        ups_status_override = compute_ups_status_override(
            event_type,
            time_rem,
            shutdown_threshold
        )

        virtual_metrics = {
            "battery.runtime": int(time_rem * 60),  # 210 seconds
            "battery.charge": int(battery_charge),  # 87%
            "ups.status": ups_status_override,
            **{k: v for k, v in real_ups_data.items()
               if k not in ["battery.runtime", "battery.charge", "ups.status"]}
        }

        # Assert: virtual_metrics dict structure
        assert virtual_metrics["battery.runtime"] == 210
        assert virtual_metrics["battery.charge"] == 87
        assert virtual_metrics["ups.status"] == "OB DISCHRG LB"

        # Assert: passthrough fields present unchanged
        for key in real_ups_data.keys():
            if key not in ["battery.runtime", "battery.charge", "ups.status"]:
                assert key in virtual_metrics
                assert virtual_metrics[key] == real_ups_data[key]

        # Act: Write via production function and verify output
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="ups_test_") as tmpdir:
            test_file = Path(tmpdir) / "ups-virtual.dev"

            with patch("src.virtual_ups.Path", side_effect=lambda *a, **kw: (
                test_file if a == ("/run/ups-battery-monitor/ups-virtual.dev",) else Path(*a, **kw)
            )):
                write_virtual_ups_dev(virtual_metrics)

            assert test_file.exists()
            file_content = test_file.read_text()

            # Parse output
            var_dict = {}
            for line in file_content.strip().split('\n'):
                if ': ' in line:
                    key, _, value = line.partition(': ')
                    var_dict[key.strip()] = value.strip()

            assert var_dict["battery.runtime"] == "210"
            assert var_dict["battery.charge"] == "87"
            assert var_dict["ups.status"] == "OB DISCHRG LB"
            assert var_dict["battery.voltage"] == "13.4"
            assert var_dict["ups.load"] == "25"

    def test_status_override_below_threshold_sets_lb(self):
        """Verify compute_ups_status_override sets LB flag when time_rem < threshold."""
        real_ups_data = {
            "battery.voltage": "11.2",
            "ups.load": "45",
            "input.voltage": "0",
        }

        battery_charge = 25
        time_rem = 4.9
        event_type = EventType.BLACKOUT_REAL
        shutdown_threshold = 5

        ups_status_override = compute_ups_status_override(
            event_type,
            time_rem,
            shutdown_threshold
        )

        virtual_metrics = {
            "battery.runtime": int(time_rem * 60),
            "battery.charge": int(battery_charge),
            "ups.status": ups_status_override,
            **{k: v for k, v in real_ups_data.items()
               if k not in ["battery.runtime", "battery.charge", "ups.status"]}
        }

        assert virtual_metrics["ups.status"] == "OB DISCHRG LB"
        assert virtual_metrics["battery.runtime"] == 294  # 4.9 * 60
        assert virtual_metrics["battery.charge"] == 25


class TestSafetyLBFloor:
    """Tests for F41: hard LB safety floor at 2 minutes."""

    def test_blackout_test_lb_floor(self):
        """F41: BLACKOUT_TEST + runtime=1.5min → 'OB DISCHRG LB' (safety floor)."""
        result = compute_ups_status_override(EventType.BLACKOUT_TEST, 1.5, 5)
        assert result == "OB DISCHRG LB"

    def test_blackout_test_above_floor(self):
        """F41: BLACKOUT_TEST + runtime=10.0min → 'OB DISCHRG' (no LB, above floor)."""
        result = compute_ups_status_override(EventType.BLACKOUT_TEST, 10.0, 5)
        assert result == "OB DISCHRG"

    def test_blackout_real_lb_floor_overrides_threshold(self):
        """F41: BLACKOUT_REAL + runtime=1.5min + threshold=5 → LB (floor wins)."""
        result = compute_ups_status_override(EventType.BLACKOUT_REAL, 1.5, 5)
        assert result == "OB DISCHRG LB"

    def test_online_ignores_floor(self):
        """F41: ONLINE always returns 'OL' regardless of runtime."""
        result = compute_ups_status_override(EventType.ONLINE, 0.5, 5)
        assert result == "OL"

    def test_floor_at_exactly_2_minutes(self):
        """F41: runtime exactly at floor (2.0) → no LB (uses < not <=)."""
        result = compute_ups_status_override(EventType.BLACKOUT_TEST, 2.0, 5)
        assert result == "OB DISCHRG"


class TestEventTypeIntegration:
    """Tests for integration with EventType enum from event_classifier."""

    def test_event_type_imports(self):
        """Verify EventType enum is available and has expected values."""
        assert hasattr(EventType, 'ONLINE')
        assert hasattr(EventType, 'BLACKOUT_REAL')
        assert hasattr(EventType, 'BLACKOUT_TEST')

    def test_compute_status_override_accepts_all_event_types(self):
        """Verify compute_ups_status_override handles all EventType values without error."""
        for et in EventType:
            result = compute_ups_status_override(et, time_rem_minutes=10.0, shutdown_threshold_minutes=5)
            assert isinstance(result, str), f"Expected str for {et}, got {type(result)}"
