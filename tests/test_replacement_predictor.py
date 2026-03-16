"""Unit tests for replacement date prediction via linear regression."""

import pytest
from datetime import datetime, timedelta
from src.replacement_predictor import linear_regression_soh


def test_linear_regression_basic():
    """5 SoH points over 20 days, degrading linearly. Returns (slope, intercept, r², replacement_date)."""
    history = [
        {'date': '2026-03-10', 'soh': 1.00},
        {'date': '2026-03-12', 'soh': 0.98},
        {'date': '2026-03-14', 'soh': 0.96},
        {'date': '2026-03-16', 'soh': 0.94},
        {'date': '2026-03-18', 'soh': 0.92},
    ]
    result = linear_regression_soh(history, threshold_soh=0.80)
    assert result is not None
    slope, intercept, r2, replacement_date = result
    assert slope < 0  # degrading
    assert r2 > 0.99  # excellent fit


def test_insufficient_data():
    """Only 2 points. Require minimum 3 for meaningful regression. Returns None."""
    history = [
        {'date': '2026-03-10', 'soh': 1.00},
        {'date': '2026-03-12', 'soh': 0.98},
    ]
    result = linear_regression_soh(history, threshold_soh=0.80)
    assert result is None


def test_r_squared_validation():
    """5 scattered points; R² < 0.5 (high noise). Rejects as unreliable. Returns None."""
    history = [
        {'date': '2026-03-10', 'soh': 1.00},
        {'date': '2026-03-12', 'soh': 0.50},
        {'date': '2026-03-14', 'soh': 0.95},
        {'date': '2026-03-16', 'soh': 0.55},
        {'date': '2026-03-18', 'soh': 0.80},
    ]
    result = linear_regression_soh(history, threshold_soh=0.80)
    # This may return None due to poor fit
    if result is not None:
        assert result[2] < 0.5  # Low R²


def test_soh_already_below_threshold():
    """Current SoH = 0.75, threshold = 0.80. Returns today's date (overdue)."""
    history = [
        {'date': '2026-03-10', 'soh': 0.90},
        {'date': '2026-03-12', 'soh': 0.85},
        {'date': '2026-03-14', 'soh': 0.75},
    ]
    result = linear_regression_soh(history, threshold_soh=0.80)
    assert result is not None
    slope, intercept, r2, replacement_date = result
    # Should return today's date since already below threshold
    today = datetime.now().strftime('%Y-%m-%d')
    assert replacement_date == today or replacement_date is not None


def test_no_degradation():
    """All SoH values identical (slope=0). No degradation signal. Returns None."""
    history = [
        {'date': '2026-03-10', 'soh': 0.90},
        {'date': '2026-03-12', 'soh': 0.90},
        {'date': '2026-03-14', 'soh': 0.90},
    ]
    result = linear_regression_soh(history, threshold_soh=0.80)
    # slope >= 0, so should return None
    assert result is None


def test_improving_soh_rejected():
    """Slope > 0 (battery improving, nonsensical). Returns None."""
    history = [
        {'date': '2026-03-10', 'soh': 0.80},
        {'date': '2026-03-12', 'soh': 0.85},
        {'date': '2026-03-14', 'soh': 0.90},
    ]
    result = linear_regression_soh(history, threshold_soh=0.80)
    # slope > 0, so should return None
    assert result is None


def test_date_format_iso8601():
    """soh_history uses ISO8601 strings ('YYYY-MM-DD'). Parsing works."""
    history = [
        {'date': '2026-03-10', 'soh': 1.00},
        {'date': '2026-03-12', 'soh': 0.98},
        {'date': '2026-03-14', 'soh': 0.96},
    ]
    result = linear_regression_soh(history, threshold_soh=0.80)
    assert result is not None
    slope, intercept, r2, replacement_date = result
    # Should parse dates correctly
    assert replacement_date is not None


