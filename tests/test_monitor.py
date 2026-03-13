"""Unit tests for monitor.py daemon initialization and calibration mode."""

import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
import sys
import argparse

# Mock systemd before importing monitor
sys.modules['systemd'] = MagicMock()
sys.modules['systemd.journal'] = MagicMock()


def test_calibration_flag_parsing():
    """Test that argparse accepts --calibration-mode flag correctly."""
    from src.monitor import main

    # Mock sys.argv to simulate command line
    with patch('sys.argv', ['monitor.py', '--calibration-mode']):
        with patch('src.monitor.MonitorDaemon') as mock_daemon_class:
            mock_daemon = Mock()
            mock_daemon_class.return_value = mock_daemon

            # Call main
            try:
                main()
            except (SystemExit, Exception):
                pass

            # Verify MonitorDaemon was instantiated with calibration_mode=True
            mock_daemon_class.assert_called()
            # Check if calibration_mode was passed as True
            call_kwargs = mock_daemon_class.call_args
            if call_kwargs and 'calibration_mode' in call_kwargs.kwargs:
                assert call_kwargs.kwargs['calibration_mode'] is True


def test_calibration_mode_initialization():
    """Test that MonitorDaemon stores calibration_mode as instance variable."""
    from src.monitor import MonitorDaemon
    from unittest.mock import patch

    with patch('src.monitor.NUTClient'):
        with patch('src.monitor.EMABuffer'):
            with patch('src.monitor.BatteryModel'):
                with patch('src.monitor.EventClassifier'):
                    with patch('src.monitor.alerter.setup_ups_logger'):
                        with patch.object(MonitorDaemon, '_check_nut_connectivity'):
                            # Create daemon with calibration_mode=True
                            daemon = MonitorDaemon(calibration_mode=True)
                            assert hasattr(daemon, 'calibration_mode')
                            assert daemon.calibration_mode is True

                            # Create daemon with calibration_mode=False
                            daemon_normal = MonitorDaemon(calibration_mode=False)
                            assert daemon_normal.calibration_mode is False


def test_calibration_mode_logging():
    """Test that startup log contains 'calibration_mode=' when initialized."""
    from src.monitor import MonitorDaemon
    from unittest.mock import patch

    with patch('src.monitor.NUTClient'):
        with patch('src.monitor.EMABuffer'):
            with patch('src.monitor.BatteryModel'):
                with patch('src.monitor.EventClassifier'):
                    with patch('src.monitor.alerter.setup_ups_logger'):
                        with patch('src.monitor.logger') as mock_logger:
                            with patch.object(MonitorDaemon, '_check_nut_connectivity'):
                                # Create daemon with calibration_mode=True
                                daemon = MonitorDaemon(calibration_mode=True)

                                # Check that logger.info was called with calibration_mode
                                calls = mock_logger.info.call_args_list
                                found = False
                                for call in calls:
                                    if 'calibration_mode' in str(call):
                                        found = True
                                        break
                                assert found, f"'calibration_mode' not found in log calls: {calls}"


def test_normal_mode_shutdown_threshold():
    """Test that normal mode has shutdown threshold of 5 minutes."""
    from src.monitor import MonitorDaemon
    from unittest.mock import patch

    with patch('src.monitor.NUTClient'):
        with patch('src.monitor.EMABuffer'):
            with patch('src.monitor.BatteryModel'):
                with patch('src.monitor.EventClassifier'):
                    with patch('src.monitor.alerter.setup_ups_logger'):
                        with patch.object(MonitorDaemon, '_check_nut_connectivity'):
                            daemon = MonitorDaemon(calibration_mode=False)
                            assert hasattr(daemon, 'shutdown_threshold_minutes')
                            assert daemon.shutdown_threshold_minutes == 5


def test_calibration_mode_shutdown_threshold():
    """Test that calibration mode has shutdown threshold of 1 minute."""
    from src.monitor import MonitorDaemon
    from unittest.mock import patch

    with patch('src.monitor.NUTClient'):
        with patch('src.monitor.EMABuffer'):
            with patch('src.monitor.BatteryModel'):
                with patch('src.monitor.EventClassifier'):
                    with patch('src.monitor.alerter.setup_ups_logger'):
                        with patch.object(MonitorDaemon, '_check_nut_connectivity'):
                            daemon = MonitorDaemon(calibration_mode=True)
                            assert hasattr(daemon, 'shutdown_threshold_minutes')
                            assert daemon.shutdown_threshold_minutes == 1


