"""Linear regression predictor for battery replacement date."""

import statistics
from datetime import datetime, timedelta
from typing import Any, List, Dict, Optional, Tuple


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
        - SoH already below threshold (returns date as 'overdue')

    Edge cases handled:
        - Non-monotonic dates: sorting applied implicitly via index order
        - Identical dates: denominator becomes 0; returns None
        - All SoH identical: no variance; returns None
        - Slope = 0 or positive: battery not degrading; returns None
        - Filtering by capacity_ah_ref with < 3 matching entries: returns None
    """
    # Phase 13: Filter by capacity baseline
    if capacity_ah_ref is not None:
        # Keep only entries matching the baseline
        # Default missing field to 7.2Ah (original rated capacity)
        filtered = [
            e for e in soh_history
            if e.get('capacity_ah_ref', 7.2) == capacity_ah_ref
        ]

        if len(filtered) < 3:
            # Not enough entries for this baseline; can't predict
            return None

        soh_history = filtered

    if len(soh_history) < 3:
        return None

    try:
        dates = [datetime.strptime(entry['date'], '%Y-%m-%d') for entry in soh_history]
        soh_values = [entry['soh'] for entry in soh_history]
    except (ValueError, KeyError, TypeError):
        return None

    first_date = dates[0]
    days_since_first = [(d - first_date).days for d in dates]

    # Least-squares regression
    n = len(days_since_first)
    x_mean = statistics.mean(days_since_first)
    y_mean = statistics.mean(soh_values)

    # slope = Σ((x - x̄)(y - ȳ)) / Σ((x - x̄)²)
    numerator = sum((days_since_first[i] - x_mean) * (soh_values[i] - y_mean) for i in range(n))
    denominator = sum((days_since_first[i] - x_mean) ** 2 for i in range(n))

    if denominator == 0:
        # No variance in x-axis; can't fit
        return None

    slope = numerator / denominator
    intercept = y_mean - slope * x_mean

    # R² = 1 - (SS_res / SS_tot)
    ss_res = sum((soh_values[i] - (slope * days_since_first[i] + intercept)) ** 2 for i in range(n))
    ss_tot = sum((y - y_mean) ** 2 for y in soh_values)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0

    # Validation
    if r_squared < 0.5:
        # High scatter; unreliable prediction
        return None

    if slope >= 0:
        # Battery not degrading or improving; no replacement prediction
        return None

    # Check if current SoH already below threshold
    current_soh = soh_values[-1]  # Last entry
    if current_soh < threshold_soh:
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
