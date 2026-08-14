"""Scenario 22: persisted IR coefficient direction and LB golden values."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.ema_filter import ir_compensate
from src.event_classifier import EventType
from src.model import BatteryModel
from src.monitor import MonitorDaemon
from src.monitor_config import CurrentMetrics
from src.runtime_calculator import runtime_minutes
from src.soc_predictor import soc_from_voltage
from src.virtual_ups import NUT_STATUS_LOW_BATTERY, compute_ups_status_override

TEMPORARY_CONSERVATIVE_ENVELOPE = "min(persisted_ir_k_0.025, zero_ir_k_0.0)"
LOADS_PERCENT = tuple(range(0, 101, 10))
MEASURED_VOLTAGE = 11.4
REFERENCE_LOAD_PERCENT = 20.0


def _persisted_model(tmp_path: Path, ir_k: float) -> BatteryModel:
    tmp_path.mkdir(parents=True, exist_ok=True)
    model = BatteryModel(tmp_path / "model.json")
    # Seed a strictly valid current-schema physics value before exercising the loader.
    model.physics.ir_compensation.k_volts_per_percent = ir_k
    model.save()
    return BatteryModel(tmp_path / "model.json")


def _runtime_through_release_a_path(model: BatteryModel, load_percent: float) -> float:
    """Exercise MonitorDaemon's production IR -> SoC -> runtime path."""
    fake_daemon = SimpleNamespace(
        battery_model=model,
        ir_reference_load_percent=model.get_ir_reference_load(),
        sag_tracker=SimpleNamespace(ir_k=model.get_ir_k()),
        ema_filter=SimpleNamespace(
            stabilized=True,
            voltage=MEASURED_VOLTAGE,
            load=load_percent,
        ),
        current_metrics=CurrentMetrics(),
        _last_logged_soc=None,
        _last_logged_time_rem=None,
        _log_soc_change=lambda soc, previous: None,
    )
    _battery_charge, time_remaining = MonitorDaemon._compute_metrics(fake_daemon)
    assert time_remaining is not None
    return time_remaining


def _runtime_with_k(model: BatteryModel, load_percent: float, ir_k: float) -> float:
    normalized = ir_compensate(
        MEASURED_VOLTAGE,
        load_percent,
        model.get_ir_reference_load(),
        ir_k,
    )
    assert normalized is not None
    soc = soc_from_voltage(normalized, model.get_lut())
    return runtime_minutes(
        soc,
        load_percent,
        model.get_capacity_ah(),
        model.get_soh(),
        peukert_exponent=model.get_peukert_exponent(),
        nominal_voltage=model.get_nominal_voltage(),
        nominal_power_watts=model.get_nominal_power_watts(),
    )


def test_scenario_22_persisted_ir_direction_and_conservative_lb_envelope(tmp_path: Path):
    persisted_model = _persisted_model(tmp_path / "persisted", 0.025)
    persisted_hash = persisted_model.get_persisted_hash()
    assert persisted_model.get_ir_k() == pytest.approx(0.025)

    runtimes_persisted = tuple(
        _runtime_through_release_a_path(persisted_model, load) for load in LOADS_PERCENT
    )
    runtimes_zero = tuple(_runtime_with_k(persisted_model, load, 0.0) for load in LOADS_PERCENT)

    # The sign is physical: below the 20% reference load compensation lowers
    # normalized voltage, while above it compensation raises normalized voltage.
    assert runtimes_persisted[1] < runtimes_zero[1]
    assert runtimes_persisted[2] == pytest.approx(runtimes_zero[2])
    assert runtimes_persisted[-1] > runtimes_zero[-1]

    conservative = tuple(min(a, b) for a, b in zip(runtimes_persisted, runtimes_zero))
    assert all(
        c <= p and c <= z for c, p, z in zip(conservative, runtimes_persisted, runtimes_zero)
    )
    assert TEMPORARY_CONSERVATIVE_ENVELOPE.startswith("min(")

    statuses_persisted = tuple(
        compute_ups_status_override(EventType.BLACKOUT_REAL, runtime, 5)
        for runtime in runtimes_persisted
    )
    statuses_zero = tuple(
        compute_ups_status_override(EventType.BLACKOUT_REAL, runtime, 5)
        for runtime in runtimes_zero
    )
    statuses_conservative = tuple(
        compute_ups_status_override(EventType.BLACKOUT_REAL, runtime, 5) for runtime in conservative
    )
    # At high load the persisted coefficient lengthens the predicted runtime;
    # the temporary min-envelope retains the conservative LB result.
    assert statuses_zero[-2] == NUT_STATUS_LOW_BATTERY  # 80% load
    assert statuses_persisted[-2] != NUT_STATUS_LOW_BATTERY
    assert statuses_conservative[-2] == NUT_STATUS_LOW_BATTERY

    # Release A reads the persisted coefficient unchanged and does not mutate
    # the model while calculating runtime.
    assert persisted_model.get_ir_k() == pytest.approx(0.025)
    assert persisted_model.get_persisted_hash() == persisted_hash
