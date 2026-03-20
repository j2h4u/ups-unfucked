"""Tests for battery_math.integrate_current() standalone function."""

import pytest
from src.battery_math import integrate_current


class TestIntegrateCurrent:
    """Tests for the standalone integrate_current() function (ARCH-01)."""

    def test_integrate_current_constant_load(self):
        """30 samples at 30% load over 580s → ~1.71 Ah (within 1%)."""
        # 30 points: t=0,20,40,...,580 (29 intervals × 20s = 580s total)
        load_percent = [30.0] * 30
        time_sec = [float(i * 20) for i in range(30)]
        nominal_power_watts = 425.0
        nominal_voltage = 12.0

        result = integrate_current(load_percent, time_sec, nominal_power_watts, nominal_voltage)

        # I = (30/100) * 425 / 12 = 1.0625 A
        # Ah = 1.0625 * 580 / 3600 = 0.1714 Ah  (29 intervals × 20s = 580s)
        # Wait: trapezoidal on constant load = same as I * total_time
        # total_time = 580s (from t[0]=0 to t[28]=560... let me recalculate)
        # t goes from 0 to 29*20=580, so 30 points, last is t[29]=580
        # Wait: range(30) → 0..29, so t[29] = 29*20 = 580
        # 29 intervals × 20s = 580s
        # I = 1.0625 A
        # Ah = 1.0625 * 580 / 3600 ≈ 0.1714... that's wrong
        # Let me recalculate: time_sec = [0,20,...,580], that's i*20 for i in range(30)
        # i goes 0..29, so last is 29*20=580
        # But plan says ~1.71 Ah, let me check: maybe the plan uses 30s intervals?
        # Plan: [float(i*20) for i in range(30)] with 425W, 12V, 30% load -> ~1.71 Ah
        # I = (30/100) * 425 / 12 = 10.625 A (not 1.0625!)
        # Ah = 10.625 * 580 / 3600 = 1.713 Ah ✓ (within 1% of 1.71)
        expected_ah = 1.713
        assert abs(result - expected_ah) / expected_ah < 0.01, (
            f"integrate_current returned {result:.4f} Ah, expected ~{expected_ah:.3f} Ah "
            f"(within 1%)"
        )

    def test_integrate_current_empty_input(self):
        """Empty lists → 0.0."""
        result = integrate_current([], [], 425.0, 12.0)
        assert result == 0.0

    def test_integrate_current_single_point(self):
        """Single element → 0.0 (need at least 2 samples for trapezoidal)."""
        result = integrate_current([30.0], [0.0], 425.0, 12.0)
        assert result == 0.0

    def test_trapezoidal_more_accurate_than_scalar(self):
        """Per-step trapezoidal integration is more accurate than scalar-average for variable loads.

        Variable load [10, 50, 10, 50, 10] over equal time steps (0,100,200,300,400 seconds).
        Analytical integral = sum of each trapezoid by hand.
        Scalar average = mean_load * total_time * P / V / 3600.

        Assert: |trapezoidal - analytical| < |scalar - analytical|
        """
        load_percent = [10.0, 50.0, 10.0, 50.0, 10.0]
        time_sec = [0.0, 100.0, 200.0, 300.0, 400.0]
        nominal_power_watts = 425.0
        nominal_voltage = 12.0

        # Analytical: sum of trapezoids for current (A), then convert to Ah
        # I(t) = (load_pct/100) * P / V
        # For each interval i → i+1:
        #   I_start = (load[i]/100) * 425 / 12
        #   I_end   = (load[i+1]/100) * 425 / 12
        #   dt = 100s
        #   contribution = (I_start + I_end)/2 * dt / 3600
        # Interval 0: I=(10+50)/2 /100 * 425/12 = 30/100 * 35.417 = 10.625A avg → 10.625*100/3600
        # Interval 1: I=(50+10)/2 same = 10.625A avg
        # Interval 2: I=(10+50)/2 same = 10.625A avg
        # Interval 3: I=(50+10)/2 same = 10.625A avg
        # All intervals have the same avg here, so analytical = scalar for this particular case?
        # That's because [10,50,10,50,10] is symmetric. Let me use asymmetric intervals.
        # Actually for [10,50,10,50,10] the trapezoidal IS the analytical.
        # The plan says to compare trapezoidal vs scalar avg approach.
        # Scalar: mean([10,50,10,50,10]) = 130/5 = 26.0%, total_time=400s
        # Scalar Ah = (26/100)*425/12 * 400/3600 = 9.208A * 0.1111h = 1.023 Ah
        # Trapezoidal (analytical for piecewise linear):
        #   Each interval avg = (load[i]+load[i+1])/2
        #   [0]: (10+50)/2=30%, [1]: (50+10)/2=30%, [2]: (10+50)/2=30%, [3]: (50+10)/2=30%
        #   Each contributes: (30/100)*425/12 * 100/3600 = 10.625 * 0.02778 = 0.2951 Ah
        #   Total = 4 * 0.2951 = 1.1806 Ah
        # Analytical (true integral of piecewise linear) = trapezoidal exactly = 1.1806 Ah
        # Scalar result = 1.023 Ah ≠ 1.1806 Ah
        # So |trapezoidal - analytical| = 0, |scalar - analytical| = 0.1576
        # Trapezoidal is exactly the analytical for piecewise-linear functions.

        trapezoidal = integrate_current(load_percent, time_sec, nominal_power_watts, nominal_voltage)

        # Analytical = trapezoidal for piecewise-linear current (exact)
        # Compute by hand for verification
        analytical = 0.0
        for i in range(len(load_percent) - 1):
            i_start = (load_percent[i] / 100.0) * nominal_power_watts / nominal_voltage
            i_end = (load_percent[i + 1] / 100.0) * nominal_power_watts / nominal_voltage
            i_avg = (i_start + i_end) / 2.0
            dt = time_sec[i + 1] - time_sec[i]
            analytical += i_avg * dt / 3600.0

        # Scalar average approach
        mean_load = sum(load_percent) / len(load_percent)
        total_time = time_sec[-1] - time_sec[0]
        scalar_current = (mean_load / 100.0) * nominal_power_watts / nominal_voltage
        scalar = scalar_current * total_time / 3600.0

        trapezoidal_error = abs(trapezoidal - analytical)
        scalar_error = abs(scalar - analytical)

        # Trapezoidal must exactly match analytical (it IS the analytical for piecewise-linear)
        assert trapezoidal_error < 1e-10, (
            f"Trapezoidal {trapezoidal:.6f} should exactly equal analytical {analytical:.6f}"
        )

        # Scalar should differ from analytical (unequal intervals × unequal loads)
        assert scalar_error > trapezoidal_error, (
            f"Expected scalar error {scalar_error:.6f} > trapezoidal error {trapezoidal_error:.6f}; "
            f"trapezoidal should be more accurate than scalar average"
        )
