"""
Remaining runtime prediction using Peukert's Law.

Pure physics formula — no empirical constants.

Peukert's Law: T = T_rated × (I_rated / I_actual)^n
where:
  T_rated = capacity_ah / I_rated (hours at C/20 rate)
  I_rated = capacity_ah / 20 (C/20 discharge rate)
  I_actual = (load_percent / 100) × nominal_power_watts / nominal_voltage
  n = Peukert exponent (1.0–1.4 for VRLA, typically 1.2)

Validation: at 17% load, n=1.15, 7.2Ah, 425W/12V → 47.0 min (matches 2026-03-12 blackout).
"""


def peukert_runtime_hours(
    load_percent: float,
    capacity_ah: float = 7.2,
    peukert_exponent: float = 1.2,
    nominal_voltage: float = 12.0,
    nominal_power_watts: float = 425.0
) -> float:
    """
    Core Peukert calculation: full-charge runtime in hours at given load.

    Returns hours at SoC=1.0, SoH=1.0. Callers scale by SoC/SoH as needed.
    Returns 24.0h (cap) if load_percent <= 0 — zero load means no current
    draw, so runtime is effectively infinite. Capped to avoid nonsensical
    values from NUT sensor glitches (load=0 would otherwise cause division
    by zero or false LB flag if returned as 0.0).
    """
    if load_percent <= 0:
        return 24.0

    I_rated = capacity_ah / 20.0
    I_actual = load_percent / 100.0 * nominal_power_watts / nominal_voltage
    T_rated = capacity_ah / I_rated  # = 20h
    return T_rated * (I_rated / I_actual) ** peukert_exponent


def runtime_minutes(
    soc: float,
    load_percent: float,
    capacity_ah: float = 7.2,
    soh: float = 1.0,
    peukert_exponent: float = 1.2,
    nominal_voltage: float = 12.0,
    nominal_power_watts: float = 425.0
) -> float:
    """
    Predict remaining battery runtime in minutes.

    Returns 0.0 if SoC=0. Returns capped value (via peukert_runtime_hours) if load=0.
    """
    if soc <= 0:
        return 0.0

    T_hours = peukert_runtime_hours(
        load_percent, capacity_ah, peukert_exponent,
        nominal_voltage, nominal_power_watts
    ) * soc * soh

    return max(0.0, T_hours * 60)
