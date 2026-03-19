"""Pure OLS linear regression kernel.

No I/O, no logging. Used by discharge_handler (IR trend) and
replacement_predictor (SoH trend).
"""

from __future__ import annotations

import statistics
from typing import NamedTuple, Optional


class LinearFit(NamedTuple):
    """Result of OLS linear regression."""
    slope: float
    intercept: float
    r_squared: float


def linear_regression(x_values: list[float], y_values: list[float]) -> Optional[LinearFit]:
    """Ordinary least-squares fit: y = slope * x + intercept.

    Returns None if fewer than 2 points or zero variance in x.
    """
    n = len(x_values)
    if n < 2 or len(y_values) < 2:
        return None

    x_mean = statistics.mean(x_values)
    y_mean = statistics.mean(y_values)

    numerator = sum((x_values[i] - x_mean) * (y_values[i] - y_mean) for i in range(n))
    denominator = sum((x_values[i] - x_mean) ** 2 for i in range(n))

    if abs(denominator) < 1e-10:
        return None

    slope = numerator / denominator
    intercept = y_mean - slope * x_mean

    ss_res = sum((y_values[i] - (slope * x_values[i] + intercept)) ** 2 for i in range(n))
    ss_tot = sum((y - y_mean) ** 2 for y in y_values)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    return LinearFit(slope=slope, intercept=intercept, r_squared=r_squared)


def linear_regression_slope(x_values: list[float], y_values: list[float]) -> Optional[float]:
    """Convenience wrapper: returns just the slope, or None if regression fails."""
    fit = linear_regression(x_values, y_values)
    return fit.slope if fit is not None else None
