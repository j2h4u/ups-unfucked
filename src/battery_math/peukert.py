"""Pure kernel: Peukert's Law runtime prediction.

Pure physics — no I/O, no state mutation. See runtime_calculator.py for
the production wrapper that applies SoC/SoH scaling and the 24h zero-load cap.
"""


def peukert_runtime_hours(
    load_percent: float,
    capacity_ah: float = 7.2,
    peukert_exponent: float = 1.2,
    nominal_voltage: float = 12.0,
    nominal_power_watts: float = 425.0
) -> float:
    """Pure function: Peukert runtime calculation. No I/O, no time.time().

    Args:
        load_percent: Load [0, 100]%
        capacity_ah: Battery capacity (Ah)
        peukert_exponent: Exponent n [1.0, 1.4]; default 1.2 for VRLA
        nominal_voltage: Battery nominal voltage (V)
        nominal_power_watts: UPS nominal power output (W)

    Returns:
        Runtime in hours at SoC=1.0, SoH=1.0
        Returns 0.0 for zero/negative load (battery_math strict; runtime_calculator uses 24h cap).
    """
    if load_percent <= 0:
        return 0.0

    I_rated = capacity_ah / 20.0
    I_actual = load_percent / 100.0 * nominal_power_watts / nominal_voltage
    T_rated = capacity_ah / I_rated
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
    """Pure function: Predict remaining battery runtime in minutes.

    Returns 0.0 if load=0 or SoC=0. No I/O, no state mutation.

    Args:
        soc: State of Charge [0.0, 1.0]
        load_percent: Load [0, 100]%
        capacity_ah: Battery capacity (Ah)
        soh: State of Health [0.0, 1.0]
        peukert_exponent: Exponent n [1.0, 1.4]
        nominal_voltage: Battery nominal voltage (V)
        nominal_power_watts: UPS nominal power output (W)

    Returns:
        Runtime in minutes at given SoC and SoH
    """
    if soc <= 0:
        return 0.0

    T_hours = peukert_runtime_hours(
        load_percent, capacity_ah, peukert_exponent,
        nominal_voltage, nominal_power_watts
    ) * soc * soh

    return max(0.0, T_hours * 60)
