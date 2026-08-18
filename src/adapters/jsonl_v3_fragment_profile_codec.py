"""Public facade for the strict, lossless fragment-profile-v2 codec."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from src.adapters.jsonl_v3_canonical import EncodedV3Record, V3CodecError
from src.adapters.jsonl_v3_fragment_packing import (
    Chunk,
    PlanContext,
    encode_chunks,
    plan_chunks,
    raw_samples,
)
from src.adapters.jsonl_v3_fragment_replay import decode_chain, descriptor_count, reconstruct
from src.adapters.jsonl_v3_fragment_wire import (
    PROFILE_MAX_LINE_BYTES,
    decode_record,
)
from src.adapters.jsonl_v3_fragment_wire import (
    PROFILE_PROVENANCE as _PROFILE_PROVENANCE,
)
from src.adapters.jsonl_v3_fragment_wire import (
    PROFILE_RECORD_TYPE as _PROFILE_RECORD_TYPE,
)
from src.adapters.jsonl_v3_fragment_wire import (
    PROFILE_SCHEMA as _PROFILE_SCHEMA,
)
from src.domain.fragment_policy import DEFAULT_DISCHARGE_FRAGMENT_POLICY
from src.domain.fragments import (
    CanonicalDischargeSample,
    DischargeFragmentProfile,
    truncate_discharge_fragment_profiles,
)

_POLICY = DEFAULT_DISCHARGE_FRAGMENT_POLICY
MAX_DERIVED_RECORDS = _POLICY.derived_tail_budget.max_derived_records
MAX_DERIVED_RECORD_BYTES = _POLICY.derived_tail_budget.max_derived_record_bytes
MAX_COMPACT_DESCRIPTORS = _POLICY.derived_tail_budget.max_compact_descriptors
MAX_DESCRIPTOR_BYTES = _POLICY.derived_tail_budget.max_descriptor_bytes
MAX_TOTAL_BYTES = _POLICY.derived_tail_budget.max_total_bytes
MAX_PROFILE_RECORDS = _POLICY.max_profile_records
PROFILE_PROVENANCE = _PROFILE_PROVENANCE
PROFILE_RECORD_TYPE = _PROFILE_RECORD_TYPE
PROFILE_SCHEMA = _PROFILE_SCHEMA


def _validate_profiles(values: tuple[DischargeFragmentProfile, ...]) -> None:
    if not values:
        raise V3CodecError("profile chain must not be empty")
    count = values[0].record_count
    if count != len(values) or tuple(item.record_count for item in values) != (count,) * count:
        raise V3CodecError("logical profile count is incomplete")
    if tuple(item.ordinal for item in values) != tuple(range(count)):
        raise V3CodecError("logical profile ordinals are not contiguous")
    if any(not isinstance(item, DischargeFragmentProfile) for item in values):
        raise V3CodecError("profile chain contains an invalid domain profile")


def encode_fragment_profiles(
    profiles: Iterable[DischargeFragmentProfile],
    raw_samples_by_slice: Mapping[str, tuple[CanonicalDischargeSample, ...]],
    *,
    seq: int = 0,
    previous_record_sha256: str | None = None,
) -> tuple[EncodedV3Record, ...]:
    values = tuple(profiles)
    _validate_profiles(values)
    samples = raw_samples(values, raw_samples_by_slice)
    chunks, context, selected_samples = _plan_physical_prefix(values, samples)
    samples = selected_samples
    return encode_chunks(chunks, context, seq, previous_record_sha256)


def _plan_physical_prefix(
    values: tuple[DischargeFragmentProfile, ...],
    samples: Mapping[str, tuple[CanonicalDischargeSample, ...]],
) -> tuple[tuple[Chunk, ...], PlanContext, dict[str, tuple[CanonicalDischargeSample, ...]]]:
    chunks, context = plan_chunks(values, samples)
    if len(chunks) <= MAX_PROFILE_RECORDS:
        return chunks, context, dict(samples)
    seen: set[tuple[object, ...]] = set()
    selected: (
        tuple[
            tuple[DischargeFragmentProfile, ...],
            tuple[Chunk, ...],
            PlanContext,
            dict[str, tuple[CanonicalDischargeSample, ...]],
        ]
        | None
    ) = None
    for descriptor_budget in range(MAX_COMPACT_DESCRIPTORS - 1, 0, -1):
        candidate = truncate_discharge_fragment_profiles(values, descriptor_budget)
        signature = _candidate_signature(candidate)
        if signature in seen or _chunk_lower_bound(candidate) > MAX_PROFILE_RECORDS:
            seen.add(signature)
            continue
        seen.add(signature)
        candidate_samples = {
            item.slice_id: samples[item.slice_id]
            for profile in candidate
            for item in profile.slices
        }
        candidate_chunks, candidate_context = plan_chunks(candidate, candidate_samples)
        if len(candidate_chunks) <= MAX_PROFILE_RECORDS:
            selected = candidate, candidate_chunks, candidate_context, candidate_samples
            break
    if selected is not None:
        candidate, chunks, context, candidate_samples = selected
        checked_chunks, checked_context = plan_chunks(candidate, candidate_samples)
        if _chunk_signature(chunks) != _chunk_signature(checked_chunks):
            raise V3CodecError("fragment physical plan is not deterministic")
        if (
            context.series != checked_context.series
            or context.profile_count != checked_context.profile_count
        ):
            raise V3CodecError("fragment physical plan context is not deterministic")
        return chunks, context, candidate_samples
    raise V3CodecError("no lossless fragment prefix fits the physical record budget")


def _candidate_signature(profiles: tuple[DischargeFragmentProfile, ...]) -> tuple[object, ...]:
    first = profiles[0]
    return (
        first.series_id,
        len(tuple(anchor for profile in profiles for anchor in profile.anchors)),
        len(tuple(item for profile in profiles for item in profile.slices)),
        len(tuple(step for profile in profiles for step in profile.load_steps)),
        first.anchor_overflow_count,
        first.slice_overflow_count,
        first.load_step_overflow_count,
        first.first_unprofiled_kind,
        first.first_unprofiled_raw_hash,
        len(profiles),
    )


def _chunk_lower_bound(profiles: tuple[DischargeFragmentProfile, ...]) -> int:
    return (
        sum(len(profile.slices) for profile in profiles)
        + sum(bool(profile.anchors) for profile in profiles)
        + sum(bool(profile.load_steps) for profile in profiles)
    )


def _chunk_signature(chunks: tuple[Chunk, ...]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            item.kind,
            item.logical_slice_id,
            item.ordinal,
            item.count,
            len(item.spans),
            len(item.anchors),
            len(item.steps),
        )
        for item in chunks
    )


def encode_fragment_profile(
    profile: DischargeFragmentProfile,
    raw_samples_by_slice: Mapping[str, tuple[CanonicalDischargeSample, ...]],
    *,
    seq: int = 0,
    previous_record_sha256: str | None = None,
) -> tuple[EncodedV3Record, ...]:
    return encode_fragment_profiles(
        (profile,), raw_samples_by_slice, seq=seq, previous_record_sha256=previous_record_sha256
    )


def decode_fragment_profile_record(line: bytes) -> EncodedV3Record:
    return decode_record(line, PROFILE_MAX_LINE_BYTES)


def decode_fragment_profile_records(lines: Iterable[bytes]) -> tuple[EncodedV3Record, ...]:
    return decode_chain(tuple(lines), PROFILE_MAX_LINE_BYTES)


def profile_descriptor_count(records: Sequence[EncodedV3Record]) -> int:
    return descriptor_count(records)


def reconstruct_fragment_profiles(
    records: Sequence[EncodedV3Record],
    raw_samples_by_slice: Mapping[str, tuple[CanonicalDischargeSample, ...]],
) -> tuple[DischargeFragmentProfile, ...]:
    return reconstruct(records, raw_samples_by_slice, PROFILE_MAX_LINE_BYTES)
