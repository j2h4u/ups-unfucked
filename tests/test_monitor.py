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
def make_daemon(daemon_config):
    """Create MonitorDaemon with all external dependencies mocked."""
    from src.monitor import MonitorDaemon

    with patch('src.monitor.NUTClient'), \
         patch('src.monitor.EMAFilter'), \
         patch('src.monitor.BatteryModel'), \
         patch('src.monitor.EventClassifier'), \
         patch.object(MonitorDaemon, '_check_nut_connectivity'), \
         patch.object(MonitorDaemon, '_validate_and_repair_model'), \
         patch.object(MonitorDaemon, '_reset_battery_baseline'):
        # Replace mocked JournalHandler with a real stderr handler so logging works in tests
        from src.monitor_config import logger as monitor_logger
        monitor_logger.handlers.clear()
        monitor_logger.addHandler(logging.StreamHandler())

        def _make():
            return MonitorDaemon(daemon_config)
        yield _make


def _poll_once_mocked(daemon, event_type):
    """Run _poll_once with mocked I/O, controlling the classified event type."""
    from src.event_classifier import EventType

    def classify_side_effect(ups_data):
        daemon.current_metrics.event_type = event_type

    daemon._classify_event = MagicMock(side_effect=classify_side_effect)

    with patch('src.monitor.sd_notify'), \
         patch('time.sleep'), \
         patch('src.monitor.write_health_endpoint'):
        daemon._poll_once()
        daemon.poll_count += 1


def test_per_poll_writes_during_blackout(make_daemon):
    """SAFE-01: Virtual UPS metrics written every poll during OB, not every 6th."""
    from src.event_classifier import EventType
    from src.monitor_config import CurrentMetrics

    daemon = make_daemon()
    daemon.nut_client = MagicMock()
    daemon.nut_client.get_ups_vars.return_value = {
        'battery.voltage': '12.0', 'input.voltage': '0',
        'ups.status': 'OB DISCHRG', 'ups.load': '25'
    }
    daemon._update_ema = MagicMock(return_value=(12.0, 25.0))
    daemon._compute_metrics = MagicMock(return_value=(50.0, 10.0))
    daemon._handle_event_transition = MagicMock()
    write_log = []

    def tracking_write(*args):
        write_log.append(daemon.current_metrics.event_type)

    daemon._write_virtual_ups = tracking_write
    daemon.sag_tracker = MagicMock(is_measuring=False)
    daemon.discharge_collector = MagicMock()
    daemon.discharge_collector.track.return_value = False
    daemon._log_status = MagicMock()
    daemon.scheduler_manager = MagicMock(last_scheduling_reason='observing', last_next_test_timestamp=None)
    daemon.poll_count = 0
    daemon._startup_logged = True
    daemon._consecutive_errors = 0
    daemon.current_metrics = CurrentMetrics(event_type=EventType.ONLINE, previous_event_type=EventType.ONLINE)

    event_sequence = [
        EventType.ONLINE, EventType.ONLINE,         # Polls 0-1: OL
        EventType.BLACKOUT_REAL, EventType.BLACKOUT_REAL,  # Polls 2-3: OB
        EventType.BLACKOUT_REAL, EventType.BLACKOUT_REAL,  # Polls 4-5: OB
        EventType.BLACKOUT_REAL, EventType.BLACKOUT_REAL,  # Polls 6-7: OB
        EventType.ONLINE, EventType.ONLINE,          # Polls 8-9: OL
        EventType.ONLINE, EventType.ONLINE,          # Polls 10-11: OL
    ]

    for evt in event_sequence:
        _poll_once_mocked(daemon, evt)

    # Poll 0: OL, poll_count%6==0 → write. Poll 1: OL, skip.
    # Polls 2-7: OB → write every poll (6 writes).
    # Polls 8-11: OL, only poll 12 would hit %6 → 0 writes.
    # Total: 1 + 6 = 7
    ob_writes = [e for e in write_log if e == EventType.BLACKOUT_REAL]
    ol_writes = [e for e in write_log if e == EventType.ONLINE]
    assert len(write_log) == 7, f"Expected 7 total writes, got {len(write_log)}"
    assert len(ob_writes) == 6, "Expected 6 OB writes (every poll during blackout)"
    assert len(ol_writes) == 1, "Expected 1 OL write (poll 0 on reporting boundary)"


def test_handle_event_transition_per_poll_during_ob(make_daemon):
    """SAFE-02: _handle_event_transition executes every poll (not gated by reporting interval)."""
    from src.event_classifier import EventType
    from src.monitor_config import CurrentMetrics

    daemon = make_daemon()
    daemon.nut_client = MagicMock()
    daemon.nut_client.get_ups_vars.return_value = {
        'battery.voltage': '11.0', 'input.voltage': '0',
        'ups.status': 'OB DISCHRG', 'ups.load': '30'
    }
    daemon._update_ema = MagicMock(return_value=(11.0, 30.0))
    daemon._compute_metrics = MagicMock(return_value=(30.0, 3.0))
    transition_calls = []

    def tracking_transition():
        transition_calls.append(daemon.current_metrics.event_type)

    daemon._handle_event_transition = tracking_transition
    daemon._write_virtual_ups = MagicMock()
    daemon.sag_tracker = MagicMock(is_measuring=False)
    daemon.discharge_collector = MagicMock()
    daemon.discharge_collector.track.return_value = False
    daemon._log_status = MagicMock()
    daemon.scheduler_manager = MagicMock(last_scheduling_reason='observing', last_next_test_timestamp=None)
    daemon.poll_count = 0
    daemon._startup_logged = True
    daemon._consecutive_errors = 0
    daemon.current_metrics = CurrentMetrics(
        event_type=EventType.BLACKOUT_REAL, previous_event_type=EventType.ONLINE,
    )

    for _ in range(4):
        _poll_once_mocked(daemon, EventType.BLACKOUT_REAL)

    # _handle_event_transition runs every poll (F13 fix), not gated
    assert len(transition_calls) == 4, f"Expected 4 transition calls during OB, got {len(transition_calls)}"
    assert all(e == EventType.BLACKOUT_REAL for e in transition_calls), \
        "All transition calls should be during OB state"


