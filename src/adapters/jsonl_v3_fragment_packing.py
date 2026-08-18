"""Deterministic physical chunk planning for fragment-profile-v2."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.adapters.jsonl_v3_canonical import (
    MAX_SEQ,
    EncodedV3Record,
    V3CodecError,
    canonical_v3_line_size,
)
from src.adapters.jsonl_v3_fragment_wire import (
    PROFILE_MAX_LINE_BYTES,
    anchor_dict,
    encode_record,
    fragment_record_envelope,
    logical_profile_id,
    series_id,
    slice_dict,
    source_identity,
    span_dict,
    step_dict,
)
from src.domain.fragments import (
    CanonicalDischargeSample,
    CanonicalSampleSpan,
    DischargeFragmentProfile,
    EndpointAnchor,
    LoadStepObservation,
    validate_canonical_sample_span,
)


@dataclass(frozen=True, slots=True)
class Chunk:
    profile: DischargeFragmentProfile
    kind: str
    logical_slice_id: str | None
    ordinal: int
    count: int
    spans: tuple[CanonicalSampleSpan, ...] = ()
    anchors: tuple[tuple[str, EndpointAnchor], ...] = ()
    steps: tuple[tuple[str, LoadStepObservation], ...] = ()


@dataclass(frozen=True, slots=True)
class PlanContext:
    source: dict[str, Any]
    digest: str
    series: str
    logical_ids: Mapping[int, str]
    profile_count: int


def raw_samples(
    profiles: tuple[DischargeFragmentProfile, ...],
    supplied: Mapping[str, tuple[CanonicalDischargeSample, ...]],
) -> dict[str, tuple[CanonicalDischargeSample, ...]]:
    result = dict(supplied)
    expected_ids = {item.slice_id for profile in profiles for item in profile.slices}
    if set(result) != expected_ids:
        raise V3CodecError("raw_samples_by_slice must match profile slices exactly")
    for profile in profiles:
        for item in profile.slices:
            samples = result.get(item.slice_id)
            if samples is None:
                raise V3CodecError("raw_samples_by_slice must contain every slice")
            if not isinstance(samples, tuple) or any(
                not isinstance(sample, CanonicalDischargeSample) for sample in samples
            ):
                raise V3CodecError("raw samples must be CanonicalDischargeSample tuples")
            if item.samples and samples != item.samples:
                raise V3CodecError("supplied raw samples differ from domain slice")
            by_sequence = {sample.sequence: sample for sample in samples}
            for span in item.spans:
                selected = tuple(
                    by_sequence.get(index)
                    for index in range(span.first_sequence, span.last_sequence + 1)
                )
                if len(selected) != span.sample_count or any(sample is None for sample in selected):
                    raise V3CodecError("raw samples do not cover a span")
                try:
                    validate_canonical_sample_span(span, selected)  # type: ignore[arg-type]
                except (TypeError, ValueError) as exc:
                    raise V3CodecError("raw samples do not validate a span") from exc
    return result


def _anchors(
    profile: DischargeFragmentProfile, samples: Mapping[str, tuple[CanonicalDischargeSample, ...]]
) -> tuple[tuple[str, EndpointAnchor], ...]:
    result: list[tuple[str, EndpointAnchor]] = []
    for anchor in profile.anchors:
        matches = tuple(
            item.slice_id
            for item in profile.slices
            if anchor.source_sample_hash is not None
            and any(
                sample.canonical_hash == anchor.source_sample_hash
                for sample in samples[item.slice_id]
            )
        )
        if not matches:
            matches = tuple(
                item.slice_id
                for item in profile.slices
                if anchor.canonical_hash
                in {
                    boundary.canonical_hash
                    for boundary in (item.start_anchor, item.end_anchor)
                    if boundary
                }
            )
        if len(matches) != 1:
            raise V3CodecError("anchor must link to exactly one slice")
        result.append((matches[0], anchor))
    return tuple(result)


def _base_payload(
    chunk: Chunk, context: PlanContext, physical_ordinal: int, physical_count: int
) -> dict[str, Any]:
    first = chunk.profile.slices[0]
    value: dict[str, Any] = {
        "profile_schema": "fragment-profile-v2",
        "series_id": context.series,
        "logical_profile_id": context.logical_ids[chunk.profile.ordinal],
        "logical_profile_ordinal": chunk.profile.ordinal,
        "logical_profile_count": context.profile_count,
        "record_ordinal": physical_ordinal,
        "record_count": physical_count,
        "policy_revision": chunk.profile.policy_revision,
        "blackout_id": first.blackout_id,
        "physical_episode_id": first.physical_episode_id,
        "battery_epoch_id": first.battery_epoch_id,
        "segment_id": first.segment_id,
        "observation_origin": first.origin.value,
        "uat_intent_id": first.uat_intent_id,
        "source_range": context.source,
        "source_digest": context.digest,
        "chunk_kind": chunk.kind,
        "logical_slice_id": chunk.logical_slice_id,
        "chunk_ordinal": chunk.ordinal,
        "chunk_count": chunk.count,
        "profile_issues": [issue.value for issue in chunk.profile.profile_issues],
        "issue_overflow_count": chunk.profile.issue_overflow_count,
        "first_unprofiled_raw_hash": chunk.profile.first_unprofiled_raw_hash,
        "first_unprofiled_kind": (
            chunk.profile.first_unprofiled_kind.value
            if chunk.profile.first_unprofiled_kind is not None
            else None
        ),
        "overflow": {
            "anchor_omitted_count": chunk.profile.anchor_overflow_count,
            "slice_omitted_count": chunk.profile.slice_overflow_count,
            "load_step_omitted_count": chunk.profile.load_step_overflow_count,
            "first_unprofiled_raw_hash": chunk.profile.first_unprofiled_raw_hash,
            "first_unprofiled_kind": (
                chunk.profile.first_unprofiled_kind.value
                if chunk.profile.first_unprofiled_kind is not None
                else None
            ),
        },
    }
    if chunk.kind == "slice_head":
        item = next(
            item for item in chunk.profile.slices if item.slice_id == chunk.logical_slice_id
        )
        value["chunk"] = {"slice": slice_dict(item, chunk.spans)}
    elif chunk.kind == "slice_span_continuation":
        value["chunk"] = {"spans": [span_dict(span) for span in chunk.spans]}
    elif chunk.kind == "anchor_chunk":
        value["chunk"] = {
            "anchors": [
                {"logical_slice_id": key, "anchor": anchor_dict(anchor)}
                for key, anchor in chunk.anchors
            ]
        }
    else:
        value["chunk"] = {
            "load_steps": [
                {"logical_slice_id": key, "load_step": step_dict(step)} for key, step in chunk.steps
            ]
        }
    return value


def _fits(chunk: Chunk, context: PlanContext) -> bool:
    payload = _base_payload(chunk, context, 95, 96)
    try:
        envelope = fragment_record_envelope(payload, context.source, MAX_SEQ, "0" * 64)
        return canonical_v3_line_size(envelope) <= PROFILE_MAX_LINE_BYTES
    except (TypeError, ValueError, V3CodecError):
        return False


def _pack(values: tuple[Any, ...], make: Any, context: PlanContext) -> tuple[Chunk, ...]:
    result: list[Chunk] = []
    start = 0
    while start < len(values):
        end = start + 1
        while end <= len(values) and _fits(make(values[start:end], len(result), 1), context):
            end += 1
        end -= 1
        if end == start:
            raise V3CodecError("one typed chunk exceeds 8 KiB")
        result.append(make(values[start:end], len(result), 0))
        start = end
    total = len(result)
    return tuple(
        Chunk(
            item.profile,
            item.kind,
            item.logical_slice_id,
            item.ordinal,
            total,
            item.spans,
            item.anchors,
            item.steps,
        )
        for item in result
    )


def plan_chunks(
    profiles: tuple[DischargeFragmentProfile, ...],
    samples: Mapping[str, tuple[CanonicalDischargeSample, ...]],
) -> tuple[tuple[Chunk, ...], PlanContext]:
    source, digest = source_identity(tuple(item for profile in profiles for item in profile.slices))
    all_anchors = tuple(
        sorted(
            (anchor for profile in profiles for anchor in profile.anchors),
            key=lambda anchor: anchor.canonical_hash,
        )
    )
    all_slices = tuple(item for profile in profiles for item in profile.slices)
    all_steps = tuple(step for profile in profiles for step in profile.load_steps)
    series = series_id(profiles[0].policy_revision, all_anchors, all_slices, all_steps)
    logical_ids = {profile.ordinal: logical_profile_id(profile, series) for profile in profiles}
    context = PlanContext(source, digest, series, logical_ids, len(profiles))
    result: list[Chunk] = []
    for profile in profiles:
        for item in profile.slices:
            groups: list[tuple[CanonicalSampleSpan, ...]] = []
            start = 0
            while start < len(item.spans):
                end = start + 1
                while end <= len(item.spans):
                    candidate = Chunk(
                        profile,
                        "slice_head" if start == 0 else "slice_span_continuation",
                        item.slice_id,
                        0,
                        1,
                        tuple(item.spans[start:end]),
                    )
                    if not _fits(candidate, context):
                        break
                    end += 1
                end -= 1
                if end == start:
                    raise V3CodecError("one typed slice chunk exceeds 8 KiB")
                groups.append(tuple(item.spans[start:end]))
                start = end
            result.extend(
                Chunk(
                    profile,
                    "slice_head" if index == 0 else "slice_span_continuation",
                    item.slice_id,
                    index,
                    len(groups),
                    spans=group,
                )
                for index, group in enumerate(groups)
            )
        anchors = _anchors(profile, samples)
        result.extend(
            _pack(
                anchors,
                lambda values, ordinal, count: Chunk(
                    profile, "anchor_chunk", None, ordinal, count, anchors=tuple(values)
                ),
                context,
            )
        )
        steps = tuple((step.parent_slice.slice_id, step) for step in profile.load_steps)
        result.extend(
            _pack(
                steps,
                lambda values, ordinal, count: Chunk(
                    profile, "load_step_chunk", None, ordinal, count, steps=tuple(values)
                ),
                context,
            )
        )
    return tuple(
        Chunk(
            item.profile,
            item.kind,
            item.logical_slice_id,
            item.ordinal,
            item.count,
            item.spans,
            item.anchors,
            item.steps,
        )
        for item in result
    ), context


def encode_chunks(
    chunks: tuple[Chunk, ...], context: PlanContext, seq: int, previous: str | None
) -> tuple[EncodedV3Record, ...]:
    records: list[EncodedV3Record] = []
    for ordinal, chunk in enumerate(chunks):
        payload = _base_payload(chunk, context, ordinal, len(chunks))
        record = encode_record(payload, context.source, seq + ordinal, previous)
        if len(record.line) > PROFILE_MAX_LINE_BYTES:
            raise V3CodecError("fragment profile record exceeds 8 KiB")
        records.append(record)
        previous = record.record_sha256
    return tuple(records)
