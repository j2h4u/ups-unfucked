"""State of Health (SoH) calculation from discharge voltage profiles."""

import logging
from typing import List, Dict
from src.runtime_calculator import peukert_runtime_hours

logger = logging.getLogger(__name__)



def calculate_soh_from_discharge(
    discharge_voltage_series: List[float],
    discharge_time_series: List[float],
    reference_soh: float = 1.0,
    anchor_voltage: float = 10.5,
    capacity_ah: float = 7.2,
    load_percent: float = 20.0,
    nominal_power_watts: float = 425.0,
    nominal_voltage: float = 12.0,
    peukert_exponent: float = 1.2
) -> float:
    """
    Calculate State of Health (SoH) from measured discharge voltage profile.

    Uses trapezoidal rule to integrate voltage over time. Reference area is
    computed from Peukert's Law (no empirical constants).

    Args:
        discharge_voltage_series: Voltage readings [V] during discharge
        discharge_time_series: Time [sec] for each voltage reading (must be monotonic)
        reference_soh: Previous SoH estimate (0.0-1.0); used as baseline
        anchor_voltage: Physical cutoff voltage (typically 10.5V for VRLA)
        capacity_ah: Full capacity in Ah
        load_percent: Average load during discharge (%)
        nominal_power_watts: UPS nominal power output (W)
        nominal_voltage: Battery nominal voltage (V)
        peukert_exponent: Peukert exponent

    Returns:
        Updated SoH estimate (0.0-1.0)

    Edge cases:
        - Empty or single-point data: returns reference_soh unchanged
        - Voltage below anchor: integration stops at anchor (physical limit)
        - Computed SoH < 0 or > 1: clamped to [0, 1]
    """
    if len(discharge_voltage_series) < 2:
        return reference_soh

    # Trim data at anchor voltage (10.5V is physical limit)
    trimmed_v = []
    trimmed_t = []
    for v, t in zip(discharge_voltage_series, discharge_time_series):
        if v <= anchor_voltage:
            break
        trimmed_v.append(v)
        trimmed_t.append(t)

    if len(trimmed_v) < 2:
        return reference_soh

    # Validate timestamp monotonicity (guard against clock jumps from NTP corrections)
    for i in range(len(trimmed_t) - 1):
        if trimmed_t[i + 1] <= trimmed_t[i]:
            logger.warning(f"Non-monotonic timestamps in discharge data at index {i}: "
                           f"{trimmed_t[i]} >= {trimmed_t[i+1]}, returning reference SoH")
            return reference_soh

    # Compute area-under-curve using trapezoidal rule
    area_measured = 0.0
    for i in range(len(trimmed_v) - 1):
        v1, v2 = trimmed_v[i], trimmed_v[i + 1]
        t1, t2 = trimmed_t[i], trimmed_t[i + 1]
        dt = t2 - t1
        area_measured += (v1 + v2) / 2.0 * dt

    # Reference area from Peukert's Law (physics, no hardcoded constants)
    T_expected_sec = peukert_runtime_hours(
        load_percent, capacity_ah, peukert_exponent,
        nominal_voltage, nominal_power_watts
    ) * 3600
    avg_voltage = sum(trimmed_v) / len(trimmed_v)
    area_reference = avg_voltage * T_expected_sec

    # SoH = (measured area / reference area) × previous SoH
    degradation_ratio = area_measured / area_reference if area_reference > 0 else 1.0
    new_soh = reference_soh * degradation_ratio

    # Clamp to [0, 1]
    new_soh = max(0.0, min(1.0, new_soh))

    return new_soh


def interpolate_cliff_region(
    lut: List[Dict],
    anchor_voltage: float = 10.5,
    cliff_start: float = 11.0,
    step_mv: float = 0.1
) -> List[Dict]:
    """
    Interpolate cliff region (11.0V–10.5V) from measured calibration data.

    Fills gaps between measured points with linear interpolation.
    Marks interpolated entries with source='interpolated'.
    Removes old 'standard' entries in cliff region.

    Args:
        lut: Current LUT entries
        anchor_voltage: Bottom of cliff (10.5V default)
        cliff_start: Top of cliff (11.0V default)
        step_mv: Interpolation resolution (0.1V = 100mV)

    Returns:
        Updated LUT with cliff region interpolated
    """
    # Separate cliff measured points from rest of LUT
    cliff_measured = [e for e in lut
                     if anchor_voltage <= e['v'] <= cliff_start
                     and e['source'] == 'measured']
    other_entries = [e for e in lut
                    if e['v'] < anchor_voltage or e['v'] > cliff_start]

    # Can't interpolate with <2 points
    if len(cliff_measured) < 2:
        return lut

    # Sort measured points ascending by voltage
    cliff_measured.sort(key=lambda x: x['v'])

    # Interpolate between consecutive measured points
    interpolated = []
    for i in range(len(cliff_measured) - 1):
        p1, p2 = cliff_measured[i], cliff_measured[i + 1]

        # Add first point
        interpolated.append(p1)

        # Linear interpolation
        v_current = p1['v'] + step_mv
        while v_current < p2['v']:
            frac = (v_current - p1['v']) / (p2['v'] - p1['v'])
            soc_interp = p1['soc'] + frac * (p2['soc'] - p1['soc'])
            interpolated.append({
                'v': round(v_current, 2),
                'soc': round(soc_interp, 3),
                'source': 'interpolated'
            })
            v_current += step_mv

    # Add last point
    interpolated.append(cliff_measured[-1])

    # Combine with non-cliff entries and re-sort
    updated_lut = other_entries + interpolated
    updated_lut.sort(key=lambda x: x['v'], reverse=True)

    return updated_lut