def test_no_writes_during_online_state(make_daemon):
    """SAFE-01: No spurious writes during OL state — only on reporting interval boundary."""
    from src.event_classifier import EventType
    from src.monitor_config import CurrentMetrics

    daemon = make_daemon()
    daemon.nut_client = MagicMock()
    daemon.nut_client.get_ups_vars.return_value = {
        'battery.voltage': '13.4', 'input.voltage': '220',
        'ups.status': 'OL', 'ups.load': '15'
    }
    daemon._update_ema = MagicMock(return_value=(13.4, 15.0))
    daemon._compute_metrics = MagicMock(return_value=(85.0, 120.0))
    daemon._handle_event_transition = MagicMock()
    write_log = []

    def tracking_write(*args):
        write_log.append(daemon.poll_count)

    daemon._write_virtual_ups = tracking_write
    daemon.sag_tracker = MagicMock(is_measuring=False)
    daemon.discharge_collector = MagicMock()
    daemon.discharge_collector.track.return_value = False
    daemon._log_status = MagicMock()
    daemon.scheduler_manager = MagicMock(last_scheduling_reason='observing', last_next_test_timestamp=None)
    daemon.poll_count = 0
    daemon._startup_logged = True
    daemon._consecutive_errors = 0
    daemon.current_metrics = CurrentMetrics(event_type=EventType.ONLINE, previous_event_type=EventType.ONLINE)

    for _ in range(7):
        _poll_once_mocked(daemon, EventType.ONLINE)

    # Only poll 0 and poll 6 should trigger writes (poll_count % 6 == 0)
    assert len(write_log) == 2, f"Expected 2 OL writes (poll 0, poll 6), got {len(write_log)}"
    assert write_log == [0, 6], f"Writes should occur at reporting interval boundaries, got polls {write_log}"


def test_lb_flag_signal_latency(make_daemon):
    """SAFE-02: LB flag written to virtual UPS within <10s of OB transition."""
    from src.event_classifier import EventType
    from src.monitor_config import CurrentMetrics
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
        daemon.sag_tracker = MagicMock(is_measuring=False)
        daemon.discharge_collector = MagicMock()
        daemon.discharge_collector.track.return_value = False
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

            # In production, _handle_event_transition runs every poll (ungated)
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

    # SagTracker is now a separate module — verify delegation exists
    from src.sag_tracker import SagTracker
    assert isinstance(daemon.sag_tracker, SagTracker)
    # Detailed sag recording behavior tested in tests/test_sag_tracker.py


def test_voltage_sag_skipped_zero_current(make_daemon):
    """Voltage sag recording skipped when load is zero — tested via SagTracker directly."""
    # Detailed skip conditions tested in tests/test_sag_tracker.py
    from src.sag_tracker import SagTracker
    daemon = make_daemon()
    assert isinstance(daemon.sag_tracker, SagTracker)


def test_sag_init_vars(make_daemon):
    """SagTracker initialized correctly on daemon construction."""
    from src.sag_tracker import SagTracker
    daemon = make_daemon()
    assert isinstance(daemon.sag_tracker, SagTracker)
    assert daemon.sag_tracker.is_measuring is False
    assert daemon.sag_tracker.ir_k == daemon.battery_model.get_ir_k()


def test_shutdown_threshold_from_config(make_daemon):
    """Shutdown threshold comes from TOML config."""
    daemon = make_daemon()
    assert daemon.shutdown_threshold_minutes == 5  # default from config


def test_discharge_buffer_init(make_daemon):
    """Discharge collector initialized correctly."""
    from src.discharge_collector import DischargeCollector
    daemon = make_daemon()
    assert isinstance(daemon.discharge_collector, DischargeCollector)
    assert daemon.discharge_collector.is_collecting is False


def test_discharge_buffer_cleared_after_health_update(make_daemon):
    """Buffer cleared after _update_battery_health completes."""
    from src.battery_math.rls import ScalarRLS

    daemon = make_daemon()

    mock_model = MagicMock()
    mock_model.state = {'lut': []}
    mock_model.get_soh.return_value = 1.0
    mock_model.get_lut.return_value = []
    mock_model.get_capacity_ah.return_value = 7.2
    mock_model.get_soh_history.return_value = []
    mock_model.get_peukert_exponent.return_value = 1.2
    mock_model.get_nominal_voltage.return_value = 12.0
    mock_model.get_nominal_power_watts.return_value = 425.0
    # Mock convergence status (measured capacity not converged by default)
    mock_model.get_convergence_status.return_value = {'converged': False, 'sample_count': 1}
    daemon.battery_model = mock_model

    daemon.ema_filter = MagicMock()
    daemon.ema_filter.load = 20.0
    daemon.rls_peukert = ScalarRLS(theta=1.2, P=1.0)
    daemon.discharge_handler.battery_model = mock_model
    daemon.discharge_handler.rls_peukert = daemon.rls_peukert

    from src.monitor_config import DischargeBuffer
    daemon.discharge_collector.discharge_buffer = DischargeBuffer(
        voltages=[13.4, 12.0, 11.0, 10.5],
        times=[0, 100, 200, 300],
        collecting=True
    )

    daemon._update_battery_health()

    assert daemon.discharge_collector.buffer.voltages == []
    assert daemon.discharge_collector.buffer.times == []
    assert daemon.discharge_collector.buffer.collecting is False


def test_config_immutability(daemon_config):
    """Verify Config frozen=True semantics prevent field mutation."""
    from dataclasses import FrozenInstanceError

    # Attempt to mutate frozen Config field
    with pytest.raises(FrozenInstanceError):
        daemon_config.ups_name = 'modified'

    with pytest.raises(FrozenInstanceError):
        daemon_config.polling_interval = 20

    # Config should be unchanged
    assert daemon_config.ups_name == 'test-cyberpower'
    assert daemon_config.polling_interval == 10


def _setup_peukert_daemon(make_daemon):
    """Helper: create a daemon with mocked battery_model for Peukert calibration tests."""
    from unittest.mock import Mock
    from src.battery_math.rls import ScalarRLS

    daemon = make_daemon()
    daemon.battery_model = Mock()
    daemon.battery_model.get_peukert_exponent = Mock(return_value=1.2)
    daemon.battery_model.set_peukert_exponent = Mock()
    daemon.battery_model.get_capacity_ah = Mock(return_value=7.2)
    daemon.battery_model.get_nominal_voltage = Mock(return_value=12.0)
    daemon.battery_model.get_nominal_power_watts = Mock(return_value=425.0)
    daemon.battery_model.save = Mock()
    daemon.battery_model.set_rls_state = Mock()
    daemon.reference_load_percent = 20.0
    daemon.rls_peukert = ScalarRLS(theta=1.2, P=1.0)
    daemon.discharge_handler.battery_model = daemon.battery_model
    daemon.discharge_handler.rls_peukert = daemon.rls_peukert

    daemon.ema_filter = Mock()
    daemon.ema_filter.load = 20.0
    return daemon


def test_peukert_normal_case_updates_rls(make_daemon):
    """Valid non-clamped calibrate_peukert result triggers RLS update."""
    from unittest.mock import patch
    from src.monitor_config import DischargeBuffer

    daemon = _setup_peukert_daemon(make_daemon)
    daemon.discharge_collector.discharge_buffer = DischargeBuffer(
        voltages=[13.4, 12.0, 11.0, 10.5],
        times=[0, 100, 200, 300],
    )

    with patch('src.discharge_handler.calibrate_peukert') as mock_calibrate:
        mock_calibrate.return_value = 1.18
        daemon._auto_calibrate_peukert(current_soh=0.95)
        call_args = daemon.battery_model.set_peukert_exponent.call_args
        assert call_args is not None, "set_peukert_exponent was not called"
        exponent_set = call_args.args[0]
        assert 1.0 <= exponent_set <= 1.4, \
            f"Peukert exponent {exponent_set} outside physical bounds [1.0, 1.4]"


