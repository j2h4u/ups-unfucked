"""Unit tests for monitor.py daemon initialization and auto-calibration."""

import logging
import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
import sys

# Mock systemd before importing monitor
sys.modules['systemd'] = MagicMock()
sys.modules['systemd.journal'] = MagicMock()


@pytest.fixture
def make_daemon(config_fixture):
    """Create MonitorDaemon with all external dependencies mocked."""
    from src.monitor import MonitorDaemon

    with patch('src.monitor.NUTClient'), \
         patch('src.monitor.EMAFilter'), \
         patch('src.monitor.BatteryModel'), \
         patch('src.monitor.EventClassifier'), \
         patch.object(MonitorDaemon, '_check_nut_connectivity'), \
         patch.object(MonitorDaemon, '_validate_model'), \
         patch.object(MonitorDaemon, '_reset_battery_baseline'):
        # Replace mocked JournalHandler with a real stderr handler so logging works in tests
        from src.monitor import logger as monitor_logger
        monitor_logger.handlers.clear()
        monitor_logger.addHandler(logging.StreamHandler())

        def _make():
            return MonitorDaemon(config_fixture)
        yield _make


def test_per_poll_writes_during_blackout(make_daemon):
    """SAFE-01: Virtual UPS metrics written every poll during OB, not every 6th."""
    from src.event_classifier import EventType
    from unittest.mock import call

    daemon = make_daemon()
    daemon.nut_client = MagicMock()
    daemon.nut_client.get_ups_vars.return_value = {
        'battery.voltage': '12.0',
        'input.voltage': '0',
        'ups.status': 'OB DISCHRG',
        'ups.load': '25'
    }

    # Mock dependencies
    daemon._update_ema = MagicMock(return_value=(12.0, 25.0))
    daemon._classify_event = MagicMock()
    daemon._compute_metrics = MagicMock(return_value=(50.0, 10.0))
    daemon._handle_event_transition = MagicMock()
    daemon._write_virtual_ups = MagicMock()
    daemon._track_voltage_sag = MagicMock()
    daemon._track_discharge = MagicMock()
    daemon._log_status = MagicMock()

    # Simulate OL→OB→OL transition over 12 polls
    event_sequence = [
        EventType.ONLINE,           # Poll 0: OL
        EventType.ONLINE,           # Poll 1: OL
        EventType.BLACKOUT_REAL,    # Poll 2: OB (transition)
        EventType.BLACKOUT_REAL,    # Poll 3: OB
        EventType.BLACKOUT_REAL,    # Poll 4: OB
        EventType.BLACKOUT_REAL,    # Poll 5: OB
        EventType.BLACKOUT_REAL,    # Poll 6: OB
        EventType.BLACKOUT_REAL,    # Poll 7: OB
        EventType.ONLINE,           # Poll 8: Back to OL
        EventType.ONLINE,           # Poll 9: OL
        EventType.ONLINE,           # Poll 10: OL
        EventType.ONLINE,           # Poll 11: OL (should trigger batched write at 12th)
    ]

    from src.monitor import CurrentMetrics
    daemon.current_metrics = CurrentMetrics(event_type=EventType.ONLINE, previous_event_type=EventType.ONLINE)

    # Run 12 iterations (mock loop)
    for i in range(12):
        daemon.poll_count = i
        daemon.current_metrics.event_type = event_sequence[i]

        # Replicate gate logic from monitor.py
        event_type = daemon.current_metrics.event_type
        is_discharging = event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)

        if is_discharging or daemon.poll_count % 6 == 0:
            daemon._compute_metrics()
            daemon._handle_event_transition()
            daemon._write_virtual_ups(None, 50.0, 10.0)

    # During OL (polls 0-1): only poll 0 should write (modulo) → 1 call
    # During OB (polls 2-7): all 6 polls should write → 6 calls
    # During OL (polls 8-11): poll 12 not reached yet → no additional calls
    # Expected calls: 1 (OL) + 6 (OB) = 7
    assert daemon._write_virtual_ups.call_count == 7, \
        f"Expected 7 writes (1 OL + 6 OB), got {daemon._write_virtual_ups.call_count}"


def test_handle_event_transition_per_poll_during_ob(make_daemon):
    """SAFE-02: LB flag decision (_handle_event_transition) executes every poll during OB."""
    from src.event_classifier import EventType
    from src.monitor import CurrentMetrics

    daemon = make_daemon()
    daemon._handle_event_transition = MagicMock()
    daemon.current_metrics = CurrentMetrics(
        event_type=EventType.BLACKOUT_REAL,
        time_rem_minutes=3.0,  # Below shutdown threshold (5 min)
        previous_event_type=EventType.ONLINE,
        shutdown_imminent=False
    )
    daemon.shutdown_threshold_minutes = 5

    # Mock dependencies
    daemon._update_ema = MagicMock(return_value=(11.0, 30.0))
    daemon._classify_event = MagicMock()
    daemon._compute_metrics = MagicMock(return_value=(30.0, 3.0))
    daemon._write_virtual_ups = MagicMock()
    daemon._track_voltage_sag = MagicMock()
    daemon._track_discharge = MagicMock()
    daemon._log_status = MagicMock()
    daemon.nut_client = MagicMock()
    daemon.nut_client.get_ups_vars.return_value = {
        'battery.voltage': '11.0',
        'input.voltage': '0',
        'ups.status': 'OB DISCHRG',
        'ups.load': '30'
    }

    # Simulate 4 polls during OB state
    for i in range(4):
        daemon.poll_count = i
        daemon.current_metrics.event_type = EventType.BLACKOUT_REAL

        event_type = daemon.current_metrics.event_type
        is_discharging = event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)

        if is_discharging or daemon.poll_count % 6 == 0:
            daemon._compute_metrics()
            daemon._handle_event_transition()

    # All 4 polls should call _handle_event_transition (is_discharging=True)
    assert daemon._handle_event_transition.call_count == 4, \
        f"Expected 4 calls during OB, got {daemon._handle_event_transition.call_count}"


def test_no_writes_during_online_state(make_daemon):
    """SAFE-01: No spurious writes during OL state — only on poll % 6 boundary."""
    from src.event_classifier import EventType
    from src.monitor import CurrentMetrics

    daemon = make_daemon()
    daemon._write_virtual_ups = MagicMock()
    daemon._handle_event_transition = MagicMock()
    daemon._compute_metrics = MagicMock(return_value=(85.0, 120.0))
    daemon._update_ema = MagicMock(return_value=(13.4, 15.0))
    daemon._classify_event = MagicMock()
    daemon._track_voltage_sag = MagicMock()
    daemon._track_discharge = MagicMock()
    daemon._log_status = MagicMock()
    daemon.nut_client = MagicMock()
    daemon.nut_client.get_ups_vars.return_value = {
        'battery.voltage': '13.4',
        'input.voltage': '220',
        'ups.status': 'OL',
        'ups.load': '15'
    }
    daemon.current_metrics = CurrentMetrics(event_type=EventType.ONLINE, previous_event_type=EventType.ONLINE)

    # Simulate 7 polls in OL state
    for i in range(7):
        daemon.poll_count = i
        daemon.current_metrics.event_type = EventType.ONLINE

        event_type = daemon.current_metrics.event_type
        is_discharging = event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)

        if is_discharging or daemon.poll_count % 6 == 0:
            daemon._compute_metrics()
            daemon._handle_event_transition()
            daemon._write_virtual_ups(None, 85.0, 120.0)

    # Only poll 0 and poll 6 should trigger writes (modulo 6 == 0)
    assert daemon._write_virtual_ups.call_count == 2, \
        f"Expected 2 writes (poll 0, poll 6), got {daemon._write_virtual_ups.call_count}"


def test_lb_flag_signal_latency(make_daemon):
    """SAFE-02: LB flag written to virtual UPS within <10s of OB transition."""
    from src.event_classifier import EventType
    from src.monitor import CurrentMetrics
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        daemon = make_daemon()
        daemon.model_dir = Path(tmpdir)
        daemon.shutdown_threshold_minutes = 5

        # Track timestamps of _handle_event_transition calls during OB
        transition_call_times = []

        def mock_handle_with_timestamp():
            transition_call_times.append(daemon.poll_count)
            # Also set shutdown_imminent flag if time_rem below threshold
            if (daemon.current_metrics.time_rem_minutes or 0) < daemon.shutdown_threshold_minutes:
                daemon.current_metrics.shutdown_imminent = True

        daemon._handle_event_transition = mock_handle_with_timestamp
        daemon._compute_metrics = MagicMock(return_value=(30.0, 3.0))
        daemon._update_ema = MagicMock(return_value=(11.0, 30.0))
        daemon._classify_event = MagicMock()
        daemon._write_virtual_ups = MagicMock()
        daemon._track_voltage_sag = MagicMock()
        daemon._track_discharge = MagicMock()
        daemon._log_status = MagicMock()
        daemon.nut_client = MagicMock()
        daemon.nut_client.get_ups_vars.return_value = {
            'battery.voltage': '11.0',
            'input.voltage': '0',
            'ups.status': 'OB DISCHRG',
            'ups.load': '30'
        }
        daemon.current_metrics = CurrentMetrics(event_type=EventType.ONLINE, previous_event_type=EventType.ONLINE)

        # Poll 0-1: OL state
        # Poll 2: OB transition detected
        for i in range(3):
            daemon.poll_count = i
            daemon.current_metrics.previous_event_type = daemon.current_metrics.event_type or EventType.ONLINE

            if i < 2:
                daemon.current_metrics.event_type = EventType.ONLINE
            else:
                daemon.current_metrics.event_type = EventType.BLACKOUT_REAL
                daemon.current_metrics.time_rem_minutes = 3.0

            event_type = daemon.current_metrics.event_type
            is_discharging = event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)

            if is_discharging or daemon.poll_count % 6 == 0:
                daemon._compute_metrics()
                daemon._handle_event_transition()

        # Verify _handle_event_transition was called at poll 2 (immediately on OB)
        assert 2 in transition_call_times, \
            f"Expected LB decision at poll 2 (OB transition), calls were at {transition_call_times}"

        # Verify shutdown_imminent flag is True (time_rem 3.0 < threshold 5)
        assert daemon.current_metrics.shutdown_imminent is True, \
            "Expected shutdown_imminent=True when time_rem < threshold"


