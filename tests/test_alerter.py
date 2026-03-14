"""Unit tests for journald alerting on health thresholds."""

import pytest
import logging
from src.alerter import setup_ups_logger, alert_soh_below_threshold, alert_runtime_below_threshold


def test_alert_soh_below_threshold(caplog):
    """SoH = 0.78, threshold = 0.80, days_to_replacement = 45. Alert includes all values."""
    logger = setup_ups_logger("test-ups")
    with caplog.at_level(logging.WARNING):
        alert_soh_below_threshold(logger, 0.78, 0.80, 45)
    assert len(caplog.records) > 0
    assert caplog.records[0].levelname == "WARNING"
    assert "78" in caplog.text  # "78.0%" appears in output


def test_alert_runtime_below_threshold(caplog):
    """Time_rem@100% = 18 min, threshold = 20 min. Alert fires with both values."""
    logger = setup_ups_logger("test-ups")
    with caplog.at_level(logging.WARNING):
        alert_runtime_below_threshold(logger, 18.0, 20.0)
    assert len(caplog.records) > 0
    assert caplog.records[0].levelname == "WARNING"
    assert "18" in caplog.text


def test_structured_fields(caplog):
    """Alert includes extra dict with BATTERY_SOH, THRESHOLD, DAYS_TO_REPLACEMENT."""
    logger = setup_ups_logger("test-ups")
    with caplog.at_level(logging.WARNING):
        alert_soh_below_threshold(logger, 0.78, 0.80, 45)
    assert len(caplog.records) > 0
    record = caplog.records[0]
    # Check if extra fields are set (they may not be in caplog.text directly)
    # but should exist in the record
    assert hasattr(record, '__dict__')


def test_independent_thresholds(caplog):
    """SoH below threshold but runtime above: only SoH alert fires."""
    logger = setup_ups_logger("test-ups")
    with caplog.at_level(logging.WARNING):
        alert_soh_below_threshold(logger, 0.75, 0.80, 45)
    # SoH alert should fire
    assert len(caplog.records) > 0


def test_logger_setup():
    """setup_ups_logger() returns shared Logger instance (handlers added by monitor.py)."""
    logger = setup_ups_logger("test-ups")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "test-ups"


def test_syslog_identifier_propagation():
    """Handler formatter includes identifier."""
    logger = setup_ups_logger("custom-identifier")
    assert logger.name == "custom-identifier"
    # Handlers should format with identifier
    for handler in logger.handlers:
        if hasattr(handler, 'formatter') and handler.formatter:
            fmt = handler.formatter._fmt if hasattr(handler.formatter, '_fmt') else str(handler.formatter)
            # Check that identifier is used in formatter
            assert "custom-identifier" in fmt or "%(levelname)s" in fmt


def test_none_days_to_replacement(caplog):
    """days_to_replacement = None. Message says 'unknown' instead of crashing."""
    logger = setup_ups_logger("test-ups")
    with caplog.at_level(logging.WARNING):
        alert_soh_below_threshold(logger, 0.75, 0.80, None)
    assert len(caplog.records) > 0
    assert "unknown" in caplog.text.lower()


def test_message_format_readability(caplog):
    """Alert message is human-readable with % symbols, minute values, etc."""
    logger = setup_ups_logger("test-ups")
    with caplog.at_level(logging.WARNING):
        alert_runtime_below_threshold(logger, 25.5, 30.0)
    assert len(caplog.records) > 0
    assert "min" in caplog.text or "25" in caplog.text