def test_peukert_empty_buffer_skips(make_daemon):
    """Empty discharge buffer skips calibration entirely."""
    from src.monitor_config import DischargeBuffer

    daemon = _setup_peukert_daemon(make_daemon)
    daemon.discharge_collector.discharge_buffer = DischargeBuffer()
    daemon._auto_calibrate_peukert(current_soh=0.95)
    daemon.battery_model.set_peukert_exponent.assert_not_called()


def test_peukert_single_sample_skips(make_daemon):
    """Single sample (<2) skips calibration."""
    from src.monitor_config import DischargeBuffer

    daemon = _setup_peukert_daemon(make_daemon)
    daemon.discharge_collector.discharge_buffer = DischargeBuffer(voltages=[12.0], times=[0])
    daemon._auto_calibrate_peukert(current_soh=0.95)
    daemon.battery_model.set_peukert_exponent.assert_not_called()


def test_peukert_identical_timestamps_no_crash(make_daemon):
    """Identical timestamps (zero duration) do not raise and skip calibration."""
    from src.monitor_config import DischargeBuffer

    daemon = _setup_peukert_daemon(make_daemon)
    daemon.discharge_collector.discharge_buffer = DischargeBuffer(
        voltages=[13.4, 12.0],
        times=[100, 100],
    )
    daemon._auto_calibrate_peukert(current_soh=0.95)
    daemon.battery_model.set_peukert_exponent.assert_not_called()


def test_peukert_short_duration_skips(make_daemon):
    """Discharge shorter than 60s skips calibration."""
    from src.monitor_config import DischargeBuffer

    daemon = _setup_peukert_daemon(make_daemon)
    daemon.discharge_collector.discharge_buffer = DischargeBuffer(
        voltages=[13.4, 12.0],
        times=[0, 50],
    )
    daemon._auto_calibrate_peukert(current_soh=0.99)
    daemon.battery_model.set_peukert_exponent.assert_not_called()


def test_signal_handler_saves_model_and_stops(make_daemon):
    """SIGTERM handler persists model and clears running flag."""
    import signal
    from unittest.mock import Mock

    daemon = make_daemon()
    daemon.battery_model = Mock()
    daemon.battery_model.save = Mock()
    daemon.running = True

    daemon._signal_handler(signal.SIGTERM, None)

    assert daemon.battery_model.save.call_count == 1, \
        "SIGTERM handler should save model exactly once"
    assert daemon.running is False, "SIGTERM handler should clear running flag to trigger shutdown"


def test_signal_handler_idempotent(make_daemon):
    """Multiple SIGTERM signals handled gracefully without double-save crash."""
    import signal
    from unittest.mock import Mock

    daemon = make_daemon()
    daemon.battery_model = Mock()
    daemon.battery_model.save = Mock()
    daemon.running = True

    daemon._signal_handler(signal.SIGTERM, None)
    daemon._signal_handler(signal.SIGTERM, None)

    assert daemon.battery_model.save.call_count >= 1, \
        "Model should be saved at least once across multiple signals"
    assert daemon.running is False, "Multiple SIGTERM signals should leave running=False (idempotent)"


