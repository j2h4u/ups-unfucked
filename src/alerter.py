"""Journald alerting for battery health thresholds."""

import logging
import logging.handlers
from typing import Optional


def setup_ups_logger(identifier: str = "ups-battery-monitor") -> logging.Logger:
    """
    Configure logger for UPS battery monitor daemon.

    Sends output to systemd journal with SyslogIdentifier.
    Also sends to stderr for interactive debugging.

    Args:
        identifier: Syslog identifier (appears in journalctl output)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(identifier)
    logger.setLevel(logging.DEBUG)
    logger.handlers = []  # Clear any existing handlers

    # journald handler (systemd integration)
    try:
        syslog_handler = logging.handlers.SysLogHandler(address='/dev/log')
        syslog_handler.setFormatter(
            logging.Formatter(f'{identifier}: %(levelname)s - %(message)s')
        )
        logger.addHandler(syslog_handler)
    except (FileNotFoundError, OSError):
        # /dev/log not available (e.g., in tests); fallback to stderr
        pass

    # Stderr handler for visibility
    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(
        logging.Formatter(f'{identifier}: %(levelname)s - %(message)s')
    )
    logger.addHandler(stderr_handler)

    return logger


def alert_soh_below_threshold(
    logger: logging.Logger,
    current_soh: float,
    threshold_soh: float,
    days_to_replacement: Optional[float] = None
):
    """
    Log SoH alert to journald.

    Args:
        logger: Configured logger instance
        current_soh: Current state of health (0.0-1.0)
        threshold_soh: Alert threshold (0.0-1.0)
        days_to_replacement: Predicted days until SoH < threshold (or None)

    Message format: Human-readable with structured fields for automated parsing.
    """
    msg = f"Battery SoH {current_soh:.1%} below alert threshold {threshold_soh:.1%}"
    if days_to_replacement is not None and days_to_replacement > 0:
        msg += f"; estimated {days_to_replacement:.0f} days to replacement"
    else:
        msg += "; replacement date unknown"

    # Log as warning
    logger.warning(msg, extra={
        'BATTERY_SOH': f'{current_soh:.4f}',
        'THRESHOLD': f'{threshold_soh:.4f}',
        'DAYS_TO_REPLACEMENT': f'{days_to_replacement:.0f}' if days_to_replacement else 'unknown',
    })


def alert_runtime_below_threshold(
    logger: logging.Logger,
    runtime_at_100_percent: float,
    threshold_minutes: float
):
    """
    Log runtime alert to journald.

    Args:
        logger: Configured logger instance
        runtime_at_100_percent: Predicted runtime at full charge (minutes)
        threshold_minutes: Alert threshold (minutes)

    Triggers when calculated Time_rem@100% falls below threshold.
    """
    msg = f"Battery runtime at 100%% charge: {runtime_at_100_percent:.0f} min (threshold: {threshold_minutes:.0f} min)"

    logger.warning(msg, extra={
        'RUNTIME_AT_100_PCT': f'{runtime_at_100_percent:.1f}',
        'THRESHOLD_MINUTES': f'{threshold_minutes:.1f}',
    })
