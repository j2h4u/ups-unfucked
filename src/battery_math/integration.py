"""Coulomb counting integration: standalone integrate_current() function."""

from typing import List


def integrate_current(
    load_percent: List[float],
    time_sec: List[float],
    nominal_power_watts: float,
    nominal_voltage: float,
) -> float:
    """Coulomb counting: convert load% → current (A) → Ah via trapezoidal integration.

    Formula:
        I(A) = (load_percent / 100) × nominal_power_watts / nominal_voltage
        Ah = ∫I dt / 3600 (convert A·s to Ah)

    Uses trapezoidal rule for numerical integration (IEEE-1106 standard).

    F27: Current computed from nominal voltage (12V), not actual battery voltage.
    Ah overestimated ~4% (same bias as F14 in runtime_calculator). Systematic
    and consistent — doesn't affect convergence because all measurements share
    the same bias direction.

    Args:
        load_percent: Load percentages [0–100] at each time point.
        time_sec: Unix timestamps (seconds, monotonic).
        nominal_power_watts: UPS rated power (W).
        nominal_voltage: Battery nominal voltage (V).

    Returns:
        float: Total charge in Ah. Returns 0.0 for fewer than 2 samples.
    """
    if len(load_percent) < 2:
        return 0.0

    ah_total = 0.0
    for i in range(len(load_percent) - 1):
        current_a_start = (load_percent[i] / 100.0) * nominal_power_watts / nominal_voltage
        current_a_end = (load_percent[i + 1] / 100.0) * nominal_power_watts / nominal_voltage
        i_avg = (current_a_start + current_a_end) / 2.0
        dt = time_sec[i + 1] - time_sec[i]
        ah_total += i_avg * dt / 3600.0

    return ah_total