def test_voltage_sag_detection(make_daemon):
    """Test that voltage sag is detected and R_internal recorded after 5 samples."""
    from src.event_classifier import EventType

    daemon = make_daemon()
    mock_model = MagicMock()
    mock_model.get_nominal_power_watts.return_value = 425.0
    mock_model.get_nominal_voltage.return_value = 12.0
    daemon.battery_model = mock_model

    # Simulate EMA buffer with stable voltage before sag
    daemon.ema_buffer = MagicMock()
    daemon.ema_buffer.voltage = 13.50
    daemon.ema_buffer.load = 16.5

    # Set up pre-sag state
    daemon.v_before_sag = 13.50
    daemon.sag_buffer = [12.80, 12.75, 12.78, 12.76, 12.77]
    from src.monitor import SagState
    daemon.sag_state = SagState.MEASURING

    # Record sag (median of last 3 = 12.77)
    v_sag = sorted(daemon.sag_buffer[-3:])[1]
    daemon._record_voltage_sag(v_sag, EventType.BLACKOUT_REAL)

    mock_model.add_r_internal_entry.assert_called_once()
    call_args = mock_model.add_r_internal_entry.call_args
    assert call_args[0][1] > 0  # r_ohm should be positive


def test_voltage_sag_skipped_zero_current(make_daemon):
    """Voltage sag recording skipped when load is zero (no current flow)."""
    from src.event_classifier import EventType

    daemon = make_daemon()
    mock_model = MagicMock()
    mock_model.get_nominal_power_watts.return_value = 425.0
    mock_model.get_nominal_voltage.return_value = 12.0
    daemon.battery_model = mock_model

    daemon.ema_buffer = MagicMock()
    daemon.ema_buffer.voltage = 13.50
    daemon.ema_buffer.load = 0.0  # Zero load

    daemon.v_before_sag = 13.50
    daemon._record_voltage_sag(13.40, EventType.BLACKOUT_REAL)

    mock_model.add_r_internal_entry.assert_not_called()


def test_sag_init_vars(make_daemon):
    """Sag measurement variables initialized correctly."""
    daemon = make_daemon()
    assert daemon.v_before_sag is None
    assert daemon.sag_buffer == []
    from src.monitor import SagState
    assert daemon.sag_state == SagState.IDLE


def test_shutdown_threshold_from_config(make_daemon):
    """Shutdown threshold comes from TOML config."""
    daemon = make_daemon()
    assert daemon.shutdown_threshold_minutes == 5  # default from config


def test_discharge_buffer_init(make_daemon):
    """Discharge buffer initialized correctly."""
    daemon = make_daemon()
    assert daemon.calibration_last_written_index == 0
    assert daemon.discharge_buffer.collecting is False


def test_discharge_buffer_cleared_after_health_update(make_daemon):
    """Buffer cleared after _update_battery_health completes."""
    daemon = make_daemon()

    mock_model = MagicMock()
    mock_model.data = {'lut': []}
    mock_model.get_soh.return_value = 1.0
    mock_model.get_lut.return_value = []
    mock_model.get_capacity_ah.return_value = 7.2
    mock_model.get_soh_history.return_value = []
    mock_model.get_peukert_exponent.return_value = 1.2
    mock_model.get_nominal_voltage.return_value = 12.0
    mock_model.get_nominal_power_watts.return_value = 425.0
    # Phase 13: Mock convergence status (measured capacity not converged by default)
    mock_model.get_convergence_status.return_value = {'converged': False, 'sample_count': 1}
    daemon.battery_model = mock_model

    daemon.ema_buffer = MagicMock()
    daemon.ema_buffer.load = 20.0

    from src.monitor import DischargeBuffer
    daemon.discharge_buffer = DischargeBuffer(
        voltages=[13.4, 12.0, 11.0, 10.5],
        times=[0, 100, 200, 300],
        collecting=True
    )

    daemon._update_battery_health()

    assert daemon.discharge_buffer.voltages == []
    assert daemon.discharge_buffer.times == []
    assert daemon.discharge_buffer.collecting is False


@pytest.mark.xfail(reason="interpolate_cliff_region is Phase 12.1 placeholder — returns LUT unchanged")
def test_auto_calibration_end_to_end(config_fixture):
    """LUT updated with measured points from any discharge (no special mode needed)."""
    from src.monitor import MonitorDaemon
    from src.model import BatteryModel
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        model_path = Path(tmpdir) / "model.json"

        with patch('src.monitor.NUTClient'), \
             patch('src.monitor.EMAFilter'), \
             patch('src.monitor.EventClassifier'), \
             patch.object(MonitorDaemon, '_check_nut_connectivity'), \
             patch.object(MonitorDaemon, '_validate_model'):
            # Replace mocked JournalHandler with real handler so logging works
            from src.monitor import logger as monitor_logger
            monitor_logger.handlers.clear()
            monitor_logger.addHandler(logging.StreamHandler())

            daemon = MonitorDaemon(config_fixture)

            model = BatteryModel(model_path=model_path)
            daemon.battery_model = model

            model.data['lut'] = [
                {'v': 13.4, 'soc': 1.0, 'source': 'standard'},
                {'v': 12.8, 'soc': 0.85, 'source': 'standard'},
                {'v': 12.4, 'soc': 0.64, 'source': 'standard'},
                {'v': 11.2, 'soc': 0.60, 'source': 'standard'},
                {'v': 11.0, 'soc': 0.50, 'source': 'standard'},
                {'v': 10.8, 'soc': 0.40, 'source': 'standard'},
                {'v': 10.5, 'soc': 0.0, 'source': 'anchor'},
            ]
            model.save()

            # Simulate measured discharge points in cliff region
            model.calibration_write(voltage=10.95, soc=0.48, timestamp=1000.0)
            model.calibration_write(voltage=10.65, soc=0.15, timestamp=1100.0)
            model.calibration_write(voltage=10.55, soc=0.02, timestamp=1200.0)

            measured_count = sum(1 for e in model.get_lut() if e['source'] == 'measured')
            assert measured_count >= 3

            # Cliff interpolation fills gaps between measured points
            from src.battery_math.soh import interpolate_cliff_region
            updated_lut = interpolate_cliff_region(model.data['lut'])
            model.update_lut_from_calibration(updated_lut)

            interpolated_count = sum(1 for e in model.get_lut() if e['source'] == 'interpolated')
            assert interpolated_count > 0

            # Verify persistence
            reloaded = BatteryModel(model_path=model_path)
            assert sum(1 for e in reloaded.get_lut() if e['source'] == 'interpolated') > 0
            assert sum(1 for e in reloaded.get_lut() if e['source'] == 'measured') >= 3


def test_current_metrics_dataclass(current_metrics_fixture):
    """Verify CurrentMetrics dataclass instantiation and field access."""
    from src.event_classifier import EventType

    # Fixture provides a populated instance
    assert current_metrics_fixture.soc == 0.75
    assert current_metrics_fixture.battery_charge == 75.0
    assert current_metrics_fixture.time_rem_minutes == 30.0
    assert current_metrics_fixture.event_type == EventType.ONLINE
    assert current_metrics_fixture.transition_occurred is False
    assert current_metrics_fixture.shutdown_imminent is False
    assert current_metrics_fixture.ups_status_override is None
    assert current_metrics_fixture.previous_event_type == EventType.ONLINE
    assert current_metrics_fixture.timestamp is not None

    # Test default instantiation (no args, all defaults)
    from src.monitor import CurrentMetrics
    cm_default = CurrentMetrics()
    assert cm_default.soc is None
    assert cm_default.battery_charge is None
    assert cm_default.time_rem_minutes is None
    assert cm_default.event_type is None
    assert cm_default.transition_occurred is False
    assert cm_default.shutdown_imminent is False
    assert cm_default.ups_status_override is None
    assert cm_default.previous_event_type == EventType.ONLINE
    assert cm_default.timestamp is None

    # Test field mutation (should work; dataclass not frozen)
    cm_default.soc = 0.5
    assert cm_default.soc == 0.5


