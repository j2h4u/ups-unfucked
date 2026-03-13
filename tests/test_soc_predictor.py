"""Tests for SoC predictor module — voltage to SoC LUT lookup with linear interpolation."""

import pytest
from src.soc_predictor import soc_from_voltage, charge_percentage


@pytest.fixture
def mock_lut_standard():
    """
    Standard VRLA 12V lookup table for testing.

    Matches the default curve from BatteryModel._default_vrla_lut().
    Used to test interpolation and edge cases.
    """
    return [
        {'v': 13.4, 'soc': 1.00, 'source': 'standard'},
        {'v': 12.8, 'soc': 0.85, 'source': 'standard'},
        {'v': 12.4, 'soc': 0.64, 'source': 'standard'},
        {'v': 12.1, 'soc': 0.40, 'source': 'standard'},
        {'v': 11.6, 'soc': 0.18, 'source': 'standard'},
        {'v': 11.0, 'soc': 0.06, 'source': 'standard'},
        {'v': 10.5, 'soc': 0.00, 'source': 'anchor'},
    ]


@pytest.fixture
def mock_lut_measured():
    """
    Lookup table with some measured points mixed with standard.

    Used to test source tracking and measured data precedence.
    """
    return [
        {'v': 13.4, 'soc': 1.00, 'source': 'standard'},
        {'v': 12.8, 'soc': 0.85, 'source': 'measured'},
        {'v': 12.4, 'soc': 0.64, 'source': 'measured'},
        {'v': 12.1, 'soc': 0.40, 'source': 'standard'},
        {'v': 11.6, 'soc': 0.18, 'source': 'measured'},
        {'v': 11.0, 'soc': 0.06, 'source': 'standard'},
        {'v': 10.5, 'soc': 0.00, 'source': 'anchor'},
    ]


class TestSocFromVoltageExactPoints:
    """Test soc_from_voltage() with voltages that exactly match LUT points."""

    def test_soc_exact_point_high(self, mock_lut_standard):
        """Voltage exactly at max LUT point → returns exact SoC."""
        result = soc_from_voltage(13.4, mock_lut_standard)
        assert result == 1.00

    def test_soc_exact_point_middle(self, mock_lut_standard):
        """Voltage exactly at middle LUT point → returns exact SoC."""
        result = soc_from_voltage(12.4, mock_lut_standard)
        assert result == 0.64

    def test_soc_exact_point_low(self, mock_lut_standard):
        """Voltage exactly at anchor point → returns 0.0."""
        result = soc_from_voltage(10.5, mock_lut_standard)
        assert result == 0.0


class TestSocFromVoltageInterpolation:
    """Test soc_from_voltage() with interpolation between points."""

    def test_soc_interpolation_simple(self, mock_lut_standard):
        """
        Voltage between two LUT points → linear interpolation.

        Between 11.8V (estimated at ~0.52 SoC from 11.0V@0.06 to 12.1V@0.40)
        and 12.4V@0.64. For test, use a midpoint:
        12.0V is between 11.6V@0.18 and 12.1V@0.40
        Expected: 0.18 + (12.0-11.6)/(12.1-11.6) * (0.40-0.18)
                = 0.18 + (0.4/0.5) * 0.22
                = 0.18 + 0.176 = 0.356 ≈ 0.36
        """
        result = soc_from_voltage(12.0, mock_lut_standard)
        assert 0.30 < result < 0.45  # Loose bounds for interpolation

    def test_soc_interpolation_midpoint(self, mock_lut_standard):
        """Voltage at midpoint between two LUT entries → expected interpolation."""
        # 12.1V (0.40) and 12.4V (0.64)
        # Midpoint: 12.25V should give approximately (0.40 + 0.64) / 2 = 0.52
        result = soc_from_voltage(12.25, mock_lut_standard)
        assert 0.50 < result < 0.54

    def test_soc_interpolation_with_measured_points(self, mock_lut_measured):
        """Interpolation works even with mixed measured/standard sources."""
        result = soc_from_voltage(12.6, mock_lut_measured)
        # Between 12.4@0.64 and 12.8@0.85
        # 12.6 is (12.6-12.4)/(12.8-12.4) = 0.2/0.4 = 0.5 of the way
        # SoC = 0.64 + 0.5 * (0.85 - 0.64) = 0.64 + 0.105 = 0.745
        assert 0.73 < result < 0.76


