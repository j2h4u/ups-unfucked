"""Factories for pure-domain tests."""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.battery_math.lut import LutPoint
from src.domain.values import FrozenModelSnapshot, PhysicalObservation


@pytest.fixture
def observation_factory():
    def make(second: float, **overrides: Any) -> PhysicalObservation:
        fields = {
            "voltage_v": 13.2,
            "load_percent": 20.0,
            "raw_status": "OB DISCHRG",
            "boot_id": "boot-a",
            "input_voltage_v": 0.0,
        }
        unknown = set(overrides).difference(fields)
        if unknown:
            raise TypeError(f"unknown observation fields: {sorted(unknown)}")
        fields.update(overrides)
        voltage_v = fields["voltage_v"]
        wall = datetime(2026, 8, 16, tzinfo=timezone.utc) + timedelta(seconds=second)
        raw = None if voltage_v is None else f"{voltage_v:.3f}"
        return PhysicalObservation(
            boot_id=fields["boot_id"],
            monotonic_ns=int(second * 1_000_000_000),
            wall_time_utc=wall,
            raw_status=fields["raw_status"],
            battery_voltage_raw=raw,
            battery_voltage_v=voltage_v,
            load_percent=fields["load_percent"],
            input_voltage_v=fields["input_voltage_v"],
        )

    return make


@pytest.fixture
def frozen_snapshot():
    return FrozenModelSnapshot(
        evaluation_revision="eval-1",
        scientific_fingerprint="f" * 64,
        rated_capacity_ah=7.2,
        nominal_voltage_v=12.0,
        nominal_power_watts=510.0,
        soh=1.0,
        peukert_exponent=1.2,
        ir_k_v_per_pp=0.015,
        ir_reference_load_percent=0.0,
        lut=(
            LutPoint(13.7, 1.0, "standard"),
            LutPoint(12.7, 0.7, "standard"),
            LutPoint(11.7, 0.3, "standard"),
            LutPoint(10.8, 0.0, "anchor"),
        ),
    )
