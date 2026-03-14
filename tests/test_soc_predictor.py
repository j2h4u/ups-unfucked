"""Unit tests for SoC predictor module (PRED-01, PRED-03)."""

import pytest
from src.soc_predictor import soc_from_voltage, charge_percentage


class TestSoCExactLookup:
    """Tests for exact voltage lookup in LUT."""

    def test_soc_exact_point(self, mock_lut_standard):
        """Test lookup at exact voltage point returns correct SoC."""
        # Voltage 12.4V is in standard LUT with SoC=0.64
        result = soc_from_voltage(12.4, mock_lut_standard)
        assert result == 0.64, f"Expected 0.64 at 12.4V, got {result}"

    def test_soc_exact_max_voltage(self, mock_lut_standard):
        """Test lookup at maximum voltage returns SoC=1.0."""
        result = soc_from_voltage(13.4, mock_lut_standard)
        assert result == 1.0, f"Expected 1.0 at 13.4V, got {result}"

    def test_soc_exact_anchor(self, mock_lut_standard):
        """Test lookup at anchor voltage returns SoC=0.0."""
        result = soc_from_voltage(10.5, mock_lut_standard)
        assert result == 0.0, f"Expected 0.0 at 10.5V, got {result}"


class TestSoCInterpolation:
    """Tests for interpolation between LUT points."""

    def test_soc_interpolation_between_points(self, mock_lut_standard):
        """Test interpolation between 12.0V and 12.4V."""
        # 12.0V has SoC=0.4, 12.4V has SoC=0.64
        # At 12.2V (midpoint): interpolate to ~0.52
        result = soc_from_voltage(12.2, mock_lut_standard)
        # Linear interpolation: 0.4 + (12.2-12.0)/(12.4-12.0) * (0.64-0.4)
        # = 0.4 + 0.2/0.4 * 0.24 = 0.4 + 0.12 = 0.52
        assert 0.51 < result < 0.53, f"Expected ~0.52, got {result}"

    def test_soc_interpolation_knee_region(self, mock_lut_standard):
        """Test interpolation in knee region (12.0V-12.4V)."""
        result = soc_from_voltage(12.0, mock_lut_standard)
        assert result == 0.4, f"Expected 0.4 at 12.0V, got {result}"


class TestSoCFloatingPointTolerance:
    """Tests for floating-point tolerance in voltage lookup (TEST-05)."""

    def test_soc_from_voltage_with_ema_filtered_voltage(self):
        """TEST-05: Floating-point tolerance in soc_from_voltage.

        Verifies that EMA-filtered voltage (12.3999999) matches LUT entry (12.4)
        within tolerance (±0.01V), not requiring exact match.

        Addresses issue: Line 36 in soc_predictor.py used exact comparison
        entry["v"] == voltage. When voltage comes from EMA filtering, float
        precision causes silent mismatches.
        """
        lut = [
            {"v": 13.4, "soc": 1.0, "source": "standard"},
            {"v": 12.4, "soc": 0.64, "source": "standard"},
            {"v": 10.5, "soc": 0.0, "source": "anchor"},
        ]

        # Case 1: EMA voltage slightly below LUT entry (12.4 - 1e-6)
        ema_voltage_below = 12.4 - 1e-6  # 12.3999990
        result_below = soc_from_voltage(ema_voltage_below, lut)
        assert result_below == 0.64, f"Expected 0.64 for voltage {ema_voltage_below}, got {result_below}"

        # Case 2: EMA voltage slightly above LUT entry (12.4 + 1e-6)
        ema_voltage_above = 12.4 + 1e-6  # 12.4000010
        result_above = soc_from_voltage(ema_voltage_above, lut)
        assert result_above == 0.64, f"Expected 0.64 for voltage {ema_voltage_above}, got {result_above}"

        # Case 3: Voltage at tolerance boundary (±0.005V still matches)
        ema_voltage_near = 12.395  # Within 0.005V of 12.4
        result_near = soc_from_voltage(ema_voltage_near, lut)
        assert result_near == 0.64, f"Expected 0.64 for voltage {ema_voltage_near}, got {result_near}"

        # Case 4: Voltage outside tolerance (should use interpolation, not exact match)
        ema_voltage_far = 12.2  # 0.2V away from 12.4, should interpolate
        result_far = soc_from_voltage(ema_voltage_far, lut)
        # 12.2 is between 12.0 (0.4) and 12.4 (0.64), interpolate ~0.52
        assert 0.50 < result_far < 0.54, f"Expected interpolation ~0.52 for 12.2V, got {result_far}"


