"""Strict JSONL v3 codec for the permanent load-sag series summary."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from src.adapters.jsonl_v3_canonical import (
    EncodedV3Record,
    V3CodecError,
    V3RecordEnvelope,
    canonical_json_bytes,
    decode_v3_record,
    encode_v3_record,
)
from src.adapters.jsonl_v3_fragment_profile_codec import (
    decode_fragment_profile_records,
    reconstruct_fragment_profiles,
)
from src.domain.fragments import CanonicalDischargeSample
from src.domain.load_sag_assessment import (
    DEFAULT_LOAD_SAG_POLICY,
    LoadSagAssessment,
    LoadSagDisposition,
    LoadSagPolicy,
    LoadSagReason,
    ObservationOrigin,
    assessment_payload,
    resolve_load_sag_policy,
)

LOAD_SAG_SUMMARY_RECORD_TYPE = "load_sag_assessment_summary"
LOAD_SAG_SUMMARY_PROVENANCE = "derived"
LOAD_SAG_SUMMARY_SCHEMA = "load-sag-assessment-summary-v1"
LOAD_SAG_SUMMARY_MAX_LINE_BYTES = 8 * 1024
LOAD_SAG_SUMMARY_MAX_RESULTS = 256
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_UTC_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z\Z")
_DISPOSITION_KEYS = tuple(item.value for item in LoadSagDisposition)
_REASON_KEYS = tuple(item.value for item in LoadSagReason)
_SUMMARY_FIELDS = frozenset(
    {
        "summary_schema",
        "policy_revision",
        "fragment_policy_revision",
        "evaluator_revision",
        "profile_series_id",
        "blackout_id",
        "physical_episode_id",
        "segment_id",
        "battery_epoch_id",
        "observation_origin",
        "uat_intent_id",
        "result_count",
        "disposition_counts",
        "reason_counts",
        "reason_overflow_count",
        "ordered_results_sha256",
        "source_profile_record_hashes_sha256",
        "source_first_boot_id",
        "source_first_wall_time_utc",
        "source_first_monotonic_ns",
        "source_last_boot_id",
        "source_last_wall_time_utc",
        "source_last_monotonic_ns",
    }
)


@dataclass(frozen=True, slots=True)
class _ProfileFacts:
    series_id: str
    policy_revision: str
    blackout_id: str
    physical_episode_id: str
    battery_epoch_id: str
    segment_id: str
    origin: ObservationOrigin
    uat_intent_id: str | None
    record_hashes: tuple[str, ...]
    slice_ids: frozenset[str]
    slice_order: tuple[str, ...]
    step_order: tuple[str, ...]
    step_parents: Mapping[str, str]
    source_range: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class LoadSagAssessmentSummary:
    """Decoded permanent summary for one ordered load-sag result series."""

    summary_schema: str
    policy_revision: str
    fragment_policy_revision: str
    evaluator_revision: str
    profile_series_id: str
    blackout_id: str
    physical_episode_id: str
    segment_id: str
    battery_epoch_id: str
    observation_origin: ObservationOrigin
    uat_intent_id: str | None
    result_count: int
    disposition_counts: tuple[tuple[str, int], ...]
    reason_counts: tuple[tuple[str, int], ...]
    reason_overflow_count: int
    ordered_results_sha256: str
    source_profile_record_hashes_sha256: str
    source_first_boot_id: str
    source_first_wall_time_utc: datetime
    source_first_monotonic_ns: int
    source_last_boot_id: str
    source_last_wall_time_utc: datetime
    source_last_monotonic_ns: int


def encode_load_sag_assessment_summary(
    results: tuple[LoadSagAssessment, ...],
    profile_records: tuple[EncodedV3Record, ...],
    *,
    raw_samples_by_slice: Mapping[str, tuple[CanonicalDischargeSample, ...]],
    **options: object,
) -> EncodedV3Record:
    """Encode one permanent summary without omitting any ordered result."""
    uat_intent_id = _scope_options(options)
    seq = options.pop("seq", 0)
    previous_record_sha256 = options.pop("previous_record_sha256", None)
    if options:
        raise V3CodecError(f"unknown load-sag summary options: {tuple(options)}")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise V3CodecError("summary seq must be a nonnegative integer")
    if previous_record_sha256 is not None and not isinstance(previous_record_sha256, str):
        raise V3CodecError("previous record hash must be lowercase SHA-256 hex")
    try:
        summary = _build_summary(
            results,
            profile_records=profile_records,
            raw_samples_by_slice=raw_samples_by_slice,
            uat_intent_id=uat_intent_id,
        )
    except (TypeError, ValueError) as exc:
        raise V3CodecError("load-sag summary inputs are invalid") from exc
    payload = _summary_payload(summary)
    envelope = V3RecordEnvelope(
        schema_version=3,
        record_type=LOAD_SAG_SUMMARY_RECORD_TYPE,
        provenance=LOAD_SAG_SUMMARY_PROVENANCE,
        blackout_id=summary.blackout_id,
        segment_id=summary.segment_id,
        seq=seq,
        boot_id=summary.source_first_boot_id,
        wall_time_utc=payload["source_first_wall_time_utc"],
        monotonic_ns=summary.source_first_monotonic_ns,
        prev_record_sha256=previous_record_sha256,
        payload=payload,
    )
    try:
        encoded = encode_v3_record(envelope)
    except (TypeError, ValueError) as exc:
        raise V3CodecError("load-sag summary cannot be canonically encoded") from exc
    if len(encoded.line) > LOAD_SAG_SUMMARY_MAX_LINE_BYTES:
        raise V3CodecError("load-sag summary exceeds 8 KiB")
    return encoded


def decode_load_sag_assessment_summary(
    line: bytes,
    results: tuple[LoadSagAssessment, ...],
    profile_records: tuple[EncodedV3Record, ...],
    *,
    raw_samples_by_slice: Mapping[str, tuple[CanonicalDischargeSample, ...]],
    **options: object,
) -> LoadSagAssessmentSummary:
    """Decode and recompute one summary against the complete concrete series."""
    uat_intent_id = _scope_options(options)
    policy = options.pop("policy", DEFAULT_LOAD_SAG_POLICY)
    if options:
        raise V3CodecError(f"unknown load-sag summary options: {tuple(options)}")
    if not isinstance(policy, LoadSagPolicy):
        raise V3CodecError("policy must be LoadSagPolicy")
    if len(line) > LOAD_SAG_SUMMARY_MAX_LINE_BYTES:
        raise V3CodecError("load-sag summary exceeds 8 KiB")
    encoded = _decode_record(line)
    envelope = encoded.envelope
    if envelope.record_type != LOAD_SAG_SUMMARY_RECORD_TYPE:
        raise V3CodecError("record_type must be load_sag_assessment_summary")
    if envelope.provenance != LOAD_SAG_SUMMARY_PROVENANCE:
        raise V3CodecError("load-sag summary provenance must be derived")
    _ensure_policy(policy)
    payload = envelope.payload
    if not isinstance(payload, Mapping) or set(payload) != _SUMMARY_FIELDS:
        raise V3CodecError("load-sag summary fields are not exact")
    try:
        expected = _build_summary(
            results,
            profile_records=profile_records,
            raw_samples_by_slice=raw_samples_by_slice,
            uat_intent_id=uat_intent_id,
        )
    except (TypeError, ValueError) as exc:
        raise V3CodecError("load-sag summary inputs are invalid") from exc
    expected_payload = _summary_payload(expected)
    try:
        _validate_summary_payload(payload, policy)
        _validate_envelope_scope(envelope, payload)
    except (TypeError, ValueError, KeyError) as exc:
        raise V3CodecError("load-sag summary payload is invalid") from exc
    if dict(payload) != expected_payload:
        raise V3CodecError("load-sag summary does not match complete ordered results")
    return expected


def decode_load_sag_assessment_summary_record(
    line: bytes,
    *,
    profile_records: tuple[EncodedV3Record, ...],
    raw_samples_by_slice: Mapping[str, tuple[CanonicalDischargeSample, ...]],
    policy: LoadSagPolicy = DEFAULT_LOAD_SAG_POLICY,
) -> EncodedV3Record:
    """Strict owner entrypoint for a linked load-sag summary record.

    Concrete result replay remains the responsibility of
    ``decode_load_sag_assessment_summary``; this entrypoint validates the
    complete bounded wire payload and its envelope/policy binding.
    """
    if len(line) > LOAD_SAG_SUMMARY_MAX_LINE_BYTES:
        raise V3CodecError("load-sag summary exceeds 8 KiB")
    encoded = _decode_record(line)
    envelope = encoded.envelope
    if envelope.record_type != LOAD_SAG_SUMMARY_RECORD_TYPE:
        raise V3CodecError("record_type must be load_sag_assessment_summary")
    if envelope.provenance != LOAD_SAG_SUMMARY_PROVENANCE:
        raise V3CodecError("load-sag summary provenance must be derived")
    _ensure_policy(policy)
    payload = envelope.payload
    if not isinstance(payload, Mapping) or set(payload) != _SUMMARY_FIELDS:
        raise V3CodecError("load-sag summary fields are not exact")
    try:
        _profile_facts(profile_records, raw_samples_by_slice)
        _validate_summary_payload(payload, policy)
        _validate_envelope_scope(envelope, payload)
    except (TypeError, ValueError, KeyError) as exc:
        raise V3CodecError("load-sag summary payload is invalid") from exc
    return encoded


def ordered_load_sag_results_sha256(results: tuple[LoadSagAssessment, ...]) -> str:
    """Hash canonical concrete result payloads in their supplied order."""
    _validate_results(results)
    digest = sha256()
    for result in results:
        digest.update(canonical_json_bytes(assessment_payload(result)))
        digest.update(b"\n")
    return digest.hexdigest()


def source_profile_record_hashes_sha256(
    profile_records: tuple[EncodedV3Record, ...],
    *,
    raw_samples_by_slice: Mapping[str, tuple[CanonicalDischargeSample, ...]],
) -> str:
    """Hash the ordered, decoded profile-record identities represented by a summary."""
    facts = _profile_facts(profile_records, raw_samples_by_slice)
    return sha256(canonical_json_bytes({"hashes": list(facts.record_hashes)})).hexdigest()


def _scope_options(options: dict[str, object]) -> str | None:
    uat_intent_id = options.pop("uat_intent_id", None)
    if uat_intent_id is not None and not isinstance(uat_intent_id, str):
        raise V3CodecError("uat_intent_id must be text or None")
    return uat_intent_id


def _build_summary(
    results: tuple[LoadSagAssessment, ...],
    *,
    profile_records: tuple[EncodedV3Record, ...],
    raw_samples_by_slice: Mapping[str, tuple[CanonicalDischargeSample, ...]],
    uat_intent_id: str | None,
) -> LoadSagAssessmentSummary:
    profile = _profile_facts(profile_records, raw_samples_by_slice)
    _validate_results(results, profile)
    _validate_uat_scope(profile.origin, uat_intent_id)
    if uat_intent_id != profile.uat_intent_id:
        raise ValueError("summary UAT intent differs from profile scope")
    first = results[0]
    source = profile.source_range
    disposition_counts = _count_dispositions(results)
    reason_counts = _count_reasons(results)
    return LoadSagAssessmentSummary(
        summary_schema=LOAD_SAG_SUMMARY_SCHEMA,
        policy_revision=first.policy_revision,
        fragment_policy_revision=first.fragment_policy_revision,
        evaluator_revision=first.evaluator_revision,
        profile_series_id=profile.series_id,
        blackout_id=profile.blackout_id,
        physical_episode_id=profile.physical_episode_id,
        segment_id=profile.segment_id,
        battery_epoch_id=profile.battery_epoch_id,
        observation_origin=profile.origin,
        uat_intent_id=uat_intent_id,
        result_count=len(results),
        disposition_counts=tuple(disposition_counts.items()),
        reason_counts=tuple(reason_counts.items()),
        reason_overflow_count=0,
        ordered_results_sha256=ordered_load_sag_results_sha256(results),
        source_profile_record_hashes_sha256=source_profile_record_hashes_sha256(
            profile_records,
            raw_samples_by_slice=raw_samples_by_slice,
        ),
        source_first_boot_id=source["first_boot_id"],
        source_first_wall_time_utc=_parse_utc(source["first_wall_time_utc"]),
        source_first_monotonic_ns=source["first_monotonic_ns"],
        source_last_boot_id=source["last_boot_id"],
        source_last_wall_time_utc=_parse_utc(source["last_wall_time_utc"]),
        source_last_monotonic_ns=source["last_monotonic_ns"],
    )


def _summary_payload(summary: LoadSagAssessmentSummary) -> dict[str, Any]:
    return {
        "summary_schema": summary.summary_schema,
        "policy_revision": summary.policy_revision,
        "fragment_policy_revision": summary.fragment_policy_revision,
        "evaluator_revision": summary.evaluator_revision,
        "profile_series_id": summary.profile_series_id,
        "blackout_id": summary.blackout_id,
        "physical_episode_id": summary.physical_episode_id,
        "segment_id": summary.segment_id,
        "battery_epoch_id": summary.battery_epoch_id,
        "observation_origin": summary.observation_origin.value,
        "uat_intent_id": summary.uat_intent_id,
        "result_count": summary.result_count,
        "disposition_counts": dict(summary.disposition_counts),
        "reason_counts": dict(summary.reason_counts),
        "reason_overflow_count": summary.reason_overflow_count,
        "ordered_results_sha256": summary.ordered_results_sha256,
        "source_profile_record_hashes_sha256": summary.source_profile_record_hashes_sha256,
        "source_first_boot_id": summary.source_first_boot_id,
        "source_first_wall_time_utc": _utc(summary.source_first_wall_time_utc),
        "source_first_monotonic_ns": summary.source_first_monotonic_ns,
        "source_last_boot_id": summary.source_last_boot_id,
        "source_last_wall_time_utc": _utc(summary.source_last_wall_time_utc),
        "source_last_monotonic_ns": summary.source_last_monotonic_ns,
    }


def _validate_summary_payload(payload: Mapping[str, Any], policy: LoadSagPolicy) -> None:
    if payload["summary_schema"] != LOAD_SAG_SUMMARY_SCHEMA:
        raise ValueError("unsupported load-sag summary schema")
    if payload["policy_revision"] != policy.revision:
        raise ValueError("load-sag summary policy revision is unsupported")
    if payload["evaluator_revision"] != policy.evaluator_revision:
        raise ValueError("load-sag summary evaluator revision differs")
    for key in (
        "fragment_policy_revision",
        "blackout_id",
        "physical_episode_id",
        "segment_id",
        "battery_epoch_id",
        "source_first_boot_id",
        "source_last_boot_id",
    ):
        _text(payload[key], key)
    _validate_hash(payload["profile_series_id"], "profile series ID")
    _validate_hash(payload["ordered_results_sha256"], "ordered result digest")
    _validate_hash(
        payload["source_profile_record_hashes_sha256"],
        "source profile-record digest",
    )
    origin = _enum(payload["observation_origin"], ObservationOrigin, "observation origin")
    _validate_uat_scope(origin, payload["uat_intent_id"])
    result_count = _int(payload["result_count"], "result count")
    if not 0 < result_count <= LOAD_SAG_SUMMARY_MAX_RESULTS:
        raise ValueError("result count is outside the summary bound")
    _validate_counts(payload["disposition_counts"], _DISPOSITION_KEYS, result_count)
    _validate_reason_counts(payload["reason_counts"], result_count)
    if _int(payload["reason_overflow_count"], "reason overflow count") != 0:
        raise ValueError("closed load-sag reasons cannot overflow")
    for key in (
        "source_first_wall_time_utc",
        "source_last_wall_time_utc",
    ):
        _parse_utc(payload[key])
    for key in ("source_first_monotonic_ns", "source_last_monotonic_ns"):
        _int(payload[key], key)


def _validate_envelope_scope(envelope: V3RecordEnvelope, payload: Mapping[str, Any]) -> None:
    if (
        envelope.blackout_id != payload["blackout_id"]
        or envelope.segment_id != payload["segment_id"]
        or envelope.boot_id != payload["source_first_boot_id"]
        or envelope.wall_time_utc != payload["source_first_wall_time_utc"]
        or envelope.monotonic_ns != payload["source_first_monotonic_ns"]
    ):
        raise ValueError("load-sag summary envelope scope is not bound")


def _validate_results(
    results: tuple[LoadSagAssessment, ...],
    profile: _ProfileFacts | None = None,
) -> None:
    if not isinstance(results, tuple) or not results:
        raise ValueError("load-sag result series must be a non-empty tuple")
    if len(results) > LOAD_SAG_SUMMARY_MAX_RESULTS:
        raise ValueError("load-sag result series exceeds 256 results")
    if any(not isinstance(result, LoadSagAssessment) for result in results):
        raise TypeError("load-sag results must be LoadSagAssessment values")
    first = results[0]
    for result in results[1:]:
        if _series_scope(result) != _series_scope(first):
            raise ValueError("load-sag results do not share exact series scope and policy")
    if profile is None:
        return
    _validate_results_against_profile(results, profile)


def _profile_facts(
    records: tuple[EncodedV3Record, ...],
    raw_samples_by_slice: Mapping[str, tuple[CanonicalDischargeSample, ...]],
) -> _ProfileFacts:
    if not isinstance(records, tuple) or not records:
        raise ValueError("profile record chain must be a non-empty tuple")
    if len(records) > LOAD_SAG_SUMMARY_MAX_RESULTS:
        raise ValueError("profile record chain exceeds 256 records")
    try:
        decoded = decode_fragment_profile_records(record.line for record in records)
        if decoded != records:
            raise ValueError("profile records are not the supplied decoded canonical chain")
        profiles = reconstruct_fragment_profiles(decoded, raw_samples_by_slice)
    except (TypeError, ValueError, V3CodecError) as exc:
        raise ValueError("profile records do not replay against supplied raw samples") from exc
    slices = tuple(item for profile in profiles for item in profile.slices)
    steps = tuple(item for profile in profiles for item in profile.load_steps)
    samples = tuple(sample for item in slices for sample in item.samples)
    if not slices or not samples:
        raise ValueError("reconstructed profile chain has no physical samples")
    first_profile = profiles[0]
    first_slice = slices[0]
    first_sample = samples[0]
    last_sample = samples[-1]
    source_range = _source_range(first_sample, last_sample, len(samples))
    return _ProfileFacts(
        series_id=first_profile.series_id,
        policy_revision=first_profile.policy_revision,
        blackout_id=first_slice.blackout_id,
        physical_episode_id=first_slice.physical_episode_id,
        battery_epoch_id=first_slice.battery_epoch_id,
        segment_id=first_slice.segment_id,
        origin=first_slice.origin,
        uat_intent_id=first_slice.uat_intent_id,
        record_hashes=tuple(record.record_sha256 for record in decoded),
        slice_ids=frozenset(item.slice_id for item in slices),
        slice_order=tuple(item.slice_id for item in slices),
        step_order=tuple(item.step_record_hash for item in steps),
        step_parents={item.step_record_hash: item.parent_slice.slice_id for item in steps},
        source_range=source_range,
    )


def _source_range(
    first: CanonicalDischargeSample,
    last: CanonicalDischargeSample,
    count: int,
) -> Mapping[str, Any]:
    return {
        "first_sequence": first.sequence,
        "last_sequence": last.sequence,
        "sample_count": count,
        "first_sample_hash": first.canonical_hash,
        "last_sample_hash": last.canonical_hash,
        "first_boot_id": first.observation.boot_id,
        "last_boot_id": last.observation.boot_id,
        "first_wall_time_utc": _utc(first.observation.wall_time_utc),
        "last_wall_time_utc": _utc(last.observation.wall_time_utc),
        "first_monotonic_ns": first.observation.monotonic_ns,
        "last_monotonic_ns": last.observation.monotonic_ns,
    }


def _validate_results_against_profile(
    results: tuple[LoadSagAssessment, ...], profile: _ProfileFacts
) -> None:
    step_positions = {value: index for index, value in enumerate(profile.step_order)}
    slice_positions = {value: index for index, value in enumerate(profile.slice_order)}
    result_positions: list[int] = []
    seen_steps: set[str] = set()
    for result in results:
        if _series_scope(result) != (
            result.policy_revision,
            result.fragment_policy_revision,
            result.evaluator_revision,
            profile.blackout_id,
            profile.physical_episode_id,
            profile.segment_id,
            profile.battery_epoch_id,
            profile.origin,
        ):
            raise ValueError("load-sag result scope differs from profile scope")
        if result.fragment_policy_revision != profile.policy_revision:
            raise ValueError("load-sag result policy differs from profile policy")
        slice_positions_for_result = tuple(
            slice_positions.get(value, -1) for value in result.source_slice_hashes
        )
        step_positions_for_result = tuple(
            step_positions.get(value, -1) for value in result.source_step_hashes
        )
        _validate_reference_order(
            result.source_slice_hashes,
            slice_positions_for_result,
            required=True,
        )
        _validate_reference_order(result.source_step_hashes, step_positions_for_result)
        if any(
            profile.step_parents[step_hash] not in result.source_slice_hashes
            for step_hash in result.source_step_hashes
        ):
            raise ValueError("load-sag step parent is absent from result slice references")
        if seen_steps.intersection(result.source_step_hashes):
            raise ValueError("load-sag results overlap profile steps")
        seen_steps.update(result.source_step_hashes)
        result_positions.append(
            step_positions_for_result[0]
            if step_positions_for_result
            else slice_positions_for_result[0]
        )
    if result_positions != sorted(result_positions) or len(set(result_positions)) != len(
        result_positions
    ):
        raise ValueError("load-sag results are not in canonical profile physical order")


def _validate_reference_order(
    hashes: tuple[str, ...], positions: tuple[int, ...], *, required: bool = False
) -> None:
    if required and not hashes:
        raise ValueError("load-sag result must reference a profile slice")
    if any(value < 0 for value in positions) or positions != tuple(sorted(positions)):
        raise ValueError("load-sag result references are not in profile physical order")
    if len(set(hashes)) != len(hashes):
        raise ValueError("load-sag result references must be unique")


def _series_scope(result: LoadSagAssessment) -> tuple[object, ...]:
    return (
        result.policy_revision,
        result.fragment_policy_revision,
        result.evaluator_revision,
        result.source_blackout_id,
        result.source_physical_episode_id,
        result.source_segment_id,
        result.battery_epoch_id,
        result.origin,
    )


def _validate_uat_scope(origin: ObservationOrigin, intent: str | None) -> None:
    if intent is not None:
        _text(intent, "UAT intent ID")
    if origin is ObservationOrigin.UAT and intent is None:
        raise ValueError("UAT summary requires UAT intent ID")
    if origin is not ObservationOrigin.UAT and intent is not None:
        raise ValueError("UAT intent ID is only valid for UAT summaries")


def _count_dispositions(results: tuple[LoadSagAssessment, ...]) -> dict[str, int]:
    counts = {key: 0 for key in _DISPOSITION_KEYS}
    for result in results:
        counts[result.disposition.value] += 1
    return counts


def _count_reasons(results: tuple[LoadSagAssessment, ...]) -> dict[str, int]:
    counts = {key: 0 for key in _REASON_KEYS}
    for result in results:
        for refusal in result.refusals:
            counts[refusal.reason.value] += 1
    return counts


def _validate_counts(value: Any, keys: tuple[str, ...], result_count: int) -> None:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("summary counts are not closed")
    counts = tuple(_int(value[key], f"count {key}") for key in keys)
    if sum(counts) != result_count:
        raise ValueError("disposition counts do not match result count")


def _validate_reason_counts(value: Any, result_count: int) -> None:
    if not isinstance(value, dict) or set(value) != set(_REASON_KEYS):
        raise ValueError("summary reason counts are not closed")
    for key in _REASON_KEYS:
        count = _int(value[key], f"reason count {key}")
        if count > result_count * DEFAULT_LOAD_SAG_POLICY.max_step_references:
            raise ValueError("reason count exceeds bounded result references")


def _decode_record(line: bytes) -> EncodedV3Record:
    try:
        return decode_v3_record(line)
    except (TypeError, ValueError) as exc:
        raise V3CodecError("invalid load-sag summary envelope") from exc


def _ensure_policy(policy: LoadSagPolicy) -> None:
    if not isinstance(policy, LoadSagPolicy):
        raise V3CodecError("policy must be LoadSagPolicy")
    try:
        resolved = resolve_load_sag_policy(policy.revision)
    except (TypeError, ValueError) as exc:
        raise V3CodecError("unsupported load-sag policy revision") from exc
    if policy != resolved:
        raise V3CodecError("load-sag policy values do not match its revision")


def _enum(value: Any, enum_type: type[Any], name: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not closed") from exc


def _text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _validate_hash(value: Any, name: str) -> None:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be lowercase SHA-256 hex")


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        raise ValueError("wall time must be canonical UTC text")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("wall time must be UTC")
    return parsed


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
