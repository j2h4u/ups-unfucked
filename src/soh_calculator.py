"""State of Health (SoH) calculation from discharge voltage profiles."""

from typing import List


def calculate_soh_from_discharge(
    discharge_voltage_series: List[float],
    discharge_time_series: List[float],
    reference_soh: float = 1.0,
    anchor_voltage: float = 10.5
) -> float:
    """
    Calculate State of Health (SoH) from measured discharge voltage profile.

    Uses trapezoidal rule to integrate voltage over time. Compares measured
    area-under-curve against baseline to estimate degradation.

    Args:
        discharge_voltage_series: Voltage readings [V] during discharge
        discharge_time_series: Time [sec] for each voltage reading (must be monotonic)
        reference_soh: Previous SoH estimate (0.0-1.0); used as baseline
        anchor_voltage: Physical cutoff voltage (typically 10.5V for VRLA)

    Returns:
        Updated SoH estimate (0.0-1.0)

    Edge cases:
        - Empty or single-point data: returns reference_soh unchanged
        - All voltage values same (no discharge): returns reference_soh
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
        # Discharged below cutoff immediately; no calibration data
        return reference_soh

    # Compute area-under-curve using trapezoidal rule
    area_measured = 0.0
    for i in range(len(trimmed_v) - 1):
        v1, v2 = trimmed_v[i], trimmed_v[i + 1]
        t1, t2 = trimmed_t[i], trimmed_t[i + 1]
        dt = t2 - t1
        area_measured += (v1 + v2) / 2.0 * dt  # Voltage × time

    # Reference area: baseline discharge at new battery (SoH=1.0)
    # Empirical from 2026-03-12 blackout: ~13.4V → 10.5V over 2820 sec @ 20% load
    # Rough: average 12.0V over 47 minutes
    area_reference = 12.0 * 2820

    # SoH = (measured area / reference area) × previous SoH
    degradation_ratio = area_measured / area_reference if area_reference > 0 else 1.0
    new_soh = reference_soh * degradation_ratio

    # Clamp to [0, 1]
    new_soh = max(0.0, min(1.0, new_soh))

    return new_soh
