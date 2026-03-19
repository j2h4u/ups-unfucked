"""Pure kernel function: Peukert exponent calibration from measured discharge.

No I/O, no logging, no state mutation.
"""

import math
from typing import Optional


def calibrate_peukert(
    actual_duration_sec: float,
    avg_load_percent: float,
    current_soh: float,
    capacity_ah: float,
    current_exponent: float,
    nominal_voltage: float = 12.0,
    nominal_power_watts: float = 425.0
) -> Optional[float]:
    """Pure function: Calibrate Peukert exponent from measured discharge.

    Returns None only if math is undefined (e.g., zero capacity or zero load).
    All data validation (< 2 samples, < 60s discharge) is orchestrator's responsibility.

    Args:
        actual_duration_sec: Measured discharge time (seconds)
        avg_load_percent: Average load during discharge [0, 100]%
        current_soh: Current SoH [0.0, 1.0]
        capacity_ah: Battery capacity (Ah)
        current_exponent: Current Peukert exponent (for iteration)
        nominal_voltage: UPS nominal voltage (12V for VRLA)
        nominal_power_watts: UPS nominal power (425W for UT850EG)

    Returns:
        Refined Peukert exponent [1.0, 1.4] or None if math undefined

    Notes:
        - Returns refined value; monitor.py decides whether to apply (VAL-02)
        - SoH dependency — T_effective = T_full * current_soh. Capacity-based SoH
          produces correct results. If Peukert starts drifting, investigate SoH
          input quality first.
    """
    if capacity_ah <= 0 or avg_load_percent <= 0:
        return None

    # Convert actual discharge duration to hours at full capacity
    actual_minutes = actual_duration_sec / 60.0

    # Compute expected runtime from Peukert's Law at current exponent
    I_rated = capacity_ah / 20.0
    I_actual = avg_load_percent / 100.0 * nominal_power_watts / nominal_voltage

    if I_actual <= 0:
        return None

    ratio = I_rated / I_actual

    # Peukert formula: T = T_rated × (I_rated / I_actual) ^ n
    # Solve for n: n = log(T / T_rated) / log(ratio)
    # where T_rated = 20 hours at C/20 rate

    T_rated = capacity_ah / I_rated  # = 20 hours

    # T_full is predicted runtime at SoC=1.0, SoH=1.0
    T_full_minutes = T_rated * 60

    # Actual effective runtime accounting for SoH
    T_effective = T_full_minutes * current_soh

    if T_effective <= 0:
        return None

    # Solve: actual_minutes = T_effective × (I_rated / I_actual) ^ n
    # n = log(actual_minutes / T_effective) / log(ratio)

    log_denominator = math.log(ratio)
    if abs(log_denominator) < 1e-10:
        return None

    log_numerator = math.log(actual_minutes / T_effective)
    new_exp = log_numerator / log_denominator

    # Clamp to physical bounds
    new_exp = max(1.0, min(1.4, new_exp))

    return new_exp
