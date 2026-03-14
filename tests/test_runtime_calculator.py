"""Unit tests for runtime calculator module — physics-based Peukert formula."""

import pytest
from src.runtime_calculator import runtime_minutes


class TestPeukertFormula:
    """Tests for pure-physics Peukert formula."""

    def test_peukert_blackout_match_n115(self):
        """Validate: 17% load, n=1.15, 7.2Ah, 425W/12V → ~47 min (2026-03-12 blackout)."""
        result = runtime_minutes(
            soc=1.0, load_percent=17.0, capacity_ah=7.2, soh=1.0,
            peukert_exponent=1.15, nominal_voltage=12.0, nominal_power_watts=425.0
        )
        assert 45.0 < result < 49.0, f"Expected ~47 min, got {result:.1f} min"

    def test_peukert_20pct_load(self):
        """20% load with default params gives reasonable runtime."""
        result = runtime_minutes(soc=1.0, load_percent=20.0)
        assert result > 20, f"Expected >20 min at 20% load, got {result:.1f}"
        assert result < 100, f"Expected <100 min at 20% load, got {result:.1f}"

    def test_peukert_zero_soc(self):
        """SoC=0% returns 0 minutes."""
        result = runtime_minutes(soc=0.0, load_percent=20.0)
        assert result == 0.0

    def test_peukert_zero_load(self):
        """Load=0% returns 0 minutes (safe handling)."""
        result = runtime_minutes(soc=1.0, load_percent=0.0)
        assert result == 0.0


class TestPeukertDegradation:
    """Tests for SoH scaling."""

    def test_soh_80_scales_runtime(self):
        """SoH=0.8 scales runtime by 0.8."""
        full = runtime_minutes(soc=1.0, load_percent=20.0, soh=1.0)
        degraded = runtime_minutes(soc=1.0, load_percent=20.0, soh=0.8)
        ratio = degraded / full
        assert 0.78 < ratio < 0.82

    def test_soh_50_scales_runtime(self):
        """SoH=0.5 scales runtime by 0.5."""
        full = runtime_minutes(soc=1.0, load_percent=20.0, soh=1.0)
        half = runtime_minutes(soc=1.0, load_percent=20.0, soh=0.5)
        ratio = half / full
        assert 0.48 < ratio < 0.52


class TestPeukertLoadNonlinearity:
    """Tests for Peukert exponent effect on load variation."""

    def test_load_nonlinearity(self):
        """Load 20% vs 80% shows Peukert nonlinearity."""
        time_20 = runtime_minutes(soc=1.0, load_percent=20.0)
        time_80 = runtime_minutes(soc=1.0, load_percent=80.0)
        # With exponent 1.2: ratio should be (80/20)^1.2 ≈ 5.28
        ratio = time_20 / time_80
        assert 5.0 < ratio < 5.6

    def test_higher_load_less_runtime(self):
        """Higher load always gives less runtime."""
        time_low = runtime_minutes(soc=1.0, load_percent=20.0)
        time_high = runtime_minutes(soc=1.0, load_percent=80.0)
        assert time_high < time_low
        assert time_high < (time_low / 5)


class TestRuntimeEdgeCases:
    """Edge cases and defensive behavior."""

    def test_partial_soc(self):
        """50% SoC gives proportionally less runtime."""
        full = runtime_minutes(soc=1.0, load_percent=20.0)
        half = runtime_minutes(soc=0.5, load_percent=20.0)
        assert half > 0
        assert half < full

    def test_no_negative_values(self):
        """Runtime never returns negative."""
        for soc in [0.0, 0.5, 1.0]:
            for load in [0.0, 20.0, 50.0, 100.0]:
                result = runtime_minutes(soc=soc, load_percent=load)
                assert result >= 0

    def test_returns_float(self):
        """Runtime returns float type."""
        result = runtime_minutes(soc=1.0, load_percent=20.0)
        assert isinstance(result, (int, float))

    def test_no_const_parameter(self):
        """Verify old 'const' parameter is removed — only physics params accepted."""
        import inspect
        sig = inspect.signature(runtime_minutes)
        assert 'const' not in sig.parameters
        assert 'peukert_exponent' in sig.parameters
        assert 'nominal_voltage' in sig.parameters
        assert 'nominal_power_watts' in sig.parameters