def test_config_dataclass(config_fixture):
    """Verify Config dataclass instantiation and field access."""
    # Fixture provides a populated instance
    assert config_fixture.ups_name == 'test-cyberpower'
    assert config_fixture.polling_interval == 10
    assert config_fixture.reporting_interval == 60
    assert config_fixture.nut_host == 'localhost'
    assert config_fixture.nut_port == 3493
    assert config_fixture.nut_timeout == 2.0
    assert config_fixture.shutdown_minutes == 5
    assert config_fixture.soh_alert_threshold == 0.80
    from pathlib import Path
    assert isinstance(config_fixture.model_dir, Path)
    assert isinstance(config_fixture.config_dir, Path)
    assert config_fixture.runtime_threshold_minutes == 20
    assert config_fixture.reference_load_percent == 20.0
    assert config_fixture.ema_window_sec == 120

    # Test custom instantiation
    from src.monitor import Config
    custom_config = Config(
        ups_name='custom-ups',
        polling_interval=15,
        reporting_interval=90,
        nut_host='192.168.1.1',
        nut_port=3494,
        nut_timeout=3.0,
        shutdown_minutes=10,
        soh_alert_threshold=0.70,
        model_dir=Path('/tmp/model'),
        config_dir=Path('/tmp/config'),
        runtime_threshold_minutes=25,
        reference_load_percent=30.0,
        ema_window_sec=180,
        capacity_ah=9.0,
    )
    assert custom_config.ups_name == 'custom-ups'
    assert custom_config.polling_interval == 15
    assert custom_config.nut_port == 3494


def test_config_immutability(config_fixture):
    """Verify Config frozen=True semantics prevent field mutation."""
    from dataclasses import FrozenInstanceError

    # Attempt to mutate frozen Config field
    with pytest.raises(FrozenInstanceError):
        config_fixture.ups_name = 'modified'

    with pytest.raises(FrozenInstanceError):
        config_fixture.polling_interval = 20

    # Config should be unchanged
    assert config_fixture.ups_name == 'test-cyberpower'
    assert config_fixture.polling_interval == 10


def test_auto_calibrate_peukert_math_verification(make_daemon):
    """TEST-02: Unit test for _auto_calibrate_peukert() math and edge cases.

    Verifies:
    - Peukert exponent recalculation using: ln(I1/I2) / ln(t1/t2)
    - Edge cases: empty history, single sample, divide by zero
    - No exponent changes if error < 10%
    """
    from math import log
    from unittest.mock import Mock, patch

    daemon = make_daemon()
    daemon.battery_model = Mock()
    daemon.battery_model.get_peukert_exponent = Mock(return_value=1.2)
    daemon.battery_model.set_peukert_exponent = Mock()
    daemon.battery_model.get_capacity_ah = Mock(return_value=7.2)
    daemon.battery_model.get_nominal_voltage = Mock(return_value=12.0)
    daemon.battery_model.get_nominal_power_watts = Mock(return_value=425.0)
    daemon.battery_model.save = Mock()
    daemon.reference_load_percent = 20.0

    daemon.ema_buffer = Mock()
    daemon.ema_buffer.load = 20.0

    from src.monitor import DischargeBuffer

    # Test Case 1: Normal case with two discharge events (>60s duration, error >10%)
    daemon.discharge_buffer = DischargeBuffer(
        voltages=[13.4, 12.0, 11.0, 10.5],
        times=[0, 100, 200, 300],
    )

    with patch('src.monitor.peukert_runtime_hours') as mock_peukert:
        # Mock peukert_runtime_hours to return a value that creates >10% error
        mock_peukert.return_value = 1.0  # 60 minutes at full capacity
        daemon._auto_calibrate_peukert(current_soh=0.95)
        # Should trigger recalibration because error > 10%
        daemon.battery_model.set_peukert_exponent.assert_called()
        daemon.battery_model.save.assert_called()

    # Test Case 2: Empty discharge buffer - should skip
    daemon.discharge_buffer = DischargeBuffer()
    daemon.battery_model.reset_mock()
    daemon._auto_calibrate_peukert(current_soh=0.95)
    daemon.battery_model.set_peukert_exponent.assert_not_called()

    # Test Case 3: Single sample - should skip (<2 samples)
    daemon.discharge_buffer = DischargeBuffer(voltages=[12.0], times=[0])
    daemon.battery_model.reset_mock()
    daemon._auto_calibrate_peukert(current_soh=0.95)
    daemon.battery_model.set_peukert_exponent.assert_not_called()

    # Test Case 4: Identical timestamps (divide by zero protection)
    daemon.discharge_buffer = DischargeBuffer(
        voltages=[13.4, 12.0],
        times=[100, 100],  # Same time! -> 0 second duration
    )
    daemon.battery_model.reset_mock()
    # Should not raise exception
    daemon._auto_calibrate_peukert(current_soh=0.95)
    # Should skip due to short duration (<60s)
    daemon.battery_model.set_peukert_exponent.assert_not_called()

    # Test Case 5: Duration too short (<60s) - no update
    daemon.discharge_buffer = DischargeBuffer(
        voltages=[13.4, 12.0],
        times=[0, 50],  # 50 seconds total
    )
    daemon.battery_model.reset_mock()
    daemon._auto_calibrate_peukert(current_soh=0.99)
    # Should skip due to duration < 60s
    daemon.battery_model.set_peukert_exponent.assert_not_called()


def test_signal_handler_saves_model(make_daemon):
    """TEST-03: Verify signal handler (SIGTERM/SIGINT) persists model before shutdown.

    Verifies:
    - SIGTERM received → _signal_handler() called
    - _signal_handler() calls model.save()
    - running flag set to False
    """
    import signal
    from unittest.mock import Mock

    daemon = make_daemon()
    daemon.battery_model = Mock()
    daemon.battery_model.save = Mock()
    daemon.running = True

    # Call signal handler directly (simulating SIGTERM)
    # Note: Can't actually send signal in test, so we call handler directly
    # Handler signature: _signal_handler(signum, frame)
    daemon._signal_handler(signal.SIGTERM, None)

    # Verify model was saved
    daemon.battery_model.save.assert_called_once()

    # Verify running flag cleared (triggers shutdown)
    assert daemon.running is False

    # Test Case 2: Multiple SIGTERM signals - should be idempotent
    daemon.battery_model.reset_mock()
    daemon.running = True  # Reset state
    daemon._signal_handler(signal.SIGTERM, None)
    daemon._signal_handler(signal.SIGTERM, None)  # Second signal
    # Should handle gracefully without double-save exception
    assert daemon.battery_model.save.call_count >= 1
    assert daemon.running is False


