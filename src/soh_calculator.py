"""SoH calculation orchestrator — capacity-based SoH (F19/F20/F21 redesign).

Algorithm: SoH = measured_capacity / rated_capacity
- Coulomb counting for Ah delivered during discharge
- LUT-based ΔSoC to extrapolate to full-discharge capacity
- Bayesian blend with reference SoH, weighted by ΔSoC depth

Replaces old area-under-curve approach which compared partial discharge
to full Peukert area, producing SoH << 1.0 on every discharge.
"""

import logging
from typing import List, Optional, Tuple
from src.soc_predictor import soc_from_voltage
from src.model import BatteryModel

logger = logging.getLogger(__name__)

# Minimum ΔSoC for meaningful SoH update (5% depth too shallow)
MIN_DELTA_SOC = 0.05

# Minimum discharge duration for SoH calculation (seconds)
MIN_DURATION_SEC = 300


def calculate_soh_from_discharge(
    discharge_voltage_series: List[float],
    discharge_time_series: List[float],
    reference_soh: float,
    battery_model: BatteryModel,
    load_percent: float,
    nominal_power_watts: float,
    nominal_voltage: float,
    peukert_exponent: float,
) -> Optional[Tuple[float, float]]:
    """Calculate SoH for completed discharge using capacity-based method.

    Algorithm:
    1. Coulomb counting: integrate current over time → Ah delivered
    2. LUT lookup: ΔSoC from voltage start/end
    3. Extrapolate: measured_capacity = Ah_delivered / ΔSoC
    4. SoH = measured_capacity / rated_capacity
    5. Bayesian blend with reference_soh weighted by ΔSoC

    Args:
        discharge_voltage_series: List of voltage readings (V)
        discharge_time_series: List of time readings (s)
        reference_soh: Current SoH estimate before update
        battery_model: BatteryModel instance with LUT and convergence data
        load_percent: Average load during discharge (%)
        nominal_power_watts: UPS rated power (W)
        nominal_voltage: Battery nominal voltage (V)
        peukert_exponent: Peukert coefficient (unused, kept for API compat)

    Returns:
        Tuple of (soh_new, capacity_ah_ref) or None if calculation failed
        - soh_new: Updated SoH estimate [0.0, 1.0]
        - capacity_ah_ref: Rated capacity used as reference (Ah)
    """
    # Guard: need at least 2 samples
    if len(discharge_voltage_series) < 2 or len(discharge_time_series) < 2:
        return None

    # Guard: minimum duration
    duration = discharge_time_series[-1] - discharge_time_series[0]
    if duration < MIN_DURATION_SEC:
        logger.debug(f"SoH skipped: discharge too short ({duration:.0f}s < {MIN_DURATION_SEC}s)")
        return None

    # Get LUT from model
    lut = battery_model.get_lut()
    if not lut:
        logger.warning("SoH skipped: no LUT available")
        return None

    # ΔSoC from voltage series via LUT
    soc_start = soc_from_voltage(discharge_voltage_series[0], lut)
    soc_end = soc_from_voltage(discharge_voltage_series[-1], lut)
    delta_soc = soc_start - soc_end

    if delta_soc < MIN_DELTA_SOC:
        logger.debug(f"SoH skipped: ΔSoC {delta_soc*100:.1f}% < {MIN_DELTA_SOC*100:.0f}%")
        return None

    # Coulomb counting: Ah delivered during discharge
    ah_delivered = 0.0
    for i in range(len(discharge_time_series) - 1):
        dt = discharge_time_series[i + 1] - discharge_time_series[i]
        if dt <= 0:
            continue
        current_a = load_percent / 100.0 * nominal_power_watts / nominal_voltage
        ah_delivered += current_a * dt / 3600.0

    if ah_delivered <= 0:
        return None

    # Extrapolate to full-discharge capacity
    measured_capacity = ah_delivered / delta_soc

    # SoH = measured / rated
    capacity_ah_ref = battery_model.get_capacity_ah()  # Rated (7.2Ah)
    soh_raw = min(1.0, measured_capacity / capacity_ah_ref)

    # Bayesian blend: weight by ΔSoC (deeper discharge = more reliable)
    weight = min(delta_soc, 1.0)
    soh_new = reference_soh * (1 - weight) + soh_raw * weight
    soh_new = max(0.0, min(1.0, soh_new))

    logger.info(
        f"SoH capacity-based: measured={measured_capacity:.2f}Ah, "
        f"rated={capacity_ah_ref:.2f}Ah, raw={soh_raw:.3f}, "
        f"blended={soh_new:.3f} (ΔSoC={delta_soc*100:.1f}%, weight={weight:.2f})"
    )

    return soh_new, capacity_ah_ref
