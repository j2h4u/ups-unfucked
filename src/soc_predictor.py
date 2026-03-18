"""SoC prediction from battery voltage using LUT with linear interpolation (PRED-01, PRED-03)."""

import bisect
import logging
from typing import List, Dict

logger = logging.getLogger('ups-battery-monitor')

# Conservative middle estimate when LUT is empty — avoids false LB flag (0.0) or false "full" (1.0)
SOC_FALLBACK = 0.5


def soc_from_voltage(voltage: float, lut: List[Dict]) -> float:
    """
    Predict SoC from battery voltage using LUT and linear interpolation.

    Algorithm:
    1. Check for exact match in LUT first (tolerance ±0.01V for floating-point precision)
    2. Linear scan to find LUT bracket (v1 ≥ voltage > v2)
    3. Linear interpolation between bracketing points
    4. Clamp above max voltage to SoC=1.0
    5. Clamp below anchor to SoC=0.0

    Args:
        voltage: Battery voltage (float)
        lut: List of LUT entries, each dict with keys: {"v": float, "soc": float, "source": str}

    Returns:
        float: SoC as decimal between 0.0 and 1.0

    Note:
        LUT is assumed sorted descending by voltage. Binary search via bisect is used
        for O(log n) bracket finding.

    Known limitations (audit 2026-03-17):
    - F8: IR compensation reference frame mismatch — LUT stores raw voltage
      during discharge calibration, but lookup receives IR-compensated voltage.
      Error = k*(L_actual - L_calibration), ≤5% SoC at typical loads. Negligible
      when load ≈ L_base=20% because the correction term approaches zero.
    - F9: Cliff region (10.5-11.0V) has no measured data initially — 0.5V span
      covers 6% SoC. Resolves organically after deep discharge populates LUT
      entries in this range. Data gap, not code bug.
    - F10: ±0.01V tolerance could match conflicting SoC values if LUT contains
      duplicate voltages. Prevented by F7 dedup in _prune_lut() which keeps
      only the most recent entry per voltage band.
    """
    if not lut:
        logger.warning("Empty LUT provided to soc_from_voltage")
        return SOC_FALLBACK

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

    # Binary search for bracketing points (LUT sorted descending by voltage)
    # Build reversed voltage list for bisect (which expects ascending order)
    voltages_asc = [e["v"] for e in reversed(lut)]
    # bisect_left finds insertion point in ascending list
    pos = bisect.bisect_left(voltages_asc, voltage)
    # Convert back to descending index: i is the upper bracket
    i = len(lut) - 1 - pos
    v1_entry = None
    v2_entry = None
    if 0 <= i < len(lut) - 1:
        v1_entry = lut[i]
        v2_entry = lut[i + 1]
    elif i == len(lut) - 1:
        # Special case: voltage equals or is very close to minimum (anchor point)
        v1_entry = lut[i]
        v2_entry = None

    # Check for match within tolerance at bracket points (handles floating-point precision from EMA filtering)
    if v1_entry is not None and abs(v1_entry["v"] - voltage) < 0.01:
        return v1_entry["soc"]
    if v2_entry is not None and abs(v2_entry["v"] - voltage) < 0.01:
        return v2_entry["soc"]

    # If no valid bracket found, something went wrong (shouldn't happen given above logic)
    if v1_entry is None or v2_entry is None:
        logger.warning(f"No LUT bracket found for voltage {voltage}")
        return SOC_FALLBACK

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
