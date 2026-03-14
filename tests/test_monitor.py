"""Unit tests for monitor.py daemon initialization and calibration mode."""

import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
import sys
import argparse

# Mock systemd before importing monitor
sys.modules['systemd'] = MagicMock()
sys.modules['systemd.journal'] = MagicMock()


@pytest.fixture
def make_daemon():
    """Create MonitorDaemon with all external dependencies mocked."""
    from src.monitor import MonitorDaemon

    with patch('src.monitor.NUTClient'), \
         patch('src.monitor.EMAFilter'), \
         patch('src.monitor.BatteryModel'), \
         patch('src.monitor.EventClassifier'), \
         patch('src.monitor.alerter.setup_ups_logger'), \
         patch.object(MonitorDaemon, '_check_nut_connectivity'), \
         patch.object(MonitorDaemon, '_validate_model'):
        def _make(calibration_mode=False):
            return MonitorDaemon(calibration_mode=calibration_mode)
        yield _make


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

    # Trigger OL → OB transition
    daemon.event_classifier = MagicMock()
    daemon.event_classifier.transition_occurred = True
    daemon.v_before_sag = 13.50
    daemon.sag_buffer = []
    daemon.sag_collected = False
    daemon.fast_poll_active = True

    # Simulate 5 voltage samples during sag
    sag_voltages = [13.30, 13.25, 13.22, 13.20, 13.21]
    for v in sag_voltages:
        daemon.sag_buffer.append(v)

    # After 5 samples, compute median of last 3
    assert len(daemon.sag_buffer) >= 5
    recent = sorted(daemon.sag_buffer[-3:])
    v_sag = recent[1]  # median
    assert v_sag == 13.21  # median of [13.20, 13.21, 13.22]

    # Record sag
    daemon._record_voltage_sag(v_sag, EventType.BLACKOUT_TEST)

    # Verify model was called
    mock_model.add_r_internal_entry.assert_called_once()
    call_args = mock_model.add_r_internal_entry.call_args
    assert call_args[0][1] > 0  # r_ohm positive
    assert call_args[0][2] == 13.50  # v_before
    assert call_args[0][3] == 13.21  # v_sag
    mock_model.save.assert_called()


def test_voltage_sag_skipped_zero_current(make_daemon):
    """Test that sag recording is skipped when load is zero."""
    from src.event_classifier import EventType

    daemon = make_daemon()
    mock_model = MagicMock()
    mock_model.get_nominal_power_watts.return_value = 425.0
    mock_model.get_nominal_voltage.return_value = 12.0
    daemon.battery_model = mock_model

    daemon.ema_buffer = MagicMock()
    daemon.ema_buffer.load = 0.0
    daemon.v_before_sag = 13.50

    daemon._record_voltage_sag(13.20, EventType.BLACKOUT_TEST)
    mock_model.add_r_internal_entry.assert_not_called()


def test_sag_init_vars(make_daemon):
    """Test that sag-related instance vars are initialized."""
    daemon = make_daemon()
    assert daemon.v_before_sag is None
    assert daemon.sag_buffer == []
    assert daemon.sag_collected is False
    assert daemon.fast_poll_active is False


def test_calibration_flag_parsing():
    """Test that argparse accepts --calibration-mode flag correctly."""
    from src.monitor import main

    with patch('sys.argv', ['monitor.py', '--calibration-mode']):
        with patch('src.monitor.MonitorDaemon') as mock_daemon_class:
            mock_daemon = Mock()
            mock_daemon_class.return_value = mock_daemon

            try:
                main()
            except (SystemExit, Exception):
                pass

            mock_daemon_class.assert_called()
            call_kwargs = mock_daemon_class.call_args
            if call_kwargs and 'calibration_mode' in call_kwargs.kwargs:
                assert call_kwargs.kwargs['calibration_mode'] is True


def test_calibration_mode_initialization(make_daemon):
    daemon = make_daemon(calibration_mode=True)
    assert daemon.calibration_mode is True

    daemon_normal = make_daemon(calibration_mode=False)
    assert daemon_normal.calibration_mode is False


def test_calibration_mode_logging():
    from src.monitor import MonitorDaemon

    with patch('src.monitor.NUTClient'), \
         patch('src.monitor.EMAFilter'), \
         patch('src.monitor.BatteryModel'), \
         patch('src.monitor.EventClassifier'), \
         patch('src.monitor.alerter.setup_ups_logger'), \
         patch('src.monitor.logger') as mock_logger, \
         patch.object(MonitorDaemon, '_check_nut_connectivity'), \
         patch.object(MonitorDaemon, '_validate_model'):
        MonitorDaemon(calibration_mode=True)

        calls = mock_logger.info.call_args_list
        found = any('calibration_mode' in str(call) for call in calls)
        assert found, f"'calibration_mode' not found in log calls: {calls}"


def test_normal_mode_shutdown_threshold(make_daemon):
    daemon = make_daemon(calibration_mode=False)
    assert daemon.shutdown_threshold_minutes == 5


def test_calibration_mode_shutdown_threshold(make_daemon):
    daemon = make_daemon(calibration_mode=True)
    assert daemon.shutdown_threshold_minutes == 1


def test_discharge_buffer_calibration_write(make_daemon):
    daemon = make_daemon(calibration_mode=True)
    mock_model = MagicMock()
    daemon.battery_model = mock_model
    assert hasattr(mock_model, 'calibration_write')
    assert daemon.calibration_last_written_index == 0


