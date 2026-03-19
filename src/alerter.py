"""Journald alerting for battery health thresholds."""

import logging
import logging.handlers
from typing import Optional

logger = logging.getLogger('ups-battery-monitor')


def alert_soh_below_threshold(
    current_soh: float,
    threshold_soh: float,
    days_to_replacement: Optional[float] = None
):
    """
    Log SoH alert to journald.

    Args:
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

    logger.warning(msg, extra={
        'event_type': 'soh_alert',
        'BATTERY_SOH': f'{current_soh:.4f}',
        'THRESHOLD': f'{threshold_soh:.4f}',
        'DAYS_TO_REPLACEMENT': f'{days_to_replacement:.0f}' if days_to_replacement else 'unknown',
    })


def alert_runtime_below_threshold(
    runtime_at_100_percent: float,
    threshold_minutes: float
):
    """
    Log runtime alert to journald.

    Args:
        runtime_at_100_percent: Predicted runtime at full charge (minutes)
        threshold_minutes: Alert threshold (minutes)

    Triggers when calculated Time_rem@100% falls below threshold.
    """
    msg = f"Battery runtime at 100%% charge: {runtime_at_100_percent:.0f} min (threshold: {threshold_minutes:.0f} min)"

    logger.warning(msg, extra={
        'event_type': 'runtime_alert',
        'RUNTIME_AT_100_PCT': f'{runtime_at_100_percent:.1f}',
        'THRESHOLD_MINUTES': f'{threshold_minutes:.1f}',
    })