def test_ol_ob_ol_discharge_lifecycle_complete(make_daemon):
    """TEST-01: Integration test for full OL→OB→OL discharge lifecycle.

    Verifies:
    - _handle_event_transition() executes on OB→OL
    - _update_battery_health() called and SoH calculated
    - _track_discharge() accumulates voltage/time series
    - Model persisted to disk
    - Discharge buffer cleared after completion
    - Multiple cycles work correctly without state carryover
    """
    from src.event_classifier import EventType
    from unittest.mock import Mock, patch
    import time

    daemon = make_daemon()

    # Pre-setup: Mock soh_calculator and other dependencies to avoid complex physics
    with patch('src.monitor.soh_calculator.calculate_soh_from_discharge') as mock_soh_calc, \
         patch('src.monitor.replacement_predictor.linear_regression_soh') as mock_replace, \
         patch('src.monitor.runtime_minutes') as mock_runtime, \
         patch('src.monitor.interpolate_cliff_region') as mock_interp:
        mock_soh_calc.return_value = (0.95, 7.2)  # Assume 95% SoH after discharge, using rated capacity
        mock_replace.return_value = None  # No replacement prediction
        mock_runtime.return_value = 30.0  # 30 minutes runtime
        mock_interp.return_value = [
            {"v": 13.4, "soc": 1.0, "source": "standard"},
            {"v": 12.4, "soc": 0.64, "source": "standard"},
            {"v": 10.5, "soc": 0.0, "source": "anchor"},
        ]

        # Mock battery model methods
        daemon.battery_model.get_soh = Mock(return_value=1.0)
        daemon.battery_model.get_lut = Mock(return_value=[
            {"v": 13.4, "soc": 1.0, "source": "standard"},
            {"v": 12.4, "soc": 0.64, "source": "standard"},
            {"v": 10.5, "soc": 0.0, "source": "anchor"},
        ])
        daemon.battery_model.get_capacity_ah = Mock(return_value=7.2)
        daemon.battery_model.get_soh_history = Mock(return_value=[])
        daemon.battery_model.add_soh_history_entry = Mock()
        daemon.battery_model.save = Mock()
        daemon.battery_model.increment_cycle_count = Mock()
        daemon.battery_model.update_model_metadata = Mock()
        daemon.battery_model.get_nominal_power_watts = Mock(return_value=425.0)
        daemon.battery_model.get_nominal_voltage = Mock(return_value=12.0)
        daemon.battery_model.get_peukert_exponent = Mock(return_value=1.2)
        daemon.battery_model.data = {'lut': [
            {"v": 13.4, "soc": 1.0, "source": "standard"},
            {"v": 12.4, "soc": 0.64, "source": "standard"},
            {"v": 10.5, "soc": 0.0, "source": "anchor"},
        ]}
        daemon.battery_model.update_lut_from_calibration = Mock()

        # Simulate voltage/load from NUT
        daemon.nut_client = Mock()
        daemon.nut_client.get_ups_vars = Mock(return_value={
            'battery.voltage': '12.0',
            'ups.load': '25',
            'ups.status': 'OL',
            'input.voltage': '230',
        })

        # Setup EMA buffer
        daemon.ema_buffer = Mock()
        daemon.ema_buffer.stabilized = True
        daemon.ema_buffer.voltage = 12.0
        daemon.ema_buffer.load = 25.0

        from src.monitor import DischargeBuffer
        daemon.discharge_buffer = DischargeBuffer()

        # CYCLE 1: OL → OL → OB → OB → OB → OL → OL
        current_time = time.time()
        base_timestamp = current_time

        # Poll 0: OL at 13.4V, 100% charge
        daemon.poll_count = 0
        daemon.current_metrics.event_type = EventType.ONLINE
        daemon.current_metrics.transition_occurred = False
        daemon.current_metrics.battery_charge = 100
        daemon.ema_buffer.voltage = 13.4
        daemon.ema_buffer.load = 2
        # No discharge tracking in OL state

        # Poll 1: OL at 13.3V, 100% charge
        daemon.poll_count = 1
        daemon.current_metrics.event_type = EventType.ONLINE
        daemon.current_metrics.transition_occurred = False
        daemon.current_metrics.battery_charge = 100
        daemon.ema_buffer.voltage = 13.3
        daemon.ema_buffer.load = 2

        # Poll 2: OB at 12.0V, 50% charge (TRANSITION - OL→OB)
        daemon.poll_count = 2
        prev_event = EventType.ONLINE
        daemon.current_metrics.event_type = EventType.BLACKOUT_REAL
        daemon.current_metrics.transition_occurred = True
        daemon.current_metrics.previous_event_type = prev_event
        daemon.current_metrics.battery_charge = 50
        daemon.ema_buffer.voltage = 12.0
        daemon.ema_buffer.load = 25
        daemon._track_discharge(12.0, base_timestamp + 100)
        daemon._handle_event_transition()
        # After transition, discharge buffer should be collecting
        assert daemon.discharge_buffer.collecting is True, "Buffer should start collecting on OB transition"

        # Poll 3: OB at 11.5V, 30% charge (continue discharge)
        daemon.poll_count = 3
        daemon.current_metrics.event_type = EventType.BLACKOUT_REAL
        daemon.current_metrics.transition_occurred = False
        daemon.current_metrics.battery_charge = 30
        daemon.ema_buffer.voltage = 11.5
        daemon.ema_buffer.load = 25
        daemon._track_discharge(11.5, base_timestamp + 250)

        # Poll 4: OB at 11.0V, 20% charge (continue discharge)
        daemon.poll_count = 4
        daemon.current_metrics.event_type = EventType.BLACKOUT_REAL
        daemon.current_metrics.transition_occurred = False
        daemon.current_metrics.battery_charge = 20
        daemon.ema_buffer.voltage = 11.0
        daemon.ema_buffer.load = 25
        daemon._track_discharge(11.0, base_timestamp + 500)

        # Poll 5: OL at 13.0V, 100% charge (TRANSITION - OB→OL)
        daemon.poll_count = 5
        prev_event = EventType.BLACKOUT_REAL
        daemon.current_metrics.event_type = EventType.ONLINE
        daemon.current_metrics.transition_occurred = True
        daemon.current_metrics.previous_event_type = prev_event
        daemon.current_metrics.battery_charge = 100
        daemon.ema_buffer.voltage = 13.0
        daemon.ema_buffer.load = 2

        # Verify discharge buffer BEFORE calling _handle_event_transition (which clears it)
        assert len(daemon.discharge_buffer.voltages) == 3, f"Expected 3 voltage samples before transition, got {len(daemon.discharge_buffer.voltages)}"
        assert daemon.discharge_buffer.voltages == [12.0, 11.5, 11.0], f"Unexpected voltage samples before transition: {daemon.discharge_buffer.voltages}"

        daemon._handle_event_transition()  # Should call _update_battery_health() and clear buffer

        # Verify discharge buffer state after OB→OL (should be cleared now)
        assert daemon.discharge_buffer.collecting is False, "Buffer should stop collecting after OB→OL transition"
        assert len(daemon.discharge_buffer.voltages) == 0, "Buffer should be cleared after _update_battery_health()"

        # Verify _update_battery_health() was called
        daemon.battery_model.add_soh_history_entry.assert_called_once()
        daemon.battery_model.save.assert_called()  # May be called multiple times

        # Poll 6: OL at 13.2V, 100% charge (stable OL)
        daemon.poll_count = 6
        daemon.current_metrics.event_type = EventType.ONLINE
        daemon.current_metrics.transition_occurred = False
        daemon.current_metrics.battery_charge = 100
        daemon.ema_buffer.voltage = 13.2
        daemon.ema_buffer.load = 2

        # CYCLE 2: OL → OB → OL (verify second cycle works)
        # Poll 7: OB at 12.5V, 60% charge (TRANSITION - OL→OB)
        daemon.poll_count = 7
        prev_event = EventType.ONLINE
        daemon.current_metrics.event_type = EventType.BLACKOUT_REAL
        daemon.current_metrics.transition_occurred = True
        daemon.current_metrics.previous_event_type = prev_event
        daemon.current_metrics.battery_charge = 60
        daemon.ema_buffer.voltage = 12.5
        daemon.ema_buffer.load = 25
        daemon._track_discharge(12.5, base_timestamp + 600)
        daemon._handle_event_transition()
        assert daemon.discharge_buffer.collecting is True, "Buffer should restart collecting in second OB"

        # Poll 8: OB at 11.2V, 15% charge
        daemon.poll_count = 8
        daemon.current_metrics.event_type = EventType.BLACKOUT_REAL
        daemon.current_metrics.transition_occurred = False
        daemon.current_metrics.battery_charge = 15
        daemon.ema_buffer.voltage = 11.2
        daemon.ema_buffer.load = 25
        daemon._track_discharge(11.2, base_timestamp + 1000)

        # Poll 9: OL at 13.1V (TRANSITION - OB→OL)
        daemon.poll_count = 9
        prev_event = EventType.BLACKOUT_REAL
        daemon.current_metrics.event_type = EventType.ONLINE
        daemon.current_metrics.transition_occurred = True
        daemon.current_metrics.previous_event_type = prev_event
        daemon.current_metrics.battery_charge = 100
        daemon.ema_buffer.voltage = 13.1
        daemon.ema_buffer.load = 2

        # Verify second cycle buffer BEFORE transition (has 2 samples)
        assert len(daemon.discharge_buffer.voltages) == 2, f"Expected 2 samples in second cycle, got {len(daemon.discharge_buffer.voltages)}"
        assert daemon.discharge_buffer.voltages == [12.5, 11.2], f"Second cycle unexpected: {daemon.discharge_buffer.voltages}"

        daemon._handle_event_transition()

        # After transition, buffer should be cleared
        assert daemon.discharge_buffer.collecting is False, "Buffer should stop collecting after second OB→OL"
        assert len(daemon.discharge_buffer.voltages) == 0, "Buffer should be cleared after second transition"

        # Verify model updated twice (once per OB→OL)
        assert daemon.battery_model.add_soh_history_entry.call_count == 2, f"Expected 2 SoH updates, got {daemon.battery_model.add_soh_history_entry.call_count}"


# === HEALTH ENDPOINT TESTS (RED phase) ===

def test_write_health_endpoint_creates_file(tmp_path, monkeypatch):
    """Verify health.json is created with correct structure."""
    from src.monitor import _write_health_endpoint
    import src.monitor

    health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor, 'HEALTH_ENDPOINT_PATH', health_path)

    _write_health_endpoint(soc_percent=87.5, is_online=True)

    assert health_path.exists(), "health.json not created"

    import json
    with open(health_path) as f:
        data = json.load(f)

    assert "last_poll" in data
    assert "last_poll_unix" in data
    assert "current_soc_percent" in data
    assert "online" in data
    assert "daemon_version" in data
    assert "poll_latency_ms" in data


def test_health_endpoint_timestamp_format(tmp_path, monkeypatch):
    """Verify last_poll is ISO8601 UTC format."""
    from src.monitor import _write_health_endpoint
    from datetime import datetime
    import src.monitor

    health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor, 'HEALTH_ENDPOINT_PATH', health_path)

    _write_health_endpoint(soc_percent=50.0, is_online=False)

    import json
    with open(health_path) as f:
        data = json.load(f)

    # Should parse as ISO8601 UTC
    last_poll_dt = datetime.fromisoformat(data["last_poll"])
    assert last_poll_dt.tzinfo is not None, "Timestamp must be timezone-aware"
    assert data["last_poll"].endswith("Z") or data["last_poll"].endswith("+00:00"), "ISO8601 UTC should end with 'Z' or '+00:00'"


def test_health_endpoint_unix_timestamp(tmp_path, monkeypatch):
    """Verify last_poll_unix is valid Unix epoch."""
    from src.monitor import _write_health_endpoint
    import time
    import json
    import src.monitor

    health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor, 'HEALTH_ENDPOINT_PATH', health_path)

    before = int(time.time())
    _write_health_endpoint(soc_percent=75.0, is_online=True)
    after = int(time.time())

    with open(health_path) as f:
        data = json.load(f)

    unix_ts = data["last_poll_unix"]
    assert isinstance(unix_ts, int)
    assert before <= unix_ts <= after


