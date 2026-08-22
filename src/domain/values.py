"""Small immutable values shared by the live safety and model-load paths."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite

from src.battery_math.lut import FrozenLut


class BlackoutKind(StrEnum):
    ONLINE = "online"
    BLACKOUT_REAL = "blackout_real"
    BLACKOUT_TEST = "blackout_test"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class IrLearningPolicy:
    revision: str
    deadband_v_per_pp: float
    min_k_v_per_pp: float
    max_k_v_per_pp: float
    max_single_commit_fraction: float
    max_epoch_decrease_fraction: float
    min_commit_interval_days: int
    max_consumed_step_hashes: int

    def __post_init__(self) -> None:
        values = (
            self.deadband_v_per_pp,
            self.min_k_v_per_pp,
            self.max_k_v_per_pp,
            self.max_single_commit_fraction,
            self.max_epoch_decrease_fraction,
        )
        if not isinstance(self.revision, str) or not self.revision.strip():
            raise ValueError("learning policy revision must be a non-empty string")
        if any(isinstance(value, bool) or not isfinite(float(value)) for value in values):
            raise ValueError("learning policy numeric values must be finite")
        if self.deadband_v_per_pp < 0.0 or self.min_k_v_per_pp <= 0.0:
            raise ValueError("learning policy IR bounds are invalid")
        if self.max_k_v_per_pp < self.min_k_v_per_pp:
            raise ValueError("learning policy IR bounds are invalid")
        if not 0.0 <= self.max_single_commit_fraction <= 1.0:
            raise ValueError("single commit fraction must be between zero and one")
        if not 0.0 <= self.max_epoch_decrease_fraction <= 1.0:
            raise ValueError("epoch decrease fraction must be between zero and one")
        if not isinstance(self.min_commit_interval_days, int) or self.min_commit_interval_days < 0:
            raise ValueError("learning policy commit interval must be a nonnegative integer")
        if not isinstance(self.max_consumed_step_hashes, int) or self.max_consumed_step_hashes <= 0:
            raise ValueError("learning policy evidence budget must be a positive integer")


DEFAULT_IR_LEARNING_POLICY = IrLearningPolicy(
    "ir-learning-v1", 0.001, 0.005, 0.040, 0.20, 0.50, 30, 256
)


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
    evaluation_revision: str
    scientific_fingerprint: str
    rated_capacity_ah: float
    nominal_voltage_v: float
    nominal_power_watts: float
    soh: float
    peukert_exponent: float
    ir_k_v_per_pp: float
    ir_reference_load_percent: float
    lut: FrozenLut
