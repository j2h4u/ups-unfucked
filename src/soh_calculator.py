"""SoH calculation orchestrator — selects capacity baseline and calls kernel.

Phase 13: When Phase 12 capacity estimation converges, use measured capacity
instead of rated. This separates aging (SoH trend) from capacity loss.
"""

import logging
from typing import Optional, Tuple
from battery_math import soh as battery_math_soh
from model import BatteryModel

logger = logging.getLogger(__name__)


def calculate_soh_from_discharge(
    discharge_voltage_series,
    discharge_time_series,
    reference_soh,
    battery_model: BatteryModel,
    load_percent,
    nominal_power_watts,
    nominal_voltage,
    peukert_exponent,
) -> Optional[Tuple[float, float]]:
    """Calculate SoH for completed discharge, using measured capacity if available.

    Args:
        discharge_voltage_series: List of voltage readings (V)
        discharge_time_series: List of time readings (s)
        reference_soh: Current SoH estimate before update
        battery_model: BatteryModel instance with convergence data
        load_percent: Average load during discharge (%)
        nominal_power_watts: UPS rated power (850W for UT850)
        nominal_voltage: UPS nominal voltage (120V for UT850)
        peukert_exponent: Peukert coefficient (1.2 for Phase 13)

    Returns:
        Tuple of (soh_new, capacity_ah_for_soh) or None if calculation failed
        - soh_new: Updated SoH estimate [0.0, 1.0]
        - capacity_ah_for_soh: Capacity used in calculation (measured or rated)
    """
    # Get convergence status from Phase 12
    convergence = battery_model.get_convergence_status()

    # Select capacity reference: measured if converged, else rated
    if convergence.get('converged', False):
        capacity_ah_for_soh = convergence.get('latest_ah')
        logger.info(f"SoH calculation using measured capacity: {capacity_ah_for_soh:.2f}Ah")
    else:
        capacity_ah_for_soh = battery_model.get_capacity_ah()  # Rated (7.2Ah)
        logger.info(f"SoH calculation using rated capacity: {capacity_ah_for_soh:.2f}Ah (measured not converged)")

    # Call kernel with selected capacity
    soh_new = battery_math_soh.calculate_soh_from_discharge(
        voltage_series=discharge_voltage_series,
        time_series=discharge_time_series,
        reference_soh=reference_soh,
        capacity_ah=capacity_ah_for_soh,  # ← Measured or rated
        load_percent=load_percent,
        nominal_power_watts=nominal_power_watts,
        nominal_voltage=nominal_voltage,
        peukert_exponent=peukert_exponent,
    )

    if soh_new is None:
        return None

    return soh_new, capacity_ah_for_soh  # Return both for caller to tag history entry
