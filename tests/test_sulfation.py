"""Unit tests for sulfation.py — compute_sulfation_score() and estimate_recovery_delta()."""

import pytest
from src.battery_math.sulfation import compute_sulfation_score, estimate_recovery_delta


class TestComputeSulfationScore:
    """Test compute_sulfation_score() function with realistic battery scenarios."""

    def test_sulfation_score_healthy_battery_low_idle(self):
        """Healthy battery with minimal idle time → low sulfation score."""
        days_since_deep = 5.0
        ir_trend_rate = 0.0
        recovery_delta = 0.08
        temp_celsius = 35.0

        score = compute_sulfation_score(
            days_since_deep=days_since_deep,
            ir_trend_rate=ir_trend_rate,
            recovery_delta=recovery_delta,
            temperature_celsius=temp_celsius,
        )

        assert score < 0.3, f"Expected score < 0.3 for healthy battery, got {score:.3f}"
        assert 0.0 <= score <= 1.0, f"Score must be in [0.0, 1.0], got {score:.3f}"

    def test_sulfation_score_old_battery_idle_high_temp(self):
        """Idle battery with poor recovery and high temp → high sulfation score."""
        days_since_deep = 60.0
        ir_trend_rate = 0.05
        recovery_delta = 0.02
        temp_celsius = 40.0

        score = compute_sulfation_score(
            days_since_deep=days_since_deep,
            ir_trend_rate=ir_trend_rate,
            recovery_delta=recovery_delta,
            temperature_celsius=temp_celsius,
        )

        assert score > 0.4, f"Expected score > 0.4 for old idle battery, got {score:.3f}"
        assert 0.0 <= score <= 1.0, f"Score must be in [0.0, 1.0], got {score:.3f}"

    def test_sulfation_score_high_ir_drift(self):
        """High internal resistance drift → significant sulfation signal."""
        days_since_deep = 10.0
        ir_trend_rate = 0.15
        recovery_delta = 0.05
        temp_celsius = 35.0

        score = compute_sulfation_score(
            days_since_deep=days_since_deep,
            ir_trend_rate=ir_trend_rate,
            recovery_delta=recovery_delta,
            temperature_celsius=temp_celsius,
        )

        assert score > 0.4, f"Expected score > 0.4 for high IR drift, got {score:.3f}"
        assert 0.0 <= score <= 1.0, f"Score must be in [0.0, 1.0], got {score:.3f}"

    def test_sulfation_score_clamped_to_range(self):
        """Extreme inputs should be clamped to [0.0, 1.0]."""
        days_since_deep = 1000.0
        ir_trend_rate = 10.0
        recovery_delta = 1.0
        temp_celsius = 50.0

        score = compute_sulfation_score(
            days_since_deep=days_since_deep,
            ir_trend_rate=ir_trend_rate,
            recovery_delta=recovery_delta,
            temperature_celsius=temp_celsius,
        )

        assert 0.0 <= score <= 1.0, f"Score must be clamped to [0.0, 1.0], got {score:.3f}"

    def test_sulfation_score_seasonal_variation(self):
        """Higher temperature increases sulfation rate."""
        days_since_deep = 30.0
        ir_trend_rate = 0.03
        recovery_delta = 0.04

        score_cool = compute_sulfation_score(
            days_since_deep=days_since_deep,
            ir_trend_rate=ir_trend_rate,
            recovery_delta=recovery_delta,
            temperature_celsius=25.0,
        )

        score_warm = compute_sulfation_score(
            days_since_deep=days_since_deep,
            ir_trend_rate=ir_trend_rate,
            recovery_delta=recovery_delta,
            temperature_celsius=40.0,
        )

        assert score_warm > score_cool, (
            f"Expected score(40°C)={score_warm:.3f} > score(25°C)={score_cool:.3f}"
        )


class TestEstimateRecoveryDelta:
    """Test estimate_recovery_delta() function.

    The recovery_delta metric captures SoH change post-discharge:
    - Positive change (improvement) → strong desulfation signal → delta near 1.0
    - Expected drop (1%) → neutral recovery → delta = 0.5
    - Larger drop → poor recovery → delta < 0.5
    - No change → unclear signal → delta = 0.0
    """

    def test_recovery_delta_excellent_improvement(self):
        """SoH improves post-discharge → excellent desulfation signal."""
        # SoH improves by 1% (0.95→0.96)
        # delta = min(1.0, 0.01 / 0.01) = 1.0
        soh_before = 0.95
        soh_after = 0.96

        delta = estimate_recovery_delta(
            soh_before_discharge=soh_before,
            soh_after_discharge=soh_after,
        )

        assert delta > 0.5, f"Expected delta > 0.5 for improvement, got {delta:.3f}"
        assert 0.0 <= delta <= 1.0, f"Delta must be in [0.0, 1.0], got {delta:.3f}"

    def test_recovery_delta_moderate_drop(self):
        """SoH drops 0.5% (half of expected 1%) → good recovery, less wear."""
        # SoH drops 0.5% (0.95→0.945)
        # soh_drop = 0.005, expected = 0.01
        # ratio = 0.005 / 0.01 = 0.5
        # recovery_score = max(0.0, 1.0 - (0.5 - 1.0)) = max(0.0, 1.0 - (-0.5)) = 1.5 → clamped to 1.0
        soh_before = 0.95
        soh_after = 0.945

        delta = estimate_recovery_delta(
            soh_before_discharge=soh_before,
            soh_after_discharge=soh_after,
        )

        # Less drop than expected = better recovery
        assert delta > 0.5, f"Expected delta > 0.5 for moderate drop, got {delta:.3f}"
        assert 0.0 <= delta <= 1.0, f"Delta must be in [0.0, 1.0], got {delta:.3f}"

    def test_recovery_delta_poor_large_drop(self):
        """SoH drops more than expected → poor recovery, high sulfation."""
        # SoH drops 2% (0.95→0.93) vs expected 1%
        # soh_drop = 0.02, expected = 0.01
        # soh_drop / expected - 1.0 = 2.0 - 1.0 = 1.0
        # recovery_score = max(0.0, 1.0 - 1.0) = 0.0
        soh_before = 0.95
        soh_after = 0.93

        delta = estimate_recovery_delta(
            soh_before_discharge=soh_before,
            soh_after_discharge=soh_after,
        )

        # Based on docstring: "SoH 0.95→0.93 (2% drop) → delta = 0.0"
        assert delta < 0.3, f"Expected delta < 0.3 for large drop, got {delta:.3f}"
        assert 0.0 <= delta <= 1.0, f"Delta must be in [0.0, 1.0], got {delta:.3f}"

    def test_recovery_delta_no_change(self):
        """No SoH change → unclear signal, returns 0.0."""
        soh_before = 0.95
        soh_after = 0.95  # No change

        delta = estimate_recovery_delta(
            soh_before_discharge=soh_before,
            soh_after_discharge=soh_after,
        )

        assert delta == 0.0, f"Expected delta == 0.0 for no change, got {delta:.3f}"
