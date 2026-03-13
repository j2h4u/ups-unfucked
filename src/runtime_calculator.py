"""
Remaining runtime prediction using Peukert's Law.

This module implements battery runtime calculation based on State of Charge (SoC),
current load, and battery health (SoH). The Peukert exponent accounts for the
nonlinear discharge behavior of lead-acid batteries under varying load.

Constants tuned to match observed behavior from real blackout on 2026-03-12:
- Observed: 47 minutes at ~20% load with SoC=1.0, SoH=1.0
- Peukert exponent: 1.2 (typical for VRLA)
- Scaling constant: 237.7 (empirically derived from 47-min blackout)

Formula:
    Time_rem = (capacity_ah * soc * soh) / (load_percent ^ peukert_exp) * const

Edge cases handled:
- load_percent ≤ 0: return 0.0 (no discharge)
- soc ≤ 0: return 0.0 (no battery available)
"""


def runtime_minutes(
    soc: float,
    load_percent: float,
    capacity_ah: float = 7.2,
    soh: float = 1.0,
    peukert_exp: float = 1.2,
    const: float = 237.7
) -> float:
    """
    Predict remaining battery runtime using Peukert's Law.

    Tuning: const=237.7 derived from 2026-03-12 blackout (47 min at 20% load).

    Args:
        soc: State of charge [0.0, 1.0]
        load_percent: Current load in percent [0, 100]
        capacity_ah: Full capacity in Ah (default 7.2 for UT850EG)
        soh: State of health [0.0, 1.0] (default 1.0 for new battery)
        peukert_exp: Peukert exponent (default 1.2 for VRLA)
        const: Scaling constant (tuned to match observed blackout duration)

    Returns:
        Minutes remaining (float); 0 if load=0 or SoC=0

    Examples:
        >>> runtime_minutes(soc=1.0, load_percent=20.0)  # ~47 min (blackout match)
        47.0
        >>> runtime_minutes(soc=0.0, load_percent=20.0)  # No battery
        0.0
        >>> runtime_minutes(soc=1.0, load_percent=0.0)   # No load
        0.0
    """
    # Guard against edge cases
    if load_percent <= 0 or soc <= 0:
        return 0.0

    # Peukert: Time = (Ah * SoC * SoH) / (Load ^ Peukert_exp) * Const
    time_rem = (capacity_ah * soc * soh) / (load_percent ** peukert_exp) * const

    return max(0.0, time_rem)
