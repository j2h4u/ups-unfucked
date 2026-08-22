"""Focused tests for the canonical pure forward/inverse LUT."""

import pytest

from src.battery_math.lut import LutPoint, soc_from_voltage

LUT = (
    LutPoint(13.4, 1.0),
    LutPoint(12.4, 0.64),
    LutPoint(10.5, 0.0),
)


@pytest.mark.parametrize(
    ("voltage_v", "expected"),
    ((13.5, 1.0), (10.4, 0.0), (12.4, 0.64), (12.9, 0.82)),
)
def test_soc_from_voltage_clamps_matches_and_interpolates(voltage_v, expected):
    assert soc_from_voltage(voltage_v, LUT) == pytest.approx(expected)


def test_soc_from_voltage_preserves_release_a_exact_match_band():
    assert soc_from_voltage(12.409, LUT) == pytest.approx(0.64)
    assert soc_from_voltage(12.391, LUT) == pytest.approx(0.64)
    assert soc_from_voltage(12.410, LUT) != pytest.approx(0.64)
    assert soc_from_voltage(12.390, LUT) != pytest.approx(0.64)
    assert soc_from_voltage(12.4101, LUT) != pytest.approx(0.64)
    assert soc_from_voltage(12.3899, LUT) != pytest.approx(0.64)


def test_ulp_stabilization_changes_only_the_mathematical_exact_band_boundary():
    voltages = tuple(
        12.4 + offset for offset in (-0.0101, -0.010, -0.009, 0.0, 0.009, 0.010, 0.0101)
    )

    differences = tuple(
        voltage
        for voltage in voltages
        if soc_from_voltage(voltage, LUT) != _legacy_soc_from_voltage(voltage, LUT)
    )

    assert differences
    assert all(round(abs(voltage - 12.4), 12) == 0.01 for voltage in differences)


def test_forward_lookup_handles_empty_lut():
    assert soc_from_voltage(12.0, ()) == 0.5


def _legacy_soc_from_voltage(voltage_v: float, lut: tuple[LutPoint, ...]) -> float:
    if voltage_v > lut[0].voltage_v:
        return 1.0
    if voltage_v < lut[-1].voltage_v:
        return 0.0
    for point in lut:
        if abs(point.voltage_v - voltage_v) < 0.01:
            return point.soc
    for upper, lower in zip(lut, lut[1:], strict=False):
        if upper.voltage_v >= voltage_v >= lower.voltage_v:
            fraction = (voltage_v - upper.voltage_v) / (lower.voltage_v - upper.voltage_v)
            return max(0.0, min(1.0, upper.soc + fraction * (lower.soc - upper.soc)))
    return 0.5
