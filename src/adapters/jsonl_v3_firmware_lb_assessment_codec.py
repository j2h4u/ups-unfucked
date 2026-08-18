"""Permanent v3 summary codec for ordered firmware-LB assessments."""

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
from src.domain.firmware_lb_assessment import (
    FirmwareLbAssessment,
    FirmwareLbDisposition,
    FirmwareLbReason,
)
from src.domain.fragments import (
    CanonicalDischargeSample,
    DischargeFragmentProfile,
    ObservationOrigin,
)

FIRMWARE_LB_SUMMARY_RECORD_TYPE = "firmware_lb_assessment_summary"
FIRMWARE_LB_SUMMARY_PROVENANCE = "derived"
FIRMWARE_LB_SUMMARY_SCHEMA = "firmware-lb-assessment-summary-v1"
MAX_FIRMWARE_LB_SUMMARY_LINE_BYTES = 8 * 1024
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
_DISPOSITIONS = {item.value for item in FirmwareLbDisposition}
_REASONS = {item.value for item in FirmwareLbReason}


@dataclass(frozen=True, slots=True)
class _ProfileChain:
    records: tuple[EncodedV3Record, ...]
    series_id: str
    blackout_id: str
    physical_episode_id: str
    battery_epoch_id: str
    observation_origin: str
    uat_intent_id: str | None
    logical_profiles: tuple["_LogicalProfile", ...]

    @property
    def record_hashes(self) -> tuple[str, ...]:
        return tuple(record.record_sha256 for record in self.records)


@dataclass(frozen=True, slots=True)
class _LogicalProfile:
    identity: str
    ordinal: int
    slice_ids: tuple[str, ...]