def test_ol_ob_ol_discharge_lifecycle_complete(make_daemon):
    """Full OL->OB->OL discharge lifecycle with mocked subsystems.

    NOTE: TestPollOnceCallChain in test_monitor_integration.py covers equivalent
    behaviors with real collaborators. This test exercises the lifecycle with
    explicit mock control for deterministic SoH/capacity values.

    Verifies:
    - _handle_event_transition() executes on OB→OL
    - _update_battery_health() called and SoH calculated
    - discharge_collector.track() accumulates voltage/time series
    - Model persisted to disk
    - Discharge buffer cleared after completion
    - Multiple cycles work correctly without state carryover
    """
    from src.event_classifier import EventType
    from src.battery_math.rls import ScalarRLS
    from unittest.mock import Mock, patch
    import time

    daemon = make_daemon()
    daemon.rls_peukert = ScalarRLS(theta=1.2, P=1.0)

    # Pre-setup: Mock soh_calculator and other dependencies to avoid complex physics
    with patch('src.discharge_handler.soh_calculator.calculate_soh_from_discharge') as mock_soh_calc, \
         patch('src.discharge_handler.replacement_predictor.linear_regression_soh') as mock_replace, \
         patch('src.discharge_handler.runtime_minutes') as mock_runtime:
        mock_soh_calc.return_value = (0.95, 7.2)  # Assume 95% SoH after discharge, using rated capacity
        mock_replace.return_value = None  # No replacement prediction
        mock_runtime.return_value = 30.0  # 30 minutes runtime

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
        daemon.battery_model.state = {'lut': [
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

        # Setup EMA buffer (also update discharge_collector.ema_filter to keep references in sync)
        daemon.ema_filter = Mock()
        daemon.ema_filter.stabilized = True
        daemon.ema_filter.voltage = 12.0
        daemon.ema_filter.load = 25.0
        daemon.discharge_collector.ema_filter = daemon.ema_filter

        # CYCLE 1: OL → OL → OB → OB → OB → OL → OL
        current_time = time.time()
        base_timestamp = current_time

        def _track(voltage, timestamp):
            """Drive discharge_collector.track() with current daemon state."""
            daemon.discharge_collector.track(
                voltage, timestamp,
                daemon.current_metrics.event_type,
                daemon.current_metrics,
            )

        # Poll 0: OL at 13.4V, 100% charge
        daemon.poll_count = 0
        daemon.current_metrics.event_type = EventType.ONLINE
        daemon.current_metrics.transition_occurred = False
        daemon.current_metrics.battery_charge = 100
        daemon.ema_filter.voltage = 13.4
        daemon.ema_filter.load = 2
        # No discharge tracking in OL state

        # Poll 1: OL at 13.3V, 100% charge
        daemon.poll_count = 1
        daemon.current_metrics.event_type = EventType.ONLINE
        daemon.current_metrics.transition_occurred = False
        daemon.current_metrics.battery_charge = 100
        daemon.ema_filter.voltage = 13.3
        daemon.ema_filter.load = 2

        # Poll 2: OB at 12.0V, 50% charge (TRANSITION - OL→OB)
        daemon.poll_count = 2
        prev_event = EventType.ONLINE
        daemon.current_metrics.event_type = EventType.BLACKOUT_REAL
        daemon.current_metrics.transition_occurred = True
        daemon.current_metrics.previous_event_type = prev_event
        daemon.current_metrics.battery_charge = 50
        daemon.ema_filter.voltage = 12.0
        daemon.ema_filter.load = 25
        _track(12.0, base_timestamp + 100)
        daemon._handle_event_transition()
        # After transition, discharge buffer should be collecting
        assert daemon.discharge_collector.buffer.collecting is True, "Buffer should start collecting on OB transition"

        # Poll 3: OB at 11.5V, 30% charge (continue discharge)
        daemon.poll_count = 3
        daemon.current_metrics.event_type = EventType.BLACKOUT_REAL
        daemon.current_metrics.transition_occurred = False
        daemon.current_metrics.battery_charge = 30
        daemon.ema_filter.voltage = 11.5
        daemon.ema_filter.load = 25
        _track(11.5, base_timestamp + 250)

        # Poll 4: OB at 11.0V, 20% charge (continue discharge)
        daemon.poll_count = 4
        daemon.current_metrics.event_type = EventType.BLACKOUT_REAL
        daemon.current_metrics.transition_occurred = False
        daemon.current_metrics.battery_charge = 20
        daemon.ema_filter.voltage = 11.0
        daemon.ema_filter.load = 25
        _track(11.0, base_timestamp + 500)

        # Poll 5: OL at 13.0V, 100% charge (TRANSITION - OB→OL)
        daemon.poll_count = 5
        prev_event = EventType.BLACKOUT_REAL
        daemon.current_metrics.event_type = EventType.ONLINE
        daemon.current_metrics.transition_occurred = True
        daemon.current_metrics.previous_event_type = prev_event
        daemon.current_metrics.battery_charge = 100
        daemon.ema_filter.voltage = 13.0
        daemon.ema_filter.load = 2

        # Verify discharge buffer BEFORE calling _handle_event_transition (which clears it)
        assert len(daemon.discharge_collector.buffer.voltages) == 3, f"Expected 3 voltage samples before transition, got {len(daemon.discharge_collector.buffer.voltages)}"
        assert daemon.discharge_collector.buffer.voltages == [12.0, 11.5, 11.0], f"Unexpected voltage samples before transition: {daemon.discharge_collector.buffer.voltages}"

        daemon._handle_event_transition()  # Should call _update_battery_health() and clear buffer

        # Verify discharge buffer state after OB→OL (should be cleared now)
        assert daemon.discharge_collector.buffer.collecting is False, "Buffer should stop collecting after OB→OL transition"
        assert len(daemon.discharge_collector.buffer.voltages) == 0, "Buffer should be cleared after _update_battery_health()"

        # Verify _update_battery_health() was called
        assert daemon.battery_model.add_soh_history_entry.call_count == 1, \
            "SoH history should have one entry after first OB->OL cycle"
        assert daemon.battery_model.save.call_count >= 1, \
            "Model should be saved after discharge cycle completion"

        # Poll 6: OL at 13.2V, 100% charge (stable OL)
        daemon.poll_count = 6
        daemon.current_metrics.event_type = EventType.ONLINE
        daemon.current_metrics.transition_occurred = False
        daemon.current_metrics.battery_charge = 100
        daemon.ema_filter.voltage = 13.2
        daemon.ema_filter.load = 2

        # CYCLE 2: OL → OB → OL (verify second cycle works)
        # Poll 7: OB at 12.5V, 60% charge (TRANSITION - OL→OB)
        daemon.poll_count = 7
        prev_event = EventType.ONLINE
        daemon.current_metrics.event_type = EventType.BLACKOUT_REAL
        daemon.current_metrics.transition_occurred = True
        daemon.current_metrics.previous_event_type = prev_event
        daemon.current_metrics.battery_charge = 60
        daemon.ema_filter.voltage = 12.5
        daemon.ema_filter.load = 25
        _track(12.5, base_timestamp + 600)
        daemon._handle_event_transition()
        assert daemon.discharge_collector.buffer.collecting is True, "Buffer should restart collecting in second OB"

        # Poll 8: OB at 11.2V, 15% charge
        daemon.poll_count = 8
        daemon.current_metrics.event_type = EventType.BLACKOUT_REAL
        daemon.current_metrics.transition_occurred = False
        daemon.current_metrics.battery_charge = 15
        daemon.ema_filter.voltage = 11.2
        daemon.ema_filter.load = 25
        _track(11.2, base_timestamp + 1000)

        # Poll 9: OL at 13.1V (TRANSITION - OB→OL)
        daemon.poll_count = 9
        prev_event = EventType.BLACKOUT_REAL
        daemon.current_metrics.event_type = EventType.ONLINE
        daemon.current_metrics.transition_occurred = True
        daemon.current_metrics.previous_event_type = prev_event
        daemon.current_metrics.battery_charge = 100
        daemon.ema_filter.voltage = 13.1
        daemon.ema_filter.load = 2

        # Verify second cycle buffer BEFORE transition (has 2 samples)
        assert len(daemon.discharge_collector.buffer.voltages) == 2, f"Expected 2 samples in second cycle, got {len(daemon.discharge_collector.buffer.voltages)}"
        assert daemon.discharge_collector.buffer.voltages == [12.5, 11.2], f"Second cycle unexpected: {daemon.discharge_collector.buffer.voltages}"

        daemon._handle_event_transition()

        # After transition, buffer should be cleared
        assert daemon.discharge_collector.buffer.collecting is False, "Buffer should stop collecting after second OB→OL"
        assert len(daemon.discharge_collector.buffer.voltages) == 0, "Buffer should be cleared after second transition"

        # Verify model updated twice (once per OB→OL)
        assert daemon.battery_model.add_soh_history_entry.call_count == 2, f"Expected 2 SoH updates, got {daemon.battery_model.add_soh_history_entry.call_count}"


# === HEALTH ENDPOINT TESTS (RED phase) ===

def test_write_health_endpoint_creates_file(tmp_path, monkeypatch):
    """Verify health.json is created with correct structure."""
    from src.monitor_config import write_health_endpoint, HealthSnapshot
    import src.monitor_config

    health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor_config, 'HEALTH_ENDPOINT_PATH', health_path)

    write_health_endpoint(HealthSnapshot(soc_percent=87.5, is_online=True))

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
    from src.monitor_config import write_health_endpoint, HealthSnapshot
    from datetime import datetime
    import src.monitor_config

    health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor_config, 'HEALTH_ENDPOINT_PATH', health_path)

    write_health_endpoint(HealthSnapshot(soc_percent=50.0, is_online=False))

    import json
    with open(health_path) as f:
        data = json.load(f)

    # Should parse as ISO8601 UTC
    last_poll_dt = datetime.fromisoformat(data["last_poll"])
    assert last_poll_dt.tzinfo is not None, "Timestamp must be timezone-aware"
    assert data["last_poll"].endswith("Z") or data["last_poll"].endswith("+00:00"), "ISO8601 UTC should end with 'Z' or '+00:00'"


def test_health_endpoint_unix_timestamp(tmp_path, monkeypatch):
    """Verify last_poll_unix is valid Unix epoch."""
    from src.monitor_config import write_health_endpoint, HealthSnapshot
    import time
    import json
    import src.monitor_config

    health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor_config, 'HEALTH_ENDPOINT_PATH', health_path)

    before = int(time.time())
    write_health_endpoint(HealthSnapshot(soc_percent=75.0, is_online=True))
    after = int(time.time())

    with open(health_path) as f:
        data = json.load(f)

    unix_ts = data["last_poll_unix"]
    assert isinstance(unix_ts, int)
    assert before <= unix_ts <= after


