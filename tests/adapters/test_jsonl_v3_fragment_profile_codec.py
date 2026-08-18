"""Contract tests for the bounded, concrete v3 fragment-profile codec."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import cast

import pytest

from src.adapters.jsonl_v3_canonical import (
    MAX_LINE_BYTES,
    MAX_MONOTONIC_NS,
    MAX_SEQ,
    V3CodecError,
    V3RecordEnvelope,
    canonical_v3_line_size,
    decode_v3_record,
    encode_v3_record,
)
from src.adapters.jsonl_v3_fragment_packing import Chunk, _fits, encode_chunks, plan_chunks
from src.adapters.jsonl_v3_fragment_profile_codec import (
    MAX_COMPACT_DESCRIPTORS,
    MAX_DERIVED_RECORD_BYTES,
    MAX_DERIVED_RECORDS,
    MAX_DESCRIPTOR_BYTES,
    MAX_PROFILE_RECORDS,
    MAX_TOTAL_BYTES,
    PROFILE_MAX_LINE_BYTES,
    PROFILE_PROVENANCE,
    PROFILE_RECORD_TYPE,
    PROFILE_SCHEMA,
    decode_fragment_profile_record,
    decode_fragment_profile_records,
    encode_fragment_profile,
    encode_fragment_profiles,
    profile_descriptor_count,
    reconstruct_fragment_profiles,
)
from src.domain.fragment_policy import DEFAULT_DISCHARGE_FRAGMENT_POLICY as POLICY_OBJECT
from src.domain.fragments import (
    MAX_MONOTONIC_NS as DOMAIN_MAX_MONOTONIC_NS,
)
from src.domain.fragments import (
    UINT64_MAX,
    AnchorKind,
    AnchorProvenance,
    CanonicalDischargeSample,
    CanonicalSampleSpan,
    DischargeFragmentProfile,
    DischargeSlice,
    EndpointAnchor,
    ObservationOrigin,
    OmittedFragmentKind,
    ProfileReason,
    build_canonical_sample_span,
    build_discharge_fragment_profiles,
    truncate_discharge_fragment_profiles,
)
from tests.domain.test_fragments import _many_profile_inputs, _sample

START = datetime(2026, 8, 18, tzinfo=timezone.utc)
POLICY = POLICY_OBJECT.revision


def _profile_inputs(count: int = 1) -> tuple[DischargeFragmentProfile, ...]:
    anchors, slices, steps = _many_profile_inputs(count)
    return build_discharge_fragment_profiles(anchors, slices, steps, POLICY)


def _raw(profiles: tuple[DischargeFragmentProfile, ...]) -> dict[str, tuple]:
    return {item.slice_id: item.samples for profile in profiles for item in profile.slices}


def _span_only(count: int) -> CanonicalSampleSpan:
    digest = hashlib.sha256()
    for sequence in range(count):
        digest.update(_hash(f"span-sample-{sequence}").encode("ascii"))
    return CanonicalSampleSpan(
        first_sequence=0,
        last_sequence=count - 1,
        sample_count=count,
        first_sample_hash=_hash("span-sample-0"),
        last_sample_hash=_hash(f"span-sample-{count - 1}"),
        ordered_sample_hashes_sha256=digest.hexdigest(),
        boot_id="boot-a",
        first_monotonic_ns=0,
        last_monotonic_ns=(count - 1) * 1_000_000_000,
        first_wall_time_utc=START,
        last_wall_time_utc=START + timedelta(seconds=count - 1),
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _span_profile(count: int) -> DischargeFragmentProfile:
    parent = DischargeSlice(
        samples=(),
        blackout_id="blackout-a",
        physical_episode_id="episode-a",
        battery_epoch_id="epoch-a",
        segment_id="segment-a",
        origin=ObservationOrigin.NATURAL,
        policy_revision=POLICY,
        spans=(_span_only(count),),
    )
    return DischargeFragmentProfile((), (parent,), (), POLICY)


def _minimal_envelope(**changes: object) -> V3RecordEnvelope:
    value: dict[str, object] = {
        "schema_version": 3,
        "record_type": PROFILE_RECORD_TYPE,
        "provenance": PROFILE_PROVENANCE,
        "blackout_id": "blackout-a",
        "segment_id": "segment-a",
        "seq": 0,
        "boot_id": "boot-a",
        "wall_time_utc": "2026-08-18T00:00:00Z",
        "monotonic_ns": 0,
        "prev_record_sha256": None,
        "payload": {},
    }
    value.update(changes)
    return V3RecordEnvelope(**value)  # type: ignore[arg-type]


def _rehash_payload(record, payload: dict) -> bytes:
    value = json.loads(record.line)
    return encode_v3_record(
        V3RecordEnvelope(**{**value, "record_sha256": None, "payload": payload})
    ).line


def _record_of_kind(kind: str):
    profiles = _profile_inputs(17)
    records = encode_fragment_profiles(profiles, _raw(profiles))
    return next(record for record in records if record.envelope.payload["chunk_kind"] == kind)


def _multi_span_profile(span_count: int = 64) -> tuple[DischargeFragmentProfile, dict[str, tuple]]:
    anchors, slices, _ = _many_profile_inputs(1)
    samples = tuple(_sample(index) for index in range(span_count))
    spans = tuple(build_canonical_sample_span((sample,)) for sample in samples)
    parent = replace(slices[0], samples=samples, spans=spans)
    profile = DischargeFragmentProfile(anchors, (parent,), (), POLICY)
    return profile, {parent.slice_id: samples}


def test_public_builder_profile_is_byte_idempotent_and_under_8k() -> None:
    profiles = _profile_inputs(17)
    records = encode_fragment_profiles(profiles, _raw(profiles))
    assert records
    assert all(len(record.line) <= PROFILE_MAX_LINE_BYTES for record in records)
    assert all(
        decode_fragment_profile_record(record.line).line == record.line for record in records
    )
    assert all(record.envelope.payload["profile_schema"] == PROFILE_SCHEMA for record in records)
    assert decode_fragment_profile_records(record.line for record in records) == records


def test_intermediate_anchor_survives_physical_profile_split_once() -> None:
    anchors, slices, steps = _many_profile_inputs(17)
    anchor = EndpointAnchor(
        _hash("intermediate-anchor"),
        AnchorKind.RAW_FIRMWARE_LB,
        AnchorProvenance.FIRMWARE,
        "boot-a",
        START + timedelta(seconds=1),
        1_000_000_000,
        slices[0].samples[1].canonical_hash,
        blackout_id="blackout-a",
        physical_episode_id="episode-a",
        segment_id="segment-a",
    )
    profiles = build_discharge_fragment_profiles(anchors + (anchor,), slices, steps, POLICY)
    records = encode_fragment_profiles(profiles, _raw(profiles))
    encoded = [
        item["anchor"]["canonical_hash"]
        for record in records
        if record.envelope.payload["chunk_kind"] == "anchor_chunk"
        for item in record.envelope.payload["chunk"]["anchors"]
    ]
    assert encoded.count(anchor.canonical_hash) == 1
    assert decode_fragment_profile_records(record.line for record in records) == records


def test_logical_profile_identity_is_explicit_and_recomputed() -> None:
    profiles = _profile_inputs(33)
    records = encode_fragment_profiles(profiles, _raw(profiles))
    logical = {
        (
            record.envelope.payload["logical_profile_ordinal"],
            record.envelope.payload["logical_profile_id"],
        )
        for record in records
    }
    assert len(logical) == 3
    assert {item[0] for item in logical} == {0, 1, 2}
    assert {record.envelope.payload["logical_profile_count"] for record in records} == {3}

    value = json.loads(records[0].line)
    payload = {**value["payload"], "logical_profile_id": "0" * 64}
    mutated = encode_v3_record(
        V3RecordEnvelope(**{**value, "record_sha256": None, "payload": payload})
    ).line
    with pytest.raises(V3CodecError):
        decode_fragment_profile_records((mutated, *(record.line for record in records[1:])))


def test_actual_derived_budget_and_descriptor_reservations() -> None:
    assert MAX_DERIVED_RECORDS == 128
    assert MAX_DERIVED_RECORD_BYTES == 8 * 1024
    assert MAX_COMPACT_DESCRIPTORS == 256
    assert MAX_DESCRIPTOR_BYTES == 256
    profiles = _profile_inputs(64)
    records = encode_fragment_profiles(profiles, _raw(profiles))
    assert len(records) <= MAX_DERIVED_RECORDS
    total = sum(len(record.line) for record in records)
    assert total < MAX_TOTAL_BYTES
    assert MAX_TOTAL_BYTES - total > 0


def test_constructible_max_builder_chain_has_no_bypass_and_exact_overflow() -> None:
    profiles = _profile_inputs(64)
    assert len(profiles) == 4
    assert all(profile.issue_overflow_count == 0 for profile in profiles)
    records = encode_fragment_profiles(profiles, _raw(profiles))
    assert len(records) <= MAX_DERIVED_RECORDS
    assert profile_descriptor_count(records) == 192
    assert all(len(record.line) <= PROFILE_MAX_LINE_BYTES for record in records)
    overflow = records[-1].envelope.payload["overflow"]
    assert (
        sum(
            overflow[key]
            for key in ("anchor_omitted_count", "slice_omitted_count", "load_step_omitted_count")
        )
        == 0
    )


def test_builder257_overflow_metadata_roundtrips_in_bounded_profile() -> None:
    built = _profile_inputs(257)
    assert len(built) == 6
    source = built[0]
    profile = replace(source, ordinal=0, record_count=1, series_id="")
    raw = {item.slice_id: item.samples for item in profile.slices}
    records = encode_fragment_profile(profile, raw)
    assert len(records) == 21
    assert max(len(record.line) for record in records) == 7_778
    assert max(len(record.line) for record in records) <= PROFILE_MAX_LINE_BYTES
    assert profile.anchor_overflow_count == 172
    assert profile.slice_overflow_count == 171
    assert profile.load_step_overflow_count == 172
    assert profile.issue_overflow_count == 515
    assert reconstruct_fragment_profiles(records, raw) == (profile,)
    payload = records[0].envelope.payload
    assert payload["overflow"] == {
        "anchor_omitted_count": 172,
        "slice_omitted_count": 171,
        "load_step_omitted_count": 172,
        "first_unprofiled_raw_hash": profile.first_unprofiled_raw_hash,
        "first_unprofiled_kind": "anchor",
    }


def test_builder257_selector_is_deterministic_and_raw_reconstructable() -> None:
    profiles = _profile_inputs(257)
    raw = _raw(profiles)
    records = encode_fragment_profiles(profiles, raw)
    repeated = encode_fragment_profiles(profiles, raw)

    assert tuple(record.line for record in records) == tuple(record.line for record in repeated)
    assert len(records) == MAX_PROFILE_RECORDS
    assert profile_descriptor_count(records) == 219
    assert sum(record.envelope.payload["chunk_kind"] == "slice_head" for record in records) == 73
    assert sum(record.envelope.payload["chunk_kind"] == "anchor_chunk" for record in records) == 9
    assert (
        sum(record.envelope.payload["chunk_kind"] == "load_step_chunk" for record in records) == 14
    )
    decoded = reconstruct_fragment_profiles(records, raw)
    assert sum(len(profile.slices) for profile in decoded) == 73
    assert all(profile.slice_overflow_count == 184 for profile in decoded)
    assert all(profile.anchor_overflow_count == 184 for profile in decoded)
    assert all(profile.load_step_overflow_count == 184 for profile in decoded)
    assert max(len(record.line) for record in records) == 7_777


def test_selector_exhaustively_keeps_largest_feasible_prefix() -> None:
    _, slices, _ = _many_profile_inputs(97)
    profiles = build_discharge_fragment_profiles((), slices, (), POLICY)
    raw = _raw(profiles)
    observed: list[tuple[int, int]] = []
    for budget in range(1, MAX_COMPACT_DESCRIPTORS + 1):
        candidate = truncate_discharge_fragment_profiles(profiles, budget)
        candidate_raw = _raw(candidate)
        planned, _ = plan_chunks(candidate, candidate_raw)
        observed.append((budget, len(planned)))

    assert max(budget for budget, count in observed if count <= MAX_PROFILE_RECORDS) == 96
    assert all(count > MAX_PROFILE_RECORDS for budget, count in observed if budget > 96)
    assert all(count == budget for budget, count in observed if budget <= 96)

    records = encode_fragment_profiles(profiles, raw)
    assert len(records) == MAX_PROFILE_RECORDS


@pytest.mark.parametrize("count", (16, 17))
def test_logical_profile_count_boundary_is_exact(count: int) -> None:
    profiles = _profile_inputs(count)
    records = encode_fragment_profiles(profiles, _raw(profiles))
    expected = len(profiles)
    assert {record.envelope.payload["logical_profile_count"] for record in records} == {expected}
    assert decode_fragment_profile_records(record.line for record in records)


@pytest.mark.parametrize("kind", tuple(OmittedFragmentKind))
def test_first_unprofiled_kind_transitions_roundtrip(kind: OmittedFragmentKind) -> None:
    profile = _profile_inputs(1)[0]
    raw_hash = profile.slices[0].spans[0].first_sample_hash
    counts = {
        OmittedFragmentKind.ANCHOR: (1, 0, 0),
        OmittedFragmentKind.SLICE: (0, 1, 0),
        OmittedFragmentKind.LOAD_STEP: (0, 0, 1),
    }[kind]
    value = replace(
        profile,
        profile_issues=(ProfileReason.FRAGMENT_BUDGET_EXHAUSTED,),
        issue_overflow_count=1,
        anchor_overflow_count=counts[0],
        slice_overflow_count=counts[1],
        load_step_overflow_count=counts[2],
        first_unprofiled_raw_hash=raw_hash,
        first_unprofiled_kind=kind,
        series_id="",
    )
    records = encode_fragment_profile(value, _raw((value,)))
    payload = records[0].envelope.payload
    assert payload["first_unprofiled_kind"] == kind.value
    assert payload["overflow"]["first_unprofiled_kind"] == kind.value
    assert reconstruct_fragment_profiles(records, _raw((value,))) == (value,)


@pytest.mark.parametrize("count", (9, 10, 99, 100))
def test_decimal_overflow_boundaries_roundtrip(count: int) -> None:
    profile = _profile_inputs(1)[0]
    value = replace(
        profile,
        profile_issues=(ProfileReason.FRAGMENT_BUDGET_EXHAUSTED,),
        issue_overflow_count=count,
        anchor_overflow_count=count,
        slice_overflow_count=0,
        load_step_overflow_count=0,
        first_unprofiled_raw_hash=profile.slices[0].spans[0].first_sample_hash,
        first_unprofiled_kind=OmittedFragmentKind.ANCHOR,
        series_id="",
    )
    records = encode_fragment_profile(value, _raw((value,)))
    overflow = records[0].envelope.payload["overflow"]
    assert overflow["anchor_omitted_count"] == count
    assert reconstruct_fragment_profiles(records, _raw((value,))) == (value,)


def test_planner_count_and_actual_line_size_cover_all_chunk_kinds() -> None:
    multi_span, multi_raw = _multi_span_profile()
    cases = (
        (_profile_inputs(1), None),
        (_profile_inputs(17), None),
        ((multi_span,), multi_raw),
    )
    seen: set[str] = set()
    for profiles, supplied_raw in cases:
        raw = _raw(profiles) if supplied_raw is None else supplied_raw
        planned, _ = plan_chunks(profiles, raw)
        records = encode_fragment_profiles(profiles, raw)
        assert len(records) == len(planned)
        assert all(
            canonical_v3_line_size(record.envelope) == len(record.line) for record in records
        )
        seen.update(record.envelope.payload["chunk_kind"] for record in records)
    assert seen == {
        "slice_head",
        "slice_span_continuation",
        "anchor_chunk",
        "load_step_chunk",
    }


def test_local_anchor_pack_fits_each_group_and_rejects_next_item() -> None:
    profiles = _profile_inputs(257)
    raw = _raw(profiles)
    planned, context = plan_chunks(profiles, raw)
    anchor_chunks = tuple(item for item in planned if item.kind == "anchor_chunk")
    groups: dict[int, list[Chunk]] = {}
    for chunk in anchor_chunks:
        groups.setdefault(chunk.profile.ordinal, []).append(chunk)
    group = max(groups.values(), key=len)
    assert len(group) > 1
    for index, chunk in enumerate(group):
        assert _fits(chunk, context)
        if index + 1 == len(group):
            continue
        next_item = group[index + 1].anchors[0]
        candidate = Chunk(
            chunk.profile,
            chunk.kind,
            chunk.logical_slice_id,
            chunk.ordinal,
            chunk.count,
            anchors=(*chunk.anchors, next_item),
        )
        assert not _fits(candidate, context)


def test_boundary_seq_multi_record_pack_matches_predicted_sizes() -> None:
    profiles = _profile_inputs(257)
    raw = _raw(profiles)
    baseline = encode_fragment_profiles(profiles, raw)
    start_seq = MAX_SEQ - len(baseline) + 1

    records = encode_fragment_profiles(profiles, raw, seq=start_seq)
    assert len(records) == len(baseline) == MAX_PROFILE_RECORDS
    assert records[-1].envelope.seq == MAX_SEQ
    assert all(
        canonical_v3_line_size(record.envelope) == len(record.line) <= PROFILE_MAX_LINE_BYTES
        for record in records
    )

    selected = reconstruct_fragment_profiles(records, raw)
    selected_raw = _raw(selected)
    planned, context = plan_chunks(selected, selected_raw)
    predicted = encode_chunks(planned, context, start_seq, None)
    assert tuple(record.line for record in predicted) == tuple(record.line for record in records)

    anchor_group = next(
        chunk for chunk in planned if chunk.kind == "anchor_chunk" and chunk.anchors
    )
    next_anchor = next(
        chunk.anchors[0]
        for chunk in planned
        if chunk.kind == "anchor_chunk"
        and chunk.profile.ordinal == anchor_group.profile.ordinal
        and chunk.ordinal == anchor_group.ordinal + 1
    )
    candidate = Chunk(
        anchor_group.profile,
        anchor_group.kind,
        anchor_group.logical_slice_id,
        anchor_group.ordinal,
        anchor_group.count,
        anchors=(*anchor_group.anchors, next_anchor),
    )
    assert _fits(anchor_group, context)
    assert not _fits(candidate, context)


def test_maximum_scoped_ids_remain_canonical_and_bounded() -> None:
    profile = _profile_inputs(1)[0]
    scope = "x" * 128
    parent = replace(
        profile.slices[0],
        blackout_id=scope,
        physical_episode_id=scope,
        battery_epoch_id=scope,
        segment_id=scope,
    )
    anchors = tuple(
        replace(
            anchor,
            blackout_id=scope,
            physical_episode_id=scope,
            segment_id=scope,
        )
        for anchor in profile.anchors
    )
    value = DischargeFragmentProfile(anchors, (parent,), (), POLICY)
    records = encode_fragment_profile(value, {parent.slice_id: parent.samples})
    assert records
    assert all(len(record.line) <= PROFILE_MAX_LINE_BYTES for record in records)
    assert all(canonical_v3_line_size(record.envelope) == len(record.line) for record in records)


def test_overflow_counters_are_uint64_and_sum_exactly() -> None:
    profile = replace(_profile_inputs(257)[0], ordinal=0, record_count=1, series_id="")
    raw = {item.slice_id: item.samples for item in profile.slices}
    record = encode_fragment_profile(profile, raw)[0]
    value = json.loads(record.line)
    overflow = {
        "anchor_omitted_count": UINT64_MAX,
        "slice_omitted_count": 0,
        "load_step_omitted_count": 0,
        "first_unprofiled_raw_hash": profile.first_unprofiled_raw_hash,
        "first_unprofiled_kind": "anchor",
    }
    payload = {
        **value["payload"],
        "issue_overflow_count": UINT64_MAX,
        "first_unprofiled_kind": "anchor",
        "overflow": overflow,
    }
    assert len(_rehash_payload(record, payload)) <= PROFILE_MAX_LINE_BYTES
    decode_fragment_profile_record(_rehash_payload(record, payload))
    for field in ("issue_overflow_count", "anchor_omitted_count"):
        bad_payload = dict(payload)
        if field == "issue_overflow_count":
            bad_payload[field] = UINT64_MAX + 1
        else:
            bad_payload["overflow"] = {**overflow, field: UINT64_MAX + 1}
        with pytest.raises(V3CodecError):
            decode_fragment_profile_record(_rehash_payload(record, bad_payload))


def test_domain_monotonic_bound_is_signed63() -> None:
    assert DOMAIN_MAX_MONOTONIC_NS == 2**63 - 1
    sample = _sample(0)
    with pytest.raises(ValueError, match="signed 63-bit"):
        replace(
            sample,
            observation=replace(sample.observation, monotonic_ns=DOMAIN_MAX_MONOTONIC_NS + 1),
        )


def test_wire_requires_exact_slice_family_identity_and_null_non_slice_id() -> None:
    profile, raw = _multi_span_profile()
    records = encode_fragment_profile(profile, raw)
    head = records[0]
    head_value = json.loads(head.line)
    with pytest.raises(V3CodecError):
        decode_fragment_profile_record(
            _rehash_payload(head, {**head_value["payload"], "logical_slice_id": None})
        )
    nested = {**head_value["payload"]["chunk"]["slice"], "slice_id": _hash("wrong-slice")}
    with pytest.raises(V3CodecError):
        decode_fragment_profile_record(
            _rehash_payload(
                head,
                {**head_value["payload"], "chunk": {"slice": nested}},
            )
        )

    continuation = records[1]
    continuation_value = json.loads(continuation.line)
    wrong_continuation = _rehash_payload(
        continuation,
        {**continuation_value["payload"], "logical_slice_id": _hash("wrong-slice")},
    )
    with pytest.raises(V3CodecError):
        decode_fragment_profile_records(
            (head.line, wrong_continuation, *(r.line for r in records[2:]))
        )

    profiles = _profile_inputs(17)
    anchor = next(
        record
        for record in encode_fragment_profiles(profiles, _raw(profiles))
        if record.envelope.payload["chunk_kind"] == "anchor_chunk"
    )
    anchor_value = json.loads(anchor.line)
    with pytest.raises(V3CodecError):
        decode_fragment_profile_record(
            _rehash_payload(
                anchor,
                {**anchor_value["payload"], "logical_slice_id": _hash("unexpected-parent")},
            )
        )


@pytest.mark.parametrize("count", (601, POLICY_OBJECT.max_physical_samples))
def test_physical_span_limit_is_compact_and_not_truncated(count: int) -> None:
    with pytest.raises(V3CodecError, match="raw_samples"):
        encode_fragment_profile(_span_profile(count), {})


def test_shortest_legal_envelope_uses_actual_bytes_without_padding() -> None:
    line = encode_v3_record(_minimal_envelope()).line
    assert len(line) == 334
    assert len(line) <= MAX_LINE_BYTES


@pytest.mark.parametrize(
    "mutator",
    (
        lambda value: value | {"unknown": True},
        lambda value: {**value, "schema_version": 2},
        lambda value: {**value, "record_sha256": "0" * 64},
    ),
)
def test_decode_rejects_schema_unknown_fields_and_hash_mutation(mutator) -> None:
    profiles = _profile_inputs(1)
    line = encode_fragment_profile(profiles[0], _raw(profiles))[0].line
    value = json.loads(line)
    mutated = json.dumps(mutator(value), separators=(",", ":"), sort_keys=True).encode() + b"\n"
    with pytest.raises(V3CodecError):
        decode_fragment_profile_record(mutated)


def test_decode_rejects_rehashed_semantic_mutations() -> None:
    profiles = _profile_inputs(17)
    record = encode_fragment_profiles(profiles, _raw(profiles))[0]
    value = json.loads(record.line)
    payload = dict(value["payload"])
    payload["source_range"] = {**payload["source_range"], "sample_count": 999}
    envelope = V3RecordEnvelope(**{**value, "record_sha256": None, "payload": payload})
    mutated = encode_v3_record(envelope).line
    with pytest.raises(V3CodecError):
        decode_fragment_profile_records((mutated,))


def test_decode_rejects_noncanonical_utc_enum_and_line_overflow() -> None:
    profiles = _profile_inputs(1)
    record = encode_fragment_profile(profiles[0], _raw(profiles))[0]
    value = json.loads(record.line)
    payload = value["payload"]
    mutations = (
        {**value, "wall_time_utc": "2026-08-18T00:00:00+00:00"},
        {**value, "payload": {**payload, "observation_origin": "future"}},
        {**value, "payload": {**payload, "profile_issues": ["future"]}},
    )
    for mutation in mutations:
        with pytest.raises(V3CodecError):
            decode_fragment_profile_record(
                json.dumps(mutation, separators=(",", ":"), sort_keys=True).encode() + b"\n"
            )
    oversized = record.line[:-1] + b" " * (PROFILE_MAX_LINE_BYTES - len(record.line) + 1) + b"\n"
    with pytest.raises(V3CodecError):
        decode_fragment_profile_record(oversized)


def test_wire_rejects_estimate_boundary_mutations() -> None:
    record = _record_of_kind("load_step_chunk")
    original = json.loads(record.line)["payload"]
    changes = (
        {"missing": None},
        {"step_id": ""},
        {"pre_sequences": [True]},
        {"transition_monotonic_ns": True},
        {"pre_slope_v_per_s": "not-a-number"},
        {"pre_slope_v_per_s": True},
        {"pre_slope_v_per_s": None},
        {"quality": "future-quality"},
        {"reasons": {"reason_codes": ["future-reason"], "reason_overflow": 0}},
        {"reasons": {}},
        {"reasons": {"reason_codes": "future-reason", "reason_overflow": 0}},
        {"reasons": {"reason_codes": [], "reason_overflow": True}},
    )
    for change in changes:
        payload = dict(original)
        chunk = dict(payload["chunk"])
        entries = [dict(entry) for entry in chunk["load_steps"]]
        entry = dict(entries[0])
        estimate = dict(entry["load_step"]["estimate"])
        estimate.update(change)
        step = {**entry["load_step"], "estimate": estimate}
        entries[0] = {**entry, "load_step": step}
        payload["chunk"] = {**chunk, "load_steps": entries}
        with pytest.raises(V3CodecError):
            decode_fragment_profile_record(_rehash_payload(record, payload))


def test_wire_rejects_source_and_readiness_boundary_mutations() -> None:
    record = _record_of_kind("slice_head")
    original = json.loads(record.line)["payload"]
    source_changes = (
        {"missing": None},
        {"sample_count": 0},
        {"first_sequence": True},
        {"first_sequence": 9, "last_sequence": 0},
        {"first_sample_hash": "bad-hash"},
        {"first_boot_id": ""},
        {"first_wall_time_utc": "2026-08-18T00:00:00+00:00"},
    )
    for change in source_changes:
        payload = {**original, "source_range": {**original["source_range"], **change}}
        with pytest.raises(V3CodecError):
            decode_fragment_profile_record(_rehash_payload(record, payload))

    readiness_values = (
        {"ready": False, "reason": "partial", "provenance": "physical"},
        {"ready": 1, "reason": "partial", "provenance": "physical"},
        {"ready": False, "reason": 1, "provenance": "physical"},
        {"ready": False, "reason": "partial", "provenance": "future"},
    )
    for readiness in readiness_values:
        slice_value = {
            **original["chunk"]["slice"],
            "readiness_context": readiness,
        }
        payload = {**original, "chunk": {"slice": slice_value}}
        if (
            readiness["ready"] is False
            and readiness["reason"] == "partial"
            and readiness["provenance"] == "physical"
        ):
            assert decode_fragment_profile_record(_rehash_payload(record, payload))
        else:
            with pytest.raises(V3CodecError):
                decode_fragment_profile_record(_rehash_payload(record, payload))


@pytest.mark.parametrize(
    "kind", ("slice_head", "slice_span_continuation", "anchor_chunk", "load_step_chunk")
)
def test_wire_rejects_empty_typed_chunk(kind: str) -> None:
    source_kind = "slice_head" if kind == "slice_span_continuation" else kind
    record = _record_of_kind(source_kind)
    payload = json.loads(record.line)["payload"]
    if kind == "slice_span_continuation":
        payload = {
            **payload,
            "chunk_kind": kind,
            "chunk": {"spans": payload["chunk"]["slice"]["spans"]},
        }
    with pytest.raises(V3CodecError):
        decode_fragment_profile_record(_rehash_payload(record, {**payload, "chunk": {}}))


def test_packing_rejects_raw_context_mismatches_and_uses_boundary_anchor_link() -> None:
    profiles = _profile_inputs(1)
    slice_id = profiles[0].slices[0].slice_id
    raw = _raw(profiles)
    invalid_maps = (
        {},
        {**raw, "extra": ()},
        {slice_id: list(raw[slice_id])},
        {slice_id: ()},
    )
    for invalid in invalid_maps:
        with pytest.raises(V3CodecError):
            encode_fragment_profiles(
                profiles,
                cast(dict[str, tuple[CanonicalDischargeSample, ...]], invalid),
            )

    anchors, slices, _ = _many_profile_inputs(1)
    boundary = EndpointAnchor(
        _hash("position-anchor"),
        AnchorKind.TRANSFER_TO_BATTERY,
        AnchorProvenance.PHYSICAL,
        "boot-a",
        START,
        0,
        None,
        blackout_id="blackout-a",
        physical_episode_id="episode-a",
        segment_id="segment-a",
    )
    bounded_slice = replace(slices[0], start_anchor=boundary)
    profile = DischargeFragmentProfile((boundary,), (bounded_slice,), (), POLICY)
    records = encode_fragment_profiles((profile,), {bounded_slice.slice_id: bounded_slice.samples})
    assert records


def test_multi_span_slice_family_roundtrips_losslessly() -> None:
    profile, raw = _multi_span_profile()
    records = encode_fragment_profile(profile, raw)
    continuations = [
        record
        for record in records
        if record.envelope.payload["chunk_kind"] == "slice_span_continuation"
    ]
    assert len(continuations) > 1
    assert records[0].envelope.payload["chunk_ordinal"] == 0
    assert all(
        record.envelope.payload["chunk_ordinal"] == index
        for index, record in enumerate(records[: len(continuations) + 1])
    )
    assert all(len(record.line) <= PROFILE_MAX_LINE_BYTES for record in records)
    assert reconstruct_fragment_profiles(records, raw) == (profile,)


def test_multi_span_slice_family_rejects_missing_duplicate_reordered_and_wrong_kind() -> None:
    profile, raw = _multi_span_profile()
    records = encode_fragment_profile(profile, raw)
    head = records[0]
    family = tuple(
        record
        for record in records
        if record.envelope.payload["chunk_kind"] in {"slice_head", "slice_span_continuation"}
    )
    assert len(family) > 2

    with pytest.raises(V3CodecError):
        decode_fragment_profile_records(record.line for record in family[:-1])
    with pytest.raises(V3CodecError):
        decode_fragment_profile_records(
            (family[0].line, family[1].line, family[1].line, *(item.line for item in family[2:]))
        )
    with pytest.raises(V3CodecError):
        decode_fragment_profile_records(
            (family[0].line, family[2].line, family[1].line, *(item.line for item in family[3:]))
        )

    continuation = family[1]
    payload = dict(continuation.envelope.payload)
    payload.update(
        {
            "chunk_kind": "slice_head",
            "chunk": {"slice": head.envelope.payload["chunk"]["slice"]},
            "chunk_ordinal": 0,
        }
    )
    wrong_kind = _rehash_payload(continuation, payload)
    with pytest.raises(V3CodecError):
        decode_fragment_profile_records(
            (family[0].line, wrong_kind, *(item.line for item in family[2:]))
        )


def test_maximum_u64_values_roundtrip_and_max_plus_one_is_rejected() -> None:
    _, slices, _ = _many_profile_inputs(1)
    sample = replace(
        _sample(0),
        sequence=UINT64_MAX,
        observation=replace(_sample(0).observation, monotonic_ns=MAX_MONOTONIC_NS),
    )
    span = build_canonical_sample_span((sample,))
    parent = replace(slices[0], samples=(sample,), spans=(span,))
    profile = DischargeFragmentProfile((), (parent,), (), POLICY)
    raw = {parent.slice_id: (sample,)}
    records = encode_fragment_profile(profile, raw, seq=MAX_SEQ)
    assert all(len(record.line) <= PROFILE_MAX_LINE_BYTES for record in records)
    assert reconstruct_fragment_profiles(records, raw) == (profile,)

    with pytest.raises(ValueError):
        replace(sample, sequence=UINT64_MAX + 1)
    with pytest.raises(ValueError):
        replace(span, last_monotonic_ns=UINT64_MAX + 1)
    with pytest.raises(V3CodecError):
        encode_fragment_profile(profile, raw, seq=MAX_SEQ + 1)


def test_profile_record_count_and_wire_counts_reject_max_plus_one() -> None:
    profiles = _profile_inputs(1)
    with pytest.raises(ValueError, match="record count"):
        replace(profiles[0], record_count=97)
    record = encode_fragment_profile(profiles[0], _raw(profiles))[0]
    payload = json.loads(record.line)["payload"]
    mutated = _rehash_payload(record, {**payload, "record_count": 97})
    with pytest.raises(V3CodecError):
        decode_fragment_profile_record(mutated)


def test_canonical_duplicate_and_nonfinite_values_are_rejected() -> None:
    with pytest.raises(V3CodecError):
        decode_v3_record(b'{"a":1,"a":2}\n')
    with pytest.raises(V3CodecError):
        encode_v3_record(_minimal_envelope(payload={"value": float("nan")}))


def test_profile_rejects_duplicate_and_orphan_descriptors() -> None:
    profiles = _profile_inputs(1)
    records = encode_fragment_profile(profiles[0], _raw(profiles))
    assert reconstruct_fragment_profiles(records, _raw(profiles)) == profiles
    anchor_record = next(
        record for record in records if record.envelope.payload["chunk_kind"] == "anchor_chunk"
    )
    value = json.loads(anchor_record.line)
    chunk = dict(value["payload"]["chunk"])
    chunk["anchors"] = [
        {**chunk["anchors"][0], "logical_slice_id": _hash("orphan-slice")},
        *chunk["anchors"][1:],
    ]
    mutated = encode_v3_record(
        V3RecordEnvelope(
            **{**value, "record_sha256": None, "payload": {**value["payload"], "chunk": chunk}}
        )
    )
    replaced = tuple(mutated if item is anchor_record else item for item in records)
    with pytest.raises(V3CodecError, match="anchor|slice|continuity"):
        reconstruct_fragment_profiles(replaced, _raw(profiles))
