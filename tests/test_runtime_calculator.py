"""Unit tests for runtime calculator module (PRED-02)."""

import pytest
from src.runtime_calculator import runtime_minutes


class TestPeukertFormula:
    """Tests for Peukert formula with exponent 1.2."""

    def test_peukert_blackout_match(self):
        """Test Peukert at blackout scenario: SoC=1.0, load=20% returns ~47 minutes."""
        # From 2026-03-12 blackout: CyberPower UT850EG, capacity=7.2Ah, load=20%
        # Expected: ~47 minutes of runtime
        result = runtime_minutes(soc=1.0, load_percent=20, capacity_ah=7.2, soh=1.0)
        # Allow ±5% tolerance
        assert 44.5 < result < 49.5, f"Expected ~47 min, got {result:.1f} min"

    def test_peukert_zero_soc(self):
        """Test Peukert at SoC=0% returns 0 minutes."""
        result = runtime_minutes(soc=0.0, load_percent=20, capacity_ah=7.2, soh=1.0)
        assert result == 0, f"Expected 0 min at SoC=0, got {result}"

    def test_peukert_zero_load(self):
        """Test Peukert at load=0% returns 0 minutes (safe handling)."""
        result = runtime_minutes(soc=1.0, load_percent=0, capacity_ah=7.2, soh=1.0)
        assert result == 0, f"Expected 0 min at load=0, got {result}"


class TestPeukertDegradation:
    """Tests for SoH (state of health) scaling in Peukert formula."""

    def test_peukert_with_degradation_80_percent(self):
        """Test SoH=0.8 scales runtime by 0.8."""
        # Full health at same conditions
        full_health = runtime_minutes(soc=1.0, load_percent=20, capacity_ah=7.2, soh=1.0)
        degraded = runtime_minutes(soc=1.0, load_percent=20, capacity_ah=7.2, soh=0.8)

        # Degraded should be ~80% of full health
        ratio = degraded / full_health
        assert 0.78 < ratio < 0.82, f"Expected 0.8x scaling, got {ratio:.2f}x"

    def test_peukert_with_degradation_50_percent(self):
        """Test SoH=0.5 scales runtime by 0.5."""
        full_health = runtime_minutes(soc=1.0, load_percent=20, capacity_ah=7.2, soh=1.0)
        severely_degraded = runtime_minutes(soc=1.0, load_percent=20, capacity_ah=7.2, soh=0.5)

        ratio = severely_degraded / full_health
        assert 0.48 < ratio < 0.52, f"Expected 0.5x scaling, got {ratio:.2f}x"


class TestPeukertLoadNonlinearity:
    """Tests for Peukert exponent effect on load variation."""

    def test_peukert_load_nonlinearity(self):
        """Test load 20% vs 80% shows nonlinear difference (1.2 exponent)."""
        time_20 = runtime_minutes(soc=1.0, load_percent=20, capacity_ah=7.2, soh=1.0)
        time_80 = runtime_minutes(soc=1.0, load_percent=80, capacity_ah=7.2, soh=1.0)

        # With exponent 1.2: time_80/time_20 should be (20/80)^1.2 ≈ 0.115
        # Meaning time_20 should be ~8.7x longer than time_80
        ratio = time_20 / time_80
        expected_ratio = (20 / 80) ** 1.2
        assert 8.0 < ratio < 9.5, f"Expected ~8.7x difference, got {ratio:.1f}x"

    def test_peukert_load_increase_drops_time(self):
        """Test load increase (20% to 80%) reduces runtime sharply."""
        time_low = runtime_minutes(soc=1.0, load_percent=20, capacity_ah=7.2, soh=1.0)
        time_high = runtime_minutes(soc=1.0, load_percent=80, capacity_ah=7.2, soh=1.0)

        # Higher load should always give less runtime
        assert time_high < time_low, f"High load ({time_high}) should be < low load ({time_low})"
        # And the difference should be significant
        assert time_high < (time_low / 5), f"Difference too small: {time_low} vs {time_high}"


class TestRuntimeEdgeCases:
    """Tests for edge cases and defensive behavior."""

    def test_runtime_partial_soc(self):
        """Test runtime at 50% SoC."""
        result = runtime_minutes(soc=0.5, load_percent=20, capacity_ah=7.2, soh=1.0)
        assert result > 0, f"Expected positive runtime at SoC=0.5, got {result}"
        assert result < runtime_minutes(soc=1.0, load_percent=20, capacity_ah=7.2, soh=1.0), \
            "50% SoC should give less time than 100% SoC"

    def test_runtime_no_negative_values(self):
        """Test that runtime never returns negative values."""
        for soc in [0.0, 0.5, 1.0]:
            for load in [0.0, 20.0, 50.0, 100.0]:
                result = runtime_minutes(soc=soc, load_percent=load, capacity_ah=7.2, soh=1.0)
                assert result >= 0, f"Runtime negative at SoC={soc}, load={load}: {result}"

    def test_runtime_returns_float(self):
        """Test runtime returns float type."""
        result = runtime_minutes(soc=1.0, load_percent=20, capacity_ah=7.2, soh=1.0)
        assert isinstance(result, (int, float)), f"Expected numeric type, got {type(result)}"
