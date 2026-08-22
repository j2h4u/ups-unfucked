"""Focused tests for observation-first natural-blackout feedback."""

from datetime import datetime, timedelta, timezone

import pytest

import src.domain.model_feedback as model_feedback
from src.domain.model_feedback import (
    IRObservation,
    ModelFeedbackProposal,
    extract_ir_observation,
    propose_ir_cohort_feedback,
)
from src.domain.values import FrozenModelSnapshot


def _snapshot(k: float = 0.020) -> FrozenModelSnapshot:
    from src.battery_math.lut import LutPoint

    return FrozenModelSnapshot(
        rated_capacity_ah=7.2,
        nominal_voltage_v=12.0,
        nominal_power_watts=510.0,
        soh=1.0,
        peukert_exponent=1.2,
        ir_k_v_per_pp=k,
        ir_reference_load_percent=20.0,
        lut=(LutPoint(13.7, 1.0), LutPoint(10.8, 0.0)),
    )


def _rows(
    *,
    cadence_s: float = 1.0,
    delta_voltage: float = -0.4,
    delta_load: float = 20.0,
) -> list[dict[str, object]]:
    start = datetime(2026, 8, 22, tzinfo=timezone.utc)
    pre_points = 31 if cadence_s == 1.0 else 3
    post_points = pre_points
    rows: list[dict[str, object]] = [
        {
            "at": (start - timedelta(seconds=cadence_s)).isoformat().replace("+00:00", "Z"),
            "battery_v": 12.6,
            "load_pct": 20.0,
            "status": "OL",
        }
    ]
    for index in range(pre_points + post_points):
        after_step = index >= pre_points
        seconds = index * cadence_s
        baseline = 12.6 - 0.001 * seconds
        rows.append(
            {
                "at": (start + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z"),
                "battery_v": baseline + (delta_voltage if after_step else 0.0),
                "load_pct": 20.0 + (delta_load if after_step else 0.0),
                "input_v": 0.0,
                "status": "OB DISCHRG",
            }
        )
    rows.append(
        {
            "at": (start + timedelta(seconds=(pre_points + post_points) * cadence_s))
            .isoformat()
            .replace("+00:00", "Z"),
            "battery_v": 12.0,
            "load_pct": 40.0,
            "status": "OL CHRG",
        }
    )
    return rows


def _observation(event_at: str, estimate: float) -> IRObservation:
    return IRObservation(
        event_at=event_at,
        estimate=estimate,
        evidence_at=event_at,
        uncertainty=0.0025,
        reason="test observation",
    )


def test_one_observation_is_only_an_observation() -> None:
    observation = extract_ir_observation(_rows(), event_at="2026-08-22T00:00:00Z")

    assert isinstance(observation, IRObservation)
    assert propose_ir_cohort_feedback((), observation, _snapshot()) is None


def test_two_conflicting_observations_produce_no_proposal() -> None:
    observations = (
        _observation("2026-08-22T00:00:00Z", 0.017),
        _observation("2026-08-23T00:00:00Z", 0.022),
    )

    assert propose_ir_cohort_feedback(observations, None, _snapshot()) is None


def test_three_consistent_observations_use_median_and_bound_decrement() -> None:
    observations = (
        _observation("2026-08-22T00:00:00Z", 0.010),
        _observation("2026-08-23T00:00:00Z", 0.011),
    )
    new = _observation("2026-08-24T00:00:00Z", 0.0105)

    proposal = propose_ir_cohort_feedback(observations, new, _snapshot(0.020))

    assert isinstance(proposal, ModelFeedbackProposal)
    assert proposal.to_value == pytest.approx(0.018)
    assert proposal.field == "physics.ir_compensation.k_volts_per_percent"


def test_replayed_event_does_not_count_twice() -> None:
    duplicate = _observation("2026-08-22T00:00:00Z", 0.010)
    saved = (duplicate, duplicate, _observation("2026-08-23T00:00:00Z", 0.010))

    assert propose_ir_cohort_feedback(saved, None, _snapshot()) is None


def test_quantization_uncertainty_above_conservative_bound_is_rejected() -> None:
    observations = (
        _observation("2026-08-22T00:00:00Z", 0.010),
        _observation("2026-08-23T00:00:00Z", 0.010),
        _observation("2026-08-24T00:00:00Z", 0.010),
    )
    uncertain = tuple(
        IRObservation(item.event_at, item.estimate, item.evidence_at, 0.004, item.reason)
        for item in observations
    )

    assert propose_ir_cohort_feedback(uncertain[:2], uncertain[2], _snapshot()) is None


def test_plateau_extraction_is_equivalent_at_one_and_eleven_seconds() -> None:
    one_second = extract_ir_observation(_rows(cadence_s=1.0))
    eleven_seconds = extract_ir_observation(_rows(cadence_s=11.0))

    assert one_second is not None
    assert eleven_seconds is not None
    assert one_second.estimate == pytest.approx(eleven_seconds.estimate)


def test_quantized_noise_does_not_become_an_observation() -> None:
    rows = _rows(delta_voltage=-0.1)

    assert extract_ir_observation(rows) is None


def test_soh_estimators_and_mutation_apis_are_absent() -> None:
    assert not hasattr(model_feedback, "propose_soh_feedback")
    assert not hasattr(model_feedback, "soh_feedback")
    assert not hasattr(ModelFeedbackProposal, "soh")
