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

    Known limitations (audit 2026-03-17):
    - F14: I_actual uses nominal voltage (12V) not actual battery voltage.
      At low SoC (10.5V), current underestimated ~14%. Error <3% at stable
      14-20% load because Peukert RLS calibrates at the same nominal V,
      absorbing the systematic bias.
    - F15: Linear SoC scaling (T_hours * soc) below 20% SoC overestimates
      runtime. Partially compensated by LUT cliff nonlinearity. Improves
      as cliff region data populates (F9).
    - F16: Peukert at 15.7x C-rate is outside empirical range (typically
      0.05-5x C). Works because RLS calibrates at the actual operating
      point — Peukert exponent is a curve-fit, not a physical constant.
    - F17: SoH linear scaling (T_hours * soh) approximates energy-based
      SoH as capacity-based. ~5% error for VRLA. Acceptable for shutdown
      timing decisions.
    """
    if soc <= 0:
        return 0.0

    T_hours = peukert_runtime_hours(
        load_percent, capacity_ah, peukert_exponent,
        nominal_voltage, nominal_power_watts
    ) * soc * soh

    return max(0.0, T_hours * 60)
