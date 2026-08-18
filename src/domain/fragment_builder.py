"""Pure dependency-closed fragment profile builder."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from math import ceil

from src.domain.fragment_policy import DischargeFragmentPolicy, resolve_fragment_policy
from src.domain.fragment_primitives import MAX_COMPACT_DESCRIPTORS, OmittedFragmentKind
from src.domain.fragment_values import (
    DischargeFragmentProfile,
    DischargeSlice,
    EndpointAnchor,
    LoadStepObservation,
    ProfileReason,
    _anchor_matches_slice,
    _validate_profile_links_values,
    _validate_scope_values,
)


def build_discharge_fragment_profiles(
    anchors: tuple[EndpointAnchor, ...],
    slices: tuple[DischargeSlice, ...],
    load_steps: tuple[LoadStepObservation, ...],
    policy_revision: str,
) -> tuple[DischargeFragmentProfile, ...]:
    """Validate ordered domain inputs and segment them into bounded records.

    Record boundaries are technical chunks only.  No gap anchor or gap issue
    is introduced when an input crosses a record boundary.  Overflow counts
    describe each category independently; any retained overflow pointer is a
    raw sample hash obtained from the affected slice.
    """
    _validate_builder_inputs(anchors, slices, load_steps, policy_revision)
    layout = _make_builder_layout(anchors, slices, load_steps, policy_revision)
    record_anchors, record_steps = _record_members(layout)
    overflow_count = layout.anchor_overflow + layout.slice_overflow + layout.step_overflow
    source = _RetentionSet(anchors, slices, load_steps)
    kept = _RetentionSet(layout.kept_anchors, layout.kept_slices, layout.kept_steps)
    first_dropped = _first_dropped_unit(source, kept)
    first_kind = first_dropped[0] if first_dropped is not None else None
    first_raw_hash = first_dropped[1] if first_dropped is not None else None
    issues = (ProfileReason.FRAGMENT_BUDGET_EXHAUSTED,) if overflow_count else ()
    # Series identity follows the immutable physical record order.  A slice
    # may be split at the byte boundary; using caller-wide category order here
    # would make the same lossless chain decode to a different identity.
    ordered_anchors = tuple(
        sorted(
            (anchor for bucket in record_anchors for anchor in bucket),
            key=lambda anchor: anchor.canonical_hash,
        )
    )
    ordered_slices = tuple(item for bucket in layout.record_slices for item in bucket)
    ordered_steps = tuple(step for bucket in record_steps for step in bucket)
    series_id = _series_identity(policy_revision, ordered_anchors, ordered_slices, ordered_steps)
    return tuple(
        DischargeFragmentProfile(
            anchors=tuple(record_anchors[ordinal]),
            slices=tuple(layout.record_slices[ordinal]),
            load_steps=tuple(record_steps[ordinal]),
            policy_revision=policy_revision,
            profile_issues=issues,
            issue_overflow_count=overflow_count,
            first_unprofiled_raw_hash=first_raw_hash,
            first_unprofiled_kind=first_kind,
            anchor_overflow_count=layout.anchor_overflow,
            slice_overflow_count=layout.slice_overflow,
            load_step_overflow_count=layout.step_overflow,
            series_id=series_id,
            ordinal=ordinal,
            record_count=layout.record_count,
        )
        for ordinal in range(layout.record_count)
    )


def truncate_discharge_fragment_profiles(
    profiles: tuple[DischargeFragmentProfile, ...],
    maximum_descriptors: int = MAX_COMPACT_DESCRIPTORS,
) -> tuple[DischargeFragmentProfile, ...]:
    """Retain a deterministic dependency-closed prefix under a descriptor budget.

    This truncates only complete scientific retention units.  The input
    profiles already carry any original builder overflow; newly omitted
    retained units are added to those counters and become the first omission
    only when physical fitting causes the new omission.
    """
    _validate_truncation_budget(profiles, maximum_descriptors)
    first = profiles[0]
    policy = resolve_fragment_policy(first.policy_revision)
    source = _RetentionSet(
        tuple(anchor for profile in profiles for anchor in profile.anchors),
        tuple(item for profile in profiles for item in profile.slices),
        tuple(step for profile in profiles for step in profile.load_steps),
    )
    kept_slices, kept_anchors, kept_steps = _retain_dependency_closed_with_budget(
        source.anchors, source.slices, source.steps, policy, maximum_descriptors
    )
    kept = _RetentionSet(kept_anchors, kept_slices, kept_steps)
    dropped = _first_dropped_unit(source, kept)
    summary = _overflow_summary(first, source, kept, dropped)
    return _repartition_profiles(
        first,
        kept,
        summary,
        policy.max_slices_per_record,
    )


def _validate_truncation_budget(
    profiles: tuple[DischargeFragmentProfile, ...], maximum_descriptors: int
) -> None:
    if not profiles:
        raise ValueError("fragment profile truncation requires profiles")
    if isinstance(maximum_descriptors, bool) or not isinstance(maximum_descriptors, int):
        raise TypeError("fragment descriptor budget must be an integer")
    if not 1 <= maximum_descriptors <= MAX_COMPACT_DESCRIPTORS:
        raise ValueError("fragment descriptor budget is outside the supported range")
    if any(item.policy_revision != profiles[0].policy_revision for item in profiles):
        raise ValueError("fragment profiles must share policy revision")


def _retain_dependency_closed_with_budget(
    anchors: tuple[EndpointAnchor, ...],
    slices: tuple[DischargeSlice, ...],
    steps: tuple[LoadStepObservation, ...],
    policy: DischargeFragmentPolicy,
    maximum_descriptors: int,
) -> tuple[tuple[DischargeSlice, ...], tuple[EndpointAnchor, ...], tuple[LoadStepObservation, ...]]:
    retained_slices: list[DischargeSlice] = []
    retained_anchors: list[EndpointAnchor] = []
    retained_steps: list[LoadStepObservation] = []
    used = 0
    for item in slices:
        related = tuple(anchor for anchor in anchors if _anchor_matches_slice(anchor, item))
        required = tuple(
            anchor
            for anchor in related
            if anchor.canonical_hash
            in {
                boundary.canonical_hash
                for boundary in (item.start_anchor, item.end_anchor)
                if boundary
            }
        )
        if used + 1 + len(required) > maximum_descriptors:
            break
        retained_slices.append(item)
        retained_anchors.extend(required)
        used += 1 + len(required)
        for anchor in related:
            if anchor in required or used >= maximum_descriptors:
                continue
            retained_anchors.append(anchor)
            used += 1
        for step in steps:
            if step.parent_slice.slice_id != item.slice_id or used >= maximum_descriptors:
                continue
            retained_steps.append(step)
            used += 1
    return tuple(retained_slices), tuple(retained_anchors), tuple(retained_steps)


def _overflow_counts(
    first: DischargeFragmentProfile, source: _RetentionSet, kept: _RetentionSet
) -> tuple[int, int, int]:
    return (
        first.anchor_overflow_count + len(source.anchors) - len(kept.anchors),
        first.slice_overflow_count + len(source.slices) - len(kept.slices),
        first.load_step_overflow_count + len(source.steps) - len(kept.steps),
    )


def _first_dropped_unit(
    source: _RetentionSet, kept: _RetentionSet
) -> tuple[OmittedFragmentKind, str] | None:
    kept_slice_ids = {item.slice_id for item in kept.slices}
    kept_anchor_hashes = {item.canonical_hash for item in kept.anchors}
    kept_step_hashes = {item.step_record_hash for item in kept.steps}
    for item in source.slices:
        if item.slice_id not in kept_slice_ids:
            return OmittedFragmentKind.SLICE, _slice_first_raw_hash(item)
        for anchor in source.anchors:
            if anchor.canonical_hash not in kept_anchor_hashes and _anchor_matches_slice(
                anchor, item
            ):
                return OmittedFragmentKind.ANCHOR, _anchor_raw_hash(anchor, item)
        for step in source.steps:
            if (
                step.step_record_hash not in kept_step_hashes
                and step.parent_slice.slice_id == item.slice_id
            ):
                return OmittedFragmentKind.LOAD_STEP, step.contributing_sample_hashes[0]
    return None


def _anchor_raw_hash(anchor: EndpointAnchor, parent: DischargeSlice) -> str:
    return anchor.source_sample_hash or _slice_first_raw_hash(parent)


def _overflow_summary(
    first: DischargeFragmentProfile,
    source: _RetentionSet,
    kept: _RetentionSet,
    dropped: tuple[OmittedFragmentKind, str] | None,
) -> _OverflowSummary:
    counts = _overflow_counts(first, source, kept)
    total = sum(counts)
    if total == 0:
        return _OverflowSummary(counts, None, None)
    if dropped is not None:
        return _OverflowSummary(counts, dropped[0], dropped[1])
    return _OverflowSummary(counts, first.first_unprofiled_kind, first.first_unprofiled_raw_hash)


def _repartition_profiles(
    first: DischargeFragmentProfile,
    retained: _RetentionSet,
    summary: _OverflowSummary,
    max_slices_per_record: int,
) -> tuple[DischargeFragmentProfile, ...]:
    groups = [
        retained.slices[index : index + max_slices_per_record]
        for index in range(0, len(retained.slices), max_slices_per_record)
    ] or [[]]
    issue_count = sum(summary.counts)
    issues = (ProfileReason.FRAGMENT_BUDGET_EXHAUSTED,) if issue_count else ()
    series = _series_identity(
        first.policy_revision, retained.anchors, retained.slices, retained.steps
    )
    return tuple(
        DischargeFragmentProfile(
            anchors=tuple(
                anchor
                for anchor in retained.anchors
                if any(_anchor_matches_slice(anchor, item) for item in group)
            ),
            slices=tuple(group),
            load_steps=tuple(
                step
                for step in retained.steps
                if any(step.parent_slice.slice_id == item.slice_id for item in group)
            ),
            policy_revision=first.policy_revision,
            profile_issues=issues,
            issue_overflow_count=issue_count,
            first_unprofiled_raw_hash=summary.raw_hash,
            anchor_overflow_count=summary.counts[0],
            slice_overflow_count=summary.counts[1],
            load_step_overflow_count=summary.counts[2],
            series_id=series,
            ordinal=ordinal,
            record_count=len(groups),
            first_unprofiled_kind=summary.kind,
        )
        for ordinal, group in enumerate(groups)
    )


def _record_members(
    layout: _BuilderLayout,
) -> tuple[list[list[EndpointAnchor]], list[list[LoadStepObservation]]]:
    return _record_anchors(layout), _record_steps(layout)


def _record_anchors(layout: _BuilderLayout) -> list[list[EndpointAnchor]]:
    return [
        [
            anchor
            for anchor in layout.kept_anchors
            if any(_anchor_matches_slice(anchor, item) for item in record_slices)
        ]
        for record_slices in layout.record_slices
    ]


def _record_steps(layout: _BuilderLayout) -> list[list[LoadStepObservation]]:
    return [
        [
            step
            for step in layout.kept_steps
            if any(step.parent_slice.slice_id == item.slice_id for item in record_slices)
        ]
        for record_slices in layout.record_slices
    ]


def _validate_builder_inputs(
    anchors: tuple[EndpointAnchor, ...],
    slices: tuple[DischargeSlice, ...],
    load_steps: tuple[LoadStepObservation, ...],
    policy_revision: str,
) -> None:
    _validate_builder_types(anchors, slices, load_steps)
    policy = resolve_fragment_policy(policy_revision)
    _validate_builder_policy(slices, anchors, policy)
    _validate_scope_values(slices, anchors)
    _validate_profile_links_values(anchors, slices, load_steps)


def _validate_builder_types(
    anchors: tuple[EndpointAnchor, ...],
    slices: tuple[DischargeSlice, ...],
    load_steps: tuple[LoadStepObservation, ...],
) -> None:
    collections = (anchors, slices, load_steps)
    if not all(isinstance(value, tuple) for value in collections):
        raise TypeError("fragment builder inputs must be tuples")
    for values, expected, message in (
        (anchors, EndpointAnchor, "fragment builder anchors must be EndpointAnchor"),
        (slices, DischargeSlice, "fragment builder slices must be DischargeSlice"),
        (
            load_steps,
            LoadStepObservation,
            "fragment builder load steps must be LoadStepObservation",
        ),
    ):
        if any(not isinstance(value, expected) for value in values):
            raise TypeError(message)


def _validate_builder_policy(
    slices: tuple[DischargeSlice, ...],
    anchors: tuple[EndpointAnchor, ...],
    policy: DischargeFragmentPolicy,
) -> None:
    if any(item.policy_revision != policy.revision for item in slices):
        raise ValueError("fragment builder slice policy differs from requested policy")
    anchor_hashes = tuple(anchor.canonical_hash for anchor in anchors)
    if len(set(anchor_hashes)) != len(anchor_hashes):
        raise ValueError("fragment builder anchors must be unique")


def _make_builder_layout(
    anchors: tuple[EndpointAnchor, ...],
    slices: tuple[DischargeSlice, ...],
    load_steps: tuple[LoadStepObservation, ...],
    policy_revision: str,
) -> _BuilderLayout:
    policy = resolve_fragment_policy(policy_revision)
    kept_slices, kept_anchors, kept_steps = _retain_dependency_closed(
        anchors, slices, load_steps, policy
    )
    record_count = max(1, ceil(len(kept_slices) / policy.max_slices_per_record))
    return _BuilderLayout(
        policy=policy,
        record_count=record_count,
        capacity=len(kept_slices),
        anchor_overflow=len(anchors) - len(kept_anchors),
        slice_overflow=len(slices) - len(kept_slices),
        step_overflow=len(load_steps) - len(kept_steps),
        kept_anchors=kept_anchors,
        kept_slices=kept_slices,
        kept_steps=kept_steps,
        record_slices=[
            list(
                kept_slices[
                    index * policy.max_slices_per_record : (index + 1)
                    * policy.max_slices_per_record
                ]
            )
            for index in range(record_count)
        ],
    )


def _retain_dependency_closed(
    anchors: tuple[EndpointAnchor, ...],
    slices: tuple[DischargeSlice, ...],
    load_steps: tuple[LoadStepObservation, ...],
    policy: DischargeFragmentPolicy,
) -> tuple[
    tuple[DischargeSlice, ...],
    tuple[EndpointAnchor, ...],
    tuple[LoadStepObservation, ...],
]:
    retained: list[DischargeSlice] = []
    retained_anchors: list[EndpointAnchor] = []
    retained_steps: list[LoadStepObservation] = []
    descriptor_total = 0
    for item in slices:
        related_anchors = tuple(anchor for anchor in anchors if _anchor_matches_slice(anchor, item))
        related_steps = tuple(
            step for step in load_steps if step.parent_slice.slice_id == item.slice_id
        )
        boundary_hashes = {
            boundary.canonical_hash
            for boundary in (item.start_anchor, item.end_anchor)
            if boundary is not None
        }
        required_anchors = tuple(
            anchor for anchor in related_anchors if anchor.canonical_hash in boundary_hashes
        )
        base_size = 1 + len(required_anchors)
        if descriptor_total + base_size > policy.derived_tail_budget.max_compact_descriptors:
            break
        retained.append(item)
        retained_anchors.extend(required_anchors)
        descriptor_total += base_size
        for anchor in related_anchors:
            if anchor in required_anchors:
                continue
            if descriptor_total >= policy.derived_tail_budget.max_compact_descriptors:
                break
            retained_anchors.append(anchor)
            descriptor_total += 1
        for step in related_steps:
            if descriptor_total >= policy.derived_tail_budget.max_compact_descriptors:
                break
            retained_steps.append(step)
            descriptor_total += 1
    return tuple(retained), tuple(retained_anchors), tuple(retained_steps)


@dataclass(frozen=True, slots=True)
class _BuilderLayout:
    policy: DischargeFragmentPolicy
    record_count: int
    capacity: int
    anchor_overflow: int
    slice_overflow: int
    step_overflow: int
    kept_anchors: tuple[EndpointAnchor, ...]
    kept_slices: tuple[DischargeSlice, ...]
    kept_steps: tuple[LoadStepObservation, ...]
    record_slices: list[list[DischargeSlice]]


@dataclass(frozen=True, slots=True)
class _RetentionSet:
    anchors: tuple[EndpointAnchor, ...]
    slices: tuple[DischargeSlice, ...]
    steps: tuple[LoadStepObservation, ...]


@dataclass(frozen=True, slots=True)
class _OverflowSummary:
    counts: tuple[int, int, int]
    kind: OmittedFragmentKind | None
    raw_hash: str | None


def _slice_first_raw_hash(item: DischargeSlice) -> str:
    return item.spans[0].first_sample_hash


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
