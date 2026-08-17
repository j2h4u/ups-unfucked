"""Pure forward and inverse voltage/SoC lookup-table functions."""

from dataclasses import dataclass
from math import isfinite
from typing import Final

SOC_FALLBACK: Final = 0.5
EXACT_MATCH_TOLERANCE_V: Final = 0.01


@dataclass(frozen=True, slots=True)
class LutPoint:
    """One immutable point in a voltage-descending lookup table."""

    voltage_v: float
    soc: float
    source: str


type FrozenLut = tuple[LutPoint, ...]


def soc_from_voltage(
    voltage_v: float,
    lut: FrozenLut,
) -> float:
    """Return clamped SoC using the Release-A exact-match and interpolation rules."""
    if not lut or not isfinite(voltage_v):
        return SOC_FALLBACK
    boundary = _boundary_soc(voltage_v, lut)
    if boundary is not None:
        return boundary
    exact_match = _exact_match_soc(voltage_v, lut)
    if exact_match is not None:
        return exact_match
    interpolated = _interpolated_soc(voltage_v, lut)
    return SOC_FALLBACK if interpolated is None else interpolated


def _boundary_soc(voltage_v: float, lut: FrozenLut) -> float | None:
    if voltage_v > lut[0].voltage_v:
        return 1.0
    if voltage_v < lut[-1].voltage_v:
        return 0.0
    return None


def _exact_match_soc(voltage_v: float, lut: FrozenLut) -> float | None:
    for point in lut:
        distance_v = round(abs(point.voltage_v - voltage_v), 12)
        if distance_v < EXACT_MATCH_TOLERANCE_V:
            return point.soc
    return None


def _interpolated_soc(voltage_v: float, lut: FrozenLut) -> float | None:
    for upper, lower in zip(lut, lut[1:], strict=False):
        if upper.voltage_v >= voltage_v >= lower.voltage_v:
            voltage_span = lower.voltage_v - upper.voltage_v
            if voltage_span == 0.0:
                return upper.soc
            fraction = (voltage_v - upper.voltage_v) / voltage_span
            return _clamp_soc(upper.soc + fraction * (lower.soc - upper.soc))
    return None


def inverse_lut_voltage(soc: float, lut: FrozenLut) -> float:
    """Return clamped voltage for SoC using the same immutable LUT."""
    if not lut or not isfinite(soc):
        return 0.0
    if soc >= lut[0].soc:
        return lut[0].voltage_v
    if soc <= lut[-1].soc:
        return lut[-1].voltage_v

    for upper, lower in zip(lut, lut[1:], strict=False):
        if upper.soc >= soc >= lower.soc:
            soc_span = lower.soc - upper.soc
            if soc_span == 0.0:
                return upper.voltage_v
            fraction = (soc - upper.soc) / soc_span
            return upper.voltage_v + fraction * (lower.voltage_v - upper.voltage_v)

    return lut[-1].voltage_v


def _clamp_soc(soc: float) -> float:
    return max(0.0, min(1.0, soc))
