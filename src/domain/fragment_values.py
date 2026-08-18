"""Slice, load-step, and profile value objects."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from math import isfinite

from src.domain.fragment_policy import (
    MAX_PROFILE_RECORDS,
    DischargeFragmentPolicy,
    resolve_fragment_policy,
)
from src.domain.fragment_primitives import (
    CanonicalDischargeSample,
    CanonicalSampleSpan,
    EndpointAnchor,
    ObservationOrigin,
    OmittedFragmentKind,
    ProfileReason,
    StartReadinessContext,
    _coerce_enum,
    _require_sha256,
    _validate_id,
    _validate_monotonic_int,
    _validate_nonnegative_int,
    _validate_positive_int,
    _validate_sample_sequence,
    build_canonical_sample_span,
    validate_canonical_sample_span,
)
from src.domain.values import LoadStepEstimate


@dataclass(frozen=True, slots=True)
class DischargeSlice:
    """A same-boot contiguous run represented by one or more sample spans.

    ``samples`` remains a transient construction/replay input.  A caller may
    supply only ``spans`` when the raw sample tuple is not materialized.
    """

    samples: tuple[CanonicalDischargeSample, ...]
    blackout_id: str
    physical_episode_id: str
    battery_epoch_id: str
    segment_id: str
    origin: ObservationOrigin
    policy_revision: str
    start_anchor: EndpointAnchor | None = None
    end_anchor: EndpointAnchor | None = None
    uat_intent_id: str | None = None
    readiness_context: StartReadinessContext | None = None
    spans: tuple[CanonicalSampleSpan, ...] = ()

    def __post_init__(self) -> None:
        _validate_slice_shape(self)
        policy = resolve_fragment_policy(self.policy_revision)
        _validate_slice_context(self)
        if self.samples:
            _validate_samples(self.samples, policy)
            if not self.spans:
                object.__setattr__(self, "spans", (build_canonical_sample_span(self.samples),))
            else:
                _validate_spans_against_samples(self.spans, self.samples, policy)
        else:
            _validate_spans(self.spans, policy)
        _validate_anchors(
            self.spans,
            self.start_anchor,
            self.end_anchor,
            self,
        )

    @property
    def slice_id(self) -> str:
        """Derive identity from immutable scope and canonical span identity."""
        return _slice_identity(self)


@dataclass(frozen=True, slots=True)
class LoadStepObservation:
    """A load-step estimate validated against its exact parent slice."""

    estimate: LoadStepEstimate
    contributing_sample_hashes: tuple[str, ...]
    parent_slice: DischargeSlice

    def __post_init__(self) -> None:
        if not isinstance(self.estimate, LoadStepEstimate):
            raise TypeError("load-step estimate must be LoadStepEstimate")
        if not isinstance(self.parent_slice, DischargeSlice):
            raise TypeError("load-step parent must be DischargeSlice")
        _validate_estimate(self.estimate, self.parent_slice)
        _validate_contributing_hashes(
            self.contributing_sample_hashes, self.estimate, self.parent_slice
        )

    @property
    def step_record_hash(self) -> str:
        """Derive the stable record identity from parent, estimate, and raw hashes."""
        return _step_identity(self)


@dataclass(frozen=True, slots=True)
class DischargeFragmentProfile:
    """Bounded, non-persistable Wave 1 profile of typed fragments."""

    anchors: tuple[EndpointAnchor, ...]
    slices: tuple[DischargeSlice, ...]
    load_steps: tuple[LoadStepObservation, ...]
    policy_revision: str
    profile_issues: tuple[ProfileReason, ...] = ()
    issue_overflow_count: int = 0
    first_unprofiled_raw_hash: str | None = None
    anchor_overflow_count: int = 0
    slice_overflow_count: int = 0
    load_step_overflow_count: int = 0
    series_id: str = ""
    ordinal: int = 0
    record_count: int = 1
    first_unprofiled_kind: OmittedFragmentKind | None = None

    def __post_init__(self) -> None:
        policy = resolve_fragment_policy(self.policy_revision)
        _validate_profile_shape(self, policy)
        _validate_profile_members(self)
        _validate_profile_scope(self)
        _validate_profile_links(self)
        _validate_profile_record_identity(self)


def _validate_slice_shape(value: DischargeSlice) -> None:
    if not isinstance(value.samples, tuple):
        raise TypeError("discharge slice samples must be a tuple")
    if any(not isinstance(sample, CanonicalDischargeSample) for sample in value.samples):
        raise TypeError("discharge slice samples must be CanonicalDischargeSample")
    if not isinstance(value.spans, tuple):
        raise TypeError("discharge slice spans must be a tuple")
    if any(not isinstance(span, CanonicalSampleSpan) for span in value.spans):
        raise TypeError("discharge slice spans must be CanonicalSampleSpan")
    if not value.samples and not value.spans:
        raise ValueError("discharge slice must contain samples or sample spans")
    for field_value, name in (
        (value.blackout_id, "blackout ID"),
        (value.physical_episode_id, "physical episode ID"),
        (value.battery_epoch_id, "battery epoch ID"),
        (value.segment_id, "segment ID"),
        (value.policy_revision, "fragment policy revision"),
    ):
        _validate_id(field_value, name)
    origin = _coerce_enum(value.origin, ObservationOrigin, "observation origin")
    object.__setattr__(value, "origin", origin)


def _validate_slice_context(value: DischargeSlice) -> None:
    if value.origin is ObservationOrigin.UAT:
        _validate_id(value.uat_intent_id, "UAT intent ID")
    elif value.uat_intent_id is not None:
        raise ValueError("UAT intent ID is only valid for UAT-origin fragments")
    if value.readiness_context is not None and not isinstance(
        value.readiness_context, StartReadinessContext
    ):
        raise TypeError("readiness context must be StartReadinessContext or None")


def _validate_samples(
    samples: tuple[CanonicalDischargeSample, ...], policy: DischargeFragmentPolicy
) -> None:
    if len(samples) > policy.max_physical_samples:
        raise ValueError("discharge slice exceeds the physical sample budget")
    _validate_sample_sequence(samples, normal_gap_s=policy.normal_gap_s)


def _validate_spans(
    spans: tuple[CanonicalSampleSpan, ...], policy: DischargeFragmentPolicy
) -> None:
    if not spans:
        raise ValueError("discharge slice must contain at least one sample span")
    if len(spans) > 64:
        raise ValueError("discharge slice span reference budget exceeded")
    total_count = 0
    previous: CanonicalSampleSpan | None = None
    for span in spans:
        total_count += span.sample_count
        if previous is not None:
            if span.boot_id != previous.boot_id:
                raise ValueError("discharge spans cannot span boot identities")
            if span.first_sequence != previous.last_sequence + 1:
                raise ValueError("discharge spans must be sequence-contiguous")
            if span.first_monotonic_ns <= previous.last_monotonic_ns:
                raise ValueError("discharge spans must have increasing monotonic time")
            gap_s = (span.first_monotonic_ns - previous.last_monotonic_ns) / 1_000_000_000
            if gap_s > policy.normal_gap_s:
                raise ValueError("discharge spans cannot span an acquisition gap")
        previous = span
    if total_count > policy.max_physical_samples:
        raise ValueError("discharge slice exceeds the physical sample budget")


def _validate_spans_against_samples(
    spans: tuple[CanonicalSampleSpan, ...],
    samples: tuple[CanonicalDischargeSample, ...],
    policy: DischargeFragmentPolicy,
) -> None:
    _validate_spans(spans, policy)
    by_sequence = {sample.sequence: sample for sample in samples}
    covered: set[int] = set()
    for span in spans:
        try:
            selected = tuple(
                by_sequence[sequence]
                for sequence in range(span.first_sequence, span.last_sequence + 1)
            )
        except KeyError as exc:
            raise ValueError("sample span sequence is absent from supplied samples") from exc
        validate_canonical_sample_span(span, selected)
        covered.update(sample.sequence for sample in selected)
    if covered != set(by_sequence):
        raise ValueError("sample spans do not cover the supplied samples exactly")


def _validate_anchors(
    spans: tuple[CanonicalSampleSpan, ...],
    start: EndpointAnchor | None,
    end: EndpointAnchor | None,
    slice_value: DischargeSlice,
) -> None:
    first = spans[0]
    last = spans[-1]
    for anchor, is_start in ((start, True), (end, False)):
        if anchor is None:
            continue
        _validate_anchor_scope(anchor, first, slice_value)
        _validate_anchor_boundary(anchor, is_start, first, last)


def _validate_anchor_scope(
    anchor: EndpointAnchor, first: CanonicalSampleSpan, slice_value: DischargeSlice
) -> None:
    if anchor.boot_id != first.boot_id:
        raise ValueError("slice anchor must use the sample boot identity")
    if (
        anchor.blackout_id != slice_value.blackout_id
        or anchor.physical_episode_id != slice_value.physical_episode_id
        or anchor.segment_id != slice_value.segment_id
    ):
        raise ValueError("slice anchor scope differs from slice")


def _validate_anchor_boundary(
    anchor: EndpointAnchor,
    is_start: bool,
    first: CanonicalSampleSpan,
    last: CanonicalSampleSpan,
) -> None:
    if anchor.source_sample_hash is not None:
        _validate_source_anchor_boundary(anchor, is_start, first, last)
        return
    _validate_position_anchor_boundary(anchor, is_start, first, last)


def _validate_source_anchor_boundary(
    anchor: EndpointAnchor,
    is_start: bool,
    first: CanonicalSampleSpan,
    last: CanonicalSampleSpan,
) -> None:
    boundary_hash = first.first_sample_hash if is_start else last.last_sample_hash
    boundary_monotonic = first.first_monotonic_ns if is_start else last.last_monotonic_ns
    boundary_wall = first.first_wall_time_utc if is_start else last.last_wall_time_utc
    if anchor.source_sample_hash != boundary_hash:
        raise ValueError("slice anchor source sample is not the boundary sample")
    if anchor.monotonic_ns != boundary_monotonic:
        raise ValueError("anchor monotonic boundary differs from source sample")
    if anchor.wall_time_utc != boundary_wall:
        raise ValueError("anchor wall boundary differs from source sample")


def _validate_position_anchor_boundary(
    anchor: EndpointAnchor,
    is_start: bool,
    first: CanonicalSampleSpan,
    last: CanonicalSampleSpan,
) -> None:
    if is_start and anchor.monotonic_ns > first.first_monotonic_ns:
        raise ValueError("start anchor cannot follow the first sample")
    if not is_start and anchor.monotonic_ns < last.last_monotonic_ns:
        raise ValueError("end anchor cannot precede the last sample")


def _validate_estimate(estimate: LoadStepEstimate, parent: DischargeSlice) -> None:
    if not parent.samples:
        raise ValueError("load-step validation requires transient parent samples")
    _validate_id(estimate.step_id, "load-step ID")
    if estimate.blackout_id != parent.blackout_id:
        raise ValueError("load-step estimate blackout differs from parent slice")
    if estimate.segment_id != parent.segment_id:
        raise ValueError("load-step estimate segment differs from parent slice")
    _validate_sequence_window(estimate.pre_sequences, parent, "pre")
    _validate_sequence_window(estimate.post_sequences, parent, "post")
    all_sequences = (*estimate.pre_sequences, *estimate.post_sequences)
    if len(set(all_sequences)) != len(all_sequences):
        raise ValueError("load-step sequence windows must not overlap")
    _validate_monotonic_int(estimate.transition_monotonic_ns, "load-step transition time")
    times = tuple(sample.observation.monotonic_ns for sample in parent.samples)
    if not min(times) <= estimate.transition_monotonic_ns <= max(times):
        raise ValueError("load-step transition must lie within parent slice")
    _validate_estimate_numerics(estimate)


def _validate_sequence_window(
    sequences: tuple[int, ...], parent: DischargeSlice, name: str
) -> None:
    if not isinstance(sequences, tuple) or not sequences:
        raise ValueError(f"load-step {name} sequence window must be non-empty")
    parent_sequences = {sample.sequence for sample in parent.samples}
    previous: int | None = None
    for sequence in sequences:
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence not in parent_sequences
        ):
            raise ValueError(f"load-step {name} sequence is absent from parent slice")
        if previous is not None and sequence <= previous:
            raise ValueError(f"load-step {name} sequence window must be ordered")
        previous = sequence


def _validate_estimate_numerics(estimate: LoadStepEstimate) -> None:
    values = (
        estimate.pre_slope_v_per_s,
        estimate.early_post_slope_v_per_s,
        estimate.late_post_slope_v_per_s,
        estimate.delta_load_pp,
        estimate.early_delta_voltage_at_transition_v,
        estimate.settled_delta_voltage_at_transition_v,
        estimate.voltage_quantum_v,
        estimate.k_transition_v_per_pp,
        estimate.k_settled_v_per_pp,
    )
    if any(isinstance(value, bool) or not isfinite(float(value)) for value in values):
        raise ValueError("load-step estimate numeric fields must be finite")


def _validate_contributing_hashes(
    hashes: tuple[str, ...], estimate: LoadStepEstimate, parent: DischargeSlice
) -> None:
    if not isinstance(hashes, tuple) or not hashes:
        raise ValueError("load-step contribution hashes must be a non-empty tuple")
    by_sequence = {sample.sequence: sample.canonical_hash for sample in parent.samples}
    expected = tuple(
        by_sequence[sequence] for sequence in (*estimate.pre_sequences, *estimate.post_sequences)
    )
    if hashes != expected:
        raise ValueError("load-step hashes must exactly match estimate windows in the parent slice")
    for value in hashes:
        _require_sha256(value, "load-step contributing sample hash")
    if len(set(hashes)) != len(hashes):
        raise ValueError("load-step contributing hashes must be unique")
    if (
        len(hashes)
        > resolve_fragment_policy(parent.policy_revision).max_contributing_hashes_per_step
    ):
        raise ValueError("load-step contribution hash budget exceeded")


def _validate_profile_shape(
    profile: DischargeFragmentProfile, policy: DischargeFragmentPolicy
) -> None:
    _validate_profile_collections(profile)
    _validate_profile_cardinalities(profile, policy)
    _validate_profile_issues(profile, policy)


def _validate_profile_collections(profile: DischargeFragmentProfile) -> None:
    if not isinstance(profile.anchors, tuple) or not isinstance(profile.slices, tuple):
        raise TypeError("profile anchors and slices must be tuples")
    if not isinstance(profile.load_steps, tuple):
        raise TypeError("profile load steps must be a tuple")


def _validate_profile_cardinalities(
    profile: DischargeFragmentProfile, policy: DischargeFragmentPolicy
) -> None:
    if len(profile.slices) > policy.max_slices_per_record:
        raise ValueError("profile slice budget exceeded")
    descriptor_count = len(profile.anchors) + len(profile.slices) + len(profile.load_steps)
    if descriptor_count > policy.derived_tail_budget.max_compact_descriptors:
        raise ValueError("combined profile descriptor budget exceeded")


def _validate_profile_issues(
    profile: DischargeFragmentProfile, policy: DischargeFragmentPolicy
) -> None:
    _validate_profile_issue_shape(profile, policy)
    _validate_profile_overflow_counts(profile)
    _validate_profile_omission_identity(profile)
    overflow_reason = ProfileReason.FRAGMENT_BUDGET_EXHAUSTED in profile.profile_issues
    if overflow_reason != (profile.issue_overflow_count > 0):
        raise ValueError("fragment budget reason and overflow count must agree")
    if overflow_reason != (profile.first_unprofiled_raw_hash is not None):
        raise ValueError("fragment budget reason and first unprofiled hash must agree")


def _validate_profile_issue_shape(
    profile: DischargeFragmentProfile, policy: DischargeFragmentPolicy
) -> None:
    if not isinstance(profile.profile_issues, tuple):
        raise TypeError("profile issues must be a tuple")
    if len(profile.profile_issues) > policy.max_profile_issues:
        raise ValueError("profile issue budget exceeded")
    if any(not isinstance(issue, ProfileReason) for issue in profile.profile_issues):
        raise TypeError("profile issues must use closed ProfileReason values")


def _validate_profile_overflow_counts(profile: DischargeFragmentProfile) -> None:
    _validate_nonnegative_int(profile.issue_overflow_count, "profile issue overflow count")
    counts = (
        profile.anchor_overflow_count,
        profile.slice_overflow_count,
        profile.load_step_overflow_count,
    )
    for value, name in zip(
        counts,
        ("anchor overflow count", "slice overflow count", "load-step overflow count"),
        strict=True,
    ):
        _validate_nonnegative_int(value, name)
    if profile.issue_overflow_count != sum(counts):
        raise ValueError("profile overflow count does not match category counts")


def _validate_profile_omission_identity(profile: DischargeFragmentProfile) -> None:
    if profile.first_unprofiled_raw_hash is not None:
        _require_sha256(profile.first_unprofiled_raw_hash, "first unprofiled raw hash")
    if profile.first_unprofiled_kind is not None:
        _coerce_enum(profile.first_unprofiled_kind, OmittedFragmentKind, "first omitted kind")
    if (profile.first_unprofiled_kind is None) != (profile.first_unprofiled_raw_hash is None):
        raise ValueError("first omitted kind and raw hash must be paired")


def _validate_profile_members(profile: DischargeFragmentProfile) -> None:
    if any(not isinstance(anchor, EndpointAnchor) for anchor in profile.anchors):
        raise TypeError("profile anchors must be EndpointAnchor")
    anchor_hashes = tuple(anchor.canonical_hash for anchor in profile.anchors)
    if len(set(anchor_hashes)) != len(anchor_hashes):
        raise ValueError("profile anchor hashes must be unique")
    if any(not isinstance(item, DischargeSlice) for item in profile.slices):
        raise TypeError("profile slices must be DischargeSlice")
    if any(not isinstance(step, LoadStepObservation) for step in profile.load_steps):
        raise TypeError("profile load steps must be LoadStepObservation")
    if any(item.policy_revision != profile.policy_revision for item in profile.slices):
        raise ValueError("slice policy revision differs from profile")


def _validate_profile_scope(profile: DischargeFragmentProfile) -> None:
    _validate_scope_values(profile.slices, profile.anchors)


def _validate_scope_values(
    slices: tuple[DischargeSlice, ...], anchors: tuple[EndpointAnchor, ...]
) -> None:
    if not slices:
        if anchors:
            raise ValueError("profile anchors and steps require a scoped slice")
        return
    first = slices[0]
    _validate_slice_scope(first, slices[1:])
    _validate_anchor_scopes(first, anchors)


def _validate_slice_scope(first: DischargeSlice, others: tuple[DischargeSlice, ...]) -> None:
    for item in others:
        if item.blackout_id != first.blackout_id:
            raise ValueError("profile slices must share blackout ID")
        if item.physical_episode_id != first.physical_episode_id:
            raise ValueError("profile slices must share physical episode ID")
        if item.battery_epoch_id != first.battery_epoch_id:
            raise ValueError("profile slices must share battery epoch ID")
        if item.origin is not first.origin:
            raise ValueError("profile slices must share observation origin")
        if item.uat_intent_id != first.uat_intent_id:
            raise ValueError("profile slices must share UAT intent identity")


def _validate_anchor_scopes(first: DischargeSlice, anchors: tuple[EndpointAnchor, ...]) -> None:
    for anchor in anchors:
        if (
            anchor.blackout_id != first.blackout_id
            or anchor.physical_episode_id != first.physical_episode_id
            or anchor.segment_id != first.segment_id
        ):
            raise ValueError("profile anchors must share slice scope")


def _validate_profile_links(profile: DischargeFragmentProfile) -> None:
    _validate_profile_links_values(profile.anchors, profile.slices, profile.load_steps)


def _validate_profile_links_values(
    anchors: tuple[EndpointAnchor, ...],
    slices: tuple[DischargeSlice, ...],
    load_steps: tuple[LoadStepObservation, ...],
) -> None:
    _validate_profile_slice_links(slices, load_steps)
    _validate_profile_sample_hashes(slices)
    _validate_profile_anchor_links(anchors, slices)
    _validate_profile_boundary_links(anchors, slices)


def _validate_profile_slice_links(
    slices: tuple[DischargeSlice, ...], load_steps: tuple[LoadStepObservation, ...]
) -> None:
    slice_ids = tuple(item.slice_id for item in slices)
    if len(set(slice_ids)) != len(slice_ids):
        raise ValueError("profile slice IDs must be unique")
    if any(step.parent_slice.slice_id not in slice_ids for step in load_steps):
        raise ValueError("load-step parent slice is absent from profile")


def _validate_profile_sample_hashes(slices: tuple[DischargeSlice, ...]) -> None:
    hashes = [sample.canonical_hash for item in slices for sample in item.samples]
    if hashes and len(set(hashes)) != len(hashes):
        raise ValueError("profile cannot duplicate canonical raw samples")


def _validate_profile_anchor_links(
    anchors: tuple[EndpointAnchor, ...], slices: tuple[DischargeSlice, ...]
) -> None:
    for anchor in anchors:
        matches = tuple(item for item in slices if _anchor_matches_slice(anchor, item))
        if len(matches) != 1:
            raise ValueError("profile anchor must link to exactly one slice")


def _validate_profile_boundary_links(
    anchors: tuple[EndpointAnchor, ...], slices: tuple[DischargeSlice, ...]
) -> None:
    for item in slices:
        for boundary in (item.start_anchor, item.end_anchor):
            if boundary is not None:
                references = tuple(
                    anchor for anchor in anchors if anchor.canonical_hash == boundary.canonical_hash
                )
                if len(references) != 1:
                    raise ValueError("slice boundary anchor is absent from profile")


def _anchor_matches_slice(anchor: EndpointAnchor, item: DischargeSlice) -> bool:
    if (anchor.blackout_id, anchor.physical_episode_id, anchor.segment_id) != (
        item.blackout_id,
        item.physical_episode_id,
        item.segment_id,
    ):
        return False
    if any(
        boundary is not None and boundary.canonical_hash == anchor.canonical_hash
        for boundary in (item.start_anchor, item.end_anchor)
    ):
        return True
    return _anchor_source_belongs(anchor, item)


def _anchor_source_belongs(anchor: EndpointAnchor, item: DischargeSlice) -> bool:
    if anchor.source_sample_hash is None:
        return False
    if item.samples:
        return any(anchor.source_sample_hash == sample.canonical_hash for sample in item.samples)
    return anchor.source_sample_hash in {
        boundary_hash
        for span in item.spans
        for boundary_hash in (span.first_sample_hash, span.last_sample_hash)
    }


def _validate_profile_record_identity(profile: DischargeFragmentProfile) -> None:
    _validate_nonnegative_int(profile.ordinal, "profile ordinal")
    _validate_positive_int(profile.record_count, "profile record count")
    if profile.record_count > MAX_PROFILE_RECORDS:
        raise ValueError("profile record count exceeds physical record budget")
    if profile.ordinal >= profile.record_count:
        raise ValueError("profile ordinal must be less than record count")
    if profile.series_id:
        _require_sha256(profile.series_id, "profile series ID")
    else:
        object.__setattr__(
            profile,
            "series_id",
            _series_identity(
                profile.policy_revision, profile.anchors, profile.slices, profile.load_steps
            ),
        )


def _series_identity(
    policy_revision: str,
    anchors: tuple[EndpointAnchor, ...],
    slices: tuple[DischargeSlice, ...],
    load_steps: tuple[LoadStepObservation, ...],
) -> str:
    fields = (
        policy_revision,
        *(anchor.canonical_hash for anchor in anchors),
        *(item.slice_id for item in slices),
        *(step.step_record_hash for step in load_steps),
    )
    return sha256("|".join(fields).encode("ascii")).hexdigest()


def _slice_identity(value: DischargeSlice) -> str:
    if value.samples:
        ordered_digest = _ordered_sample_hashes_digest(
            tuple(sample.canonical_hash for sample in value.samples)
        )
    else:
        ordered_digest = _span_sequence_digest(value.spans)
    first = value.spans[0]
    last = value.spans[-1]
    payload = "|".join(
        (
            value.blackout_id,
            value.physical_episode_id,
            value.battery_epoch_id,
            value.segment_id,
            value.origin.value,
            value.uat_intent_id or "",
            value.policy_revision,
            first.boot_id,
            str(first.first_sequence),
            str(last.last_sequence),
            str(sum(span.sample_count for span in value.spans)),
            first.first_sample_hash,
            last.last_sample_hash,
            ordered_digest,
        )
    )
    return _slice_identity_digest(payload)


@lru_cache(maxsize=4096)
def _ordered_sample_hashes_digest(hashes: tuple[str, ...]) -> str:
    return sha256("".join(hashes).encode("ascii")).hexdigest()


@lru_cache(maxsize=4096)
def _slice_identity_digest(payload: str) -> str:
    return sha256(payload.encode("utf-8")).hexdigest()


def _span_sequence_digest(spans: tuple[CanonicalSampleSpan, ...]) -> str:
    """Digest an ordered span descriptor sequence without a second identity field."""
    fields = tuple(
        "|".join(
            (
                str(span.first_sequence),
                str(span.last_sequence),
                str(span.sample_count),
                span.first_sample_hash,
                span.last_sample_hash,
                span.ordered_sample_hashes_sha256,
                span.boot_id,
                str(span.first_monotonic_ns),
                str(span.last_monotonic_ns),
            )
        )
        for span in spans
    )
    return _span_sequence_digest_cached(fields)


@lru_cache(maxsize=4096)
def _span_sequence_digest_cached(fields: tuple[str, ...]) -> str:
    return sha256("\n".join(fields).encode("ascii")).hexdigest()


def _step_identity(value: LoadStepObservation) -> str:
    estimate = value.estimate
    fields = (
        value.parent_slice.slice_id,
        estimate.step_id,
        estimate.blackout_id,
        estimate.segment_id,
        ",".join(str(item) for item in estimate.pre_sequences),
        ",".join(str(item) for item in estimate.post_sequences),
        str(estimate.transition_monotonic_ns),
        *(
            repr(item)
            for item in (
                estimate.pre_slope_v_per_s,
                estimate.early_post_slope_v_per_s,
                estimate.late_post_slope_v_per_s,
                estimate.delta_load_pp,
                estimate.early_delta_voltage_at_transition_v,
                estimate.settled_delta_voltage_at_transition_v,
                estimate.voltage_quantum_v,
                estimate.k_transition_v_per_pp,
                estimate.k_settled_v_per_pp,
            )
        ),
        estimate.quality.value,
        *(str(reason.value) for reason in estimate.reasons.values),
        *value.contributing_sample_hashes,
    )
    return _step_identity_digest(fields)


@lru_cache(maxsize=8192)
def _step_identity_digest(fields: tuple[str, ...]) -> str:
    return sha256("|".join(fields).encode("utf-8")).hexdigest()
