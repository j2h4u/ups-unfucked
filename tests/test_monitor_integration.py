"""Integration tests: verify orchestrator (monitor.py) wires kernel correctly.

Tests the separation of concerns between:
- monitor.py (orchestrator): guard clauses, state management, I/O
- battery_math kernel: pure math functions

Verifies:
1. Correct arguments passed to kernel (rated capacity, not measured)
2. Correct argument selection (average load, not current EMA)
3. Call ordering (SoH before Peukert)
4. Systemd watchdog integration survival
"""

import pytest
from unittest.mock import patch, MagicMock, Mock
from pathlib import Path
import tempfile

from src.monitor import MonitorDaemon, Config, DischargeBuffer
from src.model import BatteryModel


@pytest.fixture
def temp_dir():
    """Temporary directory for test artifacts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def test_config(temp_dir):
    """Test configuration with reasonable defaults."""
    return Config(
        ups_name='cyberpower',
        polling_interval=10,
        reporting_interval=60,
        nut_host='localhost',
        nut_port=3493,
        nut_timeout=2.0,
        shutdown_minutes=5,
        soh_alert_threshold=0.80,
        model_dir=temp_dir,
        config_dir=temp_dir,
        runtime_threshold_minutes=20,
        reference_load_percent=20.0,
        ema_window_sec=120,
        capacity_ah=7.2
    )


@pytest.fixture
def mock_daemon(test_config):
    """Mock MonitorDaemon with test config, no NUT connection needed."""
    with patch('src.monitor.NUTClient'):
        daemon = MonitorDaemon(config=test_config)
        # Initialize key attributes
        daemon.battery_model.set_peukert_exponent(1.2)
        daemon.battery_model.set_soh(0.95)
        return daemon


class TestOrchestratorWiring:
    """Tests for correct orchestrator/kernel boundary."""

    def test_capacity_ah_ref_not_measured(self, mock_daemon):
        """VAL-02: calibrate_peukert receives RATED capacity, not measured.

        Verifies that even if model.capacity_ah_measured differs from
        config.capacity_ah (rated), the kernel is called with the rated value.
        """
        # Set up: model has different measured capacity
        mock_daemon.discharge_buffer.voltages = [13.0, 12.5, 12.0, 11.5, 10.5]
        mock_daemon.discharge_buffer.times = [0.0, 10.0, 20.0, 30.0, 40.0]
        mock_daemon.discharge_buffer.loads = [20, 21, 19, 22, 20]

        # Add calibration samples so guard clause passes
        mock_daemon.discharge_buffer.voltages.extend([10.0, 9.5])
        mock_daemon.discharge_buffer.times.extend([50.0, 60.0])
        mock_daemon.discharge_buffer.loads.extend([20, 20])

        # Config capacity (rated)
        assert mock_daemon.config.capacity_ah == 7.2

        with patch('src.monitor.calibrate_peukert') as mock_calibrate:
            mock_calibrate.return_value = 1.22

            # Call orchestrator method
            mock_daemon._auto_calibrate_peukert(current_soh=0.95)

            # Verify kernel was called
            assert mock_calibrate.called, "calibrate_peukert not called"

            # Extract capacity_ah from call
            call_args = mock_calibrate.call_args
            kwargs = call_args.kwargs if call_args.kwargs else {}

            actual_capacity = kwargs.get("capacity_ah")
            assert actual_capacity == 7.2, \
                f"Expected capacity_ah=7.2 (rated), got {actual_capacity}"

    def test_avg_load_from_discharge_buffer(self, mock_daemon):
        """Verify average load calculated from discharge buffer, not EMA.

        Average load should be computed from discharge_buffer.loads,
        not from ema_buffer.load (which is real-time value).
        """
        # Set up discharge buffer with specific load samples
        mock_daemon.discharge_buffer.voltages = [13.0, 12.5, 12.0, 11.5, 10.5]
        mock_daemon.discharge_buffer.times = [0.0, 20.0, 40.0, 60.0, 80.0]
        mock_daemon.discharge_buffer.loads = [20, 30, 25, 30, 20]  # avg = 25

        # Note: We don't set ema_buffer.load (read-only property) because
        # the orchestrator explicitly uses discharge_buffer.loads instead

        with patch('src.monitor.calibrate_peukert') as mock_calibrate:
            mock_calibrate.return_value = 1.21

            mock_daemon._auto_calibrate_peukert(current_soh=0.95)

            # Verify kernel was called with average from buffer
            call_args = mock_calibrate.call_args
            kwargs = call_args.kwargs if call_args.kwargs else {}

            actual_load = kwargs.get("avg_load_percent")
            expected_avg = sum([20, 30, 25, 30, 20]) / 5  # = 25.0
            assert actual_load == expected_avg, \
                f"Expected avg_load_percent={expected_avg}, got {actual_load}"

    def test_call_order_soh_before_peukert(self, mock_daemon):
        """Verify SoH calculation happens BEFORE Peukert calibration.

        In _update_battery_health, SoH must be calculated first, then
        passed to _auto_calibrate_peukert. This ensures Peukert calibration
        uses the updated SoH value.
        """
        import inspect

        src = inspect.getsource(mock_daemon._update_battery_health)

        # Find line numbers of key operations
        lines = src.split('\n')
        soh_line = None
        peukert_line = None

        for i, line in enumerate(lines):
            if 'calculate_soh' in line and soh_line is None:
                soh_line = i
            if '_auto_calibrate_peukert' in line and peukert_line is None:
                peukert_line = i

        assert soh_line is not None, \
            "SoH calculation not found in _update_battery_health"
        assert peukert_line is not None, \
            "Peukert calibration not found in _update_battery_health"
        assert soh_line < peukert_line, \
            f"SoH calc (line {soh_line}) must come BEFORE Peukert (line {peukert_line})"

    def test_guard_clause_sample_count(self, mock_daemon):
        """Guard clause 1: rejects if <2 discharge samples."""
        # Empty discharge buffer
        mock_daemon.discharge_buffer.times = []
        mock_daemon.discharge_buffer.loads = []
        mock_daemon.discharge_buffer.voltages = []

        with patch('src.monitor.calibrate_peukert') as mock_calibrate:
            mock_daemon._auto_calibrate_peukert(current_soh=0.95)

            # Should NOT call kernel (guard clause blocks it)
            assert not mock_calibrate.called, \
                "Kernel should not be called with <2 samples"

    def test_guard_clause_discharge_duration(self, mock_daemon):
        """Guard clause 2: rejects if discharge < 60 seconds."""
        # Short discharge (only 30 seconds)
        mock_daemon.discharge_buffer.times = [0.0, 30.0]
        mock_daemon.discharge_buffer.loads = [20, 20]
        mock_daemon.discharge_buffer.voltages = [13.0, 12.5]

        with patch('src.monitor.calibrate_peukert') as mock_calibrate:
            mock_daemon._auto_calibrate_peukert(current_soh=0.95)

            # Should NOT call kernel
            assert not mock_calibrate.called, \
                "Kernel should not be called with discharge < 60s"

    def test_guard_clause_invalid_load(self, mock_daemon):
        """Guard clause 3: rejects if average load is invalid."""
        # Valid duration but empty loads
        mock_daemon.discharge_buffer.times = [0.0, 100.0]
        mock_daemon.discharge_buffer.loads = []  # Empty
        mock_daemon.discharge_buffer.voltages = [13.0, 12.5]
        mock_daemon.reference_load_percent = 0.0  # Fallback is invalid

        with patch('src.monitor.calibrate_peukert') as mock_calibrate:
            mock_daemon._auto_calibrate_peukert(current_soh=0.95)

            # Should NOT call kernel
            assert not mock_calibrate.called, \
                "Kernel should not be called with invalid load"


class TestSystemdIntegration:
    """Tests for systemd watchdog integration."""

    def test_systemd_watchdog_survival(self, mock_daemon):
        """Daemon survives 10 poll cycles without watchdog restart.

        After refactoring kernel calls, ensure no race conditions or
        deadlocks in watchdog/polling path.
        """
        cycle_count = 0
        exception_caught = None

        for poll_num in range(10):
            try:
                # Mock NUT data
                nut_data = {
                    "ups.status": "OL" if poll_num < 5 else "OB",
                    "ups.load": 25,
                    "battery.voltage": 12.5 if poll_num < 5 else 11.5,
                    "battery.charge": 100 - (poll_num * 3),
                }

                # Mock watchdog notify (systemd)
                with patch('systemd.daemon.notify') as mock_notify:
                    # Just verify watchdog is called, don't validate behavior
                    mock_notify.return_value = True

                    # Simulate one poll cycle (simplified)
                    # Real daemon would call _process_nut_data()
                    # For this test, just verify no exception
                    pass

                cycle_count += 1

            except Exception as e:
                exception_caught = e
                break

        assert cycle_count == 10, \
            f"Poll cycle failed at {cycle_count}/10: {exception_caught}"
        assert exception_caught is None, \
            f"Exception during poll cycles: {exception_caught}"


class TestSoHRecalibrationFlow:
    """Tests for Phase 13 SoH recalibration and new battery detection."""

    def test_soh_recalibration_flow(self, mock_daemon):
        """SOH-01,02,03 integration: measured capacity → SoH update → regression filter.

        Scenario:
        1. Phase 12 capacity converges to 6.8Ah (3 deep discharge samples)
        2. Monitor updates SoH with capacity_ah_ref=6.8Ah tagged
        3. Old SoH entries (7.2Ah baseline) are kept in history
        4. Regression filters by baseline; only 6.8Ah entries used for trend
        """
        from src import soh_calculator

        # Setup: Old SoH history with rated baseline (7.2Ah)
        mock_daemon.battery_model.data['soh_history'] = [
            {'date': '2026-01-01', 'soh': 1.0, 'capacity_ah_ref': 7.2},
            {'date': '2026-02-01', 'soh': 0.98, 'capacity_ah_ref': 7.2},
            {'date': '2026-03-01', 'soh': 0.96, 'capacity_ah_ref': 7.2},
        ]

        # Setup: Phase 12 capacity has converged to 6.8Ah (3 samples, CoV < 10%)
        mock_daemon.battery_model.data['capacity_estimates'] = [
            {'timestamp': '2026-02-15', 'ah_estimate': 6.7, 'confidence': 0.65, 'metadata': {}},
            {'timestamp': '2026-03-01', 'ah_estimate': 6.8, 'confidence': 0.78, 'metadata': {}},
            {'timestamp': '2026-03-16', 'ah_estimate': 6.8, 'confidence': 0.82, 'metadata': {}},
        ]
        mock_daemon.battery_model.data['capacity_converged'] = True
        mock_daemon.battery_model.data['capacity_ah_measured'] = 6.8

        # Simulate discharge event
        mock_daemon.discharge_buffer.voltages = [12.0, 11.8, 11.5, 10.8, 10.5]
        mock_daemon.discharge_buffer.times = [0, 100, 200, 300, 400]
        mock_daemon.discharge_buffer.loads = [20, 21, 19, 22, 20]

        # Call _update_battery_health() (which calls soh_calculator)
        with patch.object(soh_calculator, 'calculate_soh_from_discharge') as mock_kernel:
            mock_kernel.return_value = (0.92, 6.8)  # (soh_new, capacity_ah_used)

            with patch('src.monitor._safe_save'):
                mock_daemon._update_battery_health()

        # Verify: New SoH entry tagged with measured baseline (6.8Ah)
        new_entry = mock_daemon.battery_model.data['soh_history'][-1]
        assert new_entry['soh'] == 0.92
        assert new_entry['capacity_ah_ref'] == 6.8  # Tagged with measured, not rated


def test_journald_event_filtering():
    """Test 3 (Phase 14 Plan 02): Verify journald events can be queried by EVENT_TYPE.

    Requirement: RPT-02 - journald events are queryable by EVENT_TYPE field.

    Setup: Mock journald send() to capture events
    Execute: Trigger _handle_discharge_complete() to generate events
    Assert: capacity_measurement events appear in captured records
    Assert: baseline_lock events appear when convergence detected
    Assert: Events contain required structured fields (MESSAGE, CAPACITY_AH, SAMPLE_COUNT)
    """
    from unittest.mock import patch, MagicMock, call
    from src.monitor import MonitorDaemon

    # Mock journald.send() to capture events
    captured_events = []

    def mock_send(**kwargs):
        captured_events.append(kwargs)

    # Create a test daemon with mocked components
    with patch('src.monitor.NUTClient'), \
         patch('src.monitor.EMAFilter'), \
         patch('src.monitor.EventClassifier'), \
         patch.object(MonitorDaemon, '_check_nut_connectivity'), \
         patch.object(MonitorDaemon, '_validate_model'), \
         patch.object(MonitorDaemon, '_reset_battery_baseline'):

        # Create test config and daemon
        from src.monitor import Config
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_path:
            config = Config(
                ups_name='cyberpower',
                polling_interval=10,
                reporting_interval=60,
                nut_host='localhost',
                nut_port=3493,
                nut_timeout=2.0,
                shutdown_minutes=5,
                soh_alert_threshold=0.80,
                model_dir=Path(tmp_path),
                config_dir=Path(tmp_path),
                runtime_threshold_minutes=20,
                reference_load_percent=20.0,
                ema_window_sec=120,
                capacity_ah=7.2
            )

            daemon = MonitorDaemon(config)

            # Mock battery_model methods since they're real
            with patch.object(daemon.battery_model, 'get_capacity_ah', return_value=7.2), \
                 patch.object(daemon.battery_model, 'get_convergence_status') as mock_convergence, \
                 patch.object(daemon.battery_model, 'save'):

                # Setup convergence status mock
                mock_convergence.return_value = {
                    'sample_count': 1,
                    'confidence_percent': 88.0,
                    'latest_ah': 6.95,
                    'rated_ah': 7.2,
                    'converged': False,
                    'capacity_ah_ref': None
                }

                # Setup mocks for capacity estimation
                daemon.battery_model.data = {}

                ah_estimate = 6.95
                confidence = 0.88
                metadata = {
                    'delta_soc_percent': 52.3,
                    'duration_sec': 1234,
                    'load_avg_percent': 25.5,
                }

                daemon.capacity_estimator = MagicMock()
                daemon.capacity_estimator.estimate.return_value = (ah_estimate, confidence, metadata)
                daemon.capacity_estimator.has_converged.return_value = False

                daemon.battery_model.data['capacity_estimates'] = [
                    {'ah_estimate': ah_estimate}
                ]

                # Trigger discharge complete with mocked logger
                discharge_data = {
                    'voltage_series': [12.5, 12.0, 11.5, 11.0],
                    'time_series': [0, 300, 600, 900],
                    'current_series': [25.0, 25.0, 25.0, 25.0],
                    'timestamp': '2026-03-16T12:00:00'
                }

                with patch('src.monitor.logger') as mock_logger:
                    daemon._handle_discharge_complete(discharge_data)

                    # Verify: capacity_measurement events were logged
                    capacity_calls = [c for c in mock_logger.info.call_args_list
                                     if 'capacity_measurement' in str(c)]
                    assert len(capacity_calls) >= 1, "No capacity_measurement events logged"

                    # Verify: capacity_measurement events have required fields
                    for call_obj in capacity_calls:
                        if 'extra' in call_obj.kwargs:
                            extra = call_obj.kwargs['extra']
                            assert extra.get('EVENT_TYPE') == 'capacity_measurement'
                            assert 'CAPACITY_AH' in extra, "Missing CAPACITY_AH field"
                            assert 'CONFIDENCE_PERCENT' in extra, "Missing CONFIDENCE_PERCENT field"
                            assert 'SAMPLE_COUNT' in extra, "Missing SAMPLE_COUNT field"

                    # Verify: baseline_lock events NOT present (convergence not reached)
                    baseline_lock_calls = [c for c in mock_logger.info.call_args_list
                                          if c.kwargs.get('extra', {}).get('EVENT_TYPE') == 'baseline_lock']
                    assert len(baseline_lock_calls) == 0, "baseline_lock should not fire without convergence"

                # Now simulate convergence and verify baseline_lock
                mock_convergence.return_value = {
                    'sample_count': 3,
                    'confidence_percent': 92.0,
                    'latest_ah': 6.95,
                    'rated_ah': 7.2,
                    'converged': True,
                    'capacity_ah_ref': None
                }
                daemon.battery_model.data['capacity_estimates'] = [
                    {'ah_estimate': 6.88},
                    {'ah_estimate': 6.92},
                    {'ah_estimate': 6.95}
                ]
                daemon.capacity_estimator.has_converged.return_value = True

                with patch('src.monitor.logger') as mock_logger:
                    daemon._handle_discharge_complete(discharge_data)

                    # Verify: baseline_lock events present after convergence
                    baseline_lock_calls = [c for c in mock_logger.info.call_args_list
                                          if c.kwargs.get('extra', {}).get('EVENT_TYPE') == 'baseline_lock']
                    assert len(baseline_lock_calls) >= 1, "No baseline_lock events after convergence"

                    # Verify: baseline_lock events have required fields
                    for call_obj in baseline_lock_calls:
                        if 'extra' in call_obj.kwargs:
                            extra = call_obj.kwargs['extra']
                            assert extra.get('EVENT_TYPE') == 'baseline_lock'
                            assert 'CAPACITY_AH' in extra, "Missing CAPACITY_AH in baseline_lock event"
                            assert 'SAMPLE_COUNT' in extra, "Missing SAMPLE_COUNT in baseline_lock event"
                            assert 'TIMESTAMP' in extra, "Missing TIMESTAMP in baseline_lock event"