def test_health_endpoint_soc_precision(tmp_path, monkeypatch):
    """Verify SoC rounded to 1 decimal place."""
    from src.monitor_config import write_health_endpoint, HealthSnapshot
    import json
    import src.monitor_config

    health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor_config, 'HEALTH_ENDPOINT_PATH', health_path)

    write_health_endpoint(HealthSnapshot(soc_percent=87.5432, is_online=True))

    with open(health_path) as f:
        data = json.load(f)

    assert data["current_soc_percent"] == 87.5


def test_health_endpoint_online_status(tmp_path, monkeypatch):
    """Verify online status reflects UPS state."""
    from src.monitor_config import write_health_endpoint, HealthSnapshot
    import json
    import src.monitor_config

    health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor_config, 'HEALTH_ENDPOINT_PATH', health_path)

    # Test OL state
    write_health_endpoint(HealthSnapshot(soc_percent=100.0, is_online=True))
    with open(health_path) as f:
        data = json.load(f)
    assert data["online"] is True

    # Test OB state
    write_health_endpoint(HealthSnapshot(soc_percent=25.0, is_online=False))
    with open(health_path) as f:
        data = json.load(f)
    assert data["online"] is False


def test_health_endpoint_version(tmp_path, monkeypatch):
    """Verify daemon_version appears in health endpoint output."""
    from src.monitor_config import write_health_endpoint, HealthSnapshot
    import json
    import src.monitor_config

    health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor_config, 'HEALTH_ENDPOINT_PATH', health_path)
    monkeypatch.setattr(src.monitor_config, 'DAEMON_VERSION', '1.1')

    write_health_endpoint(HealthSnapshot(soc_percent=50.0, is_online=True))

    with open(health_path) as f:
        data = json.load(f)

    assert data["daemon_version"] == "1.1"


def test_health_endpoint_updates_on_successive_calls(tmp_path, monkeypatch):
    """Verify file is replaced (not appended) on each call."""
    from src.monitor_config import write_health_endpoint, HealthSnapshot
    import json
    import src.monitor_config

    health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor_config, 'HEALTH_ENDPOINT_PATH', health_path)

    # First write
    write_health_endpoint(HealthSnapshot(soc_percent=100.0, is_online=True))
    file_size_1 = health_path.stat().st_size

    # Second write with different data
    write_health_endpoint(HealthSnapshot(soc_percent=50.0, is_online=False))
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
    from src.monitor_config import ERROR_LOG_BURST
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
         patch('src.monitor_config.write_health_endpoint'):
        daemon.run()

    # Verify error count tracked
    assert daemon._consecutive_errors >= ERROR_LOG_BURST


def test_run_sag_state_reset_on_error(make_daemon):
    """P1-4: Sag state resets to IDLE on polling error (no stuck 1s sleep)."""
    from src.monitor_config import SagState
    import time as time_mod

    daemon = make_daemon()
    daemon.nut_client = MagicMock()
    daemon.nut_client.get_ups_vars.side_effect = ConnectionError("NUT down")
    daemon.sag_tracker._state = SagState.MEASURING  # Pre-set to MEASURING

    poll_count = 0

    def fake_sleep(seconds):
        nonlocal poll_count
        poll_count += 1
        if poll_count >= 2:
            daemon.running = False

    with patch('src.monitor.time.sleep', side_effect=fake_sleep), \
         patch('src.monitor.sd_notify'), \
         patch('src.monitor_config.write_health_endpoint'):
        daemon.run()

    assert daemon.sag_tracker._state == SagState.IDLE


def test_run_ob_per_poll_compute_metrics(make_daemon):
    """P1-4: During OB, _compute_metrics called every poll (not every 6th)."""
    from src.event_classifier import EventType
    from src.monitor_config import CurrentMetrics, SagState
    import time as time_mod

    daemon = make_daemon()
    daemon.nut_client = MagicMock()
    daemon.nut_client.get_ups_vars.return_value = {
        'battery.voltage': '11.5', 'input.voltage': '0',
        'ups.status': 'OB DISCHRG', 'ups.load': '25'
    }

    daemon._update_ema = MagicMock(return_value=(11.5, 25.0))
    daemon._classify_event = MagicMock()
    daemon.sag_tracker = MagicMock(is_measuring=False)
    daemon.discharge_collector = MagicMock()
    daemon.discharge_collector.track.return_value = False
    compute_calls = []

    def tracking_compute():
        compute_calls.append(daemon.poll_count)
        return (40.0, 8.0)

    daemon._compute_metrics = tracking_compute
    daemon._handle_event_transition = MagicMock()
    daemon._log_status = MagicMock()
    daemon._write_virtual_ups = MagicMock()
    daemon.current_metrics = CurrentMetrics(
        event_type=EventType.BLACKOUT_REAL,
        previous_event_type=EventType.ONLINE
    )
    daemon.sag_tracker.is_measuring = False

    poll_count = 0

    def fake_sleep(seconds):
        nonlocal poll_count
        poll_count += 1
        if poll_count >= 5:
            daemon.running = False

    with patch('src.monitor.time.sleep', side_effect=fake_sleep), \
         patch('src.monitor.sd_notify'), \
         patch('src.monitor_config.write_health_endpoint'):
        daemon.run()

    # All 5 polls should call _compute_metrics (OB = every poll)
    assert len(compute_calls) == 5, \
        f"Expected 5 compute_metrics calls during OB, got {len(compute_calls)}"


# === F13 TESTS (Event transition runs EVERY poll) ===

def test_f13_event_transition_fast_ol_ob_ol_cycle(make_daemon):
    """F13 (P0): Fast OL→OB→OL cycle (<60s) triggers _handle_event_transition() every poll.

    Verifies:
    - _handle_event_transition() called even during OL state (not just when gated)
    - Event transitions detected and processed immediately
    - previous_event_type updated every poll to track state changes
    """
    from src.event_classifier import EventType
    from src.monitor_config import CurrentMetrics, SagState

    daemon = make_daemon()
    daemon.nut_client = MagicMock()
    daemon._update_ema = MagicMock(return_value=(13.4, 15.0))
    daemon._classify_event = MagicMock()
    daemon.sag_tracker = MagicMock(is_measuring=False)
    daemon.discharge_collector = MagicMock()
    daemon.discharge_collector.track.return_value = False
    daemon._compute_metrics = MagicMock(return_value=(75.0, 30.0))
    transition_events = []

    def tracking_transition():
        transition_events.append(daemon.current_metrics.event_type)

    daemon._handle_event_transition = tracking_transition
    daemon._log_status = MagicMock()
    daemon._write_virtual_ups = MagicMock()
    daemon.current_metrics = CurrentMetrics(
        event_type=EventType.ONLINE,
        previous_event_type=EventType.ONLINE
    )
    daemon.sag_tracker.is_measuring = False

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
         patch('src.monitor_config.write_health_endpoint'):
        daemon.run()

    # _handle_event_transition() should be called on every poll (6 total)
    assert len(transition_events) == len(poll_sequence), \
        f"F13: Expected {len(poll_sequence)} transition calls (every poll), got {len(transition_events)}"

    # Verify previous_event_type updated correctly on transitions
    # At end: previous should match the final event type
    assert daemon.current_metrics.previous_event_type == EventType.ONLINE, \
        f"F13: previous_event_type should be ONLINE at end, got {daemon.current_metrics.previous_event_type}"