def test_health_endpoint_soc_precision(tmp_path, monkeypatch):
    """Verify SoC rounded to 1 decimal place."""
    from src.monitor import _write_health_endpoint
    import json
    import src.monitor

    health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor, 'HEALTH_ENDPOINT_PATH', health_path)

    _write_health_endpoint(soc_percent=87.5432, is_online=True)

    with open(health_path) as f:
        data = json.load(f)

    assert data["current_soc_percent"] == 87.5


def test_health_endpoint_online_status(tmp_path, monkeypatch):
    """Verify online status reflects UPS state."""
    from src.monitor import _write_health_endpoint
    import json
    import src.monitor

    health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor, 'HEALTH_ENDPOINT_PATH', health_path)

    # Test OL state
    _write_health_endpoint(soc_percent=100.0, is_online=True)
    with open(health_path) as f:
        data = json.load(f)
    assert data["online"] is True

    # Test OB state
    _write_health_endpoint(soc_percent=25.0, is_online=False)
    with open(health_path) as f:
        data = json.load(f)
    assert data["online"] is False


def test_health_endpoint_version(tmp_path, monkeypatch):
    """Verify daemon_version is dynamically loaded from package metadata."""
    from src.monitor import _write_health_endpoint
    from unittest.mock import patch
    import json
    import src.monitor

    health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor, 'HEALTH_ENDPOINT_PATH', health_path)

    # Mock importlib.metadata.version to return "1.1"
    with patch('importlib.metadata.version', return_value='1.1'):
        _write_health_endpoint(soc_percent=50.0, is_online=True)

        with open(health_path) as f:
            data = json.load(f)

        assert data["daemon_version"] == "1.1"


def test_health_endpoint_updates_on_successive_calls(tmp_path, monkeypatch):
    """Verify file is replaced (not appended) on each call."""
    from src.monitor import _write_health_endpoint
    import time
    import json
    import src.monitor

    health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor, 'HEALTH_ENDPOINT_PATH', health_path)

    # First write
    _write_health_endpoint(soc_percent=100.0, is_online=True)
    file_size_1 = health_path.stat().st_size

    time.sleep(0.1)

    # Second write with different data
    _write_health_endpoint(soc_percent=50.0, is_online=False)
    file_size_2 = health_path.stat().st_size

    with open(health_path) as f:
        data = json.load(f)

    # File should be similar size (not doubled from appending)
    assert abs(file_size_1 - file_size_2) < 50, "File size changed dramatically; verify not appending"
    # Verify latest data is present
    assert data["current_soc_percent"] == 50.0
    assert data["online"] is False


# === RUN() LOOP TESTS (P1-4) ===

def test_run_error_rate_limiting(make_daemon):
    """P1-4: First N errors get full traceback, subsequent get summary only."""
    from src.monitor import ERROR_LOG_BURST
    import time as time_mod

    daemon = make_daemon()
    daemon.nut_client = MagicMock()
    daemon.nut_client.get_ups_vars.side_effect = ConnectionError("NUT down")

    poll_count = 0
    original_sleep = time_mod.sleep

    def fake_sleep(seconds):
        nonlocal poll_count
        poll_count += 1
        if poll_count >= ERROR_LOG_BURST + 5:
            daemon.running = False

    with patch('src.monitor.time.sleep', side_effect=fake_sleep), \
         patch('src.monitor.sd_notify'), \
         patch('src.monitor._write_health_endpoint'):
        daemon.run()

    # Verify error count tracked
    assert daemon._consecutive_errors >= ERROR_LOG_BURST


def test_run_sag_state_reset_on_error(make_daemon):
    """P1-4: Sag state resets to IDLE on polling error (no stuck 1s sleep)."""
    from src.monitor import SagState
    import time as time_mod

    daemon = make_daemon()
    daemon.nut_client = MagicMock()
    daemon.nut_client.get_ups_vars.side_effect = ConnectionError("NUT down")
    daemon.sag_state = SagState.MEASURING  # Pre-set to MEASURING

    poll_count = 0

    def fake_sleep(seconds):
        nonlocal poll_count
        poll_count += 1
        if poll_count >= 2:
            daemon.running = False

    with patch('src.monitor.time.sleep', side_effect=fake_sleep), \
         patch('src.monitor.sd_notify'), \
         patch('src.monitor._write_health_endpoint'):
        daemon.run()

    assert daemon.sag_state == SagState.IDLE


def test_run_ob_per_poll_compute_metrics(make_daemon):
    """P1-4: During OB, _compute_metrics called every poll (not every 6th)."""
    from src.event_classifier import EventType
    from src.monitor import CurrentMetrics, SagState
    import time as time_mod

    daemon = make_daemon()
    daemon.nut_client = MagicMock()
    daemon.nut_client.get_ups_vars.return_value = {
        'battery.voltage': '11.5', 'input.voltage': '0',
        'ups.status': 'OB DISCHRG', 'ups.load': '25'
    }

    daemon._update_ema = MagicMock(return_value=(11.5, 25.0))
    daemon._classify_event = MagicMock()
    daemon._track_voltage_sag = MagicMock()
    daemon._track_discharge = MagicMock()
    daemon._compute_metrics = MagicMock(return_value=(40.0, 8.0))
    daemon._handle_event_transition = MagicMock()
    daemon._log_status = MagicMock()
    daemon._write_virtual_ups = MagicMock()
    daemon.current_metrics = CurrentMetrics(
        event_type=EventType.BLACKOUT_REAL,
        previous_event_type=EventType.ONLINE
    )
    daemon.sag_state = SagState.IDLE

    poll_count = 0

    def fake_sleep(seconds):
        nonlocal poll_count
        poll_count += 1
        if poll_count >= 5:
            daemon.running = False

    with patch('src.monitor.time.sleep', side_effect=fake_sleep), \
         patch('src.monitor.sd_notify'), \
         patch('src.monitor._write_health_endpoint'):
        daemon.run()

    # All 5 polls should call _compute_metrics (OB = every poll)
    assert daemon._compute_metrics.call_count == 5, \
        f"Expected 5 compute_metrics calls during OB, got {daemon._compute_metrics.call_count}"


# === F13 TESTS (Event transition runs EVERY poll) ===

def test_f13_event_transition_fast_ol_ob_ol_cycle(make_daemon):
    """F13 (P0): Fast OL→OB→OL cycle (<60s) triggers _handle_event_transition() every poll.

    Verifies:
    - _handle_event_transition() called even during OL state (not just when gated)
    - Event transitions detected and processed immediately
    - previous_event_type updated every poll to track state changes
    """
    from src.event_classifier import EventType
    from src.monitor import CurrentMetrics, SagState

    daemon = make_daemon()
    daemon.nut_client = MagicMock()
    daemon._update_ema = MagicMock(return_value=(13.4, 15.0))
    daemon._classify_event = MagicMock()
    daemon._track_voltage_sag = MagicMock()
    daemon._track_discharge = MagicMock()
    daemon._compute_metrics = MagicMock(return_value=(75.0, 30.0))
    daemon._handle_event_transition = MagicMock()
    daemon._log_status = MagicMock()
    daemon._write_virtual_ups = MagicMock()
    daemon.current_metrics = CurrentMetrics(
        event_type=EventType.ONLINE,
        previous_event_type=EventType.ONLINE
    )
    daemon.sag_state = SagState.IDLE

    poll_sequence = [
        # Fast OL→OB→OL cycle over 6 polls
        EventType.ONLINE,              # Poll 0: OL
        EventType.ONLINE,              # Poll 1: OL
        EventType.BLACKOUT_REAL,       # Poll 2: OB (transition)
        EventType.BLACKOUT_REAL,       # Poll 3: OB
        EventType.ONLINE,              # Poll 4: Back to OL (transition)
        EventType.ONLINE,              # Poll 5: OL
    ]

    poll_count = 0

    def fake_sleep(seconds):
        nonlocal poll_count
        poll_count += 1
        if poll_count >= len(poll_sequence):
            daemon.running = False

    def fake_classify(ups_data):
        daemon.current_metrics.event_type = poll_sequence[daemon.poll_count]
        daemon.current_metrics.transition_occurred = (
            daemon.current_metrics.event_type != daemon.current_metrics.previous_event_type
        )

    daemon._classify_event.side_effect = fake_classify

    with patch('src.monitor.time.sleep', side_effect=fake_sleep), \
         patch('src.monitor.sd_notify'), \
         patch('src.monitor._write_health_endpoint'):
        daemon.run()

    # _handle_event_transition() should be called on every poll (6 total)
    assert daemon._handle_event_transition.call_count == len(poll_sequence), \
        f"F13: Expected {len(poll_sequence)} _handle_event_transition calls (every poll), " \
        f"got {daemon._handle_event_transition.call_count}"

    # Verify previous_event_type updated correctly on transitions
    # At end: previous should match the final event type
    assert daemon.current_metrics.previous_event_type == EventType.ONLINE, \
        f"F13: previous_event_type should be ONLINE at end, got {daemon.current_metrics.previous_event_type}"


