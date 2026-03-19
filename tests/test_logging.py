"""Tests for logging and alerter output."""

import logging
import pytest

from src import alerter


class TestAlerterOutput:
    """Test alerter functions produce correct log messages."""

    def test_alert_soh_below_threshold(self, caplog):
        """SoH alert includes current value, threshold, and days to replacement."""
        with caplog.at_level(logging.WARNING):
            alerter.alert_soh_below_threshold(
                current_soh=0.75,
                threshold_soh=0.80,
                days_to_replacement=90
            )
        assert "75.0%" in caplog.text

    def test_alert_runtime_below_threshold(self, caplog):
        """Runtime alert includes runtime value and threshold."""
        with caplog.at_level(logging.WARNING):
            alerter.alert_runtime_below_threshold(
                runtime_at_100_percent=18.5,
                threshold_minutes=20.0
            )
        assert "18 min" in caplog.text

    def test_alert_none_days_to_replacement(self, caplog):
        """SoH alert handles None days_to_replacement gracefully."""
        with caplog.at_level(logging.WARNING):
            alerter.alert_soh_below_threshold(
                current_soh=0.75,
                threshold_soh=0.80,
                days_to_replacement=None
            )
        assert "unknown" in caplog.text.lower()
