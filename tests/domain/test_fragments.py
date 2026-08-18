"""Exact invalid-state boundaries for immutable discharge fragments."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from math import inf, nan

import pytest

from src.domain.fragment_policy import DEFAULT_DISCHARGE_FRAGMENT_POLICY
from src.domain.fragments import (
    AnchorKind,
    AnchorProvenance,
    CanonicalDischargeSample,
    CanonicalSampleSpan,
    DischargeFragmentProfile,
    DischargeSlice,
    EndpointAnchor,
    LoadStepObservation,
    ObservationOrigin,
    OmittedFragmentKind,
    ProfileReason,
    ReadinessProvenance,
    StartReadinessContext,
    build_canonical_sample_span,
    build_discharge_fragment_profiles,
    truncate_discharge_fragment_profiles,
    validate_canonical_sample_span,
)
from src.domain.values import LoadStepEstimate, PhysicalObservation, StepQuality

HASHES = tuple(f"{number:064x}" for number in range(1, 1_200))
START = datetime(2026, 8, 18, tzinfo=timezone.utc)


def _observation(second: int, *, boot_id: str = "boot-a") -> PhysicalObservation:
    return PhysicalObservation(
        boot_id=boot_id,
        monotonic_ns=second * 1_000_000_000,
        wall_time_utc=START + timedelta(seconds=second),
        raw_status="OB DISCHRG",
        battery_voltage_raw="13.200",
        battery_voltage_v=13.2,
        voltage_token_quantum_v=0.001,
        load_percent=20.0,
        input_voltage_v=0.0,
    )


def _sample(
    sequence: int, *, second: int | None = None, digest: str | None = None
) -> CanonicalDischargeSample:
    return CanonicalDischargeSample(
        sequence,
        digest or HASHES[sequence],
        _observation(sequence if second is None else second),
    )


def _slice(
    *,
    samples: tuple[CanonicalDischargeSample, ...] | None = None,
    spans: tuple[CanonicalSampleSpan, ...] = (),
    origin: ObservationOrigin = ObservationOrigin.NATURAL,
    uat_intent_id: str | None = None,
    readiness_context: StartReadinessContext | None = None,
) -> DischargeSlice:
    return DischargeSlice(
        samples=(_sample(0), _sample(1)) if samples is None else samples,
        blackout_id="blackout-a",
        physical_episode_id="episode-a",
        battery_epoch_id="epoch-a",
        segment_id="segment-a",
        origin=origin,
        policy_revision=DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision,
        uat_intent_id=uat_intent_id,
        readiness_context=readiness_context,
        spans=spans,
    )


def test_closed_enums_and_anchor_provenance_are_exact() -> None:
    assert {item.value for item in ObservationOrigin} == {"natural", "self_test", "uat"}
    assert {item.value for item in AnchorKind} == {
        "transfer_to_battery",
        "raw_firmware_lb",
        "modeled_safe_shutdown",
        "power_restored",
        "service_stop",
        "boot_boundary",
        "charge_stabilized",
        "gap",
        "corruption",
    }
    assert {item.value for item in AnchorProvenance} == {
        "physical",
        "firmware",
        "modeled",
        "operational",
    }
    assert "SYSTEM" not in AnchorProvenance.__members__
    anchor = EndpointAnchor(
        HASHES[20],
        AnchorKind.RAW_FIRMWARE_LB,
        AnchorProvenance.FIRMWARE,
        "boot-a",
        START,
        0,
        HASHES[0],
        blackout_id="blackout-a",
        physical_episode_id="episode-a",
        segment_id="segment-a",
    )
    assert anchor.source_sample_hash == HASHES[0]

    with pytest.raises(ValueError):
        EndpointAnchor(
            HASHES[20],
            AnchorKind.RAW_FIRMWARE_LB,
            AnchorProvenance.PHYSICAL,
            "boot-a",
            START,
            0,
        )


@pytest.mark.parametrize("value", ["A" * 64, "0" * 63, "0" * 65, "not-a-hash"])
def test_hashes_must_be_lowercase_sha256(value: str) -> None:
    with pytest.raises(ValueError):
        CanonicalDischargeSample(0, value, _observation(0))


def test_samples_are_immutable_same_boot_contiguous_and_monotonic() -> None:
    valid = _slice()
    with pytest.raises(FrozenInstanceError):
        valid.blackout_id = "changed"  # type: ignore[misc]

    invalid = (
        (_sample(0), _sample(2)),
        (_sample(0), _sample(1, digest=HASHES[0])),
        (_sample(0), _sample(1, second=0)),
        (_sample(0), CanonicalDischargeSample(1, HASHES[2], _observation(6))),
    )
    for samples in invalid:
        with pytest.raises(ValueError):
            _slice(samples=samples)

    # Wall time is context only: a UTC-aware wall clock correction does not
    # create a continuity gap when monotonic capture remains ordered.
    corrected_wall = PhysicalObservation(
        "boot-a",
        2_000_000_000,
        START - timedelta(seconds=1),
        "OB DISCHRG",
        "13.200",
        13.2,
        0.001,
        20.0,
        0.0,
    )
    accepted = _slice(samples=(_sample(0), CanonicalDischargeSample(1, HASHES[1], corrected_wall)))
    assert accepted.samples[-1].observation.wall_time_utc < START


def test_slice_rejects_boot_crossing_and_anchor_boundary_mismatch() -> None:
    with pytest.raises(ValueError):
        _slice(
            samples=(
                _sample(0),
                CanonicalDischargeSample(1, HASHES[1], _observation(1, boot_id="boot-b")),
            )
        )
    source_mismatch = EndpointAnchor(
        HASHES[10],
        AnchorKind.TRANSFER_TO_BATTERY,
        AnchorProvenance.PHYSICAL,
        "boot-a",
        START,
        1_000_000_000,
        HASHES[0],
        blackout_id="blackout-a",
        physical_episode_id="episode-a",
        segment_id="segment-a",
    )
    with pytest.raises(ValueError):
        DischargeSlice(
            samples=(_sample(0), _sample(1)),
            blackout_id="blackout-a",
            physical_episode_id="episode-a",
            battery_epoch_id="epoch-a",
            segment_id="segment-a",
            origin=ObservationOrigin.NATURAL,
            policy_revision=DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision,
            start_anchor=source_mismatch,
        )
    non_boundary_source = EndpointAnchor(
        HASHES[11],
        AnchorKind.TRANSFER_TO_BATTERY,
        AnchorProvenance.PHYSICAL,
        "boot-a",
        START + timedelta(seconds=1),
        1_000_000_000,
        HASHES[1],
        blackout_id="blackout-a",
        physical_episode_id="episode-a",
        segment_id="segment-a",
    )
    with pytest.raises(ValueError):
        DischargeSlice(
            samples=(_sample(0), _sample(1)),
            blackout_id="blackout-a",
            physical_episode_id="episode-a",
            battery_epoch_id="epoch-a",
            segment_id="segment-a",
            origin=ObservationOrigin.NATURAL,
            policy_revision=DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision,
            start_anchor=non_boundary_source,
        )


def test_self_test_uat_and_typed_readiness_are_representable() -> None:
    raw = StartReadinessContext(False, "not_full", ReadinessProvenance.PHYSICAL)
    assert _slice(readiness_context=raw).readiness_context == raw
    with pytest.raises(TypeError):
        _slice(readiness_context=False)  # type: ignore[arg-type]
    assert _slice(readiness_context=None).readiness_context is None
    assert _slice(origin=ObservationOrigin.SELF_TEST).origin is ObservationOrigin.SELF_TEST
    assert (
        _slice(origin=ObservationOrigin.UAT, uat_intent_id="uat-2026-08-18").origin
        is ObservationOrigin.UAT
    )
    with pytest.raises(ValueError):
        _slice(origin=ObservationOrigin.UAT)
    with pytest.raises(ValueError):
        _slice(origin=ObservationOrigin.NATURAL, uat_intent_id="wrong")
    with pytest.raises(TypeError):
        _slice(readiness_context={"model_soc": 0.8})  # type: ignore[arg-type]


def test_samples_reject_nonfinite_numeric_fields() -> None:
    with pytest.raises(ValueError):
        CanonicalDischargeSample(0, HASHES[0], replace(_observation(0), load_percent=nan))
    with pytest.raises(ValueError):
        CanonicalDischargeSample(0, HASHES[0], replace(_observation(0), battery_voltage_v=inf))


def test_canonical_sample_span_replays_ordered_identity() -> None:
    samples = tuple(_sample(index) for index in range(3))
    span = build_canonical_sample_span(samples)
    assert span.first_sequence == 0
    assert span.last_sequence == 2
    assert span.sample_count == 3
    assert span.first_sample_hash == HASHES[0]
    assert span.last_sample_hash == HASHES[2]
    assert span.boot_id == "boot-a"
    assert span.first_monotonic_ns == 0
    assert span.last_monotonic_ns == 2_000_000_000
    assert (
        span.ordered_sample_hashes_sha256
        == sha256("".join(HASHES[index] for index in range(3)).encode("ascii")).hexdigest()
    )
    validate_canonical_sample_span(span, samples)
    with pytest.raises(ValueError):
        validate_canonical_sample_span(span, (samples[0], samples[2]))
    with pytest.raises(ValueError):
        replace(span, sample_count=4)


def _estimate(
    *,
    blackout_id: str = "blackout-a",
    segment_id: str = "segment-a",
    pre: tuple[int, ...] = (0,),
    post: tuple[int, ...] = (1,),
) -> LoadStepEstimate:
    return LoadStepEstimate(
        step_id="step-a",
        blackout_id=blackout_id,
        segment_id=segment_id,
        pre_sequences=pre,
        post_sequences=post,
        transition_monotonic_ns=1_000_000_000,
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
        reasons=replace(_estimate_reasons(), values=()),
    )


def _estimate_reasons():
    from src.domain.reasons import order_reasons

    return order_reasons(())


def _load_step(
    parent: DischargeSlice, estimate: LoadStepEstimate | None = None
) -> LoadStepObservation:
    estimate = estimate or _estimate()
    by_sequence = {sample.sequence: sample.canonical_hash for sample in parent.samples}
    try:
        hashes = tuple(
            by_sequence[sequence]
            for sequence in (*estimate.pre_sequences, *estimate.post_sequences)
        )
    except KeyError:
        hashes = (HASHES[2], HASHES[1])
    return LoadStepObservation(estimate, hashes, parent)


def test_load_step_requires_exact_parent_windows_and_derives_stable_hash() -> None:
    parent = _slice()
    step = _load_step(parent)
    assert len(step.step_record_hash) == 64
    assert step.step_record_hash == _load_step(parent).step_record_hash
    assert step.parent_slice is parent

    bad_cases = (
        _estimate(blackout_id="other-blackout"),
        _estimate(segment_id="other-segment"),
        _estimate(pre=(2,)),
    )
    for estimate in bad_cases:
        with pytest.raises(ValueError):
            _load_step(parent, estimate)
    with pytest.raises(ValueError):
        LoadStepObservation(_estimate(), (HASHES[1], HASHES[0]), parent)
    with pytest.raises(ValueError):
        LoadStepObservation(
            replace(_estimate(), pre_sequences=(1, 0)),
            (HASHES[1], HASHES[0], HASHES[1]),
            parent,
        )
    with pytest.raises(ValueError):
        LoadStepObservation(replace(_estimate(), delta_load_pp=nan), (HASHES[0], HASHES[1]), parent)


def test_slice_identity_is_domain_derived_and_profile_scope_is_uniform() -> None:
    parent = _slice()
    assert parent.slice_id != "slice-a"
    assert parent.slice_id == _slice().slice_id
    assert DischargeFragmentProfile((), (parent,), (), parent.policy_revision).slices == (parent,)
    with pytest.raises(TypeError):
        DischargeFragmentProfile((), (parent,), (), parent.policy_revision, ("bad",))  # type: ignore[arg-type]

    for changes in (
        {"blackout_id": "other"},
        {"physical_episode_id": "other"},
        {"battery_epoch_id": "other"},
        {"origin": ObservationOrigin.SELF_TEST},
        {"uat_intent_id": "uat-a", "origin": ObservationOrigin.UAT},
    ):
        other = replace(parent, **changes)
        with pytest.raises(ValueError):
            DischargeFragmentProfile((), (parent, other), (), parent.policy_revision)


def test_slice_identity_is_independent_of_transient_span_chunking() -> None:
    samples = tuple(_sample(index) for index in range(4))
    chunked = (
        build_canonical_sample_span(samples[:2]),
        build_canonical_sample_span(samples[2:]),
    )
    whole = _slice(samples=samples)
    chunked_slice = _slice(samples=samples, spans=chunked)
    assert chunked_slice.spans == chunked
    assert chunked_slice.slice_id == whole.slice_id
    whole_span_slice = _slice(samples=(), spans=(build_canonical_sample_span(samples),))
    chunked_span_slice = _slice(samples=(), spans=chunked)
    assert chunked_span_slice.slice_id != whole_span_slice.slice_id
    assert chunked_span_slice.slice_id != whole.slice_id


def test_span_only_slice_and_max_physical_span_are_validated_without_samples() -> None:
    digest_fixture = sha256()
    for number in range(3_170):
        digest_fixture.update(f"{number:064x}".encode("ascii"))
    maximum = CanonicalSampleSpan(
        first_sequence=0,
        last_sequence=3_169,
        sample_count=3_170,
        first_sample_hash=f"{0:064x}",
        last_sample_hash=f"{3_169:064x}",
        ordered_sample_hashes_sha256=digest_fixture.hexdigest(),
        boot_id="boot-a",
        first_monotonic_ns=0,
        last_monotonic_ns=3_169_000_000_000,
        first_wall_time_utc=START,
        last_wall_time_utc=START + timedelta(seconds=3_169),
    )
    value = _slice(samples=(), spans=(maximum,))
    assert value.spans == (maximum,)
    assert value.spans[0].sample_count == 3_170


def test_profile_reasons_are_closed_and_bounded() -> None:
    parent = _slice()
    profile = DischargeFragmentProfile(
        (), (parent,), (), parent.policy_revision, (ProfileReason.DUPLICATE_SAMPLE_HASH,)
    )
    assert profile.profile_issues == (ProfileReason.DUPLICATE_SAMPLE_HASH,)
    with pytest.raises(TypeError):
        DischargeFragmentProfile((), (parent,), (), parent.policy_revision, ("bad",))  # type: ignore[arg-type]
    overflow = DischargeFragmentProfile(
        (),
        (parent,),
        (),
        parent.policy_revision,
        (ProfileReason.FRAGMENT_BUDGET_EXHAUSTED,),
        2,
        HASHES[10],
        anchor_overflow_count=0,
        slice_overflow_count=2,
        load_step_overflow_count=0,
        first_unprofiled_kind=OmittedFragmentKind.SLICE,
    )
    assert overflow.issue_overflow_count == 2
    with pytest.raises(ValueError):
        DischargeFragmentProfile((), (parent,), (), parent.policy_revision, (), 1, HASHES[10])
    with pytest.raises(ValueError):
        DischargeFragmentProfile(
            (),
            (parent,),
            (),
            parent.policy_revision,
            (ProfileReason.FRAGMENT_BUDGET_EXHAUSTED,),
            1,
            None,
            anchor_overflow_count=0,
            slice_overflow_count=1,
            load_step_overflow_count=0,
            first_unprofiled_kind=OmittedFragmentKind.SLICE,
        )


def _many_profile_inputs(
    count: int,
) -> tuple[
    tuple[EndpointAnchor, ...],
    tuple[DischargeSlice, ...],
    tuple[LoadStepObservation, ...],
]:
    anchors: list[EndpointAnchor] = []
    slices: list[DischargeSlice] = []
    steps: list[LoadStepObservation] = []
    for index in range(count):
        first_hash = HASHES[index * 2]
        second_hash = HASHES[index * 2 + 1]
        parent = _slice(samples=(_sample(0, digest=first_hash), _sample(1, digest=second_hash)))
        slices.append(parent)
        anchors.append(
            EndpointAnchor(
                HASHES[600 + index],
                AnchorKind.TRANSFER_TO_BATTERY,
                AnchorProvenance.PHYSICAL,
                "boot-a",
                START,
                0,
                first_hash,
                blackout_id="blackout-a",
                physical_episode_id="episode-a",
                segment_id="segment-a",
            )
        )
        estimate = replace(_estimate(), step_id=f"step-{index}")
        steps.append(LoadStepObservation(estimate, (first_hash, second_hash), parent))
    return tuple(anchors), tuple(slices), tuple(steps)


def test_profile_requires_scoped_bidirectional_anchor_links() -> None:
    anchor, parent, _ = _many_profile_inputs(1)
    with pytest.raises(ValueError):
        DischargeFragmentProfile(anchor, (), (), parent[0].policy_revision)
    with pytest.raises(ValueError):
        DischargeFragmentProfile(
            (replace(anchor[0], source_sample_hash=HASHES[5]),),
            parent,
            (),
            parent[0].policy_revision,
        )
    scoped_other = replace(anchor[0], segment_id="segment-other")
    with pytest.raises(ValueError):
        DischargeFragmentProfile((scoped_other,), parent, (), parent[0].policy_revision)


def test_public_profile_builder_segments_without_gap_injection() -> None:
    anchors, slices, steps = _many_profile_inputs(17)
    profiles = build_discharge_fragment_profiles(
        anchors, slices, steps, DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision
    )
    assert len(profiles) == 2
    assert {profile.record_count for profile in profiles} == {2}
    assert [profile.ordinal for profile in profiles] == [0, 1]
    assert len({profile.series_id for profile in profiles}) == 1
    assert all(profile.profile_issues == () for profile in profiles)


def test_profile_budget_uses_slice_id_for_copied_step_parent() -> None:
    parent = _slice()
    copied_parent = replace(parent)
    assert copied_parent == parent
    step = _load_step(copied_parent)
    profiles = build_discharge_fragment_profiles(
        (), (parent,), (step,), DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision
    )
    assert profiles[0].load_steps == (step,)
    assert (
        sum(
            len(profile.anchors) + len(profile.slices) + len(profile.load_steps)
            for profile in profiles
        )
        <= 256
    )


def test_profile_accepts_intermediate_anchor_and_multiple_steps() -> None:
    samples = tuple(_sample(index) for index in range(3))
    parent = _slice(samples=samples)
    intermediate = EndpointAnchor(
        HASHES[900],
        AnchorKind.RAW_FIRMWARE_LB,
        AnchorProvenance.FIRMWARE,
        "boot-a",
        START + timedelta(seconds=1),
        1_000_000_000,
        HASHES[1],
        blackout_id="blackout-a",
        physical_episode_id="episode-a",
        segment_id="segment-a",
    )
    first = _load_step(parent)
    second = _load_step(parent, replace(_estimate(), step_id="step-b"))
    profiles = build_discharge_fragment_profiles(
        (intermediate,),
        (parent,),
        (first, second),
        DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision,
    )
    assert profiles[0].anchors == (intermediate,)
    assert profiles[0].load_steps == (first, second)
    assert sum(len(profile.slices) for profile in profiles) == 1
    assert all(profile.profile_issues == () for profile in profiles)


def test_public_profile_builder_retains_simultaneous_category_overflow() -> None:
    anchors, slices, steps = _many_profile_inputs(257)
    profiles = build_discharge_fragment_profiles(
        anchors, slices, steps, DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision
    )
    assert len(profiles) == 6
    assert all(profile.record_count == 6 for profile in profiles)
    assert all(profile.anchor_overflow_count == 172 for profile in profiles)
    assert all(profile.slice_overflow_count == 171 for profile in profiles)
    assert all(profile.load_step_overflow_count == 172 for profile in profiles)
    assert all(profile.issue_overflow_count == 515 for profile in profiles)
    assert all(profile.first_unprofiled_kind is OmittedFragmentKind.ANCHOR for profile in profiles)
    assert all(profile.first_unprofiled_raw_hash == HASHES[170] for profile in profiles)
    assert all(
        profile.profile_issues == (ProfileReason.FRAGMENT_BUDGET_EXHAUSTED,) for profile in profiles
    )


def test_profile_overflow_identity_follows_slice_unit_order() -> None:
    first = _slice()
    later = _slice(samples=(_sample(0, digest=HASHES[300]), _sample(1, digest=HASHES[301])))
    anchors = tuple(
        EndpointAnchor(
            HASHES[600 + index],
            AnchorKind.TRANSFER_TO_BATTERY,
            AnchorProvenance.PHYSICAL,
            "boot-a",
            START,
            0,
            HASHES[0],
            blackout_id="blackout-a",
            physical_episode_id="episode-a",
            segment_id="segment-a",
        )
        for index in range(256)
    )
    profiles = build_discharge_fragment_profiles(
        anchors,
        (first, later),
        (),
        DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision,
    )
    assert profiles[0].slice_overflow_count == 1
    assert profiles[0].anchor_overflow_count == 1
    assert profiles[0].first_unprofiled_kind is OmittedFragmentKind.ANCHOR
    assert profiles[0].first_unprofiled_raw_hash == HASHES[0]


def test_truncation_rebuilds_dependency_closed_profiles_and_overflow() -> None:
    anchors, slices, steps = _many_profile_inputs(257)
    original = build_discharge_fragment_profiles(
        anchors, slices, steps, DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision
    )
    truncated = truncate_discharge_fragment_profiles(original, 220)
    assert (
        sum(
            len(profile.anchors) + len(profile.slices) + len(profile.load_steps)
            for profile in truncated
        )
        <= 220
    )
    assert all(profile.first_unprofiled_kind is OmittedFragmentKind.ANCHOR for profile in truncated)
    assert all(profile.first_unprofiled_raw_hash == HASHES[146] for profile in truncated)
    assert len({profile.series_id for profile in truncated}) == 1
    retained_slices = {item.slice_id for profile in truncated for item in profile.slices}
    assert all(
        anchor.source_sample_hash
        in {sample.canonical_hash for item in slices for sample in item.samples}
        for profile in truncated
        for anchor in profile.anchors
    )
    assert all(
        step.parent_slice.slice_id in retained_slices
        for profile in truncated
        for step in profile.load_steps
    )


@pytest.mark.parametrize("budget", (True, 0, 257))
def test_truncation_rejects_invalid_descriptor_budget(budget: object) -> None:
    profile = DischargeFragmentProfile(
        (), (_slice(),), (), DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision
    )
    with pytest.raises((TypeError, ValueError)):
        truncate_discharge_fragment_profiles((profile,), budget)  # type: ignore[arg-type]


def test_truncation_rejects_empty_profiles() -> None:
    with pytest.raises(ValueError):
        truncate_discharge_fragment_profiles((), 1)


def test_ten_minute_one_second_fragment_is_not_truncated_by_wave1_budget() -> None:
    samples = tuple(_sample(index) for index in range(601))
    value = _slice(samples=samples)
    assert len(value.samples) == 601
    assert not hasattr(value, "estimated_canonical_bytes")