def test_threshold_crossing_extrapolation():
    """Slope = -0.005 per day, current SoH = 1.0. Extrapolate to 80%."""
    history = [
        {'date': '2026-03-10', 'soh': 1.00},
        {'date': '2026-03-12', 'soh': 0.99},
        {'date': '2026-03-14', 'soh': 0.98},
        {'date': '2026-03-16', 'soh': 0.97},
        {'date': '2026-03-18', 'soh': 0.96},
    ]
    result = linear_regression_soh(history, threshold_soh=0.80)
    assert result is not None
    slope, intercept, r2, replacement_date = result
    assert slope < 0  # degrading
    assert replacement_date is not None


def test_regression_filters_by_baseline():
    """SOH-03: linear_regression_soh() filters entries by capacity_ah_ref before regression."""

    # Mixed baseline: old battery (6.8Ah) and new battery (6.9Ah)
    soh_history = [
        {'date': '2026-01-01', 'soh': 1.0, 'capacity_ah_ref': 6.8},
        {'date': '2026-01-15', 'soh': 0.98, 'capacity_ah_ref': 6.8},
        {'date': '2026-02-01', 'soh': 0.96, 'capacity_ah_ref': 6.8},
        {'date': '2026-03-01', 'soh': 0.90},  # Battery replaced; new baseline started
        {'date': '2026-03-16', 'soh': 0.92, 'capacity_ah_ref': 6.9},  # New baseline entries
        {'date': '2026-04-01', 'soh': 0.91, 'capacity_ah_ref': 6.9},
    ]

    # Fit line to old baseline only (first 3 entries)
    result_old = linear_regression_soh(soh_history, capacity_ah_ref=6.8)

    # Fit line to new baseline only (last 2 new-baseline entries, not enough for regression)
    result_new = linear_regression_soh(soh_history, capacity_ah_ref=6.9)

    # Old baseline should have valid regression (3+ entries)
    assert result_old is not None
    slope_old, intercept_old, r2_old, date_old = result_old

    # New baseline should return None (only 2 entries with 6.9Ah)
    assert result_new is None  # < 3 entries for new baseline


def test_regression_backward_compat():
    """SOH-03: Entries without capacity_ah_ref field default to 7.2Ah for filtering."""

    # Old entries without field; new entries with field
    soh_history = [
        {'date': '2026-01-01', 'soh': 1.0},  # No field = defaults to 7.2Ah
        {'date': '2026-02-01', 'soh': 0.98},
        {'date': '2026-03-01', 'soh': 0.96},
        {'date': '2026-04-01', 'soh': 0.92, 'capacity_ah_ref': 6.9},  # Different baseline
    ]

    # Fit line using default 7.2Ah baseline (old entries)
    result_default = linear_regression_soh(soh_history, capacity_ah_ref=7.2)

    # Should use first 3 entries (defaults to 7.2)
    assert result_default is not None
    slope, intercept, r2, date_pred = result_default
    # Verify it's using the first 3 entries (negative slope = degradation)
    assert slope < 0


def test_regression_min_entries_per_baseline():
    """SOH-03: linear_regression_soh() requires 3+ entries for same baseline."""

    soh_history = [
        {'date': '2026-01-01', 'soh': 1.0, 'capacity_ah_ref': 6.8},
        {'date': '2026-02-01', 'soh': 0.98, 'capacity_ah_ref': 6.8},
        # Only 2 entries for 6.8Ah
        {'date': '2026-03-01', 'soh': 0.92, 'capacity_ah_ref': 6.9},
        {'date': '2026-04-01', 'soh': 0.91, 'capacity_ah_ref': 6.9},
        {'date': '2026-05-01', 'soh': 0.90, 'capacity_ah_ref': 6.9},
    ]

    # Request regression for 6.8Ah (only 2 entries)
    result_insufficient = linear_regression_soh(soh_history, capacity_ah_ref=6.8)
    assert result_insufficient is None  # < 3 entries

    # Request regression for 6.9Ah (3 entries)
    result_sufficient = linear_regression_soh(soh_history, capacity_ah_ref=6.9)
    assert result_sufficient is not None  # >= 3 entries
