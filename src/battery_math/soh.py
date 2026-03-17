"""Pure kernel functions: SoH calculation from discharge curves and LUT smoothing.

No I/O, no logging. Time injection for simulator support.
"""

import math
import time
from typing import List, Dict, Optional, Tuple


def calculate_soh_from_discharge(
    voltage_series: List[float],
    time_series: List[float],
    reference_soh: float = 1.0,
    capacity_ah: float = 7.2,
    load_percent: float = 20.0,
    peukert_exponent: float = 1.2,
    nominal_voltage: float = 12.0,
    nominal_power_watts: float = 425.0,
    min_duration_sec: float = 30.0
) -> Optional[float]:
    """Pure function: SoH from discharge curve analysis.

    Returns None if:
    - Duration < 30s (VAL-01: flicker storm protection)
    - Insufficient voltage data
    - Math undefined

    No I/O, no time.time() calls — all inputs as parameters.

    Args:
        voltage_series: Voltage readings [V] during discharge
        time_series: Time [sec] for each reading (must be monotonic)
        reference_soh: Previous SoH estimate [0.0, 1.0]
        capacity_ah: Battery capacity (Ah)
        load_percent: Average load during discharge (%)
        peukert_exponent: Peukert exponent [1.0, 1.4]
        nominal_voltage: Battery nominal voltage (V)
        nominal_power_watts: UPS nominal power (W)
        min_duration_sec: Minimum discharge duration for valid update (default 30s VAL-01).
            F22: Kernel uses 30s for year-simulation flexibility (fast synthetic
            discharges). The daemon's operational guard in monitor.py
            _update_battery_health() enforces 300s minimum for real data.

    Returns:
        Updated SoH [0.0, 1.0] or None if insufficient data
    """
    if len(voltage_series) < 2 or len(time_series) < 2:
        return None

    discharge_duration = time_series[-1] - time_series[0]
    if discharge_duration < min_duration_sec:
        # VAL-01: Flicker storm protection
        return None

    # Trim data at anchor voltage (10.5V is physical limit)
    trimmed_v = []
    trimmed_t = []
    for v, t in zip(voltage_series, time_series):
        if v <= 10.5:
            break
        trimmed_v.append(v)
        trimmed_t.append(t)

    if len(trimmed_v) < 2:
        return None

    # Validate timestamp monotonicity
    for i in range(len(trimmed_t) - 1):
        if trimmed_t[i + 1] <= trimmed_t[i]:
            return None

    # Compute area-under-curve using trapezoidal rule
    area_measured = 0.0
    for i in range(len(trimmed_v) - 1):
        v1, v2 = trimmed_v[i], trimmed_v[i + 1]
        t1, t2 = trimmed_t[i], trimmed_t[i + 1]
        dt = t2 - t1
        area_measured += (v1 + v2) / 2.0 * dt

    # Reference area from Peukert's Law (pure physics)
    # Import here to avoid circular dependency
    from .peukert import peukert_runtime_hours

    T_expected_sec = peukert_runtime_hours(
        load_percent, capacity_ah, peukert_exponent,
        nominal_voltage, nominal_power_watts
    ) * 3600

    avg_voltage = sum(trimmed_v) / len(trimmed_v)
    area_reference = avg_voltage * T_expected_sec

    # SoH update with duration weighting (Bayesian prior-posterior blend)
    degradation_ratio = area_measured / area_reference if area_reference > 0 else 1.0

    # Weight updates by discharge duration to reduce bias on short events
    discharge_weight = min(discharge_duration / (0.30 * T_expected_sec), 1.0)

    if discharge_weight < 0.001:
        return None

    # Bayesian blend: new_soh = prior * (1 - weight) + likelihood * weight
    measured_soh = reference_soh * degradation_ratio
    new_soh = reference_soh * (1 - discharge_weight) + measured_soh * discharge_weight

    # Clamp to [0, 1]
    new_soh = max(0.0, min(1.0, new_soh))

    return new_soh


def _weighted_average_by_voltage(
    lut_entries: List[Dict],
    current_time: Optional[float] = None,
    decay_hours: float = 24 * 30
) -> float:
    """Weighted average voltage with age-based decay.

    Args:
        lut_entries: List of {"voltage": V, "timestamp": t_unix, "source": str}
        current_time: Current timestamp (seconds since epoch).
                     If None, defaults to time.time() (daemon behavior).
                     If provided, uses that value (simulator behavior).
        decay_hours: Age beyond which weight → 0 (30 days default)

    Returns:
        Weighted average voltage [V]
    """
    if current_time is None:
        current_time = time.time()  # Backward compatible default

    if not lut_entries:
        return 12.0  # Default nominal voltage

    # Compute weights based on age
    total_weight = 0.0
    weighted_voltage = 0.0
    decay_seconds = decay_hours * 3600

    for entry in lut_entries:
        if 'timestamp' not in entry:
            continue
        age_seconds = current_time - entry['timestamp']
        # Weight decays exponentially: exp(-age / decay_time)
        weight = math.exp(-age_seconds / decay_seconds)
        weighted_voltage += entry.get('voltage', 12.0) * weight
        total_weight += weight

    if total_weight > 1e-10:
        return weighted_voltage / total_weight

    # Unweighted fallback if all weights underflowed
    voltages = [e.get('voltage', 12.0) for e in lut_entries]
    return sum(voltages) / len(voltages) if voltages else 12.0


def interpolate_cliff_region(lut: tuple) -> tuple:
    """Pure function: smooth LUT near cutoff region.

    Kernel function returns new LUT; orchestrator applies it.
    No mutation of input — returns new tuple.

    For Phase 12.1, this is a placeholder that returns LUT unchanged.
    Phase 12 will implement cliff region interpolation.

    Args:
        lut: Immutable tuple of (voltage, soc, source) tuples

    Returns:
        Smoothed LUT as new tuple
    """
    # Phase 12.1 placeholder: return LUT unchanged
    # Phase 12 will add cliff region interpolation logic
    return lut