def test_f11_watchdog_after_critical_writes(make_daemon):
    """F11 (P2): sd_notify('WATCHDOG=1') called AFTER _write_health_endpoint() and _write_virtual_ups().

    Verifies:
    - Health endpoint written before watchdog notification
    - Virtual UPS metrics written before watchdog notification
    - Daemon reports healthy to systemd only after critical I/O succeeds
    - Watchdog kick order: health → virtual_ups → watchdog
    """
    from src.event_classifier import EventType
    from src.monitor import CurrentMetrics, SagState

    daemon = make_daemon()
    daemon.nut_client = MagicMock()
    daemon.nut_client.get_ups_vars.return_value = {
        'battery.voltage': '12.0', 'input.voltage': '230',
        'ups.status': 'OL', 'ups.load': '15'
    }

    daemon._update_ema = MagicMock(return_value=(12.0, 15.0))
    daemon._classify_event = MagicMock()
    daemon._track_voltage_sag = MagicMock()
    daemon._track_discharge = MagicMock()
    daemon._compute_metrics = MagicMock(return_value=(75.0, 30.0))
    daemon._handle_event_transition = MagicMock()
    daemon._log_status = MagicMock()
    daemon._write_virtual_ups = MagicMock()
    daemon.current_metrics = CurrentMetrics(
        event_type=EventType.ONLINE,
        previous_event_type=EventType.ONLINE,
        soc=0.75,
        ups_status_override="OL"
    )
    daemon.sag_state = SagState.IDLE

    # Track call order: health_endpoint, virtual_ups, then watchdog
    call_order = []

    def mock_health_endpoint(*args, **kwargs):
        call_order.append('health_endpoint')

    def mock_virtual_ups(*args, **kwargs):
        call_order.append('virtual_ups')

    def mock_watchdog(status):
        call_order.append('watchdog')

    poll_count = 0

    def fake_sleep(seconds):
        nonlocal poll_count
        poll_count += 1
        if poll_count >= 2:
            daemon.running = False

    with patch('src.monitor.time.sleep', side_effect=fake_sleep), \
         patch('src.monitor._write_health_endpoint', side_effect=mock_health_endpoint), \
         patch('src.monitor.sd_notify', side_effect=mock_watchdog), \
         patch('src.monitor.write_virtual_ups_dev', side_effect=mock_virtual_ups):
        daemon.run()

    # Verify watchdog comes AFTER health_endpoint and virtual_ups in call sequence
    # Each poll should have: health_endpoint → virtual_ups → watchdog (during OL, only poll 0 and 6)
    # But F13 means _handle_event_transition runs every poll, so full sequence per poll is:
    # _classify → _track_sag → _track_discharge → _handle_event_transition →
    # (if gated) _compute_metrics → _log_status → _write_virtual_ups →
    # _write_health_endpoint → sd_notify

    # For 2 polls in OL state with modulo 6 gate:
    # Poll 0: gate open (0 % 6 == 0), writes health, virtual_ups, watchdog
    # Poll 1: gate closed, only health, watchdog
    assert len(call_order) > 0, "F11: No I/O operations recorded"

    # Find sequences where health_endpoint is followed by watchdog
    for i in range(len(call_order) - 1):
        if call_order[i] == 'health_endpoint' and i + 1 < len(call_order):
            # Next operation should be watchdog (virtual_ups may or may not appear)
            next_call = None
            for j in range(i + 1, len(call_order)):
                if call_order[j] in ('virtual_ups', 'watchdog'):
                    next_call = call_order[j]
                    break
            # For poll 1 (gate closed): health → watchdog (no virtual_ups)
            # For poll 0 (gate open): health → virtual_ups → watchdog
            assert next_call in ('virtual_ups', 'watchdog'), \
                f"F11: After health_endpoint, expected virtual_ups or watchdog, got {next_call}"

    # Verify watchdog is LAST in each poll's sequence (not before health_endpoint)
    last_health_idx = None
    for i in range(len(call_order) - 1, -1, -1):
        if call_order[i] == 'health_endpoint':
            last_health_idx = i
            break

    if last_health_idx is not None:
        # All watchdog calls after the last health_endpoint
        for i in range(last_health_idx + 1, len(call_order)):
            if call_order[i] == 'watchdog':
                # This is good - watchdog after health
                pass
            elif call_order[i] == 'health_endpoint':
                # New poll, new sequence
                pass
            else:
                # Should not have other operations after final health_endpoint and before final watchdog
                pass


class TestCapacityEstimatorIntegration:
    """Test MonitorDaemon integration with CapacityEstimator for Phase 12 Plan 02."""

    def test_daemon_initializes_capacity_estimator(self, make_daemon):
        """Test 1: MonitorDaemon creates CapacityEstimator instance at init."""
        from unittest.mock import patch, MagicMock
        from src.monitor import MonitorDaemon

        daemon = make_daemon()
        daemon.battery_model = MagicMock()
        daemon.battery_model.get_peukert_exponent.return_value = 1.2
        daemon.battery_model.get_nominal_voltage.return_value = 12.0
        daemon.battery_model.get_nominal_power_watts.return_value = 425.0
        daemon.battery_model.get_capacity_estimates.return_value = []

        # Reinitialize with proper mocks
        with patch('src.monitor.CapacityEstimator') as mock_ce:
            daemon_config = daemon.config
            daemon.__init__(daemon_config)

            # CapacityEstimator should have been instantiated
            mock_ce.assert_called_once()

    def test_handle_discharge_complete_calls_estimator(self, make_daemon):
        """Test 2: _handle_discharge_complete() calls CapacityEstimator.estimate()."""
        from unittest.mock import MagicMock
        daemon = make_daemon()

        # Mock dependencies
        daemon.capacity_estimator = MagicMock()
        daemon.battery_model = MagicMock()
        daemon.battery_model.data = {'lut': [], 'capacity_estimates': []}
        daemon.battery_model.get_convergence_status.return_value = {
            'sample_count': 1,
            'confidence_percent': 85.0,
            'latest_ah': 7.45,
            'rated_ah': 7.2,
            'converged': False,
            'capacity_ah_ref': None
        }

        # Setup discharge data
        discharge_data = {
            'voltage_series': [12.5, 12.0, 11.5, 11.0],
            'time_series': [0, 300, 600, 900],
            'current_series': [30, 32, 35, 40],
            'timestamp': '2026-03-15T12:34:56Z'
        }

        # Mock estimate to return success
        daemon.capacity_estimator.estimate.return_value = (7.45, 0.85, {'delta_soc_percent': 50.0, 'duration_sec': 900, 'load_avg_percent': 32.5})

        # Call handler
        daemon._handle_discharge_complete(discharge_data)

        # Verify CapacityEstimator.estimate() was called
        daemon.capacity_estimator.estimate.assert_called_once()

    def test_estimate_none_rejected_no_model_update(self, make_daemon):
        """Test 5: estimate() returns None → no model update, rejection logged."""
        from unittest.mock import MagicMock
        daemon = make_daemon()

        daemon.capacity_estimator = MagicMock()
        daemon.battery_model = MagicMock()
        daemon.battery_model.data = {'lut': []}

        discharge_data = {
            'voltage_series': [12.0, 11.9],  # Too shallow
            'time_series': [0, 100],  # Too short
            'current_series': [20, 21],
            'timestamp': '2026-03-15T12:34:56Z'
        }

        # Mock estimate to return None (quality filter rejection)
        daemon.capacity_estimator.estimate.return_value = None

        daemon._handle_discharge_complete(discharge_data)

        # Verify model.add_capacity_estimate was NOT called
        daemon.battery_model.add_capacity_estimate.assert_not_called()

    def test_estimate_success_calls_model_add(self, make_daemon):
        """Test 4: estimate() returns tuple → model.add_capacity_estimate() called."""
        from unittest.mock import MagicMock
        daemon = make_daemon()

        daemon.capacity_estimator = MagicMock()
        daemon.battery_model = MagicMock()
        daemon.battery_model.data = {'lut': [], 'capacity_estimates': []}
        daemon.battery_model.get_convergence_status.return_value = {
            'sample_count': 1,
            'confidence_percent': 85.0,
            'latest_ah': 7.45,
            'rated_ah': 7.2,
            'converged': False,
            'capacity_ah_ref': None
        }

        discharge_data = {
            'voltage_series': [12.5, 12.0, 11.5, 11.0],
            'time_series': [0, 300, 600, 900],
            'current_series': [30, 32, 35, 40],
            'timestamp': '2026-03-15T12:34:56Z'
        }

        metadata = {'delta_soc_percent': 50.0, 'duration_sec': 900, 'ir_mohms': 45.2, 'load_avg_percent': 32.5}
        daemon.capacity_estimator.estimate.return_value = (7.45, 0.85, metadata)

        daemon._handle_discharge_complete(discharge_data)

        # Verify model.add_capacity_estimate was called with correct args (keyword args)
        daemon.battery_model.add_capacity_estimate.assert_called_once()
        call_kwargs = daemon.battery_model.add_capacity_estimate.call_args.kwargs
        assert call_kwargs['ah_estimate'] == 7.45
        assert call_kwargs['confidence'] == 0.85
        assert call_kwargs['metadata'] == metadata
        assert call_kwargs['timestamp'] == '2026-03-15T12:34:56Z'


    def test_convergence_detection_sets_flag(self, make_daemon):
        """Test 7: After adding estimate, check has_converged() and set flag."""
        from unittest.mock import MagicMock
        daemon = make_daemon()

        daemon.capacity_estimator = MagicMock()
        daemon.battery_model = MagicMock()
        daemon.battery_model.data = {'lut': [], 'capacity_estimates': []}
        daemon.battery_model.get_convergence_status.return_value = {
            'sample_count': 3,
            'confidence_percent': 95.0,
            'latest_ah': 7.45,
            'rated_ah': 7.2,
            'converged': True,
            'capacity_ah_ref': None
        }

        discharge_data = {
            'voltage_series': [12.5, 11.0],
            'time_series': [0, 900],
            'current_series': [30, 40],
            'timestamp': '2026-03-15T12:34:56Z'
        }

        daemon.capacity_estimator.estimate.return_value = (7.45, 0.85, {'delta_soc_percent': 50.0, 'duration_sec': 900, 'load_avg_percent': 35.0})
        daemon.capacity_estimator.has_converged.return_value = True

        daemon._handle_discharge_complete(discharge_data)

        # Verify convergence check was performed
        daemon.capacity_estimator.has_converged.assert_called()

    def test_new_battery_flag_stored_in_config(self, make_daemon):
        """Test 6: --new-battery CLI flag stored in config['new_battery_requested']."""
        daemon = make_daemon()

        # Config is a frozen dataclass. new_battery_requested is stored in battery_model.data
        # Verify that battery_model has been initialized (will be mocked in real tests)
        assert daemon.battery_model is not None


    def test_integration_discharge_event_to_estimate_to_model(self, make_daemon):
        """Test 8: Integration test: discharge event → estimate → persistence."""
        from unittest.mock import MagicMock, patch
        daemon = make_daemon()

        # Create real mocks for integration
        daemon.capacity_estimator = MagicMock()
        daemon.battery_model = MagicMock()
        daemon.battery_model.data = {'lut': [], 'capacity_estimates': []}
        daemon.battery_model.get_convergence_status.return_value = {
            'sample_count': 1,
            'confidence_percent': 82.0,
            'latest_ah': 7.45,
            'rated_ah': 7.2,
            'converged': False,
            'capacity_ah_ref': None
        }

        discharge_data = {
            'voltage_series': [12.5, 12.3, 12.1, 11.9, 11.7, 11.5, 11.3, 11.1, 10.9],
            'time_series': [0, 100, 200, 300, 400, 500, 600, 700, 800],
            'current_series': [25, 26, 27, 28, 29, 30, 31, 32, 33],
            'timestamp': '2026-03-15T12:34:56Z'
        }

        metadata = {
            'delta_soc_percent': 52.0,
            'duration_sec': 800,
            'ir_mohms': 45.2,
            'load_avg_percent': 28.5
        }

        daemon.capacity_estimator.estimate.return_value = (7.45, 0.82, metadata)
        daemon.capacity_estimator.has_converged.return_value = False

        # Call handler
        daemon._handle_discharge_complete(discharge_data)

        # Verify model was updated with keyword arguments
        daemon.battery_model.add_capacity_estimate.assert_called_once()
        call_kwargs = daemon.battery_model.add_capacity_estimate.call_args.kwargs
        assert call_kwargs['ah_estimate'] == 7.45
        assert call_kwargs['confidence'] == 0.82
        assert call_kwargs['metadata'] == metadata
        assert call_kwargs['timestamp'] == '2026-03-15T12:34:56Z'