class TestSoCClamping:
    """Tests for boundary clamping (above max, below anchor)."""

    def test_soc_clamp_above_max_voltage(self, mock_lut_standard):
        """Test voltage above max voltage clamps to SoC=1.0."""
        result = soc_from_voltage(13.5, mock_lut_standard)
        assert result == 1.0, f"Expected 1.0 (clamped) at 13.5V, got {result}"

    def test_soc_clamp_above_max_high(self, mock_lut_standard):
        """Test high voltage significantly above max clamps to SoC=1.0."""
        result = soc_from_voltage(14.0, mock_lut_standard)
        assert result == 1.0, f"Expected 1.0 (clamped) at 14.0V, got {result}"

    def test_soc_clamp_below_anchor(self, mock_lut_standard):
        """Test voltage below anchor clamps to SoC=0.0."""
        result = soc_from_voltage(10.0, mock_lut_standard)
        assert result == 0.0, f"Expected 0.0 (clamped) at 10.0V, got {result}"

    def test_soc_clamp_below_anchor_low(self, mock_lut_standard):
        """Test very low voltage clamps to SoC=0.0."""
        result = soc_from_voltage(8.0, mock_lut_standard)
        assert result == 0.0, f"Expected 0.0 (clamped) at 8.0V, got {result}"


class TestChargePercentage:
    """Tests for SoC to charge percentage conversion."""

    def test_charge_percentage_full(self):
        """Test charge_percentage(1.0) returns 100."""
        result = charge_percentage(1.0)
        assert result == 100, f"Expected 100, got {result}"

    def test_charge_percentage_three_quarters(self):
        """Test charge_percentage(0.75) returns 75."""
        result = charge_percentage(0.75)
        assert result == 75, f"Expected 75, got {result}"

    def test_charge_percentage_half(self):
        """Test charge_percentage(0.5) returns 50."""
        result = charge_percentage(0.5)
        assert result == 50, f"Expected 50, got {result}"

    def test_charge_percentage_quarter(self):
        """Test charge_percentage(0.25) returns 25."""
        result = charge_percentage(0.25)
        assert result == 25, f"Expected 25, got {result}"

    def test_charge_percentage_empty(self):
        """Test charge_percentage(0.0) returns 0."""
        result = charge_percentage(0.0)
        assert result == 0, f"Expected 0, got {result}"

    def test_charge_percentage_returns_int(self):
        """Test charge_percentage returns int type."""
        result = charge_percentage(0.75)
        assert isinstance(result, int), f"Expected int, got {type(result)}"


class TestSoCEdgeCases:
    """Tests for edge cases and empty/minimal LUTs."""

    def test_soc_single_point_lut_clamping(self):
        """Test single-point LUT doesn't crash, clamps appropriately."""
        single_point_lut = [
            {"v": 12.0, "soc": 0.5, "source": "test"}
        ]
        # Below anchor should clamp to 0.0
        result_low = soc_from_voltage(10.0, single_point_lut)
        assert result_low == 0.0, f"Expected 0.0, got {result_low}"

        # Above point should clamp to 1.0
        result_high = soc_from_voltage(14.0, single_point_lut)
        assert result_high == 1.0, f"Expected 1.0, got {result_high}"

    def test_soc_measured_lut(self, mock_lut_measured):
        """Test with measured LUT (different points)."""
        result = soc_from_voltage(12.4, mock_lut_measured)
        assert result == 0.63, f"Expected 0.63, got {result}"
