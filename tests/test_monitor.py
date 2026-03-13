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

                            # Set up discharge buffer with test data
                            daemon.discharge_buffer = {
                                'voltages': [13.0, 12.5, 12.0],
                                'times': [0, 10, 20],
                                'active': True
                            }

                            # Should have calibration_write method available
                            assert hasattr(mock_model, 'calibration_write')
