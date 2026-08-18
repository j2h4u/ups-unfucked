"""Frozen Slice 1 IR evidence, learning, and model-commit contract."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

from src.adapters import model_state_schema
from src.adapters.jsonl_event_store import JsonlEventStore
from src.adapters.jsonl_record_codec import canonical_json_bytes
from src.adapters.model_owner import ModelOwner
from src.application.assessment_codec import json_value, step_hash
from src.application.assessment_worker import AssessmentWorker, CloseRequest, PreparedClose
from src.application.close_blackout import CloseResult, close_blackout
from src.application.storage_values import EventRecord, EventRef, EventStart, ProcessingRef
from src.domain.fragment_policy import DEFAULT_DISCHARGE_FRAGMENT_POLICY
from src.domain.fragments import (
    CanonicalDischargeSample,
    DischargeSlice,
    LoadStepObservation,
    ObservationOrigin,
    validate_canonical_sample_span,
)
from src.domain.ir_identification import (
    CohortStep,
    IrCohortContext,
    IrRawObservation,
    identify_load_steps,
    select_ir_cohort,
)
from src.domain.learning import IrLearningContext, evaluate_ir_learning
from src.domain.values import (
    DEFAULT_IR_LEARNING_POLICY,
    ModelChange,
    PhysicalObservation,
    StepQuality,
    TerminalDisposition,
)

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "slice1"
GOLDEN = json.loads((FIXTURE_DIR / "golden.json").read_text())
EPOCH_ID = GOLDEN["event"]["battery_epoch_id"]


def _assert_serialized(value, expected: dict[str, str]) -> None:
    actual = canonical_json_bytes(json_value(value))
    assert actual == expected["bytes"].encode("utf-8")
    assert hashlib.sha256(actual).hexdigest() == expected["sha256"]


def _raw_step(upward: bool, k_v_per_pp: float) -> tuple[IrRawObservation, ...]:
    raw = GOLDEN["synthetic_raw"]
    pre_load = raw["pre_load_percent"] if upward else raw["post_load_percent"]
    post_load = raw["post_load_percent"] if upward else raw["pre_load_percent"]
    transition = raw["transition_second"]
    return tuple(
        IrRawObservation(
            sequence=second,
            boot_id=raw["boot_id"],
            monotonic_ns=second * 1_000_000_000,
            raw_status=raw["raw_status"],
            battery_voltage_v=(
                13.5
                - raw["drift_v_per_s"] * second
                - k_v_per_pp * (pre_load if second < transition else post_load)
            ),
            voltage_token_quantum_v=raw["voltage_token_quantum_v"],
            load_percent=pre_load if second < transition else post_load,
        )
        for second in range(raw["sample_count"])
    )


def _cohort(k_v_per_pp: float):
    event = GOLDEN["event"]
    directions = event["step_directions"]

    def make_step(index: int, blackout_id: str, segment_id: str, direction: str) -> CohortStep:
        estimate = identify_load_steps(
            blackout_id,
            segment_id,
            _raw_step(direction == "up", k_v_per_pp),
        )[0]
        return CohortStep(
            estimate,
            EPOCH_ID,
            event["evaluation_revision"],
            datetime(2026, 8, 1 + index, tzinfo=timezone.utc),
            step_hash(estimate),
        )

    steps = tuple(
        make_step(index, blackout_id, segment_id, direction)
        for index, (blackout_id, segment_id, direction) in enumerate(
            zip(
                event["blackout_ids"],
                event["segment_ids"],
                directions,
                strict=True,
            )
        )
    )
    selection = select_ir_cohort(
        steps,
        IrCohortContext(
            current_blackout_id=event["blackout_ids"][-1],
            battery_epoch_id=EPOCH_ID,
            evaluation_revision=event["evaluation_revision"],
            consumed_step_hashes=frozenset(),
            projection_available=True,
            candidate_event_overflow=0,
            learning_policy=DEFAULT_IR_LEARNING_POLICY,
        ),
    )
    return steps, selection


def _learning(selection, *, current_k: float = 0.015):
    event = GOLDEN["event"]
    return evaluate_ir_learning(
        selection.estimate,
        IrLearningContext(
            current_blackout_id=event["blackout_ids"][-1],
            current_blackout_step_count=1,
            battery_epoch_id=EPOCH_ID,
            current_k_v_per_pp=current_k,
            epoch_initial_k_v_per_pp=0.015,
            reference_load_percent=0.0,
            current_utc=datetime(2026, 8, 18, tzinfo=timezone.utc),
            previous_commit_utc=None,
            candidate_step_hashes=selection.consumed_step_hashes,
            consumed_step_hashes=frozenset(),
            learning_policy=DEFAULT_IR_LEARNING_POLICY,
        ),
    )


def _oracle(before, after):
    assert after.ir_reference_load_percent == 0.0
    return after.ir_k_v_per_pp <= before.ir_k_v_per_pp, GOLDEN["model"]["prepared_safety_oracle"]


def _assert_step_estimates(steps) -> None:
    expected_steps = GOLDEN["steps"]
    assert [step.estimate.step_id for step in steps] == expected_steps["step_ids"]
    for index, step in enumerate(steps):
        estimate = step.estimate
        _assert_serialized(estimate, GOLDEN["serialized"]["estimates"][index])
        assert step.step_record_hash == GOLDEN["event"]["step_record_hashes"][index]
        assert estimate.pre_sequences == tuple(expected_steps["pre_sequences"])
        assert estimate.post_sequences == tuple(expected_steps["post_sequences"])
        assert estimate.transition_monotonic_ns == expected_steps["transition_monotonic_ns"]
        assert estimate.delta_load_pp == expected_steps["delta_load_pp"][index]
        assert estimate.pre_slope_v_per_s == expected_steps["pre_slope_v_per_s"][index]
        assert (
            estimate.early_post_slope_v_per_s == expected_steps["early_post_slope_v_per_s"][index]
        )
        assert estimate.late_post_slope_v_per_s == expected_steps["late_post_slope_v_per_s"][index]
        assert (
            estimate.early_delta_voltage_at_transition_v
            == expected_steps["early_delta_voltage_at_transition_v"][index]
        )
        assert (
            estimate.settled_delta_voltage_at_transition_v
            == expected_steps["settled_delta_voltage_at_transition_v"][index]
        )
        assert estimate.voltage_quantum_v == expected_steps["voltage_quantum_v"]
        assert estimate.k_transition_v_per_pp == expected_steps["k_transition_v_per_pp"][index]
        assert estimate.k_settled_v_per_pp == expected_steps["k_settled_v_per_pp"][index]
        assert estimate.quality == StepQuality(expected_steps["quality"])
        assert tuple(estimate.reasons.values) == tuple(expected_steps["reasons"])


def _assert_cohort_selection(selection) -> None:
    expected_cohort = GOLDEN["cohort"]
    estimate = selection.estimate
    assert estimate.blackout_ids == tuple(expected_cohort["blackout_ids"])
    assert estimate.step_count == expected_cohort["step_count"]
    assert estimate.up_step_count == expected_cohort["up_step_count"]
    assert estimate.down_step_count == expected_cohort["down_step_count"]
    assert estimate.median_k_v_per_pp == expected_cohort["median_k_v_per_pp"]
    assert estimate.mad_ratio == expected_cohort["mad_ratio"]
    assert tuple(estimate.reasons.values) == tuple(expected_cohort["reasons"])
    assert selection.consumed_step_hashes == tuple(expected_cohort["consumed_step_hashes"])
    _assert_serialized(estimate, GOLDEN["serialized"]["cohort"])


def _assert_learning_result(learning) -> ModelChange:
    expected_learning = GOLDEN["learning"]
    _assert_serialized(learning, GOLDEN["serialized"]["learning"])
    assert learning.evidence_set_id == expected_learning["evidence_set_id"]
    assert tuple(learning.reasons.values) == tuple(expected_learning["reasons"])
    assert learning.observed_load_sag_increase is None
    expected_change = expected_learning["change"]
    expected = ModelChange(
        parameter=expected_change["parameter"],
        value_before=expected_change["value_before"],
        measured_estimate=expected_change["measured_estimate"],
        value_after=expected_change["value_after"],
        evidence_hashes=tuple(expected_change["evidence_hashes"]),
        bound_applied=expected_change["bound_applied"],
    )
    assert learning.change == expected
    assert learning.change is not None
    _assert_serialized(learning.change, GOLDEN["serialized"]["change"])
    return learning.change


def _assert_prepared(prepared, change, model) -> None:
    _assert_serialized(prepared, GOLDEN["serialized"]["prepared"])
    assert prepared.change == change
    assert prepared.model_hash_before == model["model_before_sha256"]
    assert prepared.expected_model_hash_after == model["model_after_sha256"]
    assert prepared.expected_scientific_fingerprint_after == model["scientific_fingerprint_after"]


def _assert_receipt(receipt, model) -> None:
    _assert_serialized(receipt, GOLDEN["serialized"]["receipt"])
    expected = model["receipt"]
    assert receipt.blackout_id == expected["blackout_id"]
    assert receipt.parameter == expected["parameter"]
    assert receipt.value_before == expected["value_before"]
    assert receipt.measured_estimate == expected["measured_estimate"]
    assert receipt.value_after == expected["value_after"]
    assert receipt.model_hash_before == model["model_before_sha256"]
    assert receipt.model_hash_after == model["model_after_sha256"]
    assert receipt.scientific_fingerprint_before == model["scientific_fingerprint_before"]
    assert receipt.scientific_fingerprint_after == model["scientific_fingerprint_after"]
    assert receipt.evidence_set_id == expected["evidence_set_id"]
    assert receipt.consumed_step_hashes == tuple(expected["consumed_step_hashes"])
    assert receipt.reference_reparameterization is expected["reference_reparameterization"]
    assert receipt.safety_oracle == expected["safety_oracle"]


def _assert_committed_model(change) -> None:
    model = GOLDEN["model"]
    before_path = FIXTURE_DIR / "model-before.json"
    after_state = json.loads((FIXTURE_DIR / "model-after.json").read_text())
    expected_after_bytes = model_state_schema.canonical_json(after_state).encode("utf-8")
    with TemporaryDirectory() as directory:
        model_path = Path(directory) / "model.json"
        copyfile(before_path, model_path)
        owner = ModelOwner(model_path, safety_oracle=_oracle)
        try:
            assert (
                hashlib.sha256(model_path.read_bytes()).hexdigest() == model["model_before_sha256"]
            )
            prepared = owner.prepare_commit(
                change,
                blackout_id=model["blackout_id"],
                committed_at=datetime.fromisoformat(model["commit_time"].replace("Z", "+00:00")),
            )
            _assert_prepared(prepared, change, model)
            receipt = owner.commit_prepared(prepared)
            _assert_receipt(receipt, model)
            assert model_path.read_bytes() == expected_after_bytes
            assert (
                hashlib.sha256(model_path.read_bytes()).hexdigest() == model["model_after_sha256"]
            )
        finally:
            owner.close()


def _pipeline_observations(
    *,
    day: int,
    upward: bool,
    k_v_per_pp: float,
) -> tuple[PhysicalObservation, ...]:
    raw = GOLDEN["synthetic_raw"]
    pre_load = raw["pre_load_percent"] if upward else raw["post_load_percent"]
    post_load = raw["post_load_percent"] if upward else raw["pre_load_percent"]
    event_time = datetime(2026, 8, 1 + day, tzinfo=timezone.utc)
    return tuple(
        PhysicalObservation(
            boot_id=f"boot-{day}",
            monotonic_ns=second * 1_000_000_000,
            wall_time_utc=event_time.replace(microsecond=0) + timedelta(seconds=second),
            raw_status=raw["raw_status"],
            battery_voltage_raw=(
                f"{13.5 - raw['drift_v_per_s'] * second - k_v_per_pp * (pre_load if second < raw['transition_second'] else post_load):.2f}"
            ),
            battery_voltage_v=(
                13.5
                - raw["drift_v_per_s"] * second
                - k_v_per_pp * (pre_load if second < raw["transition_second"] else post_load)
            ),
            voltage_token_quantum_v=raw["voltage_token_quantum_v"],
            load_percent=pre_load if second < raw["transition_second"] else post_load,
            input_voltage_v=0.0,
        )
        for second in range(raw["sample_count"])
    )


def _fragment_step(day: int, estimate) -> LoadStepObservation:
    event = GOLDEN["event"]
    raw = GOLDEN["synthetic_raw"]
    observations = _pipeline_observations(
        day=day,
        upward=event["step_directions"][day] == "up",
        k_v_per_pp=raw["k_v_per_pp"],
    )
    samples = tuple(
        CanonicalDischargeSample(
            sequence,
            hashlib.sha256(canonical_json_bytes(json_value(observation))).hexdigest(),
            observation,
        )
        for sequence, observation in enumerate(observations)
    )
    parent = DischargeSlice(
        samples=samples,
        blackout_id=event["blackout_ids"][day],
        physical_episode_id=f"physical-episode-{day}",
        battery_epoch_id=event["battery_epoch_id"],
        segment_id=event["segment_ids"][day],
        origin=ObservationOrigin.NATURAL,
        policy_revision=DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision,
    )
    return LoadStepObservation(
        estimate,
        tuple(
            samples[sequence].canonical_hash
            for sequence in (*estimate.pre_sequences, *estimate.post_sequences)
        ),
        parent,
    )


def _assert_fragment_linkage(steps) -> None:
    expected_parents = GOLDEN["fragment"]["parents"]
    expected_steps = GOLDEN["steps"]
    for index, step in enumerate(steps):
        linked = _fragment_step(index, step.estimate)
        span = linked.parent_slice.spans[0]
        expected = expected_parents[index]
        assert linked.parent_slice.blackout_id == expected["blackout_id"]
        assert linked.parent_slice.segment_id == expected["segment_id"]
        assert span.sample_count == expected["sample_count"]
        assert span.first_sequence == expected["first_sequence"]
        assert span.last_sequence == expected["last_sequence"]
        assert span.first_sample_hash == expected["first_sample_hash"]
        assert span.last_sample_hash == expected["last_sample_hash"]
        assert span.ordered_sample_hashes_sha256 == expected["ordered_sample_hashes_sha256"]
        assert linked.parent_slice.slice_id == expected["slice_id"]
        validate_canonical_sample_span(span, linked.parent_slice.samples)
        expected_hashes = tuple(
            linked.parent_slice.samples[sequence].canonical_hash
            for sequence in (*step.estimate.pre_sequences, *step.estimate.post_sequences)
        )
        assert linked.contributing_sample_hashes == expected_hashes
        _assert_serialized(linked.estimate, GOLDEN["serialized"]["estimates"][index])
        assert linked.step_record_hash == expected["step_record_hash"]
        assert linked.estimate.pre_sequences == tuple(expected_steps["pre_sequences"])
        assert linked.estimate.post_sequences == tuple(expected_steps["post_sequences"])


@dataclass(frozen=True, slots=True)
class _PipelineResult:
    store: JsonlEventStore
    owner: ModelOwner
    model_path: Path
    processing: ProcessingRef
    prepared: PreparedClose
    result: CloseResult


def _run_pipeline(tmp_path: Path, *, k_v_per_pp: float) -> _PipelineResult:
    event = GOLDEN["event"]
    model = GOLDEN["model"]
    model_path = tmp_path / "model.json"
    copyfile(FIXTURE_DIR / "model-before.json", model_path)
    owner = ModelOwner(model_path, safety_oracle=_oracle)
    store = JsonlEventStore(tmp_path)
    worker = AssessmentWorker(store, owner)
    worker.after_first_safety_publication()
    try:
        for day, (blackout_id, segment_id, direction) in enumerate(
            zip(event["blackout_ids"], event["segment_ids"], event["step_directions"], strict=True)
        ):
            observations = _pipeline_observations(
                day=day,
                upward=direction == "up",
                k_v_per_pp=k_v_per_pp,
            )
            snapshot = owner.current_snapshot()
            handle = store.open(
                EventStart(
                    blackout_id,
                    segment_id,
                    observations[0].boot_id,
                    observations[0].wall_time_utc.isoformat().replace("+00:00", "Z"),
                    observations[0].monotonic_ns,
                    {
                        "observation": json_value(observations[0]),
                        "frozen_model": json_value(snapshot),
                        "charge_readiness": {"ready": True},
                        "battery_epoch_id": snapshot.battery_epoch_id,
                        "evaluation_revision": snapshot.evaluation_revision,
                    },
                )
            )
            for observation in observations[1:]:
                handle = store.append(
                    handle,
                    EventRecord(
                        "observation",
                        observation.boot_id,
                        observation.wall_time_utc.isoformat().replace("+00:00", "Z"),
                        observation.monotonic_ns,
                        json_value(observation),
                        "physical",
                    ),
                )
            store.append(
                handle,
                EventRecord(
                    "end",
                    observations[-1].boot_id,
                    (observations[-1].wall_time_utc + timedelta(seconds=1))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    observations[-1].monotonic_ns + 1_000_000_000,
                    {"termination": event["termination"]},
                    "physical",
                ),
            )
            processing = next(
                item
                for item in store.work_registry().pending_processing
                if item.blackout_id == blackout_id
            )
            prepared = worker.prepare(CloseRequest(processing))
            result = close_blackout(store, owner, prepared)
        assert processing.blackout_id == model["blackout_id"]
        return _PipelineResult(store, owner, model_path, processing, prepared, result)
    except BaseException:
        owner.close()
        store.close()
        raise


def test_slice1_power_restored_golden_freezes_ir_to_model_commit() -> None:
    event = GOLDEN["event"]
    assert event["observation_origin"] == "natural"
    assert event["termination"] == "power_restored"
    steps, selection = _cohort(GOLDEN["synthetic_raw"]["k_v_per_pp"])
    _assert_step_estimates(steps)
    _assert_fragment_linkage(steps)
    _assert_cohort_selection(selection)
    change = _assert_learning_result(_learning(selection))
    _assert_committed_model(change)


def test_slice1_upward_ir_observation_is_a_zero_write_refusal() -> None:
    _, selection = _cohort(GOLDEN["refusal"]["k_v_per_pp"])
    learning = _learning(selection)
    expected = GOLDEN["refusal"]
    assert learning.change is None
    assert learning.evidence_set_id == expected["evidence_set_id"]
    assert tuple(learning.reasons.values) == (expected["reason"],)
    assert learning.observed_load_sag_increase is not None
    observation = learning.observed_load_sag_increase
    _assert_serialized(learning, GOLDEN["serialized"]["refusal_learning"])
    _assert_serialized(selection.estimate, GOLDEN["serialized"]["refusal_cohort"])
    _assert_serialized(observation, GOLDEN["serialized"]["refusal_observation"])
    assert observation.parameter == "ir_k_v_per_pp"
    assert observation.value_before == 0.015
    assert observation.measured_estimate == expected["median_k_v_per_pp"]
    assert observation.evidence_set_id == expected["evidence_set_id"]
    assert observation.evidence_hashes == tuple(expected["consumed_step_hashes"])

    before_path = FIXTURE_DIR / "model-before.json"
    with TemporaryDirectory() as directory:
        model_path = Path(directory) / "model.json"
        copyfile(before_path, model_path)
        before_bytes = model_path.read_bytes()
        owner = ModelOwner(model_path, safety_oracle=_oracle)
        try:
            assert expected["model_must_remain_unchanged"]
            assert learning.change is None
            assert model_path.read_bytes() == before_bytes
            assert (
                hashlib.sha256(model_path.read_bytes()).hexdigest()
                == GOLDEN["model"]["model_before_sha256"]
            )
            assert not owner.precommit_path.exists()
        finally:
            owner.close()


def test_slice1_real_worker_close_pipeline_commits_fixed_model(tmp_path: Path) -> None:
    steps, selection = _cohort(GOLDEN["synthetic_raw"]["k_v_per_pp"])
    _assert_step_estimates(steps)
    _assert_fragment_linkage(steps)
    _assert_cohort_selection(selection)
    _assert_learning_result(_learning(selection))
    scenario = _run_pipeline(tmp_path, k_v_per_pp=GOLDEN["synthetic_raw"]["k_v_per_pp"])
    model = GOLDEN["model"]
    try:
        result = scenario.result
        receipt = result.outcome.commit_receipt
        assert result.outcome.disposition is TerminalDisposition.LEARNED
        assert receipt is not None
        _assert_serialized(receipt, GOLDEN["serialized"]["receipt"])
        expected_after = model_state_schema.canonical_json(
            json.loads((FIXTURE_DIR / "model-after.json").read_text())
        ).encode("utf-8")
        assert scenario.model_path.read_bytes() == expected_after
        assert hashlib.sha256(expected_after).hexdigest() == model["model_after_sha256"]
        projection = scenario.store.project(
            EventRef(scenario.processing.blackout_id, scenario.processing.final_path_token)
        )
        commits = tuple(
            record for record in projection.derived_records if record.record_type == "model_commit"
        )
        assert len(commits) == 1
        assert canonical_json_bytes(commits[0].payload) == GOLDEN["serialized"]["receipt"][
            "bytes"
        ].encode("utf-8")
        assert scenario.store.work_registry().pending_processing == ()
    finally:
        scenario.owner.close()
        scenario.store.close()


def test_slice1_real_worker_close_pipeline_refuses_upward_change_without_write(
    tmp_path: Path,
) -> None:
    scenario = _run_pipeline(tmp_path, k_v_per_pp=GOLDEN["refusal"]["k_v_per_pp"])
    try:
        result = scenario.result
        assert result.outcome.disposition is TerminalDisposition.RECORDED_ONLY
        assert result.outcome.commit_receipt is None
        assert scenario.model_path.read_bytes() == (FIXTURE_DIR / "model-before.json").read_bytes()
        assert not scenario.owner.precommit_path.exists()
        assert scenario.owner.current_snapshot().ir_k_v_per_pp == 0.015
        _assert_serialized(
            scenario.prepared.observed_load_sag_increase,
            GOLDEN["serialized"]["refusal_observation"],
        )
        projection = scenario.store.project(
            EventRef(scenario.processing.blackout_id, scenario.processing.final_path_token)
        )
        assert not any(
            record.record_type == "model_commit" for record in projection.derived_records
        )
        assert projection.outcome is not None
        observed = projection.outcome.payload["observed_load_sag_increase"]
        assert canonical_json_bytes(observed) == GOLDEN["serialized"]["refusal_observation"][
            "bytes"
        ].encode("utf-8")
    finally:
        scenario.owner.close()
        scenario.store.close()
