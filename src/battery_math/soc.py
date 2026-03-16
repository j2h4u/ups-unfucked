"""Pure kernel function: SoC from voltage using LUT interpolation.

Voltage→SoC lookup with state-aware bounds checking (VAL-01: discharge-mode floor).
No I/O, no logging, no state mutation.
"""

import bisect
from typing import Optional


def soc_from_voltage(voltage: float, lut: tuple, state: str = "OL") -> Optional[float]:
    """Pure function: SoC from voltage using LUT interpolation.

    Args:
        voltage: Measured voltage (V)
        lut: Immutable tuple of (voltage, soc, source) tuples
        state: "OL" (on-line, no load floor) or "OB" (on-battery, 10.0V floor)

    Returns:
        SoC [0.0, 1.0] or None if voltage out of bounds (VAL-01 constraint)

    Notes:
        - During OB (discharge), voltage < 10.0V is physically impossible
          (UPS cuts off at 10.5V). Skip corrupted sample.
        - During OL (no load), accept down to 8.0V (worst case).
    """
    if not lut or len(lut) < 2:
        return None

    # Set floor based on state
    if state == "OB":
        floor = 10.0  # Discharge-mode: UPS minimum (VAL-01)
    else:
        floor = 8.0   # Global floor: worst case

    if voltage < floor or voltage > 15.0:
        return None

    # Extract voltages and SoCs from LUT tuple of tuples
    # LUT format: tuple of (voltage, soc, source) tuples, sorted descending by voltage
    voltages = [entry[0] for entry in lut]
    socs = [entry[1] for entry in lut]

    v_max = voltages[0]
    v_min = voltages[-1]

    # Clamp above max voltage
    if voltage > v_max:
        return 1.0

    # Clamp below min voltage (anchor)
    if voltage < v_min:
        return 0.0

    # Binary search for bracketing points (LUT sorted descending by voltage)
    # Build reversed voltage list for bisect (which expects ascending order)
    voltages_asc = list(reversed(voltages))
    pos = bisect.bisect_left(voltages_asc, voltage)
    # Convert back to descending index
    i = len(voltages) - 1 - pos

    v1 = None
    soc1 = None
    v2 = None
    soc2 = None

    if 0 <= i < len(voltages) - 1:
        v1 = voltages[i]
        soc1 = socs[i]
        v2 = voltages[i + 1]
        soc2 = socs[i + 1]
    elif i == len(voltages) - 1:
        # Special case: voltage at or near minimum
        v1 = voltages[i]
        soc1 = socs[i]
        v2 = None
        soc2 = None

    # Check for match within tolerance at bracket points
    if v1 is not None and abs(v1 - voltage) < 0.01:
        return soc1
    if v2 is not None and abs(v2 - voltage) < 0.01:
        return soc2

    # If no valid bracket, return None
    if v1 is None or v2 is None:
        return None

    # Linear interpolation
    if v1 == v2:
        return soc1  # Avoid division by zero

    soc = soc1 + (voltage - v1) / (v2 - v1) * (soc2 - soc1)
    return max(0.0, min(1.0, soc))  # Clamp to [0, 1]