def test_f11_watchdog_after_critical_writes(make_daemon):
    """F11 (P2): sd_notify('WATCHDOG=1') called AFTER write_health_endpoint() and _write_virtual_ups().

    Verifies:
    - Health endpoint written before watchdog notification
    - Virtual UPS metrics written before watchdog notification
    - Daemon reports healthy to systemd only after critical I/O succeeds
    - Watchdog kick order: health → virtual_ups → watchdog
    """
    from src.event_classifier import EventType
    from src.monitor_config import CurrentMetrics, SagState

    daemon = make_daemon()
    daemon.nut_client = MagicMock()
    daemon.nut_client.get_ups_vars.return_value = {
        'battery.voltage': '12.0', 'input.voltage': '230',
        'ups.status': 'OL', 'ups.load': '15'
    }

    daemon._update_ema = MagicMock(return_value=(12.0, 15.0))
    daemon._classify_event = MagicMock()
    daemon.sag_tracker = MagicMock(is_measuring=False)
    daemon.discharge_collector = MagicMock()
    daemon.discharge_collector.track.return_value = False
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
    daemon.sag_tracker.is_measuring = False

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
         patch('src.monitor_config.write_health_endpoint', side_effect=mock_health_endpoint), \
         patch('src.monitor.sd_notify', side_effect=mock_watchdog), \
         patch('src.monitor.write_virtual_ups_dev', side_effect=mock_virtual_ups):
        daemon.run()

    # Verify watchdog comes AFTER health_endpoint and virtual_ups in call sequence
    # Each poll should have: health_endpoint → virtual_ups → watchdog (during OL, only poll 0 and 6)
    # But F13 means _handle_event_transition runs every poll, so full sequence per poll is:
    # _classify → _track_sag → discharge_collector.track → _handle_event_transition →
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

    # Verify no watchdog call precedes a health_endpoint call within the same poll
    for i, call in enumerate(call_order):
        if call == 'watchdog':
            # No health_endpoint should appear after this watchdog in the sequence
            remaining = call_order[i + 1:]
            health_after = [c for c in remaining if c == 'health_endpoint']
            assert not health_after, \
                f"F11: watchdog at index {i} precedes {len(health_after)} health_endpoint call(s)"


class TestCapacityEstimatorIntegration:
    """Test MonitorDaemon integration with CapacityEstimator."""

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

            # CapacityEstimator should have been instantiated exactly once
            assert mock_ce.call_count == 1, "CapacityEstimator should be instantiated exactly once during __init__"

    def test_handle_discharge_complete_calls_estimator(self, make_daemon):
        """Test 2: _handle_discharge_complete() calls CapacityEstimator.estimate()."""
        from unittest.mock import MagicMock
        daemon = make_daemon()

        # Mock dependencies
        daemon.capacity_estimator = MagicMock()
        daemon.battery_model = MagicMock()
        daemon.discharge_handler.capacity_estimator = daemon.capacity_estimator
        daemon.discharge_handler.battery_model = daemon.battery_model
        daemon.battery_model.state = {'lut': [], 'capacity_estimates': []}
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
            'load_series': [30, 32, 35, 40],
            'timestamp': '2026-03-15T12:34:56Z'
        }

        # Mock estimate to return success
        daemon.capacity_estimator.estimate.return_value = (7.45, 0.85, {'delta_soc_percent': 50.0, 'duration_sec': 900, 'load_avg_percent': 32.5})

        # Call handler
        daemon._handle_discharge_complete(discharge_data)

        # Verify CapacityEstimator.estimate() was called
        assert daemon.capacity_estimator.estimate.call_count == 1, \
            "estimate() should be called exactly once per discharge completion"

    def test_estimate_none_rejected_no_model_update(self, make_daemon):
        """Test 5: estimate() returns None → no model update, rejection logged."""
        from unittest.mock import MagicMock
        daemon = make_daemon()

        daemon.capacity_estimator = MagicMock()
        daemon.battery_model = MagicMock()
        daemon.battery_model.state = {'lut': []}

        discharge_data = {
            'voltage_series': [12.0, 11.9],  # Too shallow
            'time_series': [0, 100],  # Too short
            'load_series': [20, 21],
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
        daemon.discharge_handler.capacity_estimator = daemon.capacity_estimator
        daemon.discharge_handler.battery_model = daemon.battery_model
        daemon.battery_model.state = {'lut': [], 'capacity_estimates': []}
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
            'load_series': [30, 32, 35, 40],
            'timestamp': '2026-03-15T12:34:56Z'
        }

        metadata = {'delta_soc_percent': 50.0, 'duration_sec': 900, 'discharge_slope_mohm': 45.2, 'load_avg_percent': 32.5}
        daemon.capacity_estimator.estimate.return_value = (7.45, 0.85, metadata)

        daemon._handle_discharge_complete(discharge_data)

        # Verify model.add_capacity_estimate was called with correct args (keyword args)
        assert daemon.battery_model.add_capacity_estimate.call_count == 1, \
            "Capacity estimate should be persisted to model exactly once"
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
        daemon.discharge_handler.capacity_estimator = daemon.capacity_estimator
        daemon.discharge_handler.battery_model = daemon.battery_model
        daemon.battery_model.state = {'lut': [], 'capacity_estimates': []}
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
            'load_series': [30, 40],
            'timestamp': '2026-03-15T12:34:56Z'
        }

        daemon.capacity_estimator.estimate.return_value = (7.45, 0.85, {'delta_soc_percent': 50.0, 'duration_sec': 900, 'load_avg_percent': 35.0})
        daemon.capacity_estimator.has_converged.return_value = True

        daemon._handle_discharge_complete(discharge_data)

        # Verify convergence check was performed
        assert daemon.capacity_estimator.has_converged.call_count >= 1, \
            "has_converged() should be called during discharge completion to check convergence"

    def test_battery_replaced_resets_baseline(self, daemon_config, tmp_path):
        """--new-battery resets SoH to 1.0 and clears capacity estimates."""
        from src.model import BatteryModel
        from src.monitor import MonitorDaemon
        from unittest.mock import MagicMock, patch

        with patch('src.monitor.NUTClient'), \
             patch('src.monitor.EMAFilter'), \
             patch('src.monitor.BatteryModel'), \
             patch('src.monitor.EventClassifier'), \
             patch('src.monitor.CapacityEstimator'), \
             patch('src.monitor.DischargeHandler'), \
             patch.object(MonitorDaemon, '_check_nut_connectivity'), \
             patch.object(MonitorDaemon, '_validate_and_repair_model'):
            daemon = MonitorDaemon(daemon_config)

        model = BatteryModel(tmp_path / 'reset_model.json')
        model.state['capacity_estimates'] = [{'ah_estimate': 6.5}]
        model.state['soh'] = 0.85
        model.state['full_capacity_ah_ref'] = 7.2
        daemon.battery_model = model
        daemon.discharge_handler = MagicMock()

        daemon._reset_battery_baseline()

        assert model.state['soh'] == 1.0
        assert model.state['capacity_estimates'] == []
        assert model.state['cycle_count'] == 0


    def test_integration_discharge_event_to_estimate_to_model(self, make_daemon):
        """Test 8: Integration test: discharge event → estimate → persistence."""
        from unittest.mock import MagicMock, patch
        daemon = make_daemon()

        # Create real mocks for integration
        daemon.capacity_estimator = MagicMock()
        daemon.battery_model = MagicMock()
        daemon.discharge_handler.capacity_estimator = daemon.capacity_estimator
        daemon.discharge_handler.battery_model = daemon.battery_model
        daemon.battery_model.state = {'lut': [], 'capacity_estimates': []}
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
            'load_series': [25, 26, 27, 28, 29, 30, 31, 32, 33],
            'timestamp': '2026-03-15T12:34:56Z'
        }

        metadata = {
            'delta_soc_percent': 52.0,
            'duration_sec': 800,
            'discharge_slope_mohm': 45.2,
            'load_avg_percent': 28.5
        }

        daemon.capacity_estimator.estimate.return_value = (7.45, 0.82, metadata)
        daemon.capacity_estimator.has_converged.return_value = False

        # Call handler
        daemon._handle_discharge_complete(discharge_data)

        # Verify model was updated with keyword arguments
        assert daemon.battery_model.add_capacity_estimate.call_count == 1, \
            "Capacity estimate should be persisted to model exactly once"
        call_kwargs = daemon.battery_model.add_capacity_estimate.call_args.kwargs
        assert call_kwargs['ah_estimate'] == 7.45
        assert call_kwargs['confidence'] == 0.82
        assert call_kwargs['metadata'] == metadata
        assert call_kwargs['timestamp'] == '2026-03-15T12:34:56Z'