class TestSocFromVoltageClamping:
    """Test soc_from_voltage() edge cases: clamping outside LUT range."""

    def test_soc_clamp_high(self, mock_lut_standard):
        """Voltage above max LUT point → clamps to 1.0."""
        result = soc_from_voltage(13.5, mock_lut_standard)
        assert result == 1.0

    def test_soc_clamp_high_well_above(self, mock_lut_standard):
        """Voltage far above max LUT point → clamps to 1.0."""
        result = soc_from_voltage(15.0, mock_lut_standard)
        assert result == 1.0

    def test_soc_clamp_low(self, mock_lut_standard):
        """Voltage below min LUT point (anchor) → clamps to 0.0."""
        result = soc_from_voltage(10.0, mock_lut_standard)
        assert result == 0.0

    def test_soc_clamp_low_well_below(self, mock_lut_standard):
        """Voltage far below min LUT point → clamps to 0.0."""
        result = soc_from_voltage(5.0, mock_lut_standard)
        assert result == 0.0


class TestSocFromVoltageEdgeCases:
    """Test soc_from_voltage() with edge cases: empty LUT, single point, etc."""

    def test_soc_single_point_lut(self):
        """LUT with only one point → should handle gracefully."""
        lut_single = [{'v': 12.0, 'soc': 0.5, 'source': 'test'}]
        # Single point behavior: exact match returns that SoC,
        # anything else may clamp or return 0/1
        result = soc_from_voltage(12.0, lut_single)
        assert result == 0.5

    def test_soc_empty_lut(self):
        """Empty LUT → should handle gracefully (likely ValueError or return default)."""
        # This tests the function's robustness; behavior may vary
        # Could raise ValueError, return 0, or return 1
        with pytest.raises((ValueError, IndexError)):
            soc_from_voltage(12.0, [])

    def test_soc_two_point_lut(self):
        """LUT with exactly two points (minimal)."""
        lut_two = [
            {'v': 11.0, 'soc': 0.0, 'source': 'anchor'},
            {'v': 13.0, 'soc': 1.0, 'source': 'standard'},
        ]
        # Midpoint: 12.0V should give SoC = 0.0 + (12.0-11.0)/(13.0-11.0) * (1.0-0.0) = 0.5
        result = soc_from_voltage(12.0, lut_two)
        assert result == 0.5


class TestChargePercentage:
    """Test charge_percentage() conversion from SoC fraction to percent."""

    def test_charge_percentage_zero(self):
        """SoC = 0.0 → charge = 0.0%."""
        result = charge_percentage(0.0)
        assert result == 0.0

    def test_charge_percentage_half(self):
        """SoC = 0.5 → charge = 50.0%."""
        result = charge_percentage(0.5)
        assert result == 50.0

    def test_charge_percentage_full(self):
        """SoC = 1.0 → charge = 100.0%."""
        result = charge_percentage(1.0)
        assert result == 100.0

    def test_charge_percentage_75(self):
        """SoC = 0.75 → charge = 75.0%."""
        result = charge_percentage(0.75)
        assert result == 75.0

    def test_charge_percentage_clamp_high(self):
        """SoC > 1.0 (out of bounds) → clamp to 100.0%."""
        result = charge_percentage(1.5)
        assert result == 100.0

    def test_charge_percentage_clamp_negative(self):
        """SoC < 0.0 (negative) → clamp to 0.0%."""
        result = charge_percentage(-0.3)
        assert result == 0.0

    def test_charge_percentage_type(self):
        """Result is float, not int."""
        result = charge_percentage(0.75)
        assert isinstance(result, float)
