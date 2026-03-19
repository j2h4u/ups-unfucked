"""Linear regression predictor for battery replacement date."""

import logging
from datetime import datetime, timedelta
from typing import Any, List, Dict, Optional, Tuple

from src.battery_math.regression import linear_regression

logger = logging.getLogger('ups-battery-monitor')


def linear_regression_soh(
    soh_history: List[Dict[str, Any]],
    threshold_soh: float = 0.80,
    capacity_ah_ref: Optional[float] = None
) -> Optional[Tuple[float, float, float, Optional[str]]]:
    """
    Fit line to SoH history and predict replacement date.

    Performs least-squares regression: y = mx + b
    where x = days since first measurement, y = SoH (0.0-1.0)

    Args:
        soh_history: List of {'date': 'YYYY-MM-DD', 'soh': float, 'capacity_ah_ref'?: float} dicts
        threshold_soh: SoH level requiring replacement (default 80%)
        capacity_ah_ref: If provided, use only entries with this baseline (Ah).
                        If None, use all entries (backward compatible).

    Returns:
        Tuple: (slope, intercept, r_squared, replacement_date_iso8601)
        where replacement_date is ISO8601 string or None if:
        - Insufficient data (< 3 points)
        - R² < 0.5 (unreliable fit)
        - No degradation (slope >= 0)
        - SoH already below threshold: returns today's date ("overdue").
          This path is only reachable after passing the r_squared >= 0.5
          and slope < 0 gates, so the returned fit is always valid.

    Edge cases handled:
        - Non-monotonic dates: sorting applied implicitly via index order
        - Identical dates: denominator becomes 0; returns None
        - All SoH identical: no variance; returns None
        - Slope = 0 or positive: battery not degrading; returns None
        - Filtering by capacity_ah_ref with < 3 matching entries: returns None

    Known limitations (audit 2026-03-17):
    - F55: No outlier rejection for SoH regression. Root cause was broken SoH
      formula (F19, now fixed with capacity-based SoH). With stable SoH values,
      outlier rejection is lower priority. Revisit if regression produces erratic
      predictions after extended operation.
    - F56: R²<0.5 threshold is permissive for early data. Battery degradation is
      roughly linear over months — R² will improve with more data points. Tighter
      threshold risks rejecting valid predictions during early battery lifetime.
    - F57: Multiple discharges per day give ~10% extra weight to that day in
      regression. Negligible for months-ahead extrapolation. Could deduplicate
      to daily average if needed, but added complexity not justified.
    """
    # Filter by capacity baseline
    if capacity_ah_ref is not None:
        # Keep only entries matching the baseline
        # Default missing field to 7.2Ah (original rated capacity)
        baseline_matched = [
            e for e in soh_history
            if e.get('capacity_ah_ref', 7.2) == capacity_ah_ref
        ]

        if len(baseline_matched) < 3:
            return None

        soh_history = baseline_matched

    if len(soh_history) < 3:
        return None

    try:
        dates = [datetime.strptime(entry['date'], '%Y-%m-%d') for entry in soh_history]
        soh_values = [entry['soh'] for entry in soh_history]
    except (ValueError, KeyError, TypeError) as e:
        logger.warning("SoH history parse failed (corrupt data?): %s", e,
                       extra={'event_type': 'soh_history_parse_error'})
        return None

    first_date = dates[0]
    days_since_first = [float((d - first_date).days) for d in dates]

    fit = linear_regression(days_since_first, soh_values)
    if fit is None:
        return None

    slope, intercept, r_squared = fit

    if r_squared < 0.5:
        # High scatter; unreliable prediction
        return None

    if slope >= 0:
        # Battery not degrading or improving; no replacement prediction
        return None

    # Check if most recent recorded SoH already below threshold
    latest_recorded_soh = soh_values[-1]
    if latest_recorded_soh < threshold_soh:
        # Return today as overdue
        return (slope, intercept, r_squared, datetime.now().strftime('%Y-%m-%d'))

    # Predict replacement date: when SoH hits threshold
    # threshold_soh = slope * days_to_threshold + intercept
    # days_to_threshold = (threshold_soh - intercept) / slope
    days_to_threshold = (threshold_soh - intercept) / slope

    if days_to_threshold <= 0:
        # Already past threshold or threshold in past
        return (slope, intercept, r_squared, datetime.now().strftime('%Y-%m-%d'))

    replacement_date = (first_date + timedelta(days=days_to_threshold)).strftime('%Y-%m-%d')

    return (slope, intercept, r_squared, replacement_date)
