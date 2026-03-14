"""SoC prediction from battery voltage using LUT with linear interpolation (PRED-01, PRED-03)."""

import logging
from typing import List, Dict

logger = logging.getLogger(__name__)


def soc_from_voltage(voltage: float, lut: List[Dict]) -> float:
    """
    Predict SoC from battery voltage using LUT and linear interpolation.

    Algorithm:
    1. Check for exact match in LUT first
    2. Binary search to find LUT bracket (v1 ≤ v ≤ v2)
    3. Linear interpolation between bracketing points
    4. Clamp above max voltage to SoC=1.0
    5. Clamp below anchor to SoC=0.0

    Args:
        voltage: Battery voltage (float)
        lut: List of LUT entries, each dict with keys: {"v": float, "soc": float, "source": str}

    Returns:
        float: SoC as decimal between 0.0 and 1.0
    """
    if not lut:
        logger.warning("Empty LUT provided to soc_from_voltage")
        return 0.5  # Fallback to middle estimate

    # Check for exact match first
    for entry in lut:
        if entry["v"] == voltage:
            return entry["soc"]

    # LUT is maintained sorted descending by voltage in BatteryModel
    v_max = lut[0]["v"]
    v_min = lut[-1]["v"]

    # Clamp above max voltage
    if voltage > v_max:
        logger.debug(f"Voltage {voltage} > max {v_max}, clamping SoC to 1.0")
        return 1.0

    # Clamp below min voltage (anchor)
    if voltage < v_min:
        logger.debug(f"Voltage {voltage} < min {v_min}, clamping SoC to 0.0")
        return 0.0

    # Binary search for bracketing points
    # Find v1 < voltage < v2 where v1.v > voltage >= v2.v (since sorted descending)
    v1_entry = None
    v2_entry = None

    for i in range(len(lut) - 1):
        if lut[i]["v"] >= voltage > lut[i + 1]["v"]:
            v1_entry = lut[i]
            v2_entry = lut[i + 1]
            break

    # If no bracket found, something went wrong (shouldn't happen given above logic)
    if v1_entry is None or v2_entry is None:
        logger.warning(f"No LUT bracket found for voltage {voltage}")
        return 0.5

    # Linear interpolation
    v1, soc1 = v1_entry["v"], v1_entry["soc"]
    v2, soc2 = v2_entry["v"], v2_entry["soc"]

    if v1 == v2:
        return soc1  # Avoid division by zero

    soc = soc1 + (voltage - v1) / (v2 - v1) * (soc2 - soc1)
    logger.debug(f"Interpolated voltage {voltage}: {soc1} (at {v1}V) -> {soc2} (at {v2}V) = {soc}")

    return soc


def charge_percentage(soc: float) -> int:
    """
    Convert SoC decimal to charge percentage.

    Args:
        soc: State of charge as decimal (0.0 to 1.0)

    Returns:
        int: Charge percentage (0-100)
    """
    # Clamp SoC to [0, 1] first
    soc_clamped = max(0.0, min(1.0, soc))
    # Convert to percentage and round to nearest integer
    return int(round(soc_clamped * 100))