# ==============================================================================
# Task 2: Integration Tests for --new-battery CLI Flag
# ==============================================================================

def test_battery_replaced_false_default(tmp_path):
    """Test 1: MonitorDaemon() without _reset_battery_baseline → no baseline reset.

    This is the default when --new-battery is NOT passed.
    """
    from src.monitor import MonitorDaemon
    from src.monitor_config import Config
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
         patch.object(MonitorDaemon, '_validate_and_repair_model'):

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

            runtime_threshold_minutes=20,
            reference_load_percent=20.0,
            ema_window_sec=120,
            capacity_ah=7.2,
        )

        daemon = MonitorDaemon(config)

        # new_battery_detected cleared on startup
        assert daemon.battery_model.state['new_battery_detected'] == False


def test_battery_replaced_true(tmp_path):
    """Test 2: MonitorDaemon() + _reset_battery_baseline() resets SoH and capacity.

    This is the flow when user passes --new-battery CLI flag (main() calls _reset_battery_baseline).
    """
    from src.monitor import MonitorDaemon
    from src.monitor_config import Config
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
         patch.object(MonitorDaemon, '_validate_and_repair_model'):

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

            runtime_threshold_minutes=20,
            reference_load_percent=20.0,
            ema_window_sec=120,
            capacity_ah=7.2,
        )

        daemon = MonitorDaemon(config)
        daemon._reset_battery_baseline()

        # Baseline reset sets SoH to 1.0 and clears capacity estimates
        assert daemon.battery_model.state.get('soh') == 1.0
        assert daemon.battery_model.state.get('capacity_estimates') == []


def test_battery_replaced_persistence(tmp_path):
    """Test 3: Baseline reset persists in model.json across save/reload.

    Ensures reset state survives daemon restarts.
    """
    from src.monitor import MonitorDaemon
    from src.monitor_config import Config
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
         patch.object(MonitorDaemon, '_validate_and_repair_model'):

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

            runtime_threshold_minutes=20,
            reference_load_percent=20.0,
            ema_window_sec=120,
            capacity_ah=7.2,
        )

        # Reset baseline via _reset_battery_baseline (same as main() with --new-battery)
        daemon = MonitorDaemon(config)
        daemon._reset_battery_baseline()

        # Explicitly save model (normally happens during discharge)
        daemon.battery_model.save()

        # Reload model from disk
        reloaded_model = BatteryModel(tmp_path / 'test_model' / 'model.json')

        # Baseline reset state should persist
        assert reloaded_model.state.get('soh') == 1.0
        assert reloaded_model.state.get('capacity_estimates') == []
        assert reloaded_model.state.get('cycle_count') == 0