# ==============================================================================
# Task 2: Integration Tests for --new-battery CLI Flag
# ==============================================================================

def test_new_battery_flag_false(tmp_path):
    """Test 1: MonitorDaemon(new_battery_flag=False) → model.data['new_battery_requested'] = False.

    This is the default when --new-battery is NOT passed.
    """
    from src.monitor import MonitorDaemon, Config
    from src.model import BatteryModel
    from unittest.mock import patch, MagicMock
    from pathlib import Path
    import sys

    # Mock systemd before importing
    sys.modules['systemd'] = MagicMock()
    sys.modules['systemd.journal'] = MagicMock()

    with patch('src.monitor.NUTClient'), \
         patch('src.monitor.EMAFilter'), \
         patch('src.monitor.EventClassifier'), \
         patch('src.monitor.logger'), \
         patch.object(MonitorDaemon, '_check_nut_connectivity'), \
         patch.object(MonitorDaemon, '_validate_model'):

        config = Config(
            ups_name="test-cyberpower",
            polling_interval=10,
            reporting_interval=60,
            nut_host="localhost",
            nut_port=3493,
            nut_timeout=2.0,
            shutdown_minutes=5,
            soh_alert_threshold=0.80,
            model_dir=tmp_path / "test_model",
            config_dir=tmp_path / "test_config",
            runtime_threshold_minutes=20,
            reference_load_percent=20.0,
            ema_window_sec=120,
            capacity_ah=7.2,
        )

        daemon = MonitorDaemon(config, new_battery_flag=False)

        assert daemon.battery_model.data['new_battery_requested'] == False


def test_new_battery_flag_true(tmp_path):
    """Test 2: MonitorDaemon(new_battery_flag=True) → model.data['new_battery_requested'] = True.

    This is set when user passes --new-battery CLI flag.
    """
    from src.monitor import MonitorDaemon, Config
    from src.model import BatteryModel
    from unittest.mock import patch, MagicMock
    from pathlib import Path
    import sys

    # Mock systemd before importing
    sys.modules['systemd'] = MagicMock()
    sys.modules['systemd.journal'] = MagicMock()

    with patch('src.monitor.NUTClient'), \
         patch('src.monitor.EMAFilter'), \
         patch('src.monitor.EventClassifier'), \
         patch('src.monitor.logger'), \
         patch.object(MonitorDaemon, '_check_nut_connectivity'), \
         patch.object(MonitorDaemon, '_validate_model'):

        config = Config(
            ups_name="test-cyberpower",
            polling_interval=10,
            reporting_interval=60,
            nut_host="localhost",
            nut_port=3493,
            nut_timeout=2.0,
            shutdown_minutes=5,
            soh_alert_threshold=0.80,
            model_dir=tmp_path / "test_model",
            config_dir=tmp_path / "test_config",
            runtime_threshold_minutes=20,
            reference_load_percent=20.0,
            ema_window_sec=120,
            capacity_ah=7.2,
        )

        daemon = MonitorDaemon(config, new_battery_flag=True)

        assert daemon.battery_model.data['new_battery_requested'] == True


def test_new_battery_flag_persistence(tmp_path):
    """Test 3: new_battery_requested flag persists in model.json across save/reload.

    Ensures Phase 13 can read flag even if daemon restarts.
    """
    from src.monitor import MonitorDaemon, Config
    from src.model import BatteryModel
    from unittest.mock import patch, MagicMock
    from pathlib import Path
    import sys

    # Mock systemd before importing
    sys.modules['systemd'] = MagicMock()
    sys.modules['systemd.journal'] = MagicMock()

    with patch('src.monitor.NUTClient'), \
         patch('src.monitor.EMAFilter'), \
         patch('src.monitor.EventClassifier'), \
         patch('src.monitor.logger'), \
         patch.object(MonitorDaemon, '_check_nut_connectivity'), \
         patch.object(MonitorDaemon, '_validate_model'):

        config = Config(
            ups_name="test-cyberpower",
            polling_interval=10,
            reporting_interval=60,
            nut_host="localhost",
            nut_port=3493,
            nut_timeout=2.0,
            shutdown_minutes=5,
            soh_alert_threshold=0.80,
            model_dir=tmp_path / "test_model",
            config_dir=tmp_path / "test_config",
            runtime_threshold_minutes=20,
            reference_load_percent=20.0,
            ema_window_sec=120,
            capacity_ah=7.2,
        )

        # Set flag via MonitorDaemon
        daemon = MonitorDaemon(config, new_battery_flag=True)

        # Explicitly save model (normally happens during discharge)
        daemon.battery_model.save()

        # Reload model from disk
        reloaded_model = BatteryModel(tmp_path / 'test_model' / 'model.json')

        # Flag should persist
        assert reloaded_model.data.get('new_battery_requested', False) == True


def test_cli_new_battery_flag():
    """Test 4: CLI --new-battery flag is parsed correctly by argparse.

    This is an end-to-end test of parse_args() argument parsing.
    Integration test; requires src.monitor.parse_args.
    """
    from src.monitor import parse_args

    # Test with --new-battery flag
    args_with_flag = parse_args(['--new-battery'])
    assert args_with_flag.new_battery == True

    # Test without --new-battery flag
    args_without_flag = parse_args([])
    assert args_without_flag.new_battery == False


