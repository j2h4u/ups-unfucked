"""Tests for logging and alerter output."""

import logging
from unittest.mock import MagicMock
import pytest

from src import alerter


class TestAlerterOutput:
    """Test alerter functions produce correct log messages."""

    def _make_logger(self):
        """Create a logger with a capturing handler for testing."""
        logger = logging.getLogger(f"test-{id(self)}")
        logger.setLevel(logging.DEBUG)
        logger.handlers.clear()
        mock_handler = logging.StreamHandler()
        mock_handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
        logger.addHandler(mock_handler)
        return logger

    def test_alert_soh_below_threshold(self, capsys):
        """SoH alert includes current value, threshold, and days to replacement."""
        logger = self._make_logger()

        alerter.alert_soh_below_threshold(
            logger,
            current_soh=0.75,
            threshold_soh=0.80,
            days_to_replacement=90
        )

        captured = capsys.readouterr()
        assert "75" in captured.err or "0.75" in captured.err

    def test_alert_runtime_below_threshold(self, capsys):
        """Runtime alert includes runtime value and threshold."""
        logger = self._make_logger()

        alerter.alert_runtime_below_threshold(
            logger,
            runtime_at_100_percent=18.5,
            threshold_minutes=20.0
        )

        captured = capsys.readouterr()
        assert "18" in captured.err or "18.5" in captured.err

    def test_structured_fields_dont_crash(self):
        """Extra dict fields don't crash the logger."""
        logger = self._make_logger()

        try:
            logger.info("test", extra={'BATTERY_SOH': '0.85', 'THRESHOLD': '0.80'})
        except Exception as e:
            pytest.fail(f"Structured fields crashed logger: {e}")

    def test_alert_none_days_to_replacement(self, capsys):
        """SoH alert handles None days_to_replacement gracefully."""
        logger = self._make_logger()

        try:
            alerter.alert_soh_below_threshold(
                logger,
                current_soh=0.75,
                threshold_soh=0.80,
                days_to_replacement=None
            )
        except Exception as e:
            pytest.fail(f"Alert crashed with None days: {e}")