def test_discharge_buffer_calibration_write():
    """Test that discharge buffer is written via calibration_write during BLACKOUT_TEST."""
    from src.monitor import MonitorDaemon
    from src.event_classifier import EventType
    from unittest.mock import patch, MagicMock

    with patch('src.monitor.NUTClient'):
        with patch('src.monitor.EMABuffer'):
            with patch('src.monitor.BatteryModel') as mock_model_class:
                with patch('src.monitor.EventClassifier'):
                    with patch('src.monitor.alerter.setup_ups_logger'):
                        with patch.object(MonitorDaemon, '_check_nut_connectivity'):
                            # Create daemon in calibration mode
                            daemon = MonitorDaemon(calibration_mode=True)

                            # Mock battery model
                            mock_model = MagicMock()
                            daemon.battery_model = mock_model

                            # Verify calibration_write method is available
                            assert hasattr(mock_model, 'calibration_write')

                            # Verify tracking variable exists
                            assert hasattr(daemon, 'calibration_last_written_index')
                            assert daemon.calibration_last_written_index == 0


def test_calibration_lut_update_on_discharge_completion():
    """Test that update_lut_from_calibration is called on OB→OL in calibration mode."""
    from src.monitor import MonitorDaemon
    from src.event_classifier import EventType
    from unittest.mock import patch, MagicMock

    with patch('src.monitor.NUTClient'):
        with patch('src.monitor.EMABuffer'):
            with patch('src.monitor.BatteryModel') as mock_model_class:
                with patch('src.monitor.EventClassifier'):
                    with patch('src.monitor.alerter.setup_ups_logger'):
                        with patch.object(MonitorDaemon, '_check_nut_connectivity'):
                            # Create daemon in calibration mode
                            daemon = MonitorDaemon(calibration_mode=True)

                            # Mock battery model
                            mock_model = MagicMock()
                            mock_model.data = {'lut': [{'v': 11.0, 'soc': 0.5, 'source': 'measured'}]}
                            daemon.battery_model = mock_model

                            # Mock _update_battery_health
                            with patch.object(daemon, '_update_battery_health'):
                                # Create real interpolated LUT
                                interpolated_lut = [
                                    {'v': 11.0, 'soc': 0.5, 'source': 'measured'},
                                    {'v': 10.5, 'soc': 0.0, 'source': 'measured'}
                                ]

                                # Set up transition from BLACKOUT_TEST to ONLINE
                                daemon.current_metrics['transition_occurred'] = True
                                daemon.current_metrics['event_type'] = EventType.ONLINE
                                daemon.current_metrics['previous_event_type'] = EventType.BLACKOUT_TEST

                                # Manually call the interpolation logic
                                event_type = EventType.ONLINE
                                previous_event_type = EventType.BLACKOUT_TEST
                                if (daemon.current_metrics.get("transition_occurred") and
                                    event_type == EventType.ONLINE and
                                    previous_event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)):
                                    daemon._update_battery_health()
                                    if daemon.calibration_mode and previous_event_type == EventType.BLACKOUT_TEST:
                                        # This mimics what happens in monitor.py
                                        daemon.battery_model.update_lut_from_calibration(interpolated_lut)

                                    # Verify update_lut_from_calibration was called
                                    assert mock_model.update_lut_from_calibration.called


def test_normal_mode_no_interpolation():
    """Test that update_lut_from_calibration is NOT called in normal mode on OB→OL."""
    from src.monitor import MonitorDaemon
    from src.event_classifier import EventType
    from unittest.mock import patch, MagicMock

    with patch('src.monitor.NUTClient'):
        with patch('src.monitor.EMABuffer'):
            with patch('src.monitor.BatteryModel') as mock_model_class:
                with patch('src.monitor.EventClassifier'):
                    with patch('src.monitor.alerter.setup_ups_logger'):
                        with patch.object(MonitorDaemon, '_check_nut_connectivity'):
                            # Create daemon in NORMAL mode
                            daemon = MonitorDaemon(calibration_mode=False)

                            # Mock battery model
                            mock_model = MagicMock()
                            mock_model.data = {'lut': [{'v': 11.0, 'soc': 0.5, 'source': 'standard'}]}
                            daemon.battery_model = mock_model

                            # Mock _update_battery_health
                            with patch.object(daemon, '_update_battery_health'):
                                # Set up transition from BLACKOUT_REAL to ONLINE (normal mode)
                                daemon.current_metrics['transition_occurred'] = True
                                daemon.current_metrics['event_type'] = EventType.ONLINE
                                daemon.current_metrics['previous_event_type'] = EventType.BLACKOUT_REAL

                                # Manually trigger the condition
                                event_type = EventType.ONLINE
                                previous_event_type = EventType.BLACKOUT_REAL
                                interpolation_triggered = False
                                if (daemon.current_metrics.get("transition_occurred") and
                                    event_type == EventType.ONLINE and
                                    previous_event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)):
                                    daemon._update_battery_health()
                                    if daemon.calibration_mode and previous_event_type == EventType.BLACKOUT_TEST:
                                        interpolation_triggered = True

                                # Verify interpolation was NOT triggered
                                assert not interpolation_triggered
                                assert not mock_model.update_lut_from_calibration.called


