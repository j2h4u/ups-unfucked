"""Strict lossless wire values for fragment-profile-v2 records."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from math import isfinite
from typing import Any

from src.adapters.jsonl_v3_canonical import (
    EncodedV3Record,
    V3CodecError,
    V3RecordEnvelope,
    canonical_json_bytes,
    canonical_v3_line_size,
    decode_v3_record,
    encode_v3_record,
)
from src.domain.fragments import (
    MAX_CONTRIBUTING_HASHES,
    MAX_MONOTONIC_NS,
    MAX_PHYSICAL_SAMPLES,
    MAX_PROFILE_ISSUES,
    MAX_PROFILE_RECORDS,
    MAX_SPANS_PER_SLICE,
    UINT64_MAX,
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
from src.domain.reasons import _REASON_TYPES
from src.domain.values import LoadStepEstimate, StepQuality

PROFILE_SCHEMA = "fragment-profile-v2"
PROFILE_RECORD_TYPE = "fragment_profile"
PROFILE_PROVENANCE = "derived"
PROFILE_MAX_LINE_BYTES = 8 * 1024
PAYLOAD_FIELDS = frozenset(
    {
        "profile_schema",
        "series_id",
        "logical_profile_id",
        "logical_profile_ordinal",
        "logical_profile_count",
        "record_ordinal",
        "record_count",
        "policy_revision",
        "blackout_id",
        "physical_episode_id",
        "battery_epoch_id",
        "segment_id",
        "observation_origin",
        "uat_intent_id",
        "source_range",
        "source_digest",
        "chunk_kind",
        "logical_slice_id",
        "chunk_ordinal",
        "chunk_count",
        "chunk",
        "profile_issues",
        "issue_overflow_count",
        "first_unprofiled_raw_hash",
        "first_unprofiled_kind",
        "overflow",
    }
)
CHUNK_KINDS = frozenset(
    {"slice_head", "slice_span_continuation", "anchor_chunk", "load_step_chunk"}
)
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
UTC_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z\Z")
SPAN_FIELDS = frozenset(
    {
        "first_sequence",
        "last_sequence",
        "sample_count",
        "first_sample_hash",
        "last_sample_hash",
        "ordered_sample_hashes_sha256",
        "boot_id",
        "first_monotonic_ns",
        "last_monotonic_ns",
        "first_wall_time_utc",
        "last_wall_time_utc",
    }
)
ANCHOR_FIELDS = frozenset(
    {
        "canonical_hash",
        "kind",
        "provenance",
        "boot_id",
        "wall_time_utc",
        "monotonic_ns",
        "source_sample_hash",
        "blackout_id",
        "physical_episode_id",
        "segment_id",
    }
)
SLICE_FIELDS = frozenset(
    {
        "slice_id",
        "blackout_id",
        "physical_episode_id",
        "battery_epoch_id",
        "segment_id",
        "observation_origin",
        "policy_revision",
        "uat_intent_id",
        "readiness_context",
        "spans",
        "start_anchor_hash",
        "end_anchor_hash",
    }
)
ESTIMATE_FIELDS = frozenset(
    {
        "step_id",
        "blackout_id",
        "segment_id",
        "pre_sequences",
        "post_sequences",
        "transition_monotonic_ns",
        "pre_slope_v_per_s",
        "early_post_slope_v_per_s",
        "late_post_slope_v_per_s",
        "delta_load_pp",
        "early_delta_voltage_at_transition_v",
        "settled_delta_voltage_at_transition_v",
        "voltage_quantum_v",
        "k_transition_v_per_pp",
        "k_settled_v_per_pp",
        "quality",
        "reasons",
    }
)
STEP_FIELDS = frozenset(
    {
        "estimate",
        "parent_slice_id",
        "step_record_hash",
        "contributor_count",
        "ordered_contributor_hashes_sha256",
    }
)
ORIGINS = frozenset(item.value for item in ObservationOrigin)
ANCHOR_KINDS = frozenset(item.value for item in AnchorKind)
ANCHOR_PROVENANCE = frozenset(item.value for item in AnchorProvenance)
READINESS_PROVENANCE = frozenset(item.value for item in ReadinessProvenance)
STEP_QUALITY = frozenset(item.value for item in StepQuality)
PROFILE_REASONS = frozenset(item.value for item in ProfileReason)
OMITTED_FRAGMENT_KINDS = frozenset(item.value for item in OmittedFragmentKind)


def utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise V3CodecError("datetime must be UTC")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds" if value.microsecond else "seconds")
        .replace("+00:00", "Z")
    )


def text(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 128
        or any(ord(c) < 0x20 for c in value)
    ):
        raise V3CodecError(f"{name} is not bounded text")
    return value


def hash_value(value: Any, name: str) -> str:
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise V3CodecError(f"{name} must be lowercase SHA-256")
    return value


def nonnegative(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= UINT64_MAX:
        raise V3CodecError(f"{name} must be unsigned 64-bit integer")
    return value


def monotonic(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_MONOTONIC_NS:
        raise V3CodecError(f"{name} must be within signed 63-bit range")
    return value


def bounded_count(value: Any, name: str, maximum: int) -> int:
    result = nonnegative(value, name)
    if result > maximum:
        raise V3CodecError(f"{name} exceeds {maximum}")
    return result


def span_dict(span: CanonicalSampleSpan) -> dict[str, Any]:
    return {
        "first_sequence": span.first_sequence,
        "last_sequence": span.last_sequence,
        "sample_count": span.sample_count,
        "first_sample_hash": span.first_sample_hash,
        "last_sample_hash": span.last_sample_hash,
        "ordered_sample_hashes_sha256": span.ordered_sample_hashes_sha256,
        "boot_id": span.boot_id,
        "first_monotonic_ns": span.first_monotonic_ns,
        "last_monotonic_ns": span.last_monotonic_ns,
        "first_wall_time_utc": utc(span.first_wall_time_utc),
        "last_wall_time_utc": utc(span.last_wall_time_utc),
    }


def anchor_dict(anchor: EndpointAnchor) -> dict[str, Any]:
    return {
        "canonical_hash": anchor.canonical_hash,
        "kind": anchor.kind.value,
        "provenance": anchor.provenance.value,
        "boot_id": anchor.boot_id,
        "wall_time_utc": utc(anchor.wall_time_utc),
        "monotonic_ns": anchor.monotonic_ns,
        "source_sample_hash": anchor.source_sample_hash,
        "blackout_id": anchor.blackout_id,
        "physical_episode_id": anchor.physical_episode_id,
        "segment_id": anchor.segment_id,
    }


def readiness_dict(value: StartReadinessContext | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "ready": value.ready,
        "reason": value.reason,
        "provenance": value.provenance.value if value.provenance else None,
    }


def slice_dict(
    item: DischargeSlice, spans: tuple[CanonicalSampleSpan, ...] | None = None
) -> dict[str, Any]:
    return {
        "slice_id": item.slice_id,
        "blackout_id": item.blackout_id,
        "physical_episode_id": item.physical_episode_id,
        "battery_epoch_id": item.battery_epoch_id,
        "segment_id": item.segment_id,
        "observation_origin": item.origin.value,
        "policy_revision": item.policy_revision,
        "uat_intent_id": item.uat_intent_id,
        "readiness_context": readiness_dict(item.readiness_context),
        "spans": [span_dict(span) for span in (spans or item.spans)],
        "start_anchor_hash": item.start_anchor.canonical_hash if item.start_anchor else None,
        "end_anchor_hash": item.end_anchor.canonical_hash if item.end_anchor else None,
    }


def estimate_dict(value: LoadStepEstimate) -> dict[str, Any]:
    return {
        "step_id": value.step_id,
        "blackout_id": value.blackout_id,
        "segment_id": value.segment_id,
        "pre_sequences": list(value.pre_sequences),
        "post_sequences": list(value.post_sequences),
        "transition_monotonic_ns": value.transition_monotonic_ns,
        "pre_slope_v_per_s": value.pre_slope_v_per_s,
        "early_post_slope_v_per_s": value.early_post_slope_v_per_s,
        "late_post_slope_v_per_s": value.late_post_slope_v_per_s,
        "delta_load_pp": value.delta_load_pp,
        "early_delta_voltage_at_transition_v": value.early_delta_voltage_at_transition_v,
        "settled_delta_voltage_at_transition_v": value.settled_delta_voltage_at_transition_v,
        "voltage_quantum_v": value.voltage_quantum_v,
        "k_transition_v_per_pp": value.k_transition_v_per_pp,
        "k_settled_v_per_pp": value.k_settled_v_per_pp,
        "quality": value.quality.value,
        "reasons": {
            "reason_codes": [item.value for item in value.reasons.values],
            "reason_overflow": value.reasons.overflow_count,
        },
    }


def step_dict(step: LoadStepObservation) -> dict[str, Any]:
    hashes = step.contributing_sample_hashes
    return {
        "estimate": estimate_dict(step.estimate),
        "parent_slice_id": step.parent_slice.slice_id,
        "step_record_hash": step.step_record_hash,
        "contributor_count": len(hashes),
        "ordered_contributor_hashes_sha256": hashlib.sha256(
            "".join(hashes).encode("ascii")
        ).hexdigest(),
    }


def source_identity(slices: tuple[DischargeSlice, ...]) -> tuple[dict[str, Any], str]:
    spans = tuple(span for item in slices for span in item.spans)
    if not spans:
        raise V3CodecError("profile source spans must not be empty")
    first, last = spans[0], spans[-1]
    source = {
        "first_sequence": first.first_sequence,
        "last_sequence": last.last_sequence,
        "sample_count": sum(span.sample_count for span in spans),
        "first_sample_hash": first.first_sample_hash,
        "last_sample_hash": last.last_sample_hash,
        "first_boot_id": first.boot_id,
        "last_boot_id": last.boot_id,
        "first_monotonic_ns": first.first_monotonic_ns,
        "last_monotonic_ns": last.last_monotonic_ns,
        "first_wall_time_utc": utc(first.first_wall_time_utc),
        "last_wall_time_utc": utc(last.last_wall_time_utc),
    }
    digest = hashlib.sha256(
        canonical_json_bytes({"spans": [span_dict(span) for span in spans]})
    ).hexdigest()
    return source, digest


def series_id(
    policy: str,
    anchors: tuple[EndpointAnchor, ...],
    slices: tuple[DischargeSlice, ...],
    steps: tuple[LoadStepObservation, ...],
) -> str:
    return hashlib.sha256(
        "|".join(
            (
                policy,
                *(a.canonical_hash for a in anchors),
                *(s.slice_id for s in slices),
                *(x.step_record_hash for x in steps),
            )
        ).encode("ascii")
    ).hexdigest()


def logical_profile_id(profile: DischargeFragmentProfile, series: str) -> str:
    value = {
        "series_id": series,
        "policy_revision": profile.policy_revision,
        "ordinal": profile.ordinal,
        "count": profile.record_count,
        "anchors": [a.canonical_hash for a in profile.anchors],
        "slices": [s.slice_id for s in profile.slices],
        "steps": [s.step_record_hash for s in profile.load_steps],
    }
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def validate_span(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != SPAN_FIELDS:
        raise V3CodecError("span fields are not exact")
    first, last, count = (
        nonnegative(value[key], f"span {key}")
        for key in ("first_sequence", "last_sequence", "sample_count")
    )
    bounded_count(count, "span sample_count", MAX_PHYSICAL_SAMPLES)
    if not count or last - first + 1 != count:
        raise V3CodecError("span cardinality is invalid")
    for key in ("first_sample_hash", "last_sample_hash", "ordered_sample_hashes_sha256"):
        hash_value(value[key], key)
    text(value["boot_id"], "span boot_id")
    for key in ("first_monotonic_ns", "last_monotonic_ns"):
        monotonic(value[key], key)
    for key in ("first_wall_time_utc", "last_wall_time_utc"):
        if not isinstance(value[key], str) or UTC_RE.fullmatch(value[key]) is None:
            raise V3CodecError(f"{key} is not canonical UTC")


def validate_anchor(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != ANCHOR_FIELDS:
        raise V3CodecError("anchor fields are not exact")
    hash_value(value["canonical_hash"], "anchor hash")
    if value["kind"] not in ANCHOR_KINDS or value["provenance"] not in ANCHOR_PROVENANCE:
        raise V3CodecError("anchor enum is not closed")
    text(value["boot_id"], "anchor boot_id")
    if UTC_RE.fullmatch(value["wall_time_utc"]) is None:
        raise V3CodecError("anchor wall time is not canonical UTC")
    monotonic(value["monotonic_ns"], "anchor monotonic_ns")
    if value["source_sample_hash"] is not None:
        hash_value(value["source_sample_hash"], "anchor source hash")
    for key in ("blackout_id", "physical_episode_id", "segment_id"):
        text(value[key], key)


def validate_slice(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != SLICE_FIELDS:
        raise V3CodecError("slice fields are not exact")
    _validate_slice_identity(value)
    _validate_slice_spans(value)
    _validate_slice_readiness(value)


def _validate_slice_identity(value: Mapping[str, Any]) -> None:
    for key in (
        "slice_id",
        "blackout_id",
        "physical_episode_id",
        "battery_epoch_id",
        "segment_id",
        "policy_revision",
        "observation_origin",
    ):
        text(value[key], key)
    if value["observation_origin"] not in ORIGINS or (
        value["uat_intent_id"] is not None and value["observation_origin"] != "uat"
    ):
        raise V3CodecError("slice origin/UAT linkage is invalid")
    if value["uat_intent_id"] is not None:
        text(value["uat_intent_id"], "uat_intent_id")


def _validate_slice_spans(value: Mapping[str, Any]) -> None:
    if (
        not isinstance(value["spans"], list)
        or not value["spans"]
        or len(value["spans"]) > MAX_SPANS_PER_SLICE
    ):
        raise V3CodecError("slice spans are invalid")
    for span in value["spans"]:
        validate_span(span)
    for key in ("start_anchor_hash", "end_anchor_hash"):
        if value[key] is not None:
            hash_value(value[key], key)


def _validate_slice_readiness(value: Mapping[str, Any]) -> None:
    readiness = value["readiness_context"]
    if readiness is None:
        return
    if not isinstance(readiness, dict) or set(readiness) != {"ready", "reason", "provenance"}:
        raise V3CodecError("readiness fields are not exact")
    if readiness["ready"] is not None and not isinstance(readiness["ready"], bool):
        raise V3CodecError("readiness fact is invalid")
    if readiness["reason"] is not None:
        text(readiness["reason"], "readiness reason")
    if readiness["provenance"] is not None and readiness["provenance"] not in READINESS_PROVENANCE:
        raise V3CodecError("readiness provenance is not closed")


def validate_estimate(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != ESTIMATE_FIELDS:
        raise V3CodecError("estimate fields are not exact")
    _validate_estimate_identity(value)
    _validate_estimate_sequences(value)
    monotonic(value["transition_monotonic_ns"], "transition monotonic")
    _validate_estimate_numerics(value)
    _validate_estimate_quality(value)
    _validate_estimate_reasons(value)


def _validate_estimate_identity(value: Mapping[str, Any]) -> None:
    for key in ("step_id", "blackout_id", "segment_id"):
        text(value[key], key)


def _validate_estimate_sequences(value: Mapping[str, Any]) -> None:
    for key in ("pre_sequences", "post_sequences"):
        if not isinstance(value[key], list) or len(value[key]) > MAX_CONTRIBUTING_HASHES:
            raise V3CodecError("estimate sequence window is invalid")
        for sequence in value[key]:
            nonnegative(sequence, f"estimate {key} sequence")


def _validate_estimate_numerics(value: Mapping[str, Any]) -> None:
    for key in ESTIMATE_FIELDS - {
        "step_id",
        "blackout_id",
        "segment_id",
        "pre_sequences",
        "post_sequences",
        "transition_monotonic_ns",
        "quality",
        "reasons",
    }:
        if (
            isinstance(value[key], bool)
            or not isinstance(value[key], (int, float))
            or not isfinite(float(value[key]))
        ):
            raise V3CodecError("estimate numeric field is invalid")


def _validate_estimate_quality(value: Mapping[str, Any]) -> None:
    if value["quality"] not in STEP_QUALITY:
        raise V3CodecError("estimate quality is invalid")


def _validate_estimate_reasons(value: Mapping[str, Any]) -> None:
    reasons = value["reasons"]
    known = {reason.value for typ in _REASON_TYPES for reason in typ}
    if (
        not isinstance(reasons, dict)
        or set(reasons) != {"reason_codes", "reason_overflow"}
        or not isinstance(reasons["reason_codes"], list)
        or any(item not in known for item in reasons["reason_codes"])
    ):
        raise V3CodecError("estimate reasons are invalid")
    nonnegative(reasons["reason_overflow"], "reason overflow")


def validate_step(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != STEP_FIELDS:
        raise V3CodecError("load-step fields are not exact")
    validate_estimate(value["estimate"])
    for key in ("parent_slice_id", "step_record_hash", "ordered_contributor_hashes_sha256"):
        hash_value(value[key], key)
    bounded_count(value["contributor_count"], "contributor count", MAX_CONTRIBUTING_HASHES)


def validate_payload(payload: Mapping[str, Any]) -> None:
    if set(payload) != PAYLOAD_FIELDS or payload.get("profile_schema") != PROFILE_SCHEMA:
        raise V3CodecError("fragment-profile-v2 fields/schema are not exact")
    if payload["chunk_kind"] not in CHUNK_KINDS:
        raise V3CodecError("chunk kind is not closed")
    _validate_payload_identity(payload)
    _validate_source(payload["source_range"])
    _validate_chunk(payload["chunk_kind"], payload["chunk"])
    _validate_slice_link_identity(payload)
    _validate_profile_overflow(payload)


def _validate_slice_link_identity(payload: Mapping[str, Any]) -> None:
    if payload["chunk_kind"] == "slice_head":
        if payload["logical_slice_id"] != payload["chunk"]["slice"]["slice_id"]:
            raise V3CodecError("slice head identity differs from nested slice")


def _validate_payload_identity(payload: Mapping[str, Any]) -> None:
    for key in ("series_id", "logical_profile_id", "source_digest"):
        hash_value(payload[key], key)
    for key in (
        "policy_revision",
        "blackout_id",
        "physical_episode_id",
        "battery_epoch_id",
        "segment_id",
    ):
        text(payload[key], key)
    for key in (
        "logical_profile_ordinal",
        "logical_profile_count",
        "record_ordinal",
        "record_count",
        "chunk_ordinal",
        "chunk_count",
    ):
        bounded_count(payload[key], key, MAX_PROFILE_RECORDS)
    if payload["chunk_kind"] in {"slice_head", "slice_span_continuation"}:
        if payload["logical_slice_id"] is None:
            raise V3CodecError("slice chunks require a logical slice id")
        hash_value(payload["logical_slice_id"], "logical slice id")
    elif payload["logical_slice_id"] is not None:
        raise V3CodecError("non-slice chunks must not have a logical slice id")
    if payload["observation_origin"] not in ORIGINS:
        raise V3CodecError("profile origin is not closed")
    if payload["uat_intent_id"] is not None:
        text(payload["uat_intent_id"], "uat intent id")


def _validate_source(source: Any) -> None:
    _validate_source_shape(source)
    _validate_source_range(source)
    _validate_source_hashes(source)
    _validate_source_clocks(source)


def _validate_source_shape(source: Any) -> None:
    fields = {
        "first_sequence",
        "last_sequence",
        "sample_count",
        "first_sample_hash",
        "last_sample_hash",
        "first_boot_id",
        "last_boot_id",
        "first_monotonic_ns",
        "last_monotonic_ns",
        "first_wall_time_utc",
        "last_wall_time_utc",
    }
    if not isinstance(source, dict) or set(source) != fields:
        raise V3CodecError("source range fields are not exact")


def _validate_source_range(source: Mapping[str, Any]) -> None:
    for key in (
        "first_sequence",
        "last_sequence",
        "sample_count",
        "first_monotonic_ns",
        "last_monotonic_ns",
    ):
        (monotonic if "monotonic" in key else nonnegative)(source[key], key)
    bounded_count(source["sample_count"], "source sample_count", MAX_PHYSICAL_SAMPLES)
    if source["sample_count"] <= 0 or source["first_sequence"] > source["last_sequence"]:
        raise V3CodecError("source range is invalid")


def _validate_source_hashes(source: Mapping[str, Any]) -> None:
    for key in ("first_sample_hash", "last_sample_hash"):
        hash_value(source[key], key)


def _validate_source_clocks(source: Mapping[str, Any]) -> None:
    for key in ("first_boot_id", "last_boot_id"):
        text(source[key], key)
    for key in ("first_wall_time_utc", "last_wall_time_utc"):
        if not isinstance(source[key], str) or UTC_RE.fullmatch(source[key]) is None:
            raise V3CodecError("source time is not canonical UTC")


def _validate_chunk(kind: str, chunk: Any) -> None:
    if not isinstance(chunk, dict):
        raise V3CodecError("chunk must be an object")
    if kind == "slice_head":
        if set(chunk) != {"slice"}:
            raise V3CodecError("slice chunk fields are not exact")
        validate_slice(chunk["slice"])
    elif kind == "slice_span_continuation":
        if (
            set(chunk) != {"spans"}
            or not isinstance(chunk["spans"], list)
            or not chunk["spans"]
            or len(chunk["spans"]) > MAX_SPANS_PER_SLICE
        ):
            raise V3CodecError("slice continuation fields are not exact")
        for span in chunk["spans"]:
            validate_span(span)
    elif kind == "anchor_chunk":
        _validate_anchor_chunk(chunk)
    else:
        _validate_step_chunk(chunk)


def _validate_anchor_chunk(chunk: Mapping[str, Any]) -> None:
    if set(chunk) != {"anchors"} or not isinstance(chunk["anchors"], list) or not chunk["anchors"]:
        raise V3CodecError("anchor chunk is invalid")
    for entry in chunk["anchors"]:
        if not isinstance(entry, dict) or set(entry) != {"logical_slice_id", "anchor"}:
            raise V3CodecError("anchor linkage fields are not exact")
        hash_value(entry["logical_slice_id"], "anchor logical slice id")
        validate_anchor(entry["anchor"])


def _validate_step_chunk(chunk: Mapping[str, Any]) -> None:
    if (
        set(chunk) != {"load_steps"}
        or not isinstance(chunk["load_steps"], list)
        or not chunk["load_steps"]
    ):
        raise V3CodecError("load-step chunk is invalid")
    for entry in chunk["load_steps"]:
        if not isinstance(entry, dict) or set(entry) != {"logical_slice_id", "load_step"}:
            raise V3CodecError("load-step linkage fields are not exact")
        hash_value(entry["logical_slice_id"], "step logical slice id")
        validate_step(entry["load_step"])


def _validate_profile_overflow(payload: Mapping[str, Any]) -> None:
    _validate_profile_issue_values(payload["profile_issues"])
    bounded_count(payload["issue_overflow_count"], "issue overflow", UINT64_MAX)
    _validate_omission_identity(payload)
    overflow = payload["overflow"]
    _validate_omission_fields(overflow)
    counts = tuple(
        overflow[key]
        for key in ("anchor_omitted_count", "slice_omitted_count", "load_step_omitted_count")
    )
    if payload["issue_overflow_count"] != sum(counts):
        raise V3CodecError("profile overflow counts are inconsistent")
    if overflow["first_unprofiled_raw_hash"] != payload["first_unprofiled_raw_hash"]:
        raise V3CodecError("profile overflow raw hash is inconsistent")
    if overflow["first_unprofiled_kind"] != payload["first_unprofiled_kind"]:
        raise V3CodecError("profile overflow omitted kind is inconsistent")


def _validate_profile_issue_values(issues: Any) -> None:
    if (
        not isinstance(issues, list)
        or len(issues) > MAX_PROFILE_ISSUES
        or any(item not in PROFILE_REASONS for item in issues)
    ):
        raise V3CodecError("profile issues are invalid")


def _validate_omission_identity(payload: Mapping[str, Any]) -> None:
    kind = payload["first_unprofiled_kind"]
    raw_hash = payload["first_unprofiled_raw_hash"]
    if kind is not None and kind not in OMITTED_FRAGMENT_KINDS:
        raise V3CodecError("first omitted kind is not closed")
    if raw_hash is not None:
        hash_value(raw_hash, "first overflow raw hash")
    if (kind is None) != (raw_hash is None):
        raise V3CodecError("first omitted kind and raw hash must be paired")


def _validate_omission_fields(overflow: Any) -> None:
    fields = {
        "anchor_omitted_count",
        "slice_omitted_count",
        "load_step_omitted_count",
        "first_unprofiled_raw_hash",
        "first_unprofiled_kind",
    }
    if not isinstance(overflow, dict) or set(overflow) != fields:
        raise V3CodecError("overflow fields are not exact")
    for key in ("anchor_omitted_count", "slice_omitted_count", "load_step_omitted_count"):
        bounded_count(overflow[key], key, UINT64_MAX)
    if overflow["first_unprofiled_raw_hash"] is not None:
        hash_value(overflow["first_unprofiled_raw_hash"], "overflow raw hash")
    if overflow["first_unprofiled_kind"] is not None:
        if overflow["first_unprofiled_kind"] not in OMITTED_FRAGMENT_KINDS:
            raise V3CodecError("overflow omitted kind is not closed")
    if (overflow["first_unprofiled_kind"] is None) != (
        overflow["first_unprofiled_raw_hash"] is None
    ):
        raise V3CodecError("overflow omitted kind and raw hash must be paired")


def fragment_record_envelope(
    payload: Mapping[str, Any], source: Mapping[str, Any], seq: int, previous: str | None
) -> V3RecordEnvelope:
    nonnegative(seq, "record sequence")
    return V3RecordEnvelope(
        3,
        PROFILE_RECORD_TYPE,
        PROFILE_PROVENANCE,
        payload["blackout_id"],
        payload["segment_id"],
        seq,
        source["first_boot_id"],
        source["first_wall_time_utc"],
        source["first_monotonic_ns"],
        previous,
        dict(payload),
    )


def encode_record(
    payload: Mapping[str, Any], source: Mapping[str, Any], seq: int, previous: str | None
) -> EncodedV3Record:
    validate_payload(payload)
    envelope = fragment_record_envelope(payload, source, seq, previous)
    if canonical_v3_line_size(envelope) > PROFILE_MAX_LINE_BYTES:
        raise V3CodecError("fragment profile record exceeds 8 KiB")
    return encode_v3_record(envelope)


def decode_record(line: bytes, max_bytes: int) -> EncodedV3Record:
    if not isinstance(line, bytes) or len(line) > max_bytes:
        raise V3CodecError("fragment profile record exceeds line budget")
    record = decode_v3_record(line)
    nonnegative(record.envelope.seq, "record sequence")
    if (
        record.envelope.record_type != PROFILE_RECORD_TYPE
        or record.envelope.provenance != PROFILE_PROVENANCE
    ):
        raise V3CodecError("record is not a derived fragment profile")
    validate_payload(record.envelope.payload)
    payload = record.envelope.payload
    if (
        record.envelope.blackout_id != payload["blackout_id"]
        or record.envelope.segment_id != payload["segment_id"]
    ):
        raise V3CodecError("envelope scope is not bound")
    source = payload["source_range"]
    if (record.envelope.boot_id, record.envelope.wall_time_utc, record.envelope.monotonic_ns) != (
        source["first_boot_id"],
        source["first_wall_time_utc"],
        source["first_monotonic_ns"],
    ):
        raise V3CodecError("envelope source boundary is not bound")
    return record
