"""Raw-backed reconstruction and strict chain checks for fragment-profile-v2."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.adapters.jsonl_v3_canonical import (
    EncodedV3Record,
    V3CodecError,
    canonical_json_bytes,
)
from src.adapters.jsonl_v3_fragment_wire import decode_record, logical_profile_id
from src.domain.fragments import (
    AnchorKind,
    AnchorProvenance,
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
)
from src.domain.reasons import _REASON_TYPES, order_reasons
from src.domain.values import LoadStepEstimate, StepQuality


@dataclass(slots=True)
class _ChainContext:
    first: Mapping[str, Any]
    record_count: int
    seen: set[tuple[str, str]]
    chunk_ordinals: dict[tuple[str, str, str | None], set[int]]
    slice_families: dict[tuple[str, str], dict[str, Any]]


def _time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except (TypeError, ValueError) as exc:
        raise V3CodecError("wire time cannot be decoded") from exc


def _span(value: Mapping[str, Any]) -> CanonicalSampleSpan:
    try:
        return CanonicalSampleSpan(
            value["first_sequence"],
            value["last_sequence"],
            value["sample_count"],
            value["first_sample_hash"],
            value["last_sample_hash"],
            value["ordered_sample_hashes_sha256"],
            value["boot_id"],
            value["first_monotonic_ns"],
            value["last_monotonic_ns"],
            _time(value["first_wall_time_utc"]),
            _time(value["last_wall_time_utc"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise V3CodecError("wire span cannot be decoded") from exc


def _anchor(value: Mapping[str, Any]) -> EndpointAnchor:
    try:
        return EndpointAnchor(
            value["canonical_hash"],
            AnchorKind(value["kind"]),
            AnchorProvenance(value["provenance"]),
            value["boot_id"],
            _time(value["wall_time_utc"]),
            value["monotonic_ns"],
            value["source_sample_hash"],
            value["blackout_id"],
            value["physical_episode_id"],
            value["segment_id"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise V3CodecError("wire anchor cannot be decoded") from exc


def _readiness(value: Any) -> StartReadinessContext | None:
    if value is None:
        return None
    try:
        return StartReadinessContext(
            value["ready"],
            value["reason"],
            ReadinessProvenance(value["provenance"]) if value["provenance"] else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise V3CodecError("wire readiness cannot be decoded") from exc


def _reasons(value: Mapping[str, Any]):
    known = {reason.value: reason for typ in _REASON_TYPES for reason in typ}
    try:
        reasons = tuple(known[item] for item in value["reason_codes"])
        return order_reasons(reasons)
    except (KeyError, TypeError, ValueError) as exc:
        raise V3CodecError("wire reasons cannot be decoded") from exc


def _estimate(value: Mapping[str, Any]) -> LoadStepEstimate:
    try:
        return LoadStepEstimate(
            step_id=value["step_id"],
            blackout_id=value["blackout_id"],
            segment_id=value["segment_id"],
            pre_sequences=tuple(value["pre_sequences"]),
            post_sequences=tuple(value["post_sequences"]),
            transition_monotonic_ns=value["transition_monotonic_ns"],
            pre_slope_v_per_s=value["pre_slope_v_per_s"],
            early_post_slope_v_per_s=value["early_post_slope_v_per_s"],
            late_post_slope_v_per_s=value["late_post_slope_v_per_s"],
            delta_load_pp=value["delta_load_pp"],
            early_delta_voltage_at_transition_v=value["early_delta_voltage_at_transition_v"],
            settled_delta_voltage_at_transition_v=value["settled_delta_voltage_at_transition_v"],
            voltage_quantum_v=value["voltage_quantum_v"],
            k_transition_v_per_pp=value["k_transition_v_per_pp"],
            k_settled_v_per_pp=value["k_settled_v_per_pp"],
            quality=StepQuality(value["quality"]),
            reasons=_reasons(value["reasons"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise V3CodecError("wire estimate cannot be decoded") from exc


def decode_chain(lines: Sequence[bytes], max_line_bytes: int) -> tuple[EncodedV3Record, ...]:
    records = tuple(decode_record(line, max_line_bytes) for line in lines)
    if not records:
        raise V3CodecError("profile chain must not be empty")
    first = records[0].envelope.payload
    if first["record_count"] != len(records):
        raise V3CodecError("profile chain record count is incomplete")
    context = _ChainContext(first, len(records), set(), {}, {})
    for ordinal, record in enumerate(records):
        _validate_chain_record(
            record,
            ordinal,
            records[ordinal - 1] if ordinal else None,
            context,
        )
    _validate_slice_families(context.slice_families)
    for group, ordinals in context.chunk_ordinals.items():
        count = next(
            record.envelope.payload["chunk_count"]
            for record in records
            if (
                record.envelope.payload["logical_profile_id"],
                record.envelope.payload["chunk_kind"],
                record.envelope.payload["logical_slice_id"],
            )
            == group
        )
        if ordinals != set(range(count)):
            raise V3CodecError("profile chunk ordinals are incomplete")
    _validate_source_binding(records)
    return records


def _validate_chain_record(
    record: EncodedV3Record,
    ordinal: int,
    previous: EncodedV3Record | None,
    context: _ChainContext,
) -> None:
    first = context.first
    record_count = context.record_count
    payload = record.envelope.payload
    if payload["record_ordinal"] != ordinal or payload["record_count"] != record_count:
        raise V3CodecError("profile physical ordinals are not contiguous")
    if any(
        payload[key] != first[key]
        for key in (
            "series_id",
            "policy_revision",
            "blackout_id",
            "physical_episode_id",
            "battery_epoch_id",
            "segment_id",
            "observation_origin",
            "uat_intent_id",
            "source_range",
            "source_digest",
        )
    ):
        raise V3CodecError("profile chain scope differs")
    if previous is not None and (
        record.envelope.seq != previous.envelope.seq + 1
        or record.envelope.prev_record_sha256 != previous.record_sha256
    ):
        raise V3CodecError("profile chain continuity is invalid")
    key = (
        payload["logical_profile_id"],
        payload["chunk_kind"]
        + ":"
        + str(payload["logical_slice_id"])
        + ":"
        + str(payload["chunk_ordinal"]),
    )
    if key in context.seen:
        raise V3CodecError("duplicate profile chunk")
    context.seen.add(key)
    if payload["chunk_ordinal"] >= payload["chunk_count"]:
        raise V3CodecError("profile chunk ordinal is out of range")
    if payload["chunk_kind"] in {"slice_head", "slice_span_continuation"}:
        _record_slice_family(payload, context)
    else:
        group = (
            payload["logical_profile_id"],
            payload["chunk_kind"],
            payload["logical_slice_id"],
        )
        context.chunk_ordinals.setdefault(group, set()).add(payload["chunk_ordinal"])


def _record_slice_family(payload: Mapping[str, Any], context: _ChainContext) -> None:
    key = (payload["logical_profile_id"], payload["logical_slice_id"])
    family = context.slice_families.setdefault(
        key,
        {"count": payload["chunk_count"], "head": set(), "continuation": set()},
    )
    if family["count"] != payload["chunk_count"]:
        raise V3CodecError("slice chunk count differs across family")
    kind = payload["chunk_kind"]
    ordinal = payload["chunk_ordinal"]
    if kind == "slice_head" and ordinal != 0:
        raise V3CodecError("slice head must have ordinal zero")
    if kind == "slice_span_continuation" and ordinal == 0:
        raise V3CodecError("slice continuation cannot have head ordinal")
    ordinals: set[int] = family["head" if kind == "slice_head" else "continuation"]
    if ordinal in ordinals:
        raise V3CodecError("duplicate slice-family chunk")
    expected = len(family["head"]) + len(family["continuation"])
    if ordinal != expected:
        raise V3CodecError("slice-family chunks are reordered")
    ordinals.add(ordinal)


def _validate_slice_families(families: Mapping[tuple[str, str], Mapping[str, Any]]) -> None:
    for family in families.values():
        count = family["count"]
        if family["head"] != {0} or family["continuation"] != set(range(1, count)):
            raise V3CodecError("slice-family chunks are incomplete")


def _validate_source_binding(records: Sequence[EncodedV3Record]) -> None:
    first = records[0].envelope.payload
    spans: list[Mapping[str, Any]] = []
    for record in records:
        payload = record.envelope.payload
        chunk = payload["chunk"]
        if payload["chunk_kind"] == "slice_head":
            spans.extend(chunk["slice"]["spans"])
        elif payload["chunk_kind"] == "slice_span_continuation":
            spans.extend(chunk["spans"])
    if not spans:
        raise V3CodecError("profile source spans must not be empty")
    source = first["source_range"]
    expected = {
        "first_sequence": spans[0]["first_sequence"],
        "last_sequence": spans[-1]["last_sequence"],
        "sample_count": sum(item["sample_count"] for item in spans),
        "first_sample_hash": spans[0]["first_sample_hash"],
        "last_sample_hash": spans[-1]["last_sample_hash"],
        "first_boot_id": spans[0]["boot_id"],
        "last_boot_id": spans[-1]["boot_id"],
        "first_monotonic_ns": spans[0]["first_monotonic_ns"],
        "last_monotonic_ns": spans[-1]["last_monotonic_ns"],
        "first_wall_time_utc": spans[0]["first_wall_time_utc"],
        "last_wall_time_utc": spans[-1]["last_wall_time_utc"],
    }
    digest = hashlib.sha256(canonical_json_bytes({"spans": spans})).hexdigest()
    if expected != source or digest != first["source_digest"]:
        raise V3CodecError("profile source digest/range is not reproducible")


def descriptor_count(records: Sequence[EncodedV3Record]) -> int:
    count = 0
    slices: set[str] = set()
    anchors: set[str] = set()
    steps: set[str] = set()
    for record in records:
        payload = record.envelope.payload
        chunk = payload["chunk"]
        if payload["chunk_kind"] == "slice_head":
            slices.add(chunk["slice"]["slice_id"])
        elif payload["chunk_kind"] == "anchor_chunk":
            anchors.update(item["anchor"]["canonical_hash"] for item in chunk["anchors"])
        elif payload["chunk_kind"] == "load_step_chunk":
            steps.update(item["load_step"]["step_record_hash"] for item in chunk["load_steps"])
    count = len(slices) + len(anchors) + len(steps)
    if count > 256:
        raise V3CodecError("combined descriptor budget exceeded")
    return count


def reconstruct(
    records: Sequence[EncodedV3Record],
    raw_samples_by_slice: Mapping[str, tuple[Any, ...]],
    max_line_bytes: int,
) -> tuple[DischargeFragmentProfile, ...]:
    values = decode_chain(tuple(record.line for record in records), max_line_bytes)
    descriptor_count(values)
    groups: dict[int, list[Mapping[str, Any]]] = {}
    for record in values:
        payload = record.envelope.payload
        groups.setdefault(payload["logical_profile_ordinal"], []).append(payload)
    return tuple(
        _rebuild_profile(ordinal, payloads, raw_samples_by_slice)
        for ordinal, payloads in sorted(groups.items())
    )


def _rebuild_profile(
    ordinal: int, payloads: list[Mapping[str, Any]], raw_samples: Mapping[str, tuple[Any, ...]]
) -> DischargeFragmentProfile:
    first = payloads[0]
    by_id = _slice_parts(payloads)
    anchors, steps = _linked_values(payloads)
    slices = _rebuild_slices(by_id, anchors, raw_samples)
    load_steps = _rebuild_steps(steps, slices, raw_samples)
    try:
        profile = DischargeFragmentProfile(
            tuple(anchors.values()),
            tuple(slices.values()),
            tuple(load_steps),
            first["policy_revision"],
            tuple(ProfileReason(value) for value in first["profile_issues"]),
            first["issue_overflow_count"],
            first["first_unprofiled_raw_hash"],
            first["overflow"]["anchor_omitted_count"],
            first["overflow"]["slice_omitted_count"],
            first["overflow"]["load_step_omitted_count"],
            first["series_id"],
            ordinal,
            first["logical_profile_count"],
            OmittedFragmentKind(first["first_unprofiled_kind"])
            if first["first_unprofiled_kind"] is not None
            else None,
        )
    except (TypeError, ValueError) as exc:
        raise V3CodecError("profile cannot be reconstructed") from exc
    if logical_profile_id(profile, first["series_id"]) != first["logical_profile_id"]:
        raise V3CodecError("logical profile identity is not reproducible")
    return profile


def _slice_parts(payloads: list[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    heads = [item for item in payloads if item["chunk_kind"] == "slice_head"]
    result = {item["chunk"]["slice"]["slice_id"]: [item] for item in heads}
    if len(result) != len(heads):
        raise V3CodecError("duplicate slice head")
    for item in payloads:
        if item["chunk_kind"] == "slice_span_continuation":
            key = item["logical_slice_id"]
            if key not in result:
                raise V3CodecError("slice continuation has no head")
            result[key].append(item)
    return result


def _linked_values(
    payloads: list[Mapping[str, Any]],
) -> tuple[dict[str, EndpointAnchor], list[tuple[str, Mapping[str, Any]]]]:
    anchors: dict[str, EndpointAnchor] = {}
    steps: list[tuple[str, Mapping[str, Any]]] = []
    for item in payloads:
        chunk = item["chunk"]
        if item["chunk_kind"] == "anchor_chunk":
            for entry in chunk["anchors"]:
                value = _anchor(entry["anchor"])
                if value.canonical_hash in anchors:
                    raise V3CodecError("duplicate anchor")
                anchors[value.canonical_hash] = value
        elif item["chunk_kind"] == "load_step_chunk":
            steps.extend(
                (entry["logical_slice_id"], entry["load_step"]) for entry in chunk["load_steps"]
            )
    return anchors, steps


def _rebuild_slices(
    parts: Mapping[str, list[Mapping[str, Any]]],
    anchors: Mapping[str, EndpointAnchor],
    raw_samples: Mapping[str, tuple[Any, ...]],
) -> dict[str, DischargeSlice]:
    result: dict[str, DischargeSlice] = {}
    for slice_id, values in parts.items():
        head = values[0]["chunk"]["slice"]
        spans = tuple(_span(span) for span in head["spans"])
        spans += tuple(_span(span) for value in values[1:] for span in value["chunk"]["spans"])
        samples = raw_samples.get(slice_id)
        if samples is None:
            raise V3CodecError("raw samples are required for replay")
        try:
            item = DischargeSlice(
                samples=samples,
                blackout_id=head["blackout_id"],
                physical_episode_id=head["physical_episode_id"],
                battery_epoch_id=head["battery_epoch_id"],
                segment_id=head["segment_id"],
                origin=ObservationOrigin(head["observation_origin"]),
                policy_revision=head["policy_revision"],
                start_anchor=anchors.get(head["start_anchor_hash"]),
                end_anchor=anchors.get(head["end_anchor_hash"]),
                uat_intent_id=head["uat_intent_id"],
                readiness_context=_readiness(head["readiness_context"]),
                spans=spans,
            )
        except (TypeError, ValueError) as exc:
            raise V3CodecError("slice cannot be reconstructed") from exc
        if item.slice_id != slice_id:
            raise V3CodecError("slice identity is not reproducible")
        result[slice_id] = item
    return result


def _rebuild_steps(
    values: list[tuple[str, Mapping[str, Any]]],
    slices: Mapping[str, DischargeSlice],
    raw_samples: Mapping[str, tuple[Any, ...]],
) -> list[LoadStepObservation]:
    import hashlib

    result: list[LoadStepObservation] = []
    for slice_id, value in values:
        parent = slices.get(slice_id)
        if parent is None:
            raise V3CodecError("load-step parent slice is absent")
        estimate = _estimate(value["estimate"])
        by_sequence = {sample.sequence: sample.canonical_hash for sample in raw_samples[slice_id]}
        try:
            hashes = tuple(
                by_sequence[seq] for seq in (*estimate.pre_sequences, *estimate.post_sequences)
            )
        except KeyError as exc:
            raise V3CodecError("load-step sequence absent from raw samples") from exc
        if (
            len(hashes) != value["contributor_count"]
            or hashlib.sha256("".join(hashes).encode("ascii")).hexdigest()
            != value["ordered_contributor_hashes_sha256"]
        ):
            raise V3CodecError("load-step contributor digest differs")
        step = LoadStepObservation(estimate, hashes, parent)
        if step.step_record_hash != value["step_record_hash"]:
            raise V3CodecError("load-step identity is not reproducible")
        result.append(step)
    return result
