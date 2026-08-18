"""Consumer-specific Wave 2A curve admission tests."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from src.domain.curve_assessment import (
    DEFAULT_CURVE_POLICY,
    CurveDisposition,
    CurvePolicy,
    CurveReason,
    assess_curve,
)
from src.domain.forward_comparison import compare_admitted_observations
from src.domain.fragment_policy import DEFAULT_DISCHARGE_FRAGMENT_POLICY
from src.domain.fragments import (
    CanonicalDischargeSample,
    DischargeFragmentProfile,
    DischargeSlice,
    ObservationOrigin,
    OmittedFragmentKind,
    ProfileReason,
)


def _profile(observations, *, origin=ObservationOrigin.NATURAL, epoch="epoch-a", policy=None):
    samples = tuple(
        CanonicalDischargeSample(
            index,
            hashlib.sha256(f"curve-sample-{index}".encode()).hexdigest(),
            observation,
        )
        for index, observation in enumerate(observations)
    )
    parent = DischargeSlice(
        samples=samples,
        blackout_id="blackout-a",
        physical_episode_id="episode-a",
        battery_epoch_id=epoch,
        segment_id="segment-a",
        origin=origin,
        policy_revision=policy or DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision,
        uat_intent_id="uat-a" if origin is ObservationOrigin.UAT else None,
    )
    return DischargeFragmentProfile((), (parent,), (), parent.policy_revision)


def _observations(observation_factory, count=241, *, voltage_span=0.5, gap=None):
    values = []
    for index in range(count):
        second = index if gap is None or index < count // 2 else index + gap
        values.append(
            observation_factory(
                second,
                voltage_v=13.2 - voltage_span * index / max(1, count - 1),
                load_percent=20.0,
            )
        )
    return tuple(values)


def test_natural_slice_is_admitted_and_comparison_is_local(observation_factory, frozen_snapshot):
    profile = _profile(_observations(observation_factory))
    assessment = assess_curve(profile, frozen_snapshot)[0]
    assert assessment.disposition is CurveDisposition.ADMITTED
    assert assessment.comparison is not None
    assert assessment.slice_id
    assert assessment.raw_sample_count == 241
    assert assessment.raw_ordered_hashes_sha256
    assert assessment.profile_series_id
    assert assessment.comparison == compare_admitted_observations(
        tuple(sample.observation for sample in profile.slices[0].samples), frozen_snapshot
    )


def test_partial_deep_prefix_is_not_globally_refused(observation_factory, frozen_snapshot):
    profile = _profile(_observations(observation_factory, count=121, voltage_span=0.3))
    assessment = assess_curve(profile, frozen_snapshot)[0]
    assert assessment.disposition is CurveDisposition.ADMITTED
    assert assessment.comparison is not None


@pytest.mark.parametrize("origin", (ObservationOrigin.SELF_TEST, ObservationOrigin.UAT))
def test_self_test_and_uat_are_diagnostic_only(observation_factory, frozen_snapshot, origin):
    assessment = assess_curve(
        _profile(_observations(observation_factory), origin=origin), frozen_snapshot
    )[0]
    assert assessment.disposition is CurveDisposition.DIAGNOSTIC_ONLY
    assert assessment.comparison is None
    assert CurveReason.NON_NATURAL_ORIGIN in assessment.reasons


@pytest.mark.parametrize(
    "mutator, reason",
    (
        (
            lambda snapshot: replace(snapshot, battery_epoch_id="other"),
            CurveReason.SNAPSHOT_EPOCH_MISMATCH,
        ),
        (lambda snapshot: replace(snapshot, battery_epoch_id="epoch-a"), None),
    ),
)
def test_epoch_and_policy_are_slice_scoped(observation_factory, frozen_snapshot, mutator, reason):
    snapshot = mutator(frozen_snapshot)
    profile = _profile(_observations(observation_factory), epoch="epoch-a")
    assessment = assess_curve(profile, snapshot)[0]
    if reason is not None:
        assert assessment.disposition is CurveDisposition.REFUSED
        assert reason in assessment.reasons
    else:
        assert assessment.disposition is CurveDisposition.ADMITTED

    refused = assess_curve(
        profile,
        frozen_snapshot,
        CurvePolicy(fragment_policy_revision="other-policy"),
    )[0]
    assert refused.disposition is CurveDisposition.REFUSED
    assert CurveReason.POLICY_REVISION_MISMATCH in refused.reasons


@pytest.mark.parametrize(
    "kwargs, reason",
    (
        ({"voltage_span": 0.05}, CurveReason.VOLTAGE_SPAN_TOO_SMALL),
        ({"gap": 4}, CurveReason.ACQUISITION_GAP),
    ),
)
def test_local_voltage_and_gap_gates(observation_factory, frozen_snapshot, kwargs, reason):
    policy = (
        CurvePolicy(max_gap_s=4.0)
        if reason is CurveReason.ACQUISITION_GAP
        else DEFAULT_CURVE_POLICY
    )
    assessment = assess_curve(
        _profile(_observations(observation_factory, **kwargs)), frozen_snapshot, policy
    )[0]
    assert assessment.disposition is CurveDisposition.REFUSED
    assert reason in assessment.reasons


def test_post_slice_profile_overflow_does_not_refuse_valid_slice(
    observation_factory, frozen_snapshot
):
    profile = _profile(_observations(observation_factory))
    profile = replace(
        profile,
        profile_issues=(ProfileReason.FRAGMENT_BUDGET_EXHAUSTED,),
        issue_overflow_count=2,
        slice_overflow_count=2,
        first_unprofiled_raw_hash="a" * 64,
        first_unprofiled_kind=OmittedFragmentKind.SLICE,
    )
    assessment = assess_curve(profile, frozen_snapshot)[0]
    assert assessment.disposition is CurveDisposition.ADMITTED
    assert assessment.profile_issue_overflow_count == 2


def test_maximum_physical_slice_is_bounded_by_domain(observation_factory, frozen_snapshot):
    profile = _profile(
        _observations(
            observation_factory, count=DEFAULT_CURVE_POLICY.max_raw_sample_count, voltage_span=1.0
        )
    )
    assessment = assess_curve(profile, frozen_snapshot)[0]
    assert assessment.raw_sample_count == DEFAULT_CURVE_POLICY.max_raw_sample_count
    assert assessment.raw_first_sample_hash is not None
