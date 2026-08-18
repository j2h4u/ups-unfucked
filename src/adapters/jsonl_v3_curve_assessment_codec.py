"""Permanent v3 summary codec for ordered curve assessments."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
from src.domain.curve_assessment import CurveAssessment, CurveDisposition, CurveReason
from src.domain.fragments import (
    CanonicalDischargeSample,
    DischargeFragmentProfile,
    ObservationOrigin,
)

CURVE_SUMMARY_RECORD_TYPE = "curve_assessment_summary"
CURVE_SUMMARY_PROVENANCE = "derived"
CURVE_SUMMARY_SCHEMA = "curve-assessment-summary-v1"
MAX_CURVE_SUMMARY_LINE_BYTES = 8 * 1024
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = frozenset(
    {
        "schema",
        "evaluator_revision",
        "profile_series_id",
        "blackout_id",
        "physical_episode_id",
        "battery_epoch_id",
        "observation_origin",
        "uat_intent_id",
        "result_count",
        "disposition_counts",
        "reason_counts",
        "reason_overflow_count",
        "ordered_results_sha256",
        "source_profile_record_hashes_sha256",
        "source_first_profile_record_hash",
        "source_last_profile_record_hash",
    }
)
_DISPOSITIONS = {item.value for item in CurveDisposition}
_REASONS = {item.value for item in CurveReason}


@dataclass(frozen=True, slots=True)
class _ProfileChain:
    records: tuple[EncodedV3Record, ...]
    profiles: tuple[DischargeFragmentProfile, ...]
    series_id: str
    blackout_id: str
    physical_episode_id: str
    battery_epoch_id: str
    observation_origin: str
    uat_intent_id: str | None
    slice_keys: tuple[tuple[str, str], ...]
    logical_profiles: tuple["_LogicalProfile", ...]

    @property
    def record_hashes(self) -> tuple[str, ...]:
        return tuple(record.record_sha256 for record in self.records)


@dataclass(frozen=True, slots=True)
class _LogicalProfile:
    identity: str
    ordinal: int
    slice_keys: tuple[tuple[str, str], ...]


def encode_curve_assessment_summary(
    assessments: Sequence[CurveAssessment],
    profile_records: Sequence[EncodedV3Record],
    *,
    raw_samples_by_slice: Mapping[str, tuple[CanonicalDischargeSample, ...]],
    seq: int = 0,
    previous_record_sha256: str | None = None,
) -> EncodedV3Record:
    profile = _profile_chain(profile_records, raw_samples_by_slice)
    ordered = _ordered_assessments(assessments, profile)
    payload = _summary_payload(ordered, profile)
    _validate_summary_payload(payload)
    record = encode_v3_record(
        V3RecordEnvelope(
            3,
            CURVE_SUMMARY_RECORD_TYPE,
            CURVE_SUMMARY_PROVENANCE,
            payload["blackout_id"],
            "summary",
            seq,
            "derived",
            "1970-01-01T00:00:00Z",
            0,
            previous_record_sha256,
            payload,
        )
    )
    if len(record.line) > MAX_CURVE_SUMMARY_LINE_BYTES:
        raise V3CodecError("curve assessment summary exceeds 8 KiB")
    return record


def decode_curve_assessment_summary(line: bytes) -> EncodedV3Record:
    if len(line) > MAX_CURVE_SUMMARY_LINE_BYTES:
        raise V3CodecError("curve assessment summary exceeds 8 KiB")
    record = decode_v3_record(line)
    if not _is_summary_envelope(record):
        raise V3CodecError("record is not a curve assessment summary")
    _validate_summary_payload(record.envelope.payload)
    if record.envelope.blackout_id != record.envelope.payload["blackout_id"]:
        raise V3CodecError("curve summary envelope scope is not bound")
    return record


def decode_curve_assessment_summary_record(
    line: bytes,
    *,
    profile_records: Sequence[EncodedV3Record],
    raw_samples_by_slice: Mapping[str, tuple[CanonicalDischargeSample, ...]],
) -> EncodedV3Record:
    """Strict owner entrypoint for a linked curve summary record."""
    profile = _profile_chain(profile_records, raw_samples_by_slice)
    record = decode_curve_assessment_summary(line)
    _validate_source_binding(record, profile)
    return record


def verify_curve_assessment_summary(
    record: EncodedV3Record | bytes,
    assessments: Sequence[CurveAssessment],
    profile_records: Sequence[EncodedV3Record],
    *,
    raw_samples_by_slice: Mapping[str, tuple[CanonicalDischargeSample, ...]],
) -> None:
    """Replay concrete results and reject missing, reordered, or mutated values."""
    encoded = decode_curve_assessment_summary(record) if isinstance(record, bytes) else record
    profile = _profile_chain(profile_records, raw_samples_by_slice)
    expected = _summary_payload(_ordered_assessments(assessments, profile), profile)
    if dict(encoded.envelope.payload) != expected:
        raise V3CodecError("curve summary replay does not match concrete results")


def _profile_chain(
    records: Sequence[EncodedV3Record],
    raw_samples_by_slice: Mapping[str, tuple[CanonicalDischargeSample, ...]],
) -> _ProfileChain:
    values = tuple(records)
    if not values or any(not isinstance(item, EncodedV3Record) for item in values):
        raise V3CodecError("decoded profile records are required")
    decoded = decode_fragment_profile_records(item.line for item in values)
    if decoded != values:
        raise V3CodecError("profile records are not canonical decoded values")
    profiles = reconstruct_fragment_profiles(decoded, raw_samples_by_slice)
    first = profiles[0]
    slices = tuple(item for profile in profiles for item in profile.slices)
    if not slices:
        raise V3CodecError("profile chain contains no physical slices")
    first_slice = slices[0]
    logical_profiles = _logical_profiles(profiles)
    return _ProfileChain(
        decoded,
        profiles,
        first.series_id,
        first_slice.blackout_id,
        first_slice.physical_episode_id,
        first_slice.battery_epoch_id,
        first_slice.origin.value,
        first_slice.uat_intent_id,
        tuple((item.segment_id, item.slice_id) for item in slices),
        logical_profiles,
    )


def _logical_profiles(
    profiles: tuple[DischargeFragmentProfile, ...],
) -> tuple[_LogicalProfile, ...]:
    return tuple(
        _LogicalProfile(
            profile.series_id,
            profile.ordinal,
            tuple((item.segment_id, item.slice_id) for item in profile.slices),
        )
        for profile in profiles
    )


def _validate_source_binding(record: EncodedV3Record, profile: _ProfileChain) -> None:
    payload = record.envelope.payload
    hashes = list(profile.record_hashes)
    if payload["profile_series_id"] != profile.series_id:
        raise V3CodecError("curve summary profile series differs")
    if payload["source_profile_record_hashes_sha256"] != _digest(hashes):
        raise V3CodecError("curve summary profile-record digest differs")
    if payload["source_first_profile_record_hash"] != hashes[0]:
        raise V3CodecError("curve summary first profile record differs")
    if payload["source_last_profile_record_hash"] != hashes[-1]:
        raise V3CodecError("curve summary last profile record differs")


def _ordered_assessments(
    assessments: Sequence[CurveAssessment], profile: _ProfileChain
) -> tuple[CurveAssessment, ...]:
    values = tuple(assessments)
    if not values or any(not isinstance(item, CurveAssessment) for item in values):
        raise V3CodecError("curve summary requires concrete assessments")
    keys = tuple((item.segment_id, item.slice_id) for item in values)
    logical_order = tuple(key for logical in profile.logical_profiles for key in logical.slice_keys)
    if keys != profile.slice_keys or keys != logical_order:
        raise V3CodecError("curve results are not in canonical profile order")
    _validate_profile_scope(values, profile)
    return values


def _validate_profile_scope(values: tuple[CurveAssessment, ...], profile: _ProfileChain) -> None:
    for value in values:
        if not _same_profile_scope(value, profile):
            raise V3CodecError("curve results do not match profile scope")


def _same_profile_scope(value: CurveAssessment, profile: _ProfileChain) -> bool:
    pairs = (
        (value.profile_series_id, profile.series_id),
        (value.blackout_id, profile.blackout_id),
        (value.physical_episode_id, profile.physical_episode_id),
        (value.battery_epoch_id, profile.battery_epoch_id),
        (value.observation_origin.value, profile.observation_origin),
        (value.uat_intent_id, profile.uat_intent_id),
    )
    return all(left == right for left, right in pairs)


def _summary_payload(
    assessments: Sequence[CurveAssessment], profile: _ProfileChain
) -> dict[str, Any]:
    ordered = tuple(assessments)
    _validate_scope(ordered)
    hashes = _validated_hashes(profile.record_hashes)
    results = [_result_payload(item) for item in ordered]
    return {
        "schema": CURVE_SUMMARY_SCHEMA,
        "evaluator_revision": ordered[0].evaluator_revision,
        "profile_series_id": ordered[0].profile_series_id,
        "blackout_id": ordered[0].blackout_id,
        "physical_episode_id": ordered[0].physical_episode_id,
        "battery_epoch_id": ordered[0].battery_epoch_id,
        "observation_origin": ordered[0].observation_origin.value,
        "uat_intent_id": ordered[0].uat_intent_id,
        "result_count": len(ordered),
        "disposition_counts": {
            item: sum(value.disposition.value == item for value in ordered)
            for item in _DISPOSITIONS
        },
        "reason_counts": {
            item: sum(item in {reason.value for reason in value.reasons} for value in ordered)
            for item in _REASONS
        },
        "reason_overflow_count": sum(value.reason_overflow_count for value in ordered),
        "ordered_results_sha256": _digest(results),
        "source_profile_record_hashes_sha256": _digest(list(hashes)),
        "source_first_profile_record_hash": hashes[0],
        "source_last_profile_record_hash": hashes[-1],
    }


def _result_payload(value: CurveAssessment) -> dict[str, Any]:
    return {
        "policy_revision": value.policy_revision,
        "evaluator_revision": value.evaluator_revision,
        "profile_series_id": value.profile_series_id,
        "slice_id": value.slice_id,
        "blackout_id": value.blackout_id,
        "physical_episode_id": value.physical_episode_id,
        "battery_epoch_id": value.battery_epoch_id,
        "segment_id": value.segment_id,
        "observation_origin": value.observation_origin.value,
        "uat_intent_id": value.uat_intent_id,
        "disposition": value.disposition.value,
        "raw_sample_count": value.raw_sample_count,
        "raw_first_sample_hash": value.raw_first_sample_hash,
        "raw_last_sample_hash": value.raw_last_sample_hash,
        "raw_ordered_hashes_sha256": value.raw_ordered_hashes_sha256,
        "raw_span_digests": list(value.raw_span_digests),
        "coverage_ratio": value.coverage_ratio,
        "max_gap_s": value.max_gap_s,
        "voltage_span_v": value.voltage_span_v,
        "voltage_quantum_v": value.voltage_quantum_v,
        "reasons": [item.value for item in value.reasons],
        "reason_overflow_count": value.reason_overflow_count,
        "profile_issue_overflow_count": value.profile_issue_overflow_count,
        "first_unprofiled_raw_hash": value.first_unprofiled_raw_hash,
        "source_boot_id": value.source_boot_id,
        "source_wall_time_utc": None
        if value.source_wall_time_utc is None
        else value.source_wall_time_utc.isoformat(),
        "source_monotonic_ns": value.source_monotonic_ns,
        "comparison": None if value.comparison is None else _comparison_payload(value.comparison),
    }


def _comparison_payload(value: Any) -> dict[str, Any]:
    return {
        "mode": value.mode.value,
        "evaluation_origin_monotonic_ns": value.evaluation_origin_monotonic_ns,
        "evaluated_duration_s": value.evaluated_duration_s,
        "point_count": value.point_count,
        "start_residual_v": value.start_residual_v,
        "end_residual_v": value.end_residual_v,
        "mean_residual_v": value.mean_residual_v,
        "rmse_v": value.rmse_v,
        "observed_slope_v_per_s": value.observed_slope_v_per_s,
        "predicted_slope_v_per_s": value.predicted_slope_v_per_s,
        "delivered_ah_proxy": value.delivered_ah_proxy,
        "reasons": list(value.reasons.values),
    }


def _validate_scope(values: tuple[CurveAssessment, ...]) -> None:
    first = values[0]
    keys = (
        "evaluator_revision",
        "profile_series_id",
        "blackout_id",
        "physical_episode_id",
        "battery_epoch_id",
        "observation_origin",
        "uat_intent_id",
    )
    for value in values[1:]:
        if any(getattr(value, key) != getattr(first, key) for key in keys):
            raise V3CodecError("curve summary assessments do not share exact scope")


def _validated_hashes(values: Sequence[str]) -> tuple[str, ...]:
    hashes = tuple(values)
    if not hashes or any(
        not isinstance(item, str) or _HASH_RE.fullmatch(item) is None for item in hashes
    ):
        raise V3CodecError("source profile record hashes are invalid")
    return hashes


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes({"value": value})).hexdigest()


def _validate_summary_payload(value: Mapping[str, Any]) -> None:
    _validate_summary_shape(value)
    _validate_summary_scope(value)
    _validate_summary_counts(value)
    _validate_summary_digests(value)


def _is_summary_envelope(record: EncodedV3Record) -> bool:
    envelope = record.envelope
    return (
        envelope.record_type == CURVE_SUMMARY_RECORD_TYPE
        and envelope.provenance == CURVE_SUMMARY_PROVENANCE
        and envelope.segment_id == "summary"
        and envelope.boot_id == "derived"
        and envelope.wall_time_utc == "1970-01-01T00:00:00Z"
        and envelope.monotonic_ns == 0
    )


def _validate_summary_shape(value: Mapping[str, Any]) -> None:
    if set(value) != _FIELDS or value["schema"] != CURVE_SUMMARY_SCHEMA:
        raise V3CodecError("curve summary fields/schema are not exact")


def _validate_summary_scope(value: Mapping[str, Any]) -> None:
    for key in ("evaluator_revision", "blackout_id", "physical_episode_id", "battery_epoch_id"):
        if not isinstance(value[key], str) or not value[key]:
            raise V3CodecError(f"{key} must be non-empty text")
    if _HASH_RE.fullmatch(value["profile_series_id"]) is None:
        raise V3CodecError("profile series ID is invalid")
    if value["observation_origin"] not in {item.value for item in ObservationOrigin}:
        raise V3CodecError("curve summary origin is not closed")
    if value["observation_origin"] != "uat" and value["uat_intent_id"] is not None:
        raise V3CodecError("curve summary UAT scope is invalid")
    if value["uat_intent_id"] is not None and not isinstance(value["uat_intent_id"], str):
        raise V3CodecError("curve summary UAT scope is invalid")


def _validate_summary_counts(value: Mapping[str, Any]) -> None:
    if (
        isinstance(value["result_count"], bool)
        or not isinstance(value["result_count"], int)
        or value["result_count"] <= 0
    ):
        raise V3CodecError("curve summary result count is invalid")
    _validate_counts(
        value["disposition_counts"], _DISPOSITIONS, value["result_count"], "dispositions"
    )
    _validate_counts(value["reason_counts"], _REASONS, None, "reasons")
    if (
        isinstance(value["reason_overflow_count"], bool)
        or not isinstance(value["reason_overflow_count"], int)
        or value["reason_overflow_count"] < 0
    ):
        raise V3CodecError("curve summary reason overflow is invalid")


def _validate_summary_digests(value: Mapping[str, Any]) -> None:
    for key in (
        "ordered_results_sha256",
        "source_profile_record_hashes_sha256",
        "source_first_profile_record_hash",
        "source_last_profile_record_hash",
    ):
        if _HASH_RE.fullmatch(value[key]) is None:
            raise V3CodecError(f"{key} is invalid")


def _validate_counts(value: Any, allowed: set[str], total: int | None, name: str) -> None:
    if not isinstance(value, dict) or set(value) != allowed:
        raise V3CodecError(f"curve summary {name} are not closed")
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in value.values()
    ):
        raise V3CodecError(f"curve summary {name} counts are invalid")
    if total is not None and sum(value.values()) != total:
        raise V3CodecError(f"curve summary {name} do not sum to result count")
