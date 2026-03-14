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
        # Replace mocked JournalHandler with a real stderr handler so logging works in tests
        from src.monitor import logger as monitor_logger
        monitor_logger.handlers.clear()
        monitor_logger.addHandler(logging.StreamHandler())

        def _make():
            return MonitorDaemon()
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


def test_auto_calibration_end_to_end():
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
            daemon = MonitorDaemon()

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