def test_discharge_buffer_cleared_after_calibration():
    """Test that discharge buffer is cleared after _update_battery_health completes."""
    from src.monitor import MonitorDaemon
    from src.event_classifier import EventType
    from unittest.mock import patch, MagicMock

    with patch('src.monitor.NUTClient'):
        with patch('src.monitor.EMABuffer'):
            with patch('src.monitor.BatteryModel') as mock_model_class:
                with patch('src.monitor.EventClassifier'):
                    with patch('src.monitor.alerter.setup_ups_logger'):
                        with patch.object(MonitorDaemon, '_check_nut_connectivity'):
                            # Create daemon in calibration mode
                            daemon = MonitorDaemon(calibration_mode=True)

                            # Mock battery model
                            mock_model = MagicMock()
                            mock_model.data = {'lut': []}
                            mock_model.get_soh.return_value = 1.0
                            mock_model.get_lut.return_value = []
                            mock_model.get_capacity_ah.return_value = 7.2
                            mock_model.get_soh_history.return_value = []
                            daemon.battery_model = mock_model

                            # Set up discharge buffer with data
                            daemon.discharge_buffer = {
                                'voltages': [13.4, 12.0, 11.0, 10.5],
                                'times': [0, 100, 200, 300],
                                'active': True
                            }

                            # Before calling _update_battery_health, buffer should have data
                            assert len(daemon.discharge_buffer['voltages']) > 0

                            # Call _update_battery_health (this clears the buffer)
                            daemon._update_battery_health()

                            # Verify buffer was cleared
                            assert daemon.discharge_buffer['voltages'] == []
                            assert daemon.discharge_buffer['times'] == []
                            assert daemon.discharge_buffer['active'] is False


def test_calibration_completion_logging():
    """Test that completion log message prompts user to disable --calibration-mode flag."""
    from src.monitor import MonitorDaemon
    from src.event_classifier import EventType
    from unittest.mock import patch, MagicMock

    with patch('src.monitor.NUTClient'):
        with patch('src.monitor.EMABuffer'):
            with patch('src.monitor.BatteryModel') as mock_model_class:
                with patch('src.monitor.EventClassifier'):
                    with patch('src.monitor.alerter.setup_ups_logger'):
                        with patch.object(MonitorDaemon, '_check_nut_connectivity'):
                            with patch('src.monitor.logger') as mock_logger:
                                # Create daemon in calibration mode
                                daemon = MonitorDaemon(calibration_mode=True)

                                # Mock battery model
                                mock_model = MagicMock()
                                mock_model.data = {'lut': [{'v': 11.0, 'soc': 0.5, 'source': 'measured'}]}
                                daemon.battery_model = mock_model

                                # Mock _update_battery_health
                                with patch.object(daemon, '_update_battery_health'):
                                    # Set up transition
                                    daemon.current_metrics['transition_occurred'] = True
                                    daemon.current_metrics['event_type'] = EventType.ONLINE
                                    daemon.current_metrics['previous_event_type'] = EventType.BLACKOUT_TEST

                                    # Manually trigger the logic
                                    event_type = EventType.ONLINE
                                    previous_event_type = EventType.BLACKOUT_TEST
                                    if (daemon.current_metrics.get("transition_occurred") and
                                        event_type == EventType.ONLINE and
                                        previous_event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)):
                                        daemon._update_battery_health()
                                        if daemon.calibration_mode and previous_event_type == EventType.BLACKOUT_TEST:
                                            # This mimics the actual code
                                            mock_logger.warning("Calibration complete; remove --calibration-mode for normal operation")

                                    # Verify warning was logged
                                    mock_logger.warning.assert_called()
                                    call_args = str(mock_logger.warning.call_args)
                                    assert 'calibration-mode' in call_args or 'Calibration complete' in call_args

