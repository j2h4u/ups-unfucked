"""Consumer-specific load-sag admission and refusal boundaries."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.fragment_policy import DEFAULT_DISCHARGE_FRAGMENT_POLICY
from src.domain.fragments import (
    AnchorKind,
    AnchorProvenance,
    CanonicalDischargeSample,
    DischargeFragmentProfile,
    DischargeSlice,
    EndpointAnchor,
    LoadStepObservation,
    ObservationOrigin,
    StartReadinessContext,
)
from src.domain.load_sag_assessment import (
    DEFAULT_LOAD_SAG_POLICY,
    LoadSagAssessmentContext,
    LoadSagDisposition,
    LoadSagReason,
    assess_load_sag,
    assessment_from_payload,
    assessment_payload,
    source_profile_hash,
)
from src.domain.reasons import order_reasons
from src.domain.values import LoadStepEstimate, PhysicalObservation, StepQuality

START = datetime(2026, 8, 18, tzinfo=timezone.utc)
HASHES = tuple(f"{number:064x}" for number in range(1, 2_000))


def _sample(sequence: int, *, boot: str = "boot-a") -> CanonicalDischargeSample:
    return CanonicalDischargeSample(
        sequence,
        HASHES[sequence],
        PhysicalObservation(
            boot,
            sequence * 1_000_000_000,
            START + timedelta(seconds=sequence),
            "OB DISCHRG",
            "13.200",
            13.2,
            0.001,
            20.0 + sequence,
            0.0,
        ),
    )


def _slice(
    *,
    origin: ObservationOrigin = ObservationOrigin.NATURAL,
    uat: str | None = None,
    boot: str = "boot-a",
    offset: int = 0,
) -> DischargeSlice:
    samples = (
        _sample(offset, boot=boot),
        _sample(offset + 1, boot=boot),
    )
    return DischargeSlice(
        samples=samples,
        blackout_id="blackout-a",
        physical_episode_id="episode-a",
        battery_epoch_id="epoch-a",
        segment_id="segment-a",
        origin=origin,
        policy_revision=DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision,
        uat_intent_id=uat,
        readiness_context=StartReadinessContext(False, "arbitrary", None),
    )


def _step(parent: DischargeSlice, *, step_id: str = "step-a") -> LoadStepObservation:
    first, second = parent.samples
    estimate = LoadStepEstimate(
        step_id=step_id,
        blackout_id=parent.blackout_id,
        segment_id=parent.segment_id,
        pre_sequences=(first.sequence,),
        post_sequences=(second.sequence,),
        transition_monotonic_ns=second.observation.monotonic_ns,
        pre_slope_v_per_s=0.0,
        early_post_slope_v_per_s=0.0,
        late_post_slope_v_per_s=0.0,
        delta_load_pp=20.0,
        early_delta_voltage_at_transition_v=-0.1,
        settled_delta_voltage_at_transition_v=-0.1,
        voltage_quantum_v=0.001,
        k_transition_v_per_pp=0.005,
        k_settled_v_per_pp=0.005,
        quality=StepQuality.QUALIFYING,
        reasons=order_reasons(()),
    )
    return LoadStepObservation(estimate, (first.canonical_hash, second.canonical_hash), parent)


def _assessment_fixture(
    *, origin: ObservationOrigin = ObservationOrigin.NATURAL, uat: str | None = None
) -> tuple[DischargeFragmentProfile, LoadStepObservation]:
    parent = _slice(origin=origin, uat=uat)
    step = _step(parent)
    return DischargeFragmentProfile((), (parent,), (step,), parent.policy_revision), step


def _context(profile: DischargeFragmentProfile, **changes: object) -> LoadSagAssessmentContext:
    return LoadSagAssessmentContext(
        battery_epoch_id=changes.get("battery_epoch_id", "epoch-a"),  # type: ignore[arg-type]
        policy_revision=changes.get("policy_revision", DEFAULT_LOAD_SAG_POLICY.revision),  # type: ignore[arg-type]
        evaluator_revision=changes.get(
            "evaluator_revision", DEFAULT_LOAD_SAG_POLICY.evaluator_revision
        ),  # type: ignore[arg-type]
        origin=changes.get("origin", profile.slices[0].origin),  # type: ignore[arg-type]
        fragment_policy_revision=changes.get(
            "fragment_policy_revision", DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision
        ),  # type: ignore[arg-type]
        expected_profile_hash=changes.get("expected_profile_hash"),  # type: ignore[arg-type]
    )


def test_natural_assessment_admits_qualified_step_with_arbitrary_readiness() -> None:
    profile, step = _assessment_fixture()
    result = assess_load_sag(profile, _context(profile), DEFAULT_LOAD_SAG_POLICY)
    assert result.disposition is LoadSagDisposition.ADMITTED
    assert result.admitted_steps == (step,)
    assert result.refusals == ()
    assert result.source_profile_hash == source_profile_hash(profile)
    assert result.source_step_hashes == (step.step_record_hash,)


def test_assessment_payload_round_trip_resolves_admitted_step() -> None:
    profile, _ = _assessment_fixture()
    original = assess_load_sag(profile, _context(profile), DEFAULT_LOAD_SAG_POLICY)

    reconstructed = assessment_from_payload(
        profile,
        assessment_payload(original),
        policy=DEFAULT_LOAD_SAG_POLICY,
    )

    assert reconstructed == original


def test_assessment_payload_requires_exact_profile_policy_and_object() -> None:
    profile, _ = _assessment_fixture()
    original = assess_load_sag(profile, _context(profile), DEFAULT_LOAD_SAG_POLICY)
    payload = assessment_payload(original)

    with pytest.raises(ValueError, match="payload must be an object"):
        assessment_from_payload(profile, [], policy=DEFAULT_LOAD_SAG_POLICY)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="policy"):
        assessment_from_payload(profile, payload, policy=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="policy values"):
        assessment_from_payload(
            profile,
            payload,
            policy=replace(DEFAULT_LOAD_SAG_POLICY, max_gap_s=3.0),
        )


def test_assessment_payload_round_trip_resolves_refused_step() -> None:
    profile, step = _assessment_fixture()
    refused = replace(step, estimate=replace(step.estimate, quality=StepQuality.OBSERVED_ONLY))
    profile = replace(profile, load_steps=(refused,))
    original = assess_load_sag(profile, _context(profile), DEFAULT_LOAD_SAG_POLICY)

    reconstructed = assessment_from_payload(
        profile,
        assessment_payload(original),
        policy=DEFAULT_LOAD_SAG_POLICY,
    )

    assert reconstructed == original
    assert reconstructed.refusals[0].reason is LoadSagReason.STEP_NOT_QUALIFYING


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("assessment_schema", "load-sag-assessment-v2"),
        ("source_profile_hash", "f" * 64),
        ("source_first_wall_time_utc", "not-utc"),
        ("source_first_monotonic_ns", -1),
        ("step_count", -1),
        ("observation_origin", "future"),
        ("disposition", "future"),
        ("admitted_steps", ()),
    ],
)
def test_assessment_payload_rejects_invalid_primitives(field: str, value: object) -> None:
    profile, _ = _assessment_fixture()
    original = assess_load_sag(profile, _context(profile), DEFAULT_LOAD_SAG_POLICY)
    payload = assessment_payload(original)
    payload[field] = value

    with pytest.raises((TypeError, ValueError)):
        assessment_from_payload(profile, payload, policy=DEFAULT_LOAD_SAG_POLICY)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_blackout_id", "blackout-other"),
        ("source_first_wall_time_utc", "2026-08-18T00:00:01.000000Z"),
        ("battery_epoch_id", "epoch-other"),
        ("observation_origin", ObservationOrigin.SELF_TEST.value),
        ("first_unprofiled_step_hash", "f" * 64),
    ],
)
def test_assessment_payload_rejects_scope_and_overflow_context(field: str, value: object) -> None:
    profile, _ = _assessment_fixture()
    original = assess_load_sag(profile, _context(profile), DEFAULT_LOAD_SAG_POLICY)
    payload = assessment_payload(original)
    payload[field] = value

    with pytest.raises(ValueError, match="payload|first unprofiled"):
        assessment_from_payload(profile, payload, policy=DEFAULT_LOAD_SAG_POLICY)


@pytest.mark.parametrize("mutation", ("length", "step", "parent", "raw"))
def test_assessment_payload_rejects_admitted_step_reference_mutations(mutation: str) -> None:
    profile, step = _assessment_fixture()
    original = assess_load_sag(profile, _context(profile), DEFAULT_LOAD_SAG_POLICY)
    payload = assessment_payload(original)
    if mutation == "length":
        payload["admitted_steps"] = [[step.step_record_hash, step.parent_slice.slice_id]]
    elif mutation == "step":
        payload["admitted_steps"] = [
            ["f" * 64, step.parent_slice.slice_id, list(step.contributing_sample_hashes)]
        ]
    elif mutation == "parent":
        payload["admitted_steps"] = [
            [step.step_record_hash, "f" * 64, list(step.contributing_sample_hashes)]
        ]
    else:
        payload["admitted_steps"] = [
            [step.step_record_hash, step.parent_slice.slice_id, ["f" * 64]]
        ]

    with pytest.raises(ValueError, match="admitted step"):
        assessment_from_payload(profile, payload, policy=DEFAULT_LOAD_SAG_POLICY)


@pytest.mark.parametrize("mutation", ("length", "step", "parent", "raw", "reason"))
def test_assessment_payload_rejects_refusal_reference_mutations(mutation: str) -> None:
    profile, step = _assessment_fixture()
    refused = replace(step, estimate=replace(step.estimate, quality=StepQuality.OBSERVED_ONLY))
    profile = replace(profile, load_steps=(refused,))
    original = assess_load_sag(profile, _context(profile), DEFAULT_LOAD_SAG_POLICY)
    payload = assessment_payload(original)
    refusal = payload["refusals"][0]
    if mutation == "length":
        payload["refusals"] = [refusal[:3]]
    elif mutation == "step":
        payload["refusals"] = [["f" * 64, *refusal[1:]]]
    elif mutation == "parent":
        payload["refusals"] = [[refusal[0], "f" * 64, *refusal[2:]]]
    elif mutation == "raw":
        payload["refusals"] = [[refusal[0], refusal[1], ["f" * 64], refusal[3]]]
    else:
        payload["refusals"] = [[*refusal[:3], "future"]]

    with pytest.raises((TypeError, ValueError), match="refus|step|LoadSagReason"):
        assessment_from_payload(profile, payload, policy=DEFAULT_LOAD_SAG_POLICY)


@pytest.mark.parametrize(
    "origin,uat", [(ObservationOrigin.SELF_TEST, None), (ObservationOrigin.UAT, "uat-a")]
)
def test_self_test_and_uat_are_diagnostic_not_science(
    origin: ObservationOrigin, uat: str | None
) -> None:
    profile, _ = _assessment_fixture(origin=origin, uat=uat)
    result = assess_load_sag(profile, _context(profile), DEFAULT_LOAD_SAG_POLICY)
    assert result.disposition is LoadSagDisposition.DIAGNOSTIC
    assert result.admitted_steps == ()
    assert result.refusals[0].reason is LoadSagReason.NON_NATURAL_ORIGIN


def test_damage_after_step_does_not_refuse_that_step() -> None:
    profile, step = _assessment_fixture()
    anchor = EndpointAnchor(
        HASHES[100],
        AnchorKind.CORRUPTION,
        AnchorProvenance.OPERATIONAL,
        "boot-a",
        START + timedelta(seconds=2),
        2_000_000_000,
        HASHES[1],
        blackout_id="blackout-a",
        physical_episode_id="episode-a",
        segment_id="segment-a",
    )
    damaged_profile = replace(profile, anchors=(anchor,))
    result = assess_load_sag(damaged_profile, _context(damaged_profile), DEFAULT_LOAD_SAG_POLICY)
    assert result.admitted_steps == (step,)


def test_damage_overlapping_one_step_refuses_only_that_step() -> None:
    first_parent = _slice(offset=10)
    second_parent = _slice(boot="boot-b", offset=20)
    first_step = _step(first_parent, step_id="step-a")
    second_step = _step(second_parent, step_id="step-b")
    anchor = EndpointAnchor(
        HASHES[101],
        AnchorKind.CORRUPTION,
        AnchorProvenance.OPERATIONAL,
        "boot-a",
        START,
        10_000_000_000,
        HASHES[10],
        blackout_id="blackout-a",
        physical_episode_id="episode-a",
        segment_id="segment-a",
    )
    profile = DischargeFragmentProfile(
        (anchor,),
        (first_parent, second_parent),
        (first_step, second_step),
        first_parent.policy_revision,
    )
    result = assess_load_sag(profile, _context(profile), DEFAULT_LOAD_SAG_POLICY)
    assert result.disposition is LoadSagDisposition.ADMITTED
    assert result.admitted_steps == (second_step,)
    assert result.refusals[0].step_record_hash == first_step.step_record_hash
    assert result.refusals[0].reason is LoadSagReason.DAMAGE_OVERLAPS_STEP


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"battery_epoch_id": "epoch-other"}, LoadSagReason.BATTERY_EPOCH_MISMATCH),
        ({"policy_revision": "load-sag-other"}, LoadSagReason.POLICY_REVISION_MISMATCH),
        ({"evaluator_revision": "evaluator-other"}, LoadSagReason.EVALUATOR_REVISION_MISMATCH),
        ({"expected_profile_hash": HASHES[500]}, LoadSagReason.SOURCE_PROFILE_HASH_MISMATCH),
    ],
)
def test_context_mismatch_refuses_steps(changes: dict[str, object], reason: LoadSagReason) -> None:
    profile, _ = _assessment_fixture()
    result = assess_load_sag(profile, _context(profile, **changes), DEFAULT_LOAD_SAG_POLICY)
    assert result.disposition is LoadSagDisposition.REFUSED
    assert result.admitted_steps == ()
    assert result.refusals[0].reason is reason


def test_non_qualifying_step_is_refused_independently() -> None:
    profile, step = _assessment_fixture()
    refused_step = replace(step, estimate=replace(step.estimate, quality=StepQuality.OBSERVED_ONLY))
    profile = replace(profile, load_steps=(refused_step,))
    result = assess_load_sag(profile, _context(profile), DEFAULT_LOAD_SAG_POLICY)
    assert result.disposition is LoadSagDisposition.REFUSED
    assert result.refusals[0].reason is LoadSagReason.STEP_NOT_QUALIFYING
