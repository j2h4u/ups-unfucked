"""Runtime prediction — re-exports from battery_math.peukert.

Production wrapper: applies 24h cap for zero-load to prevent false LB flags.
"""

from src.battery_math.peukert import peukert_runtime_hours as _peukert_runtime_hours
from src.battery_math.peukert import runtime_minutes as _kernel_runtime_minutes


def peukert_runtime_hours(
    load_percent: float,
    capacity_ah: float = 7.2,
    peukert_exponent: float = 1.2,
    nominal_voltage: float = 12.0,
    nominal_power_watts: float = 425.0
) -> float:
    """Peukert runtime with 24h cap for zero/negative load.

    Production wrapper — battery_math kernel returns 0.0 for zero load,
    which would trigger a false LB flag. This wrapper caps at 24h instead.
    """
    if load_percent <= 0:
        return 24.0
    return _peukert_runtime_hours(
        load_percent, capacity_ah, peukert_exponent,
        nominal_voltage, nominal_power_watts
    )


def runtime_minutes(
    soc: float,
    load_percent: float,
    capacity_ah: float = 7.2,
    soh: float = 1.0,
    peukert_exponent: float = 1.2,
    nominal_voltage: float = 12.0,
    nominal_power_watts: float = 425.0
) -> float:
    """Predict remaining battery runtime in minutes.

    Production wrapper — applies 24h cap for zero load (kernel returns 0.0,
    which would trigger a false LB flag), then delegates to kernel.
    """
    if load_percent <= 0:
        return 24.0 * 60  # 24h cap in minutes
    return _kernel_runtime_minutes(
        soc, load_percent, capacity_ah, soh,
        peukert_exponent, nominal_voltage, nominal_power_watts
    )
