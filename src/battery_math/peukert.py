"""Canonical Peukert runtime prediction with the safety zero-load cap."""

from dataclasses import dataclass

from src.battery_math.constants import NOMINAL_POWER_WATTS, RATED_CAPACITY_AH


@dataclass(frozen=True, slots=True)
class PeukertParameters:
    """Independent physical inputs for one remaining-runtime prediction."""

    capacity_ah: float = RATED_CAPACITY_AH
    soh: float = 1.0
    peukert_exponent: float = 1.2
    nominal_voltage: float = 12.0
    nominal_power_watts: float = NOMINAL_POWER_WATTS


DEFAULT_PEUKERT_PARAMETERS = PeukertParameters()


def peukert_runtime_hours(
    load_percent: float,
    capacity_ah: float = RATED_CAPACITY_AH,
    peukert_exponent: float = 1.2,
    nominal_voltage: float = 12.0,
    nominal_power_watts: float = NOMINAL_POWER_WATTS,
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
        Returns the canonical 24-hour safety cap for zero/negative load.
    """
    if load_percent <= 0:
        return 24.0

    I_rated = capacity_ah / 20.0
    I_actual = load_percent / 100.0 * nominal_power_watts / nominal_voltage
    T_rated = capacity_ah / I_rated
    return T_rated * (I_rated / I_actual) ** peukert_exponent


def runtime_minutes(
    soc: float,
    load_percent: float,
    parameters: PeukertParameters = DEFAULT_PEUKERT_PARAMETERS,
) -> float:
    """Pure function: Predict remaining battery runtime in minutes.

    Returns the canonical 24-hour cap if load is zero/negative and 0 if SoC is zero.

    Args:
        soc: State of Charge [0.0, 1.0]
        load_percent: Load [0, 100]%
        parameters: Frozen battery and UPS physics inputs.

    Returns:
        Runtime in minutes at given SoC and SoH
    """
    if soc <= 0:
        return 0.0
    if load_percent <= 0:
        return 24.0 * 60.0

    T_hours = (
        peukert_runtime_hours(
            load_percent,
            parameters.capacity_ah,
            parameters.peukert_exponent,
            parameters.nominal_voltage,
            parameters.nominal_power_watts,
        )
        * soc
        * parameters.soh
    )

    return max(0.0, T_hours * 60)
