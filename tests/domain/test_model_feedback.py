"""Focused tests for proposal-only natural-blackout feedback."""

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.model_feedback import (
    ModelFeedbackProposal,
    propose_model_feedback,
    propose_soh_feedback,
)
from src.domain.values import FrozenModelSnapshot


def _snapshot(k: float = 0.010, *, soh: float = 1.0) -> FrozenModelSnapshot:
    from src.battery_math.lut import LutPoint

    return FrozenModelSnapshot(
        rated_capacity_ah=7.2,
        nominal_voltage_v=12.0,
        nominal_power_watts=510.0,
        soh=soh,
        peukert_exponent=1.2,
        ir_k_v_per_pp=k,
        ir_reference_load_percent=20.0,
        lut=(LutPoint(13.7, 1.0), LutPoint(10.8, 0.0)),
    )


def _rows(*, status: str = "OB DISCHRG", input_v: float = 0.0) -> list[dict[str, object]]:
    start = datetime(2026, 8, 22, tzinfo=timezone.utc)
    rows: list[dict[str, object]] = [
        {
            "at": (start - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            "battery_v": 12.6,
            "battery_pct": 100.0,
            "runtime_s": 1000.0,
            "load_pct": 20.0,
            "input_v": 230.0,
            "output_v": 230.0,
            "status": "OL",
        }
    ]
    for index in range(12):
        load = 20.0 if index < 6 else 40.0
        voltage = 12.6 - 0.001 * index - (0.3 if index >= 6 else 0.0)
        rows.append(
            {
                "at": (start + timedelta(seconds=index)).isoformat().replace("+00:00", "Z"),
                "battery_v": voltage,
                "battery_pct": 100.0 - index,
                "runtime_s": 1000.0 - index,
                "load_pct": load,
                "input_v": input_v,
                "output_v": 230.0,
                "status": status,
            }
        )
    rows.append(
        {
            "at": (start + timedelta(seconds=12)).isoformat().replace("+00:00", "Z"),
            "battery_v": 12.2,
            "battery_pct": 90.0,
            "runtime_s": 900.0,
            "load_pct": 40.0,
            "input_v": 230.0,
            "output_v": 230.0,
            "status": "OL CHRG",
        }
    )
    return rows


def _curve_rows(
    *, start_voltage: float = 12.8, end_voltage: float = 11.5, load: float = 20.0
) -> list[dict[str, object]]:
    start = datetime(2026, 8, 22, tzinfo=timezone.utc)
    rows: list[dict[str, object]] = []
    for index in range(301):
        voltage = start_voltage + (end_voltage - start_voltage) * index / 300.0
        rows.append(
            {
                "at": (start + timedelta(seconds=index)).isoformat().replace("+00:00", "Z"),
                "battery_v": voltage,
                "battery_pct": 100.0 - index,
                "runtime_s": 1000.0 - index,
                "load_pct": load,
                "input_v": 0.0,
                "output_v": 230.0,
                "status": "OB DISCHRG" + (" LB" if index >= 290 else ""),
            }
        )
    return rows


def test_natural_load_step_proposes_only_bounded_ir_correction() -> None:
    proposal = propose_model_feedback(_rows(), _snapshot(0.020))

    assert isinstance(proposal, ModelFeedbackProposal)
    assert proposal.to_value == pytest.approx(0.018)
    assert proposal.evidence_at == "2026-08-22T00:00:06Z"
    assert "natural blackout" in proposal.reason
    assert proposal.field == "physics.ir_compensation.k_volts_per_percent"


def test_model_derived_percentage_and_runtime_do_not_change_proposal() -> None:
    baseline = propose_model_feedback(_rows(), _snapshot(0.020))
    altered = _rows()
    for row in altered:
        row["battery_pct"] = 1.0
        row["runtime_s"] = 1.0

    assert propose_model_feedback(altered, _snapshot(0.020)) == baseline


def test_self_test_and_open_or_censored_events_are_rejected() -> None:
    self_test = propose_model_feedback(_rows(status="CAL DISCHRG", input_v=230.0), _snapshot())
    open_event = _rows()[:-1]
    censored = _rows(status="OB DISCHRG LB")

    assert self_test is None
    assert propose_model_feedback(open_event, _snapshot()) is None
    assert propose_model_feedback(censored, _snapshot()) is None


def test_clean_step_before_later_low_battery_sample_remains_usable() -> None:
    rows = _rows()
    rows[9]["status"] = "OB DISCHRG LB"

    proposal = propose_model_feedback(rows, _snapshot(0.020))

    assert proposal is not None
    assert proposal.to_value == pytest.approx(0.018)


def test_upward_estimate_is_observation_only() -> None:
    rows = _rows()
    for row in rows:
        if "OB" in str(row["status"]).split() and str(row["at"]) >= "2026-08-22T00:00:06Z":
            voltage = row["battery_v"]
            assert isinstance(voltage, (int, float))
            row["battery_v"] = voltage + 0.3

    assert propose_model_feedback(rows, _snapshot(0.010)) is None


def test_weak_or_non_step_evidence_returns_no_proposal() -> None:
    rows = _rows()
    for row in rows:
        if "OB" in str(row["status"]).split():
            row["load_pct"] = 20.0

    assert propose_model_feedback(rows, _snapshot()) is None


def test_no_capacity_soh_or_peukert_update_is_exposed() -> None:
    proposal = propose_model_feedback(_rows(), _snapshot(0.020))

    assert proposal is not None
    assert not hasattr(proposal, "capacity_ah")
    assert not hasattr(proposal, "soh")
    assert not hasattr(proposal, "peukert_exponent")


def test_censored_curve_proposes_bounded_downward_soh() -> None:
    proposal = propose_model_feedback(_curve_rows(), _snapshot(soh=0.80))

    assert isinstance(proposal, ModelFeedbackProposal)
    assert proposal.field == "soh"
    assert proposal.to_value == pytest.approx(0.75)
    assert proposal.evidence_at == "2026-08-22T00:05:00Z"
    assert "discharge curve" in proposal.reason


def test_soh_curve_ignores_model_fields_and_later_online_gap() -> None:
    baseline = propose_soh_feedback(_curve_rows(), _snapshot(soh=0.80))
    altered = _curve_rows()
    for row in altered:
        row["battery_pct"] = 1.0
        row["runtime_s"] = 1.0
    altered.append(
        {
            "at": "2026-08-22T01:00:00Z",
            "battery_v": 13.5,
            "battery_pct": 100.0,
            "runtime_s": 1000.0,
            "load_pct": 20.0,
            "input_v": 230.0,
            "output_v": 230.0,
            "status": "OL CHRG",
        }
    )

    assert propose_soh_feedback(altered, _snapshot(soh=0.80)) == baseline


def test_soh_curve_rejects_cal_and_out_of_range_load() -> None:
    cal = _curve_rows()
    for row in cal:
        row["status"] = "CAL DISCHRG"
    too_high = _curve_rows(load=51.0)

    assert propose_soh_feedback(cal, _snapshot(soh=0.80)) is None
    assert propose_soh_feedback(too_high, _snapshot(soh=0.80)) is None
