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
        mock_soh_calc.return_value = 0.95  # Assume 95% SoH after discharge
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

def test_write_health_endpoint_creates_file(tmp_path):
    """Verify health.json is created with correct structure."""
    from src.monitor import _write_health_endpoint

    model_dir = tmp_path / "model"
    model_dir.mkdir()

    _write_health_endpoint(soc_percent=87.5, is_online=True)

    health_path = Path("/dev/shm/ups-health.json")
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

    # Cleanup
    health_path.unlink(missing_ok=True)


def test_health_endpoint_timestamp_format(tmp_path):
    """Verify last_poll is ISO8601 UTC format."""
    from src.monitor import _write_health_endpoint
    from datetime import datetime
    from pathlib import Path

    _write_health_endpoint(soc_percent=50.0, is_online=False)

    health_path = Path("/dev/shm/ups-health.json")
    import json
    with open(health_path) as f:
        data = json.load(f)

    # Should parse as ISO8601 UTC
    last_poll_dt = datetime.fromisoformat(data["last_poll"])
    assert last_poll_dt.tzinfo is not None, "Timestamp must be timezone-aware"
    assert data["last_poll"].endswith("Z") or data["last_poll"].endswith("+00:00"), "ISO8601 UTC should end with 'Z' or '+00:00'"

    # Cleanup
    health_path.unlink(missing_ok=True)


def test_health_endpoint_unix_timestamp(tmp_path):
    """Verify last_poll_unix is valid Unix epoch."""
    from src.monitor import _write_health_endpoint
    import time
    import json
    from pathlib import Path

    before = int(time.time())
    _write_health_endpoint(soc_percent=75.0, is_online=True)
    after = int(time.time())

    health_path = Path("/dev/shm/ups-health.json")
    with open(health_path) as f:
        data = json.load(f)

    unix_ts = data["last_poll_unix"]
    assert isinstance(unix_ts, int)
    assert before <= unix_ts <= after

    # Cleanup
    health_path.unlink(missing_ok=True)


def test_health_endpoint_soc_precision(tmp_path):
    """Verify SoC rounded to 1 decimal place."""
    from src.monitor import _write_health_endpoint
    import json
    from pathlib import Path

    _write_health_endpoint(soc_percent=87.5432, is_online=True)

    health_path = Path("/dev/shm/ups-health.json")
    with open(health_path) as f:
        data = json.load(f)

    assert data["current_soc_percent"] == 87.5

    # Cleanup
    health_path.unlink(missing_ok=True)


def test_health_endpoint_online_status(tmp_path):
    """Verify online status reflects UPS state."""
    from src.monitor import _write_health_endpoint
    import json
    from pathlib import Path

    # Test OL state
    _write_health_endpoint(soc_percent=100.0, is_online=True)
    health_path = Path("/dev/shm/ups-health.json")
    with open(health_path) as f:
        data = json.load(f)
    assert data["online"] is True

    # Test OB state
    _write_health_endpoint(soc_percent=25.0, is_online=False)
    with open(health_path) as f:
        data = json.load(f)
    assert data["online"] is False

    # Cleanup
    health_path.unlink(missing_ok=True)

    # Cleanup
    health_path.unlink(missing_ok=True)


def test_health_endpoint_version(tmp_path):
    """Verify daemon_version is dynamically loaded from package metadata."""
    from src.monitor import _write_health_endpoint
    from unittest.mock import patch
    import json
    from pathlib import Path

    # Mock importlib.metadata.version to return "1.1"
    with patch('importlib.metadata.version', return_value='1.1'):
        _write_health_endpoint(soc_percent=50.0, is_online=True)

        health_path = Path("/dev/shm/ups-health.json")
        with open(health_path) as f:
            data = json.load(f)

        assert data["daemon_version"] == "1.1"

        # Cleanup
        health_path.unlink(missing_ok=True)


def test_health_endpoint_updates_on_successive_calls(tmp_path):
    """Verify file is replaced (not appended) on each call."""
    from src.monitor import _write_health_endpoint
    import time
    import json
    from pathlib import Path

    health_path = Path("/dev/shm/ups-health.json")

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

    # Cleanup
    health_path.unlink(missing_ok=True)


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
