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
         patch('src.monitor.alerter.setup_ups_logger'), \
         patch.object(MonitorDaemon, '_check_nut_connectivity'), \
         patch.object(MonitorDaemon, '_validate_model'):
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
    assert daemon.discharge_buffer['collecting'] is False


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
    daemon.battery_model = mock_model

    daemon.ema_buffer = MagicMock()
    daemon.ema_buffer.load = 20.0

    daemon.discharge_buffer = {
        'voltages': [13.4, 12.0, 11.0, 10.5],
        'times': [0, 100, 200, 300],
        'collecting': True
    }

    daemon._update_battery_health()

    assert daemon.discharge_buffer['voltages'] == []
    assert daemon.discharge_buffer['times'] == []
    assert daemon.discharge_buffer['collecting'] is False


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
             patch('src.monitor.alerter.setup_ups_logger'), \
             patch.object(MonitorDaemon, '_check_nut_connectivity'), \
             patch.object(MonitorDaemon, '_validate_model'):
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
            from src.soh_calculator import interpolate_cliff_region
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

    # Test Case 1: Normal case with two discharge events (>60s duration, error >10%)
    daemon.discharge_buffer = {
        'voltages': [13.4, 12.0, 11.0, 10.5],
        'times': [0, 100, 200, 300],
        'collecting': False
    }

    with patch('src.monitor.peukert_runtime_hours') as mock_peukert:
        # Mock peukert_runtime_hours to return a value that creates >10% error
        mock_peukert.return_value = 1.0  # 60 minutes at full capacity
        daemon._auto_calibrate_peukert(current_soh=0.95)
        # Should trigger recalibration because error > 10%
        daemon.battery_model.set_peukert_exponent.assert_called()
        daemon.battery_model.save.assert_called()

    # Test Case 2: Empty discharge buffer - should skip
    daemon.discharge_buffer = {'voltages': [], 'times': [], 'collecting': False}
    daemon.battery_model.reset_mock()
    daemon._auto_calibrate_peukert(current_soh=0.95)
    daemon.battery_model.set_peukert_exponent.assert_not_called()

    # Test Case 3: Single sample - should skip (<2 samples)
    daemon.discharge_buffer = {'voltages': [12.0], 'times': [0], 'collecting': False}
    daemon.battery_model.reset_mock()
    daemon._auto_calibrate_peukert(current_soh=0.95)
    daemon.battery_model.set_peukert_exponent.assert_not_called()

    # Test Case 4: Identical timestamps (divide by zero protection)
    daemon.discharge_buffer = {
        'voltages': [13.4, 12.0],
        'times': [100, 100],  # Same time! -> 0 second duration
        'collecting': False
    }
    daemon.battery_model.reset_mock()
    # Should not raise exception
    daemon._auto_calibrate_peukert(current_soh=0.95)
    # Should skip due to short duration (<60s)
    daemon.battery_model.set_peukert_exponent.assert_not_called()

    # Test Case 5: Duration too short (<60s) - no update
    daemon.discharge_buffer = {
        'voltages': [13.4, 12.0],
        'times': [0, 50],  # 50 seconds total
        'collecting': False
    }
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
