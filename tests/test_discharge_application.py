"""Focused tests for the Phase 2 authoritative discharge transaction."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.battery_math import ScalarRLS
from src.capacity_estimator import CapacityEstimator
from src.discharge_handler import DischargeHandler
from src.discharge_types import CompletedDischarge
from src.model import BatteryModel, ModelLoadError


def _handler(tmp_path):
    model = BatteryModel(tmp_path / "model.json")
    config = SimpleNamespace(
        capacity_ah=7.2,
        runtime_threshold_minutes=0,
    )
    estimator = CapacityEstimator(
        peukert_exponent=model.get_peukert_exponent(),
        nominal_voltage=model.get_nominal_voltage(),
        nominal_power_watts=model.get_nominal_power_watts(),
        capacity_ah=model.get_capacity_ah(),
    )
    handler = DischargeHandler(
        battery_model=model,
        config=config,
        capacity_estimator=estimator,
        rls_peukert=ScalarRLS(theta=1.2),
        reference_load_percent=25.0,
        soh_threshold=0.8,
    )
    return handler, model


def _completion(
    *,
    event_id="event-1",
    evidence_class="controlled_capacity_test",
    eligible=True,
    reasons=(),
):
    return CompletedDischarge(
        event_id=event_id,
        lifecycle="closed_power_restored",
        evidence_class=evidence_class,
        voltages=(13.0, 12.0, 11.0),
        times=(0.0, 300.0, 900.0),
        loads=(30.0, 30.0, 30.0),
        model_processing_eligible=eligible,
        eligibility_reasons=tuple(reasons),
    )


def _calculation_patches():
    return (
        patch(
            "src.discharge_handler.soh_calculator.calculate_soh_from_discharge",
            return_value=(0.94, 7.2),
        ),
        patch(
            "src.capacity_estimator.CapacityEstimator.estimate",
            return_value=(6.8, 0.9, {"delta_soc_percent": 50.0, "duration_sec": 900}),
        ),
        patch("src.discharge_handler.calibrate_peukert", return_value=1.25),
    )


def test_partial_and_gapped_events_do_not_mutate_or_save(tmp_path):
    handler, model = _handler(tmp_path)
    before = deepcopy(model.state)

    for completion in (
        _completion(evidence_class="operational_partial", eligible=False),
        _completion(event_id="event-2", evidence_class="operational_gapped", eligible=False),
    ):
        with patch.object(model, "save", wraps=model.save) as save:
            result = handler.apply_completed_discharge(completion)
        assert result.status == "skipped"
        assert result.model_hash is None
        save.assert_not_called()
        assert model.state == before


def test_eligible_event_has_one_commit_and_all_authoritative_fields(tmp_path):
    handler, model = _handler(tmp_path)

    with _calculation_patches()[0], _calculation_patches()[1], _calculation_patches()[2]:
        with patch.object(model, "save", wraps=model.save) as save:
            result = handler.apply_completed_discharge(_completion())

    assert result.status == "applied"
    assert result.model_hash
    save.assert_called_once()
    event = model.state["discharge_events"][-1]
    assert event["event_id"] == "event-1"
    assert event["evidence_class"] == "controlled_capacity_test"
    assert model.state["soh"] == 0.94
    assert model.state["capacity_estimates"][-1]["metadata"]["event_id"] == "event-1"
    assert model.get_peukert_exponent() == pytest.approx(1.2254, abs=1e-4)


def test_soh_calculation_receives_isolated_candidate_model(tmp_path):
    handler, model = _handler(tmp_path)
    seen_models = []

    def calculate_soh(**kwargs):
        candidate_model = kwargs["battery_model"]
        seen_models.append(candidate_model)
        assert candidate_model is not model
        assert candidate_model.state is not model.state
        return 0.94, 7.2

    with (
        patch(
            "src.discharge_handler.soh_calculator.calculate_soh_from_discharge",
            side_effect=calculate_soh,
        ),
        patch(
            "src.capacity_estimator.CapacityEstimator.estimate",
            return_value=(6.8, 0.9, {"delta_soc_percent": 50.0, "duration_sec": 900}),
        ),
        patch("src.discharge_handler.calibrate_peukert", return_value=None),
    ):
        result = handler.apply_completed_discharge(_completion())

    assert result.status == "applied"
    assert len(seen_models) == 1


@pytest.mark.parametrize(
    ("voltages", "times", "loads", "reason"),
    (
        (
            (13.0, float("nan"), 11.0),
            (0.0, 300.0, 900.0),
            (30.0, 30.0, 30.0),
            "non_finite_voltage_sample",
        ),
        (
            (13.0, 12.0, 11.0),
            (0.0, float("inf"), 900.0),
            (30.0, 30.0, 30.0),
            "non_finite_time_sample",
        ),
        (
            (13.0, 12.0, 11.0),
            (0.0, 300.0, 900.0),
            (30.0, float("nan"), 30.0),
            "non_finite_load_sample",
        ),
        ((13.0, 12.0, 11.0), (0.0, 300.0, 200.0), (30.0, 30.0, 30.0), "non_increasing_time_series"),
    ),
)
def test_invalid_samples_are_skipped_without_save(tmp_path, voltages, times, loads, reason):
    handler, model = _handler(tmp_path)
    completion = replace(_completion(), voltages=voltages, times=times, loads=loads)
    before = deepcopy(model.state)

    with patch.object(model, "save", wraps=model.save) as save:
        result = handler.apply_completed_discharge(completion)

    assert result.status == "skipped"
    assert reason in result.eligibility_reasons
    save.assert_not_called()
    assert model.state == before


def test_duplicate_event_is_deduplicated_without_save(tmp_path):
    handler, model = _handler(tmp_path)
    with _calculation_patches()[0], _calculation_patches()[1], _calculation_patches()[2]:
        with patch.object(model, "save", wraps=model.save) as save:
            first = handler.apply_completed_discharge(_completion())
            second = handler.apply_completed_discharge(_completion())

    assert first.status == "applied"
    assert second.status == "already_applied"
    save.assert_called_once()
    assert len(model.state["discharge_events"]) == 1


def test_save_failure_rolls_back_model_and_handler_tracking(tmp_path):
    handler, model = _handler(tmp_path)
    before_state = deepcopy(model.state)
    before_physics = deepcopy(model.physics)
    before_rls = deepcopy(handler.rls_peukert)
    before_tracking = (
        handler.last_days_since_deep,
        handler.last_ir_trend_rate,
        handler.last_cycle_budget_remaining,
        handler.last_discharge_timestamp,
    )

    with _calculation_patches()[0], _calculation_patches()[1], _calculation_patches()[2]:
        with patch.object(model, "save", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                handler.apply_completed_discharge(_completion())

    assert model.state == before_state
    assert model.physics == before_physics
    assert handler.rls_peukert.__dict__ == before_rls.__dict__
    assert (
        handler.last_days_since_deep,
        handler.last_ir_trend_rate,
        handler.last_cycle_budget_remaining,
        handler.last_discharge_timestamp,
    ) == before_tracking
    assert not model.has_discharge_event("event-1")


def test_atomic_model_save_syncs_parent_directory(tmp_path):
    model = BatteryModel(tmp_path / "model.json")

    with patch("src.model._sync_parent_directory", wraps=lambda parent: None) as sync_parent:
        model_hash = model.save()

    sync_parent.assert_called_once_with(tmp_path)
    assert len(model_hash) == 64
    assert model.get_persisted_hash() == model_hash

    model.model_path.unlink()
    with pytest.raises(ModelLoadError):
        model.get_persisted_hash()


def test_post_commit_alert_failure_still_returns_applied(tmp_path):
    handler, model = _handler(tmp_path)

    with _calculation_patches()[0], _calculation_patches()[1], _calculation_patches()[2]:
        with patch.object(
            handler, "_check_alerts", side_effect=RuntimeError("journal unavailable")
        ):
            result = handler.apply_completed_discharge(_completion())

    assert result.status == "applied"
    assert model.has_discharge_event("event-1")


def test_post_commit_log_failure_still_returns_applied(tmp_path):
    handler, model = _handler(tmp_path)

    with _calculation_patches()[0], _calculation_patches()[1], _calculation_patches()[2]:
        with patch("src.discharge_handler.logger.info", side_effect=RuntimeError("logger down")):
            result = handler.apply_completed_discharge(_completion())

    assert result.status == "applied"
    assert model.has_discharge_event("event-1")
