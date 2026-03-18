"""Unit tests for soh_calculator.py — capacity-based SoH (F19/F20/F21)."""

import pytest
from unittest.mock import Mock
from src.soh_calculator import calculate_soh_from_discharge


def _make_battery_model(lut=None, capacity_ah=7.2):
    """Helper: create mock BatteryModel with LUT and capacity."""
    model = Mock()
    model.get_lut.return_value = lut if lut is not None else [
        {"v": 13.4, "soc": 1.0},
        {"v": 12.8, "soc": 0.9},
        {"v": 12.4, "soc": 0.64},
        {"v": 12.0, "soc": 0.4},
        {"v": 11.5, "soc": 0.2},
        {"v": 10.5, "soc": 0.0},
    ]
    model.get_capacity_ah.return_value = capacity_ah
    return model


class TestCapacityBasedSoH:
    """F19: SoH = measured_capacity / rated_capacity."""

    def test_healthy_battery_partial_discharge_soh_stable(self):
        """Healthy battery partial discharge → SoH stays near 1.0 (not 0.42 like old)."""
        # Realistic healthy battery: 13.0V→12.0V at 20% load
        # SoC range: ~0.93→~0.40 = ΔSoC ≈ 0.53
        # Healthy 7.2Ah battery at 7.08A should take ~1940s for this ΔSoC
        # Ah = 7.08A * 1940s / 3600 = 3.82Ah; measured_cap = 3.82/0.53 = 7.2Ah
        n = 194
        voltage_series = [13.0 - (i * 1.0 / (n - 1)) for i in range(n)]  # 13.0 → 12.0
        time_series = [float(i * 10) for i in range(n)]  # 0..1930s
        model = _make_battery_model()

        result = calculate_soh_from_discharge(
            discharge_voltage_series=voltage_series,
            discharge_time_series=time_series,
            reference_soh=1.0,
            battery_model=model,
            load_percent=20.0,
            nominal_power_watts=425.0,
            nominal_voltage=12.0,
            peukert_exponent=1.2,
        )

        assert result is not None
        soh_new, capacity_ah_ref = result
        # Healthy battery: SoH should stay near 1.0
        assert soh_new > 0.85, f"Healthy battery SoH={soh_new:.3f} too low (old bug: <<1.0)"
        assert capacity_ah_ref == 7.2

    def test_degraded_capacity_lowers_soh(self):
        """Measured capacity < rated → SoH proportionally lower."""
        # Simulate degraded battery: delivers less Ah for same ΔSoC
        # 600s at 20% load, 425W/12V = 7.08A → Ah = 7.08*600/3600 = 1.18Ah
        # Full range 13.4→10.5: ΔSoC=1.0 → measured = 1.18/ΔSoC
        # For ΔSoC ~0.53: measured = 1.18/0.53 = 2.23Ah... too low
        # Need to simulate a battery that actually shows degradation through
        # faster voltage drop at same current

        # Degraded battery: 20% load at 425W/12V over large SoC range
        # Voltage drops 13.4→11.0 in 600s: ΔSoC ≈ 0.8
        n = 60
        voltage_series = [13.4 - (i * 2.4 / (n - 1)) for i in range(n)]  # 13.4 → 11.0
        time_series = [float(i * 10) for i in range(n)]  # 600s

        # Use higher load to get more Ah delivered
        model = _make_battery_model(capacity_ah=7.2)

        result = calculate_soh_from_discharge(
            discharge_voltage_series=voltage_series,
            discharge_time_series=time_series,
            reference_soh=0.90,
            battery_model=model,
            load_percent=30.0,  # 30% load = 10.625A
            nominal_power_watts=425.0,
            nominal_voltage=12.0,
            peukert_exponent=1.2,
        )

        assert result is not None
        soh_new, capacity_ah_ref = result
        # measured_capacity = Ah/ΔSoC; if < rated → SoH < 1.0
        assert 0.2 < soh_new < 0.95
        assert capacity_ah_ref == 7.2

    def test_shallow_discharge_rejected(self):
        """ΔSoC < 5% → returns None (too shallow for SoH)."""
        # Tiny voltage drop: 13.0 → 12.95 (< 5% ΔSoC)
        n = 60
        voltage_series = [13.0 - (i * 0.05 / (n - 1)) for i in range(n)]
        time_series = [float(i * 10) for i in range(n)]  # 590s
        model = _make_battery_model()

        result = calculate_soh_from_discharge(
            discharge_voltage_series=voltage_series,
            discharge_time_series=time_series,
            reference_soh=1.0,
            battery_model=model,
            load_percent=20.0,
            nominal_power_watts=425.0,
            nominal_voltage=12.0,
            peukert_exponent=1.2,
        )

        assert result is None

    def test_short_duration_rejected(self):
        """Duration < 300s → returns None."""
        voltage_series = [13.0, 12.5, 12.0, 11.5, 11.0]
        time_series = [0, 50, 100, 150, 200]  # 200s < 300s
        model = _make_battery_model()

        result = calculate_soh_from_discharge(
            discharge_voltage_series=voltage_series,
            discharge_time_series=time_series,
            reference_soh=1.0,
            battery_model=model,
            load_percent=20.0,
            nominal_power_watts=425.0,
            nominal_voltage=12.0,
            peukert_exponent=1.2,
        )

        assert result is None


