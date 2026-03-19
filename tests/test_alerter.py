"""Unit tests for journald alerting on health thresholds."""

import pytest
import logging
from src import alerter


def test_alert_soh_below_threshold(caplog):
    """SoH = 0.78, threshold = 0.80, days_to_replacement = 45. Alert includes all values."""
    with caplog.at_level(logging.WARNING):
        alerter.alert_soh_below_threshold(0.78, 0.80, 45)
    assert len(caplog.records) > 0
    assert caplog.records[0].levelname == "ERROR"
    assert "78.0%" in caplog.text


def test_alert_runtime_below_threshold(caplog):
    """Time_rem@100% = 18 min, threshold = 20 min. Alert fires with both values."""
    with caplog.at_level(logging.WARNING):
        alerter.alert_runtime_below_threshold(18.0, 20.0)
    assert len(caplog.records) > 0
    assert caplog.records[0].levelname == "ERROR"
    assert "18 min" in caplog.text


def test_independent_thresholds(caplog):
    """SoH and runtime alerts are independent — firing one does not produce the other."""
    with caplog.at_level(logging.WARNING):
        alerter.alert_soh_below_threshold(0.75, 0.80, 45)
    assert len(caplog.records) == 1
    assert "SoH" in caplog.text
    assert "runtime" not in caplog.text.lower()

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        alerter.alert_runtime_below_threshold(18.0, 20.0)
    assert len(caplog.records) == 1
    assert "runtime" in caplog.text.lower()
    assert "SoH" not in caplog.text


def test_none_days_to_replacement(caplog):
    """days_to_replacement = None. Message says 'unknown' instead of crashing."""
    with caplog.at_level(logging.WARNING):
        alerter.alert_soh_below_threshold(0.75, 0.80, None)
    assert len(caplog.records) > 0
    assert "unknown" in caplog.text.lower()


def test_message_format_readability(caplog):
    """Alert message is human-readable with % symbols, minute values, etc."""
    with caplog.at_level(logging.WARNING):
        alerter.alert_runtime_below_threshold(25.5, 30.0)
    assert len(caplog.records) > 0
    assert "26 min" in caplog.text