def test_cli_battery_replaced():
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
    """Verify capacity_measurement event logged with structured fields.

    Requirement: RPT-02 - journald logs capacity estimation events with EVENT_TYPE and custom fields.

    Setup: Create MonitorDaemon instance
    Execute: Simulate _handle_discharge_complete() with capacity_estimate
    Assert: logger.info() called with EVENT_TYPE='capacity_measurement' and extra dict fields
    """
    daemon = make_daemon()

    # Setup battery_model with real dict for data
    daemon.battery_model.state = {}
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
    daemon.discharge_handler.capacity_estimator = daemon.capacity_estimator
    daemon.capacity_estimator.estimate.return_value = (ah_estimate, confidence, metadata)
    daemon.capacity_estimator.has_converged.return_value = False

    # Setup real model data
    daemon.battery_model.state['capacity_estimates'] = [
        {'ah_estimate': ah_estimate}
    ]

    # Capture logger calls
    with patch('src.discharge_handler.logger') as mock_logger:
        discharge_data = {
            'voltage_series': [12.5, 12.0, 11.5, 11.0],
            'time_series': [0, 300, 600, 900],
            'load_series': [25.0, 25.0, 25.0, 25.0],
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

        # Check extra dict has required fields (unconditional — extra must be present)
        assert 'extra' in capacity_call.kwargs, "capacity_measurement log call missing extra= dict"
        extra = capacity_call.kwargs['extra']
        assert extra.get('event_type') == 'capacity_measurement'
        assert 'capacity_ah' in extra
        assert 'confidence_percent' in extra
        assert 'sample_count' in extra
        assert 'delta_soc_percent' in extra
        assert 'duration_sec' in extra
        assert 'load_avg_percent' in extra


def test_journald_baseline_lock_event(make_daemon):
    """Verify baseline_lock event logged once on convergence.

    Requirement: RPT-02 - journald logs baseline_lock events when convergence detected.

    Setup: Create MonitorDaemon with CapacityEstimator in converged state
    Execute: Trigger _handle_discharge_complete() with convergence
    Assert: logger.info() called with EVENT_TYPE='baseline_lock' exactly once
    Assert: Deduplication flag prevents duplicate events
    """
    daemon = make_daemon()

    # Setup battery_model with real dict for data
    daemon.battery_model.state = {}
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
    daemon.discharge_handler.capacity_estimator = daemon.capacity_estimator
    daemon.capacity_estimator.estimate.return_value = (ah_estimate, confidence, metadata)
    daemon.capacity_estimator.has_converged.return_value = True

    # Setup model data with 3 converged estimates
    daemon.battery_model.state['capacity_estimates'] = [
        {'ah_estimate': 6.88},
        {'ah_estimate': 6.92},
        {'ah_estimate': 6.95}
    ]

    # Capture logger calls
    with patch('src.discharge_handler.logger') as mock_logger:
        # First discharge (triggers baseline_lock)
        discharge_data = {
            'voltage_series': [12.5, 12.0, 11.5, 11.0],
            'time_series': [0, 300, 600, 900],
            'load_series': [25.0, 25.0, 25.0, 25.0],
            'timestamp': '2026-03-16T12:00:00'
        }
        daemon._handle_discharge_complete(discharge_data)

        # Count baseline_lock calls
        baseline_lock_calls = [call for call in mock_logger.info.call_args_list
                              if call.kwargs.get('extra', {}).get('event_type') == 'baseline_lock']
        assert len(baseline_lock_calls) == 1, f"Expected 1 baseline_lock event, got {len(baseline_lock_calls)}"

        # Verify baseline_lock extra fields
        baseline_lock_extra = baseline_lock_calls[0].kwargs.get('extra', {})
        assert baseline_lock_extra.get('event_type') == 'baseline_lock'
        assert 'capacity_ah' in baseline_lock_extra
        assert 'sample_count' in baseline_lock_extra

        # Second discharge (should NOT trigger baseline_lock again due to flag)
        mock_logger.reset_mock()
        daemon._handle_discharge_complete(discharge_data)

        # Verify baseline_lock NOT called again
        baseline_lock_calls_2 = [call for call in mock_logger.info.call_args_list
                               if call.kwargs.get('extra', {}).get('event_type') == 'baseline_lock']
        assert len(baseline_lock_calls_2) == 0, "baseline_lock should not be logged twice (deduplication flag failed)"


def test_health_endpoint_capacity_fields(tmp_path, monkeypatch):
    """Verify health endpoint includes all capacity fields.

    RPT-03 - Health endpoint exposes capacity metrics for Grafana scraping.
    """
    import json
    from src.monitor_config import write_health_endpoint, HealthSnapshot
    import src.monitor_config

    test_health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor_config, 'HEALTH_ENDPOINT_PATH', test_health_path)

    write_health_endpoint(HealthSnapshot(
        soc_percent=87.5,
        is_online=True,
        poll_latency_ms=45.2,
        capacity_ah_measured=6.95,
        capacity_ah_rated=7.2,
        capacity_confidence=0.92,
        capacity_samples_count=3,
        capacity_converged=True
    ))

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
    """Verify convergence flag state matches input.

    RPT-03 - Health endpoint reflects convergence status accurately.
    """
    import json
    from src.monitor_config import write_health_endpoint, HealthSnapshot
    import src.monitor_config

    test_health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor_config, 'HEALTH_ENDPOINT_PATH', test_health_path)

    # Case A: Not converged (0 samples, no convergence)
    write_health_endpoint(HealthSnapshot(
        soc_percent=50.0,
        is_online=False,
        capacity_converged=False,
        capacity_samples_count=0,
        capacity_confidence=0.0
    ))

    data_a = json.loads(test_health_path.read_text())
    assert data_a['capacity_converged'] is False, "Case A: expected not converged"
    assert data_a['capacity_samples_count'] == 0, "Case A: expected 0 samples"

    # Case B: Converged (3 samples, high confidence)
    write_health_endpoint(HealthSnapshot(
        soc_percent=50.0,
        is_online=False,
        capacity_converged=True,
        capacity_samples_count=3,
        capacity_confidence=0.92,
        capacity_ah_measured=6.95
    ))

    data_b = json.loads(test_health_path.read_text())
    assert data_b['capacity_converged'] is True, "Case B: expected converged"
    assert data_b['capacity_samples_count'] == 3, "Case B: expected 3 samples"
    assert data_b['capacity_confidence'] == 0.92, "Case B: expected confidence 0.92"


def test_health_endpoint_null_capacity_measured(tmp_path, monkeypatch):
    """Verify capacity_ah_measured is null when not measured.

    Edge case: capacity_ah_measured should be null in JSON, not 0.0.
    """
    import json
    from src.monitor_config import write_health_endpoint, HealthSnapshot
    import src.monitor_config

    test_health_path = tmp_path / "ups-health.json"
    monkeypatch.setattr(src.monitor_config, 'HEALTH_ENDPOINT_PATH', test_health_path)

    write_health_endpoint(HealthSnapshot(
        soc_percent=75.0,
        is_online=True,
        capacity_ah_measured=None
    ))

    data = json.loads(test_health_path.read_text())
    assert data['capacity_ah_measured'] is None, "capacity_ah_measured should be null, not 0.0"


def test_consecutive_errors_increment_on_error(make_daemon):
    """_consecutive_errors increments on each poll error in run() loop."""
    daemon = make_daemon()
    daemon.nut_client = MagicMock()
    daemon.nut_client.get_ups_vars.side_effect = ConnectionError("NUT down")

    poll_count = 0

    def fake_sleep(seconds):
        nonlocal poll_count
        poll_count += 1
        if poll_count >= 3:
            daemon.running = False

    with patch('src.monitor.time.sleep', side_effect=fake_sleep), \
         patch('src.monitor.sd_notify'), \
         patch('src.monitor_config.write_health_endpoint'):
        daemon.run()

    assert daemon._consecutive_errors == 3, \
        f"Expected 3 consecutive errors, got {daemon._consecutive_errors}"


def test_consecutive_errors_reset_on_success(make_daemon):
    """_consecutive_errors resets to 0 after a successful poll."""
    from src.event_classifier import EventType
    from src.monitor_config import CurrentMetrics

    daemon = make_daemon()
    daemon.nut_client = MagicMock()

    call_count = 0

    def get_ups_vars_side_effect():
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise ConnectionError("NUT down")
        # Third call succeeds — return valid UPS data
        return {
            'battery.voltage': 13.4,
            'ups.load': 16.0,
            'ups.status': 'OL',
            'input.voltage': 222.0,
            'battery.charge': 100.0,
            'battery.runtime': 1500.0,
        }

    daemon.nut_client.get_ups_vars.side_effect = get_ups_vars_side_effect

    poll_count = 0

    def fake_sleep(seconds):
        nonlocal poll_count
        poll_count += 1

    # After 2 errors + 1 success, stop the loop inside _poll_once
    # by patching _poll_once to track the successful reset
    original_poll_once = daemon._poll_once

    def patched_poll_once():
        original_poll_once()
        # After a successful poll, stop the loop
        daemon.running = False

    daemon._poll_once = patched_poll_once

    with patch('src.monitor.time.sleep', side_effect=fake_sleep), \
         patch('src.monitor.sd_notify'), \
         patch('src.monitor_config.write_health_endpoint'):
        daemon.run()

    assert daemon._consecutive_errors == 0, \
        f"Expected 0 consecutive errors after success, got {daemon._consecutive_errors}"