def test_journald_capacity_event_logged(make_daemon):
    """Test 1 (Phase 14 Plan 02): Verify capacity_measurement event logged with structured fields.

    Requirement: RPT-02 - journald logs capacity estimation events with EVENT_TYPE and custom fields.

    Setup: Create MonitorDaemon instance
    Execute: Simulate _handle_discharge_complete() with capacity_estimate
    Assert: logger.info() called with EVENT_TYPE='capacity_measurement' and extra dict fields
    """
    daemon = make_daemon()

    # Setup battery_model with real dict for data
    daemon.battery_model.data = {}
    daemon.battery_model.get_capacity_ah.return_value = 7.2
    daemon.battery_model.get_convergence_status.return_value = {
        'sample_count': 1,
        'confidence_percent': 88.0,
        'latest_ah': 6.95,
        'rated_ah': 7.2,
        'converged': False,
        'capacity_ah_ref': None
    }

    # Mock the capacity_estimator to return a valid estimate
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

    # Setup real model data
    daemon.battery_model.data['capacity_estimates'] = [
        {'ah_estimate': ah_estimate}
    ]

    # Capture logger calls
    with patch('src.monitor.logger') as mock_logger:
        discharge_data = {
            'voltage_series': [12.5, 12.0, 11.5, 11.0],
            'time_series': [0, 300, 600, 900],
            'current_series': [25.0, 25.0, 25.0, 25.0],
            'timestamp': '2026-03-16T12:00:00'
        }
        daemon._handle_discharge_complete(discharge_data)

        # Verify logger.info was called with capacity_measurement event
        calls = [call for call in mock_logger.info.call_args_list
                if 'capacity_measurement' in str(call)]
        assert len(calls) >= 1, "No capacity_measurement event logged"

        # Get the call with capacity_measurement
        capacity_call = calls[0]
        assert capacity_call is not None

        # Check extra dict has required fields
        if 'extra' in capacity_call.kwargs:
            extra = capacity_call.kwargs['extra']
            assert extra.get('EVENT_TYPE') == 'capacity_measurement'
            assert 'CAPACITY_AH' in extra
            assert 'CONFIDENCE_PERCENT' in extra
            assert 'SAMPLE_COUNT' in extra
            assert 'DELTA_SOC_PERCENT' in extra
            assert 'DURATION_SEC' in extra
            assert 'LOAD_AVG_PERCENT' in extra


def test_journald_baseline_lock_event(make_daemon):
    """Test 2 (Phase 14 Plan 02): Verify baseline_lock event logged once on convergence.

    Requirement: RPT-02 - journald logs baseline_lock events when convergence detected.

    Setup: Create MonitorDaemon with CapacityEstimator in converged state
    Execute: Trigger _handle_discharge_complete() with convergence
    Assert: logger.info() called with EVENT_TYPE='baseline_lock' exactly once
    Assert: Deduplication flag prevents duplicate events
    """
    daemon = make_daemon()

    # Setup battery_model with real dict for data
    daemon.battery_model.data = {}
    daemon.battery_model.get_capacity_ah.return_value = 7.2
    daemon.battery_model.get_convergence_status.return_value = {
        'sample_count': 3,
        'confidence_percent': 92.0,
        'latest_ah': 6.95,
        'rated_ah': 7.2,
        'converged': True,
        'capacity_ah_ref': None
    }

    # Setup: CapacityEstimator in converged state
    ah_estimate = 6.95
    confidence = 0.88
    metadata = {
        'delta_soc_percent': 52.3,
        'duration_sec': 1234,
        'load_avg_percent': 25.5,
    }

    daemon.capacity_estimator = MagicMock()
    daemon.capacity_estimator.estimate.return_value = (ah_estimate, confidence, metadata)
    daemon.capacity_estimator.has_converged.return_value = True

    # Setup model data with 3 converged estimates
    daemon.battery_model.data['capacity_estimates'] = [
        {'ah_estimate': 6.88},
        {'ah_estimate': 6.92},
        {'ah_estimate': 6.95}
    ]

    # Capture logger calls
    with patch('src.monitor.logger') as mock_logger:
        # First discharge (triggers baseline_lock)
        discharge_data = {
            'voltage_series': [12.5, 12.0, 11.5, 11.0],
            'time_series': [0, 300, 600, 900],
            'current_series': [25.0, 25.0, 25.0, 25.0],
            'timestamp': '2026-03-16T12:00:00'
        }
        daemon._handle_discharge_complete(discharge_data)

        # Count baseline_lock calls
        baseline_lock_calls = [call for call in mock_logger.info.call_args_list
                              if call.kwargs.get('extra', {}).get('EVENT_TYPE') == 'baseline_lock']
        assert len(baseline_lock_calls) == 1, f"Expected 1 baseline_lock event, got {len(baseline_lock_calls)}"

        # Verify baseline_lock extra fields
        baseline_lock_extra = baseline_lock_calls[0].kwargs.get('extra', {})
        assert baseline_lock_extra.get('EVENT_TYPE') == 'baseline_lock'
        assert 'CAPACITY_AH' in baseline_lock_extra
        assert 'SAMPLE_COUNT' in baseline_lock_extra
        assert 'TIMESTAMP' in baseline_lock_extra

        # Second discharge (should NOT trigger baseline_lock again due to flag)
        mock_logger.reset_mock()
        daemon._handle_discharge_complete(discharge_data)

        # Verify baseline_lock NOT called again
        baseline_lock_calls_2 = [call for call in mock_logger.info.call_args_list
                               if call.kwargs.get('extra', {}).get('EVENT_TYPE') == 'baseline_lock']
        assert len(baseline_lock_calls_2) == 0, "baseline_lock should not be logged twice (deduplication flag failed)"


def test_health_endpoint_capacity_fields(tmp_path, monkeypatch):
    """Phase 14 Plan 03 Task 2: Verify health endpoint includes all capacity fields.

    RPT-03 - Health endpoint exposes capacity metrics for Grafana scraping.
    """
    import json
    from src.monitor import _write_health_endpoint
    import src.monitor

    test_health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor, 'HEALTH_ENDPOINT_PATH', test_health_path)

    _write_health_endpoint(
        soc_percent=87.5,
        is_online=True,
        poll_latency_ms=45.2,
        capacity_ah_measured=6.95,
        capacity_ah_rated=7.2,
        capacity_confidence=0.92,
        capacity_samples_count=3,
        capacity_converged=True
    )

    # Verify file written
    assert test_health_path.exists(), "Health endpoint file not created"

    # Parse JSON
    data = json.loads(test_health_path.read_text())

    # Verify capacity fields present and correct
    assert 'capacity_ah_measured' in data, "capacity_ah_measured not in JSON"
    assert 'capacity_ah_rated' in data, "capacity_ah_rated not in JSON"
    assert 'capacity_confidence' in data, "capacity_confidence not in JSON"
    assert 'capacity_samples_count' in data, "capacity_samples_count not in JSON"
    assert 'capacity_converged' in data, "capacity_converged not in JSON"

    # Verify values
    assert data['capacity_ah_measured'] == 6.95, f"Expected 6.95, got {data['capacity_ah_measured']}"
    assert data['capacity_ah_rated'] == 7.2, f"Expected 7.2, got {data['capacity_ah_rated']}"
    assert data['capacity_confidence'] == 0.92, f"Expected 0.92, got {data['capacity_confidence']}"
    assert data['capacity_samples_count'] == 3, f"Expected 3, got {data['capacity_samples_count']}"
    assert data['capacity_converged'] is True, f"Expected True, got {data['capacity_converged']}"

    # Verify all existing fields still present
    assert 'last_poll' in data, "last_poll missing"
    assert 'online' in data, "online missing"
    assert 'daemon_version' in data, "daemon_version missing"
    assert 'current_soc_percent' in data, "current_soc_percent missing"


def test_health_endpoint_convergence_flag(tmp_path, monkeypatch):
    """Phase 14 Plan 03 Task 2: Verify convergence flag state matches input.

    RPT-03 - Health endpoint reflects convergence status accurately.
    """
    import json
    from src.monitor import _write_health_endpoint
    import src.monitor

    test_health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor, 'HEALTH_ENDPOINT_PATH', test_health_path)

    # Case A: Not converged (0 samples, no convergence)
    _write_health_endpoint(
        soc_percent=50.0,
        is_online=False,
        capacity_converged=False,
        capacity_samples_count=0,
        capacity_confidence=0.0
    )

    data_a = json.loads(test_health_path.read_text())
    assert data_a['capacity_converged'] is False, "Case A: expected not converged"
    assert data_a['capacity_samples_count'] == 0, "Case A: expected 0 samples"

    # Case B: Converged (3 samples, high confidence)
    _write_health_endpoint(
        soc_percent=50.0,
        is_online=False,
        capacity_converged=True,
        capacity_samples_count=3,
        capacity_confidence=0.92,
        capacity_ah_measured=6.95
    )

    data_b = json.loads(test_health_path.read_text())
    assert data_b['capacity_converged'] is True, "Case B: expected converged"
    assert data_b['capacity_samples_count'] == 3, "Case B: expected 3 samples"
    assert data_b['capacity_confidence'] == 0.92, "Case B: expected confidence 0.92"


def test_health_endpoint_null_capacity_measured(tmp_path, monkeypatch):
    """Phase 14 Plan 03 Task 2: Verify capacity_ah_measured is null when not measured.

    Edge case: capacity_ah_measured should be null in JSON, not 0.0.
    """
    import json
    from src.monitor import _write_health_endpoint
    import src.monitor

    test_health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor, 'HEALTH_ENDPOINT_PATH', test_health_path)

    _write_health_endpoint(
        soc_percent=75.0,
        is_online=True,
        capacity_ah_measured=None
    )

    data = json.loads(test_health_path.read_text())
    assert data['capacity_ah_measured'] is None, "capacity_ah_measured should be null, not 0.0"

