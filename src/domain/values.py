"""Small immutable values shared by the live safety and model-load paths."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from src.battery_math.lut import FrozenLut


class BlackoutKind(StrEnum):
    ONLINE = "online"
    BLACKOUT_REAL = "blackout_real"
    BLACKOUT_TEST = "blackout_test"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PhysicalObservation:
    boot_id: str
    monotonic_ns: int
    wall_time_utc: datetime
    raw_status: str
    battery_voltage_raw: str | None
    battery_voltage_v: float | None
    load_percent: float | None
    input_voltage_v: float | None
    battery_pct: float | None = None
    runtime_s: float | None = None
    output_v: float | None = None


@dataclass(frozen=True, slots=True)
class FrozenModelSnapshot:
    rated_capacity_ah: float
    nominal_voltage_v: float
    nominal_power_watts: float
    soh: float
    peukert_exponent: float
    ir_k_v_per_pp: float
    ir_reference_load_percent: float
    lut: FrozenLut
