from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class BatteryState:
    """Immutable battery state snapshot for kernel functions.

    Every kernel function takes frozen BatteryState, returns new BatteryState.
    Frozen dataclass makes circular dependencies structurally visible at type level.
    No mutation allowed — returns new state with updated fields.
    """
    soh: float
    """State of Health [0.0, 1.0] — capacity at given age / capacity when new"""

    peukert_exponent: float
    """Peukert exponent [1.0, 1.4] — discharge curve shape (VRLA ≈ 1.2)"""

    capacity_ah_rated: float
    """Rated capacity from manufacturer label (Ah)"""

    capacity_ah_measured: Optional[float]
    """Measured capacity from discharge analysis (Ah), None until Phase 12 converges"""

    lut: tuple
    """Voltage→SoC lookup table (immutable tuple of tuples: ((V, SoC, source), ...))"""

    cycle_count: int
    """Number of OL→OB transitions (discharge events, counting full + partial)"""

    cumulative_on_battery_sec: float
    """Total time on battery (seconds), all discharges"""
