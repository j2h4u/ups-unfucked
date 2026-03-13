"""
Tests for IR compensation and battery model functions.
"""

import pytest
from src.ema_ring_buffer import ir_compensate


class TestIRCompensation:
    """Test IR compensation formula: V_norm = V_ema + k*(L_ema - L_base)."""

    def test_ir_compensation_basic(self):
        """Basic IR compensation at different load levels."""
        # At base load (20%), V_norm should equal V_ema
        v_norm = ir_compensate(v_ema=12.0, l_ema=20.0, l_base=20.0, k=0.015)
        assert abs(v_norm - 12.0) < 1e-9

    def test_ir_compensation_higher_load(self):
        """Higher load → higher normalized voltage (accounting for IR drop)."""
        # At load 50% vs base 20%: delta_load = 30%
        # V_norm = 12.0 + 0.015 * (50 - 20) = 12.0 + 0.45 = 12.45
        v_norm = ir_compensate(v_ema=12.0, l_ema=50.0, l_base=20.0, k=0.015)
        assert abs(v_norm - 12.45) < 1e-9

    def test_ir_compensation_lower_load(self):
        """Lower load than base → lower normalized voltage."""
        # At load 10% vs base 20%: delta_load = -10%
        # V_norm = 12.0 + 0.015 * (10 - 20) = 12.0 - 0.15 = 11.85
        v_norm = ir_compensate(v_ema=12.0, l_ema=10.0, l_base=20.0, k=0.015)
        assert abs(v_norm - 11.85) < 1e-9

    def test_ir_compensation_different_k(self):
        """Different k values scale compensation appropriately."""
        # With k=0.01 (lower sensitivity)
        v_norm_01 = ir_compensate(v_ema=12.0, l_ema=50.0, l_base=20.0, k=0.01)
        assert abs(v_norm_01 - 12.3) < 1e-9

        # With k=0.02 (higher sensitivity)
        v_norm_02 = ir_compensate(v_ema=12.0, l_ema=50.0, l_base=20.0, k=0.02)
        assert abs(v_norm_02 - 12.6) < 1e-9

    def test_ir_compensation_none_inputs(self):
        """None inputs return None (pre-stabilization safety)."""
        assert ir_compensate(v_ema=None, l_ema=50.0) is None
        assert ir_compensate(v_ema=12.0, l_ema=None) is None
        assert ir_compensate(v_ema=None, l_ema=None) is None

    def test_ir_compensation_formula_verification(self):
        """Verify exact formula from plan: V_norm = V_ema + k*(L_ema - L_base)."""
        # Test case from plan: 12.0 + 0.01*(50-20) = 12.3
        v_norm = ir_compensate(v_ema=12.0, l_ema=50.0, l_base=20.0, k=0.01)
        assert abs(v_norm - 12.3) < 1e-9


class TestIRCompensationEdgeCases:
    """Test edge cases for IR compensation."""

    def test_ir_compensation_zero_voltage(self):
        """Compensation on zero voltage."""
        v_norm = ir_compensate(v_ema=0.0, l_ema=50.0, l_base=20.0, k=0.015)
        assert abs(v_norm - 0.45) < 1e-9

    def test_ir_compensation_negative_compensation(self):
        """Negative voltage (should handle, though unrealistic for battery)."""
        v_norm = ir_compensate(v_ema=-5.0, l_ema=50.0, l_base=20.0, k=0.015)
        assert abs(v_norm - (-5.0 + 0.45)) < 1e-9

    def test_ir_compensation_extreme_load(self):
        """Compensation with extreme load values."""
        # Load 100% vs base 20%
        v_norm = ir_compensate(v_ema=12.0, l_ema=100.0, l_base=20.0, k=0.015)
        assert abs(v_norm - (12.0 + 0.015 * 80)) < 1e-9  # 12.0 + 1.2 = 13.2

    def test_ir_compensation_load_zero(self):
        """Compensation at zero load."""
        v_norm = ir_compensate(v_ema=12.0, l_ema=0.0, l_base=20.0, k=0.015)
        assert abs(v_norm - (12.0 - 0.015 * 20)) < 1e-9  # 12.0 - 0.3 = 11.7

    def test_ir_compensation_default_parameters(self):
        """Test with default parameters (k=0.015, l_base=20.0)."""
        v_norm = ir_compensate(v_ema=12.5, l_ema=30.0)
        # V_norm = 12.5 + 0.015 * (30 - 20) = 12.5 + 0.15 = 12.65
        assert abs(v_norm - 12.65) < 1e-9

    def test_ir_compensation_preserves_precision(self):
        """Compensation maintains floating-point precision."""
        v_norm = ir_compensate(v_ema=12.123456, l_ema=50.987654, l_base=20.0, k=0.015)
        # V_norm = 12.123456 + 0.015 * (50.987654 - 20) = 12.123456 + 0.464814806
        expected = 12.123456 + 0.015 * (50.987654 - 20.0)
        assert abs(v_norm - expected) < 1e-9
