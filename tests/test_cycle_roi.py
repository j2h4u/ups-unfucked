"""Unit tests for cycle_roi.py — compute_cycle_roi() pure function."""

import pytest
from src.battery_math.cycle_roi import compute_cycle_roi


class TestComputeCycleROI:
    """Test compute_cycle_roi() function covering benefit/cost scenarios."""

    def test_cycle_roi_high_benefit_low_cost(self):
        """High sulfation with many cycles left → positive ROI."""
        roi = compute_cycle_roi(
            depth_of_discharge=0.7,
            cycle_budget_remaining=80,
            ir_trend_rate=0.1,
            sulfation_score=0.8,
        )

        assert roi > 0.2, f"Expected roi > 0.2 for high benefit, got {roi:.3f}"
        assert -1.0 <= roi <= 1.0, f"ROI must be in [-1.0, 1.0], got {roi:.3f}"

    def test_cycle_roi_negative_roi_few_cycles(self):
        """Few cycles left with low sulfation → negative ROI, don't test."""
        roi = compute_cycle_roi(
            depth_of_discharge=0.8,
            cycle_budget_remaining=3,
            ir_trend_rate=0.01,
            sulfation_score=0.1,
        )

        assert roi < -0.5, f"Expected roi < -0.5 for few cycles, got {roi:.3f}"
        assert -1.0 <= roi <= 1.0, f"ROI must be in [-1.0, 1.0], got {roi:.3f}"

    def test_cycle_roi_break_even(self):
        """Balanced inputs → near break-even ROI."""
        roi = compute_cycle_roi(
            depth_of_discharge=0.5,
            cycle_budget_remaining=50,
            ir_trend_rate=0.05,
            sulfation_score=0.5,
        )

        assert -0.1 < roi < 0.1, f"Expected roi near 0.0 (break-even), got {roi:.3f}"
        assert -1.0 <= roi <= 1.0, f"ROI must be in [-1.0, 1.0], got {roi:.3f}"

    def test_cycle_roi_edge_no_signals(self):
        """Zero benefit + zero cost → break-even."""
        roi = compute_cycle_roi(
            depth_of_discharge=0.0,
            cycle_budget_remaining=100,
            ir_trend_rate=0.0,
            sulfation_score=0.0,
        )

        assert roi == 0.0, f"Expected roi == 0.0 for no signals, got {roi:.3f}"

    def test_cycle_roi_clamped_to_range(self):
        """Extreme values clamped to [-1.0, 1.0]."""
        roi = compute_cycle_roi(
            depth_of_discharge=1.0,
            cycle_budget_remaining=1000,
            ir_trend_rate=10.0,
            sulfation_score=1.0,
        )

        assert -1.0 <= roi <= 1.0, f"ROI must be clamped to [-1.0, 1.0], got {roi:.3f}"

    def test_cycle_roi_formula_sanity(self):
        """Doubling sulfation_score increases ROI (benefit increases)."""
        # Low sulfation scenario
        roi1 = compute_cycle_roi(
            depth_of_discharge=0.5,
            cycle_budget_remaining=50,
            ir_trend_rate=0.05,
            sulfation_score=0.3,
        )

        # High sulfation scenario (doubled)
        roi2 = compute_cycle_roi(
            depth_of_discharge=0.5,
            cycle_budget_remaining=50,
            ir_trend_rate=0.05,
            sulfation_score=0.6,
        )

        assert roi2 > roi1, (
            f"Expected roi2={roi2:.3f} > roi1={roi1:.3f} for doubled sulfation"
        )