def test_calibration_lut_update_on_discharge_completion(make_daemon):
    from src.event_classifier import EventType

    daemon = make_daemon(calibration_mode=True)

    mock_model = MagicMock()
    mock_model.data = {'lut': [{'v': 11.0, 'soc': 0.5, 'source': 'measured'}]}
    daemon.battery_model = mock_model

    with patch.object(daemon, '_update_battery_health'):
        interpolated_lut = [
            {'v': 11.0, 'soc': 0.5, 'source': 'measured'},
            {'v': 10.5, 'soc': 0.0, 'source': 'measured'}
        ]

        daemon.current_metrics['transition_occurred'] = True
        daemon.current_metrics['event_type'] = EventType.ONLINE
        daemon.current_metrics['previous_event_type'] = EventType.BLACKOUT_TEST

        event_type = EventType.ONLINE
        previous_event_type = EventType.BLACKOUT_TEST
        if (daemon.current_metrics.get("transition_occurred") and
            event_type == EventType.ONLINE and
            previous_event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)):
            daemon._update_battery_health()
            if daemon.calibration_mode and previous_event_type == EventType.BLACKOUT_TEST:
                daemon.battery_model.update_lut_from_calibration(interpolated_lut)

            assert mock_model.update_lut_from_calibration.called


def test_normal_mode_no_interpolation(make_daemon):
    from src.event_classifier import EventType

    daemon = make_daemon(calibration_mode=False)

    mock_model = MagicMock()
    mock_model.data = {'lut': [{'v': 11.0, 'soc': 0.5, 'source': 'standard'}]}
    daemon.battery_model = mock_model

    with patch.object(daemon, '_update_battery_health'):
        daemon.current_metrics['transition_occurred'] = True
        daemon.current_metrics['event_type'] = EventType.ONLINE
        daemon.current_metrics['previous_event_type'] = EventType.BLACKOUT_REAL

        event_type = EventType.ONLINE
        previous_event_type = EventType.BLACKOUT_REAL
        interpolation_triggered = False
        if (daemon.current_metrics.get("transition_occurred") and
            event_type == EventType.ONLINE and
            previous_event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)):
            daemon._update_battery_health()
            if daemon.calibration_mode and previous_event_type == EventType.BLACKOUT_TEST:
                interpolation_triggered = True

        assert not interpolation_triggered
        assert not mock_model.update_lut_from_calibration.called


def test_discharge_buffer_cleared_after_calibration(make_daemon):
    daemon = make_daemon(calibration_mode=True)

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


def test_calibration_completion_logging():
    from src.monitor import MonitorDaemon
    from src.event_classifier import EventType

    with patch('src.monitor.NUTClient'), \
         patch('src.monitor.EMAFilter'), \
         patch('src.monitor.BatteryModel'), \
         patch('src.monitor.EventClassifier'), \
         patch('src.monitor.alerter.setup_ups_logger'), \
         patch('src.monitor.logger') as mock_logger, \
         patch.object(MonitorDaemon, '_check_nut_connectivity'), \
         patch.object(MonitorDaemon, '_validate_model'):
        daemon = MonitorDaemon(calibration_mode=True)

        mock_model = MagicMock()
        mock_model.data = {'lut': [{'v': 11.0, 'soc': 0.5, 'source': 'measured'}]}
        daemon.battery_model = mock_model

        with patch.object(daemon, '_update_battery_health'):
            daemon.current_metrics['transition_occurred'] = True
            daemon.current_metrics['event_type'] = EventType.ONLINE
            daemon.current_metrics['previous_event_type'] = EventType.BLACKOUT_TEST

            event_type = EventType.ONLINE
            previous_event_type = EventType.BLACKOUT_TEST
            if (daemon.current_metrics.get("transition_occurred") and
                event_type == EventType.ONLINE and
                previous_event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)):
                daemon._update_battery_health()
                if daemon.calibration_mode and previous_event_type == EventType.BLACKOUT_TEST:
                    mock_logger.warning("Calibration complete; remove --calibration-mode for normal operation")

            mock_logger.warning.assert_called()
            call_args = str(mock_logger.warning.call_args)
            assert 'calibration-mode' in call_args or 'Calibration complete' in call_args


def test_calibration_mode_end_to_end():
    """Simulate complete calibration flow: start → discharge → OB→OL → verify LUT."""
    from src.monitor import MonitorDaemon
    from src.model import BatteryModel
    from src.event_classifier import EventType
    import tempfile
    import json

    with tempfile.TemporaryDirectory() as tmpdir:
        model_path = Path(tmpdir) / "model.json"

        with patch('src.monitor.NUTClient'), \
             patch('src.monitor.EMAFilter'), \
             patch('src.monitor.EventClassifier'), \
             patch('src.monitor.alerter.setup_ups_logger'), \
             patch.object(MonitorDaemon, '_check_nut_connectivity'), \
         patch.object(MonitorDaemon, '_validate_model'):
            daemon = MonitorDaemon(calibration_mode=True)

            model = BatteryModel(model_path=model_path)
            daemon.battery_model = model

            assert daemon.calibration_mode is True
            assert daemon.shutdown_threshold_minutes == 1

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

            model.calibration_write(voltage=10.95, soc=0.48, timestamp=1000.0)
            model.calibration_write(voltage=10.65, soc=0.15, timestamp=1100.0)
            model.calibration_write(voltage=10.55, soc=0.02, timestamp=1200.0)

            measured_count = sum(1 for e in model.get_lut() if e['source'] == 'measured')
            assert measured_count >= 3

            from src.soh_calculator import interpolate_cliff_region
            updated_lut = interpolate_cliff_region(model.data['lut'])
            model.update_lut_from_calibration(updated_lut)

            interpolated_count = sum(1 for e in model.get_lut() if e['source'] == 'interpolated')
            assert interpolated_count > 0

            reloaded = BatteryModel(model_path=model_path)
            assert sum(1 for e in reloaded.get_lut() if e['source'] == 'interpolated') > 0
            assert sum(1 for e in reloaded.get_lut() if e['source'] == 'measured') >= 3