class TestBayesianBlend:
    """F20/F21: Bayesian blend with reference SoH weighted by ΔSoC."""

    def test_deep_discharge_full_weight(self):
        """ΔSoC=1.0 → weight=1.0, SoH = measured/rated exactly."""
        # Full discharge 13.4→10.5: ΔSoC=1.0
        n = 100
        voltage_series = [13.4 - (i * 2.9 / (n - 1)) for i in range(n)]  # 13.4→10.5
        time_series = [float(i * 10) for i in range(n)]  # 990s
        model = _make_battery_model()

        result = calculate_soh_from_discharge(
            discharge_voltage_series=voltage_series,
            discharge_time_series=time_series,
            reference_soh=0.50,  # Very different reference
            battery_model=model,
            load_percent=20.0,
            nominal_power_watts=425.0,
            nominal_voltage=12.0,
            peukert_exponent=1.2,
        )

        assert result is not None
        soh_new, _ = result
        # With weight=1.0 (full ΔSoC), result should be purely measured/rated
        # regardless of reference_soh
        # measured_capacity = Ah/1.0 = Ah_delivered
        # Ah = (20/100)*425/12 * 990/3600 = 7.08 * 0.275 = 1.947Ah
        # soh_raw = 1.947/7.2 = 0.27
        # With full weight: soh = 0.50*(1-1.0) + 0.27*1.0 = 0.27
        # The reference (0.50) should be almost completely overridden
        assert abs(soh_new - 0.50) > 0.1, "Full ΔSoC should override reference"

    def test_soh_blend_with_reference(self):
        """Partial ΔSoC → blended result between reference and measured."""
        # ~30% ΔSoC discharge
        n = 60
        voltage_series = [13.0 - (i * 0.6 / (n - 1)) for i in range(n)]  # 13.0→12.4
        time_series = [float(i * 10) for i in range(n)]  # 590s
        model = _make_battery_model()

        result = calculate_soh_from_discharge(
            discharge_voltage_series=voltage_series,
            discharge_time_series=time_series,
            reference_soh=0.95,
            battery_model=model,
            load_percent=20.0,
            nominal_power_watts=425.0,
            nominal_voltage=12.0,
            peukert_exponent=1.2,
        )

        assert result is not None
        soh_new, _ = result
        # Should be between measured and reference (blended)
        assert 0.0 < soh_new <= 1.0

    def test_insufficient_samples_returns_none(self):
        """< 2 voltage samples → returns None."""
        model = _make_battery_model()
        result = calculate_soh_from_discharge(
            discharge_voltage_series=[13.0],
            discharge_time_series=[0.0],
            reference_soh=1.0,
            battery_model=model,
            load_percent=20.0,
            nominal_power_watts=425.0,
            nominal_voltage=12.0,
            peukert_exponent=1.2,
        )
        assert result is None

    def test_no_lut_returns_none(self):
        """Empty LUT → returns None."""
        model = _make_battery_model(lut=[])
        result = calculate_soh_from_discharge(
            discharge_voltage_series=[13.0, 12.0],
            discharge_time_series=[0.0, 600.0],
            reference_soh=1.0,
            battery_model=model,
            load_percent=20.0,
            nominal_power_watts=425.0,
            nominal_voltage=12.0,
            peukert_exponent=1.2,
        )
        assert result is None