def encode_firmware_lb_assessment_summary(
    assessments: Sequence[FirmwareLbAssessment],
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
            FIRMWARE_LB_SUMMARY_RECORD_TYPE,
            FIRMWARE_LB_SUMMARY_PROVENANCE,
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
    if len(record.line) > MAX_FIRMWARE_LB_SUMMARY_LINE_BYTES:
        raise V3CodecError("firmware-LB assessment summary exceeds 8 KiB")
    return record


def decode_firmware_lb_assessment_summary(line: bytes) -> EncodedV3Record:
    if len(line) > MAX_FIRMWARE_LB_SUMMARY_LINE_BYTES:
        raise V3CodecError("firmware-LB assessment summary exceeds 8 KiB")
    record = decode_v3_record(line)
    if not _is_summary_envelope(record):
        raise V3CodecError("record is not a firmware-LB assessment summary")
    _validate_summary_payload(record.envelope.payload)
    if record.envelope.blackout_id != record.envelope.payload["blackout_id"]:
        raise V3CodecError("firmware-LB summary envelope scope is not bound")
    return record


def decode_firmware_lb_assessment_summary_record(
    line: bytes,
    *,
    profile_records: Sequence[EncodedV3Record],
    raw_samples_by_slice: Mapping[str, tuple[CanonicalDischargeSample, ...]],
) -> EncodedV3Record:
    """Strict owner entrypoint for a linked firmware summary record."""
    profile = _profile_chain(profile_records, raw_samples_by_slice)
    record = decode_firmware_lb_assessment_summary(line)
    _validate_source_binding(record, profile)
    return record


def verify_firmware_lb_assessment_summary(
    record: EncodedV3Record | bytes,
    assessments: Sequence[FirmwareLbAssessment],
    profile_records: Sequence[EncodedV3Record],
    *,
    raw_samples_by_slice: Mapping[str, tuple[CanonicalDischargeSample, ...]],
) -> None:
    encoded = decode_firmware_lb_assessment_summary(record) if isinstance(record, bytes) else record
    profile = _profile_chain(profile_records, raw_samples_by_slice)
    expected = _summary_payload(_ordered_assessments(assessments, profile), profile)
    if dict(encoded.envelope.payload) != expected:
        raise V3CodecError("firmware-LB summary replay does not match concrete results")


def _ordered_assessments(
    assessments: Sequence[FirmwareLbAssessment],
    profile: _ProfileChain,
) -> tuple[FirmwareLbAssessment, ...]:
    values = tuple(assessments)
    if not values or any(not isinstance(item, FirmwareLbAssessment) for item in values):
        raise V3CodecError("firmware-LB summary requires concrete assessments")
    if len(values) != len(profile.logical_profiles):
        raise V3CodecError("firmware-LB results do not match logical profile count")
    _validate_profile_scope(values, profile)
    for value, logical in zip(values, profile.logical_profiles, strict=True):
        if not value.slice_ids or tuple(value.slice_ids) != logical.slice_ids:
            raise V3CodecError("firmware-LB results are not in canonical profile order")
    return values


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
    if not profiles or not profiles[0].slices:
        raise V3CodecError("profile chain contains no physical slices")
    first = profiles[0]
    first_slice = first.slices[0]
    logical_profiles = _logical_profiles(profiles)
    return _ProfileChain(
        decoded,
        first.series_id,
        first_slice.blackout_id,
        first_slice.physical_episode_id,
        first_slice.battery_epoch_id,
        first_slice.origin.value,
        first_slice.uat_intent_id,
        logical_profiles,
    )


def _logical_profiles(
    profiles: tuple[DischargeFragmentProfile, ...],
) -> tuple[_LogicalProfile, ...]:
    return tuple(
        _LogicalProfile(
            profile.series_id,
            profile.ordinal,
            tuple(item.slice_id for item in profile.slices),
        )
        for profile in profiles
    )


def _validate_source_binding(record: EncodedV3Record, profile: _ProfileChain) -> None:
    payload = record.envelope.payload
    hashes = list(profile.record_hashes)
    if payload["profile_series_id"] != profile.series_id:
        raise V3CodecError("firmware summary profile series differs")
    if payload["source_profile_record_hashes_sha256"] != _digest(hashes):
        raise V3CodecError("firmware summary profile-record digest differs")
    if payload["source_first_profile_record_hash"] != hashes[0]:
        raise V3CodecError("firmware summary first profile record differs")
    if payload["source_last_profile_record_hash"] != hashes[-1]:
        raise V3CodecError("firmware summary last profile record differs")


def _validate_profile_scope(
    values: tuple[FirmwareLbAssessment, ...], profile: _ProfileChain
) -> None:
    for value in values:
        if (
            value.profile_series_id != profile.series_id
            or value.blackout_id != profile.blackout_id
            or value.physical_episode_id != profile.physical_episode_id
            or value.battery_epoch_id != profile.battery_epoch_id
            or value.observation_origin.value != profile.observation_origin
        ):
            raise V3CodecError("firmware-LB results do not match profile scope")


def _summary_payload(
    assessments: Sequence[FirmwareLbAssessment], profile: _ProfileChain
) -> dict[str, Any]:
    ordered = tuple(assessments)
    _validate_scope(ordered, profile.uat_intent_id)
    hashes = _validated_hashes(profile.record_hashes)
    results = [_result_payload(item) for item in ordered]
    return {
        "schema": FIRMWARE_LB_SUMMARY_SCHEMA,
        "evaluator_revision": ordered[0].evaluator_revision,
        "profile_series_id": ordered[0].profile_series_id,
        "blackout_id": ordered[0].blackout_id,
        "physical_episode_id": ordered[0].physical_episode_id,
        "battery_epoch_id": ordered[0].battery_epoch_id,
        "observation_origin": ordered[0].observation_origin.value,
        "uat_intent_id": profile.uat_intent_id,
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


def _result_payload(value: FirmwareLbAssessment) -> dict[str, Any]:
    return {
        "policy_revision": value.policy_revision,
        "evaluator_revision": value.evaluator_revision,
        "profile_series_id": value.profile_series_id,
        "slice_ids": list(value.slice_ids),
        "blackout_id": value.blackout_id,
        "physical_episode_id": value.physical_episode_id,
        "battery_epoch_id": value.battery_epoch_id,
        "segment_id": value.segment_id,
        "observation_origin": value.observation_origin.value,
        "readiness": value.readiness,
        "readiness_reason": value.readiness_reason,
        "readiness_provenance": None
        if value.readiness_provenance is None
        else value.readiness_provenance.value,
        "disposition": value.disposition.value,
        "comparable": value.comparable,
        "evaluation_origin_sequence": value.evaluation_origin_sequence,
        "evaluation_origin_sample_hash": value.evaluation_origin_sample_hash,
        "lb_sequence": value.lb_sequence,
        "lb_sample_hash": value.lb_sample_hash,
        "lb_raw_status": value.lb_raw_status,
        "raw_sample_count": value.raw_sample_count,
        "raw_first_sample_hash": value.raw_first_sample_hash,
        "raw_last_sample_hash": value.raw_last_sample_hash,
        "raw_ordered_hashes_sha256": value.raw_ordered_hashes_sha256,
        "raw_span_digests": list(value.raw_span_digests),
        "coverage_ratio": value.coverage_ratio,
        "max_gap_s": value.max_gap_s,
        "reason_overflow_count": value.reason_overflow_count,
        "profile_issue_overflow_count": value.profile_issue_overflow_count,
        "first_unprofiled_raw_hash": value.first_unprofiled_raw_hash,
        "source_boot_id": value.source_boot_id,
        "source_wall_time_utc": value.source_wall_time_utc.isoformat(),
        "source_monotonic_ns": value.source_monotonic_ns,
        "shadow_only": value.shadow_only,
    }


def _validate_scope(values: tuple[FirmwareLbAssessment, ...], uat_intent_id: str | None) -> None:
    first = values[0]
    keys = (
        "evaluator_revision",
        "profile_series_id",
        "blackout_id",
        "physical_episode_id",
        "battery_epoch_id",
        "observation_origin",
    )
    if any(getattr(value, key) != getattr(first, key) for value in values[1:] for key in keys):
        raise V3CodecError("firmware-LB summary assessments do not share exact scope")
    if first.observation_origin is ObservationOrigin.UAT and not uat_intent_id:
        raise V3CodecError("UAT firmware-LB summary requires intent scope")
    if first.observation_origin is not ObservationOrigin.UAT and uat_intent_id is not None:
        raise V3CodecError("non-UAT firmware-LB summary cannot carry intent scope")


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
        envelope.record_type == FIRMWARE_LB_SUMMARY_RECORD_TYPE
        and envelope.provenance == FIRMWARE_LB_SUMMARY_PROVENANCE
        and envelope.segment_id == "summary"
        and envelope.boot_id == "derived"
        and envelope.wall_time_utc == "1970-01-01T00:00:00Z"
        and envelope.monotonic_ns == 0
    )


def _validate_summary_shape(value: Mapping[str, Any]) -> None:
    if set(value) != _FIELDS or value["schema"] != FIRMWARE_LB_SUMMARY_SCHEMA:
        raise V3CodecError("firmware-LB summary fields/schema are not exact")


def _validate_summary_scope(value: Mapping[str, Any]) -> None:
    for key in ("evaluator_revision", "blackout_id", "physical_episode_id", "battery_epoch_id"):
        if not isinstance(value[key], str) or not value[key]:
            raise V3CodecError(f"{key} must be non-empty text")
    if (
        not isinstance(value["profile_series_id"], str)
        or _HASH_RE.fullmatch(value["profile_series_id"]) is None
    ):
        raise V3CodecError("profile series ID is invalid")
    if value["observation_origin"] not in {item.value for item in ObservationOrigin}:
        raise V3CodecError("firmware-LB summary origin is not closed")
    if value["observation_origin"] == "uat" and not isinstance(value["uat_intent_id"], str):
        raise V3CodecError("UAT scope is invalid")
    if value["observation_origin"] != "uat" and value["uat_intent_id"] is not None:
        raise V3CodecError("non-UAT scope is invalid")


def _validate_summary_counts(value: Mapping[str, Any]) -> None:
    if (
        isinstance(value["result_count"], bool)
        or not isinstance(value["result_count"], int)
        or value["result_count"] <= 0
    ):
        raise V3CodecError("firmware-LB summary result count is invalid")
    _validate_counts(
        value["disposition_counts"], _DISPOSITIONS, value["result_count"], "dispositions"
    )
    _validate_counts(value["reason_counts"], _REASONS, None, "reasons")
    if (
        isinstance(value["reason_overflow_count"], bool)
        or not isinstance(value["reason_overflow_count"], int)
        or value["reason_overflow_count"] < 0
    ):
        raise V3CodecError("firmware-LB summary reason overflow is invalid")


def _validate_summary_digests(value: Mapping[str, Any]) -> None:
    for key in (
        "ordered_results_sha256",
        "source_profile_record_hashes_sha256",
        "source_first_profile_record_hash",
        "source_last_profile_record_hash",
    ):
        if not isinstance(value[key], str) or _HASH_RE.fullmatch(value[key]) is None:
            raise V3CodecError(f"{key} is invalid")


def _validate_counts(value: Any, allowed: set[str], total: int | None, name: str) -> None:
    if not isinstance(value, dict) or set(value) != allowed:
        raise V3CodecError(f"firmware-LB summary {name} are not closed")
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in value.values()
    ):
        raise V3CodecError(f"firmware-LB summary {name} counts are invalid")
    if total is not None and sum(value.values()) != total:
        raise V3CodecError(f"firmware-LB summary {name} do not sum to result count")
