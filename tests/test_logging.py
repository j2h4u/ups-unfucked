"""Tests for logging infrastructure and JournalHandler fallback behavior."""

import logging
import sys
from unittest.mock import patch, MagicMock
import pytest

from src import alerter


class TestJournalHandlerFallback:
    """Test JournalHandler graceful degradation when /dev/log unavailable."""

    def test_journalhandler_fallback_to_stderr(self, capsys):
        """
        Verify logger falls back to stderr when SysLogHandler fails.

        Simulates /dev/log unavailable by mocking SysLogHandler constructor
        to raise OSError. Ensures logger still works with stderr fallback.
        """
        # Setup: mock SysLogHandler to raise OSError (simulate /dev/log missing)
        with patch('logging.handlers.SysLogHandler') as mock_syslog:
            mock_syslog.side_effect = OSError("No such file: /dev/log")

            # Call alerter's logger setup
            test_logger = alerter.setup_ups_logger("test-logger")

            # Verify we have at least one handler (stderr fallback)
            assert len(test_logger.handlers) > 0

            # Find the StreamHandler (stderr)
            stderr_handler = None
            for handler in test_logger.handlers:
                if isinstance(handler, logging.StreamHandler):
                    stderr_handler = handler
                    break

            assert stderr_handler is not None, "StreamHandler not found for stderr fallback"

            # Log a message and verify it appears on stderr
            test_logger.info("Test message from fallback")
            captured = capsys.readouterr()

            assert "Test message from fallback" in captured.err
            assert "test-logger" in captured.err

    def test_journalhandler_success(self):
        """
        Verify logger successfully uses SysLogHandler when available.

        Mocks SysLogHandler to succeed (doesn't raise) and verifies
        logger is configured without exceptions.
        """
        # Setup: patch SysLogHandler to return a real handler-like object
        # that won't raise exceptions
        with patch('logging.handlers.SysLogHandler') as mock_syslog:
            # Create a real SysLogHandler mock that won't crash on formatting
            real_handler = logging.StreamHandler()
            real_handler.setFormatter(
                logging.Formatter('test: %(levelname)s - %(message)s')
            )
            mock_syslog.return_value = real_handler

            # Call alerter's logger setup
            test_logger = alerter.setup_ups_logger("test-logger")

            # Verify we have handlers
            assert len(test_logger.handlers) > 0

            # Verify at least stderr handler exists
            has_stream = any(
                isinstance(h, logging.StreamHandler)
                for h in test_logger.handlers
            )
            assert has_stream, "StreamHandler (stderr) not found"

            # Verify logging works without exceptions
            try:
                test_logger.info("Test with success")
                # No exception = handler works correctly
            except Exception as e:
                pytest.fail(f"Logger raised exception: {e}")

    def test_structured_fields_compatible_with_fallback(self, capsys):
        """
        Verify structured extra fields don't crash fallback handler.

        Tests that logging with extra dict keys works correctly
        when using StreamHandler (stderr) fallback. Ensures robustness
        for both journald and test environments.
        """
        # Setup: mock SysLogHandler to fail
        with patch('logging.handlers.SysLogHandler') as mock_syslog:
            mock_syslog.side_effect = OSError("No /dev/log")

            test_logger = alerter.setup_ups_logger("test-logger")

            # Log with structured extra fields (as alerter does)
            try:
                test_logger.info(
                    "Test message",
                    extra={
                        'BATTERY_SOH': '0.85',
                        'THRESHOLD': '0.80',
                        'DAYS_TO_REPLACEMENT': '90',
                    }
                )
                # No exception = fallback handler handles extra fields gracefully
            except Exception as e:
                pytest.fail(f"Fallback handler crashed on structured fields: {e}")

            captured = capsys.readouterr()
            assert "Test message" in captured.err

    def test_alerter_logger_fallback(self, capsys):
        """
        Test alerter.setup_ups_logger() returns working logger with fallback.

        Verifies the alerter module's logger setup function correctly
        configures fallback behavior and handles structured fields.
        """
        # Mock SysLogHandler to fail
        with patch('logging.handlers.SysLogHandler') as mock_syslog:
            mock_syslog.side_effect = OSError("No /dev/log")

            # Use alerter's setup function
            logger = alerter.setup_ups_logger("ups-battery-monitor")

            # Verify handler is StreamHandler (fallback)
            assert len(logger.handlers) > 0
            has_stream = any(
                isinstance(h, logging.StreamHandler)
                for h in logger.handlers
            )
            assert has_stream, "No StreamHandler found in alerter logger"

            # Log warning with structured fields (as alert functions do)
            try:
                logger.warning(
                    "Battery health alert",
                    extra={
                        'BATTERY_SOH': '0.75',
                        'THRESHOLD': '0.80',
                    }
                )
            except Exception as e:
                pytest.fail(f"Alerter logger crashed: {e}")

            captured = capsys.readouterr()
            assert "Battery health alert" in captured.err
            assert "ups-battery-monitor" in captured.err

    def test_alert_soh_below_threshold_fallback(self, capsys):
        """
        Test alert_soh_below_threshold() with fallback handler.

        Verifies the alerter's alert function works correctly
        with fallback logging (no /dev/log).
        """
        with patch('logging.handlers.SysLogHandler') as mock_syslog:
            mock_syslog.side_effect = OSError("No /dev/log")

            logger = alerter.setup_ups_logger("test")

            # Call alert function as the daemon would
            try:
                alerter.alert_soh_below_threshold(
                    logger,
                    current_soh=0.75,
                    threshold_soh=0.80,
                    days_to_replacement=90.5
                )
            except Exception as e:
                pytest.fail(f"Alert function crashed: {e}")

            captured = capsys.readouterr()
            assert "75" in captured.err or "0.75" in captured.err
            assert "80" in captured.err or "0.80" in captured.err

    def test_alert_runtime_below_threshold_fallback(self, capsys):
        """
        Test alert_runtime_below_threshold() with fallback handler.

        Verifies runtime alert function works with fallback logging.
        """
        with patch('logging.handlers.SysLogHandler') as mock_syslog:
            mock_syslog.side_effect = OSError("No /dev/log")

            logger = alerter.setup_ups_logger("test")

            try:
                alerter.alert_runtime_below_threshold(
                    logger,
                    runtime_at_100_percent=18.5,
                    threshold_minutes=20.0
                )
            except Exception as e:
                pytest.fail(f"Runtime alert crashed: {e}")

            captured = capsys.readouterr()
            assert "18" in captured.err or "18.5" in captured.err
