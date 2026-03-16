"""Capacity estimation kernel functions.

Phase 12 adds actual capacity measurement here.
Phase 12.1 prepares the infrastructure (frozen BatteryState, pure functions).
"""

from typing import Optional, Tuple, Dict
from .types import BatteryState


def estimate_capacity(
    voltage_series: list,
    time_series: list,
    load_series: list,
    battery_state: BatteryState
) -> Tuple[Optional[float], float, Dict]:
    """Pure function: estimate capacity from deep discharge.

    Phase 12 implementation will:
    - Compute coulomb count ∫I·dt
    - Anchor to voltage cutoff (10.5V → SoC=0)
    - Return (capacity_ah, confidence_score, metadata)

    For Phase 12.1: returns (None, 0.0, {})

    Args:
        voltage_series: Voltage readings during discharge [V]
        time_series: Time readings [sec]
        load_series: Load readings during discharge [%]
        battery_state: Frozen BatteryState with current parameters

    Returns:
        Tuple of (capacity_ah, confidence_score, metadata_dict)
        - capacity_ah: Estimated capacity in Ah, or None if insufficient data
        - confidence_score: Confidence [0.0, 1.0] based on measurement quality
        - metadata_dict: Additional info (sample count, error bounds, etc.)
    """
    # Placeholder for Phase 12
    return (None, 0.0, {})
