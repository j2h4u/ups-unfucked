"""Consumer-specific, shadow-only assessment of a raw firmware-LB prefix."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from statistics import pstdev

from src.domain.fragments import (
    DischargeFragmentProfile,
    DischargeSlice,
    ObservationOrigin,
    ReadinessProvenance,
)
from src.domain.lifecycle import TEST_INPUT_VOLTAGE_THRESHOLD_V
from src.domain.values import PhysicalObservation

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


class FirmwareLbDisposition(StrEnum):
    COMPARABLE = "comparable"
    DIAGNOSTIC_ONLY = "diagnostic_only"
    REFUSED = "refused"


class FirmwareLbReason(StrEnum):
    MISSING_RAW_SAMPLES = "missing_raw_samples"
    NO_EVALUATION_ORIGIN = "no_evaluation_origin"
    NO_FIRMWARE_LB = "no_firmware_lb"
    NON_NATURAL_ORIGIN = "non_natural_origin"
    CALIBRATION_STATUS = "calibration_status"
    HIGH_INPUT_VOLTAGE = "high_input_voltage"
    PRE_LB_ACQUISITION_GAP = "pre_lb_acquisition_gap"
    PRE_LB_REBOOT = "pre_lb_reboot"
    INVALID_LOAD = "invalid_load"
    INVALID_VOLTAGE = "invalid_voltage"
    POLICY_REVISION_MISMATCH = "policy_revision_mismatch"
    READINESS_UNAVAILABLE = "readiness_unavailable"
    START_NOT_READY = "start_not_ready"


@dataclass(frozen=True, slots=True)
class FirmwareLbPolicy:
    """Versioned bounded policy for a raw-LB shadow prefix."""

    revision: str = "firmware-lb-assessment-v1"
    fragment_policy_revision: str = "discharge-fragments-v1"
    evaluator_revision: str = "firmware-lb-prefix-v1"
    evaluation_origin_delay_s: float = 60.0
    evaluation_origin_window_points: int = 31
    stable_max_gap_s: float = 2.5
    stable_load_stddev_pp: float = 2.0
    max_gap_s: float = 5.0
    high_input_voltage_v: float = TEST_INPUT_VOLTAGE_THRESHOLD_V
    max_reasons: int = 8
    max_raw_sample_count: int = 3170

    def __post_init__(self) -> None:
        if not self.revision or not self.fragment_policy_revision or not self.evaluator_revision:
            raise ValueError("firmware-LB policy revisions must be non-empty")
        values = (
            self.evaluation_origin_delay_s,
            self.stable_max_gap_s,
            self.stable_load_stddev_pp,
            self.max_gap_s,
            self.high_input_voltage_v,
        )
        if any(not isfinite(value) or value <= 0.0 for value in values):
            raise ValueError("firmware-LB policy thresholds must be positive and finite")
        if self.evaluation_origin_window_points < 2:
            raise ValueError("firmware-LB origin window must contain at least two points")
        if self.max_reasons <= 0 or self.max_raw_sample_count <= 0:
            raise ValueError("firmware-LB policy bounds must be positive")


DEFAULT_FIRMWARE_LB_POLICY = FirmwareLbPolicy()


@dataclass(frozen=True, slots=True)
class FirmwareLbAssessment:
    """A bounded raw-LB observation; it cannot authorize shutdown or writes."""

    policy_revision: str
    evaluator_revision: str
    profile_series_id: str
    slice_ids: tuple[str, ...]
    blackout_id: str
    physical_episode_id: str
    battery_epoch_id: str
    segment_id: str
    observation_origin: ObservationOrigin
    readiness: bool | None
    readiness_reason: str | None
    readiness_provenance: ReadinessProvenance | None
    disposition: FirmwareLbDisposition
    comparable: bool
    evaluation_origin_sequence: int | None
    evaluation_origin_sample_hash: str | None
    lb_sequence: int | None
    lb_sample_hash: str | None
    lb_raw_status: str | None
    raw_sample_count: int
    raw_first_sample_hash: str | None
    raw_last_sample_hash: str | None
    raw_ordered_hashes_sha256: str | None
    raw_span_digests: tuple[str, ...]
    coverage_ratio: float
    max_gap_s: float
    reasons: tuple[FirmwareLbReason, ...]
    reason_overflow_count: int
    profile_issue_overflow_count: int
    first_unprofiled_raw_hash: str | None
    source_boot_id: str
    source_wall_time_utc: datetime
    source_monotonic_ns: int
    shadow_only: bool = True

    def __post_init__(self) -> None:
        _validate_assessment_identity(self)
        _validate_assessment_semantics(self)
        _validate_assessment_raw(self)


def assess_firmware_lb(
    profile: DischargeFragmentProfile,
    policy: FirmwareLbPolicy = DEFAULT_FIRMWARE_LB_POLICY,
) -> FirmwareLbAssessment:
    """Assess one profile through the first post-origin raw firmware LB."""
    if not isinstance(profile, DischargeFragmentProfile):
        raise TypeError("firmware-LB profile must be DischargeFragmentProfile")
    if not isinstance(policy, FirmwareLbPolicy):
        raise TypeError("firmware-LB policy must be FirmwareLbPolicy")
    points = _flatten_points(profile)
    first_slice = profile.slices[0] if profile.slices else None
    scope = _scope(first_slice)
    reasons: list[FirmwareLbReason] = []
    if profile.policy_revision != policy.fragment_policy_revision:
        reasons.append(FirmwareLbReason.POLICY_REVISION_MISMATCH)
    if not points:
        reasons.append(FirmwareLbReason.MISSING_RAW_SAMPLES)
        return _build_assessment(_AssessmentContext(profile, scope, None, None, reasons, policy))
    origin_index = _evaluation_origin_index(points, policy)
    if origin_index is None:
        reasons.append(FirmwareLbReason.NO_EVALUATION_ORIGIN)
        return _build_assessment(_AssessmentContext(profile, scope, points, None, reasons, policy))
    lb_index = _first_lb_after_origin(points, origin_index)
    if lb_index is None:
        reasons.append(FirmwareLbReason.NO_FIRMWARE_LB)
        return _build_assessment(
            _AssessmentContext(profile, scope, points, None, reasons, policy, origin_index)
        )
    prefix = points[: lb_index + 1]
    reasons.extend(_prefix_reasons(prefix, profile, policy))
    readiness, readiness_reason, readiness_provenance = _readiness(first_slice)
    if not reasons and readiness is not True:
        reasons.append(
            FirmwareLbReason.START_NOT_READY
            if readiness is False
            else FirmwareLbReason.READINESS_UNAVAILABLE
        )
    disposition = (
        FirmwareLbDisposition.REFUSED
        if _science_refused(reasons, readiness)
        else FirmwareLbDisposition.DIAGNOSTIC_ONLY
        if readiness is not True
        else FirmwareLbDisposition.COMPARABLE
    )
    return _build_assessment(
        _AssessmentContext(
            profile,
            scope,
            points,
            prefix,
            reasons,
            policy,
            origin_index,
            readiness,
            readiness_reason,
            readiness_provenance,
            disposition,
        )
    )


@dataclass(frozen=True, slots=True)
class _Point:
    sample_hash: str
    sequence: int
    observation: PhysicalObservation
    slice_id: str


def _flatten_points(profile: DischargeFragmentProfile) -> tuple[_Point, ...]:
    return tuple(
        _Point(sample.canonical_hash, sample.sequence, sample.observation, item.slice_id)
        for item in profile.slices
        for sample in item.samples
    )


def _scope(
    item: DischargeSlice | None,
) -> tuple[str, str, str, str, ObservationOrigin, tuple[str, ...]]:
    if item is None:
        return "", "", "", "", ObservationOrigin.NATURAL, ()
    return (
        item.blackout_id,
        item.physical_episode_id,
        item.battery_epoch_id,
        item.segment_id,
        item.origin,
        (),
    )


def _readiness(item: object) -> tuple[bool | None, str | None, ReadinessProvenance | None]:
    context = getattr(item, "readiness_context", None)
    if context is None:
        return None, None, None
    return context.ready, context.reason, context.provenance


def _evaluation_origin_index(points: tuple[_Point, ...], policy: FirmwareLbPolicy) -> int | None:
    window_size = policy.evaluation_origin_window_points
    if len(points) < window_size:
        return None
    start_ns = points[0].observation.monotonic_ns
    for index in range(len(points) - window_size + 1):
        elapsed = (points[index].observation.monotonic_ns - start_ns) / 1_000_000_000
        if elapsed < policy.evaluation_origin_delay_s:
            continue
        window = points[index : index + window_size]
        if _stable_window(window, policy):
            return index + window_size // 2
    return None


def _stable_window(points: tuple[_Point, ...], policy: FirmwareLbPolicy) -> bool:
    if not points or len({point.observation.boot_id for point in points}) != 1:
        return False
    loads = tuple(
        value
        for point in points
        if (value := point.observation.load_percent) is not None and isfinite(value)
    )
    voltages = tuple(
        value
        for point in points
        if (value := point.observation.battery_voltage_v) is not None and isfinite(value)
    )
    if len(loads) != len(points) or len(voltages) != len(points):
        return False
    gaps = _gaps(points)
    return (
        bool(gaps)
        and max(gaps) <= policy.stable_max_gap_s
        and pstdev(loads) <= policy.stable_load_stddev_pp
    )


def _first_lb_after_origin(points: tuple[_Point, ...], origin_index: int) -> int | None:
    return next(
        (
            index
            for index in range(origin_index, len(points))
            if "LB" in points[index].observation.raw_status.split()
        ),
        None,
    )


def _prefix_reasons(
    prefix: tuple[_Point, ...], profile: DischargeFragmentProfile, policy: FirmwareLbPolicy
) -> tuple[FirmwareLbReason, ...]:
    reasons = (
        [FirmwareLbReason.NON_NATURAL_ORIGIN]
        if profile.slices and profile.slices[0].origin is not ObservationOrigin.NATURAL
        else []
    )
    reasons.extend(reason for point in prefix for reason in _point_reasons(point, policy))
    reasons.extend(_edge_reasons(prefix, policy))
    return tuple(dict.fromkeys(reasons))


def _point_reasons(point: _Point, policy: FirmwareLbPolicy) -> tuple[FirmwareLbReason, ...]:
    observation = point.observation
    flags = set(observation.raw_status.split())
    reasons: list[FirmwareLbReason] = []
    if flags.intersection({"CAL", "SELFTEST", "TEST"}):
        reasons.append(FirmwareLbReason.CALIBRATION_STATUS)
    if "OB" not in flags:
        reasons.append(FirmwareLbReason.NON_NATURAL_ORIGIN)
    if (
        observation.input_voltage_v is not None
        and observation.input_voltage_v >= policy.high_input_voltage_v
    ):
        reasons.append(FirmwareLbReason.HIGH_INPUT_VOLTAGE)
    if observation.load_percent is None or not isfinite(observation.load_percent):
        reasons.append(FirmwareLbReason.INVALID_LOAD)
    if observation.battery_voltage_v is None or not isfinite(observation.battery_voltage_v):
        reasons.append(FirmwareLbReason.INVALID_VOLTAGE)
    return tuple(reasons)


def _edge_reasons(
    prefix: tuple[_Point, ...], policy: FirmwareLbPolicy
) -> tuple[FirmwareLbReason, ...]:
    if not prefix:
        return ()
    first_boot = prefix[0].observation.boot_id
    reasons: list[FirmwareLbReason] = []
    for left, right in zip(prefix, prefix[1:], strict=False):
        delta_s = (right.observation.monotonic_ns - left.observation.monotonic_ns) / 1_000_000_000
        if right.observation.boot_id != first_boot or delta_s <= 0.0:
            reasons.append(FirmwareLbReason.PRE_LB_REBOOT)
        if right.sequence != left.sequence + 1 or delta_s > policy.max_gap_s:
            reasons.append(FirmwareLbReason.PRE_LB_ACQUISITION_GAP)
    return tuple(reasons)


def _science_refused(reasons: list[FirmwareLbReason], readiness: bool | None) -> bool:
    return bool(reasons) and not (
        readiness is not True
        and set(reasons).issubset(
            {FirmwareLbReason.START_NOT_READY, FirmwareLbReason.READINESS_UNAVAILABLE}
        )
    )


@dataclass(frozen=True, slots=True)
class _AssessmentContext:
    profile: DischargeFragmentProfile
    scope: tuple[str, str, str, str, ObservationOrigin, tuple[str, ...]]
    points: tuple[_Point, ...] | None
    prefix: tuple[_Point, ...] | None
    reasons: list[FirmwareLbReason]
    policy: FirmwareLbPolicy
    origin_index: int | None = None
    readiness: bool | None = None
    readiness_reason: str | None = None
    readiness_provenance: ReadinessProvenance | None = None
    disposition: FirmwareLbDisposition = FirmwareLbDisposition.REFUSED


@dataclass(frozen=True, slots=True)
class _PrefixDetails:
    origin_hash: str | None
    lb: _Point | None
    first_hash: str | None
    last_hash: str | None
    raw_digest: str | None
    coverage: float
    max_gap: float
    source_boot_id: str
    source_wall_time_utc: datetime
    source_monotonic_ns: int


def _prefix_details(
    selected: tuple[_Point, ...],
    all_points: tuple[_Point, ...],
    origin_index: int | None,
) -> _PrefixDetails:
    hashes = tuple(point.sample_hash for point in selected)
    source = selected[0].observation if selected else None
    return _PrefixDetails(
        None if origin_index is None or not all_points else all_points[origin_index].sample_hash,
        selected[-1] if selected else None,
        hashes[0] if hashes else None,
        hashes[-1] if hashes else None,
        sha256("".join(hashes).encode("ascii")).hexdigest() if hashes else None,
        *_coverage(selected),
        source.boot_id if source else "derived",
        source.wall_time_utc if source else datetime(1970, 1, 1, tzinfo=timezone.utc),
        source.monotonic_ns if source else 0,
    )


def _build_assessment(context: _AssessmentContext) -> FirmwareLbAssessment:
    selected = () if context.prefix is None else context.prefix
    all_points = () if context.points is None else context.points
    bounded, overflow = _bound_reasons(context.reasons, context.policy.max_reasons)
    details = _prefix_details(selected, all_points, context.origin_index)
    blackout_id, episode_id, epoch_id, segment_id, origin, _ = context.scope
    slice_ids = tuple(dict.fromkeys(point.slice_id for point in selected)) or tuple(
        item.slice_id for item in context.profile.slices
    )
    return FirmwareLbAssessment(
        context.policy.revision,
        context.policy.evaluator_revision,
        context.profile.series_id,
        slice_ids,
        blackout_id,
        episode_id,
        epoch_id,
        segment_id,
        origin,
        context.readiness,
        context.readiness_reason,
        context.readiness_provenance,
        context.disposition,
        context.disposition is FirmwareLbDisposition.COMPARABLE,
        context.origin_index,
        details.origin_hash,
        None if details.lb is None else details.lb.sequence,
        None if details.lb is None else details.lb.sample_hash,
        None if details.lb is None else details.lb.observation.raw_status,
        len(selected),
        details.first_hash,
        details.last_hash,
        details.raw_digest,
        tuple(
            span.ordered_sample_hashes_sha256
            for item in context.profile.slices
            for span in item.spans
        ),
        details.coverage,
        details.max_gap,
        bounded,
        overflow,
        context.profile.issue_overflow_count,
        context.profile.first_unprofiled_raw_hash,
        details.source_boot_id,
        details.source_wall_time_utc,
        details.source_monotonic_ns,
    )


def _coverage(points: tuple[_Point, ...]) -> tuple[float, float]:
    if not points:
        return 0.0, 0.0
    gaps = _gaps(points)
    expected = points[-1].sequence - points[0].sequence + 1
    return len(points) / expected if expected > 0 else 0.0, max(gaps, default=0.0)


def _gaps(points: tuple[_Point, ...]) -> tuple[float, ...]:
    return tuple(
        (right.observation.monotonic_ns - left.observation.monotonic_ns) / 1_000_000_000
        for left, right in zip(points, points[1:], strict=False)
    )


def _bound_reasons(
    reasons: list[FirmwareLbReason], limit: int
) -> tuple[tuple[FirmwareLbReason, ...], int]:
    ordered = tuple(dict.fromkeys(reasons))
    return ordered[:limit], max(0, len(ordered) - limit)


def _validate_assessment_identity(value: FirmwareLbAssessment) -> None:
    for item, name in (
        (value.policy_revision, "firmware-LB policy revision"),
        (value.evaluator_revision, "firmware-LB evaluator revision"),
        (value.profile_series_id, "firmware-LB profile series ID"),
        (value.blackout_id, "firmware-LB blackout ID"),
        (value.physical_episode_id, "firmware-LB physical episode ID"),
        (value.battery_epoch_id, "firmware-LB battery epoch ID"),
        (value.segment_id, "firmware-LB segment ID"),
    ):
        if not isinstance(item, str) or not item:
            raise ValueError(f"{name} must be non-empty")
    if _HASH_RE.fullmatch(value.profile_series_id) is None:
        raise ValueError("firmware-LB profile series ID must be lowercase SHA-256")
    if any(_HASH_RE.fullmatch(item) is None for item in value.slice_ids):
        raise ValueError("firmware-LB slice IDs must be lowercase SHA-256")


def _validate_assessment_semantics(value: FirmwareLbAssessment) -> None:
    if not value.shadow_only:
        raise ValueError("firmware-LB assessment must remain shadow-only")
    if value.comparable != (value.disposition is FirmwareLbDisposition.COMPARABLE):
        raise ValueError("firmware-LB comparable flag does not match disposition")
    if value.disposition is FirmwareLbDisposition.REFUSED and not value.reasons:
        raise ValueError("refused firmware-LB assessment requires a reason")
    if value.readiness_provenance is not None and value.readiness is None:
        raise ValueError("readiness provenance requires a readiness fact")


def _validate_assessment_raw(value: FirmwareLbAssessment) -> None:
    if (
        min(value.raw_sample_count, value.reason_overflow_count, value.profile_issue_overflow_count)
        < 0
    ):
        raise ValueError("firmware-LB counts must be nonnegative")
    for item, name in (
        (value.raw_first_sample_hash, "firmware-LB first raw hash"),
        (value.raw_last_sample_hash, "firmware-LB last raw hash"),
        (value.raw_ordered_hashes_sha256, "firmware-LB ordered raw hash"),
        (value.lb_sample_hash, "firmware-LB marker hash"),
        (value.evaluation_origin_sample_hash, "firmware-LB origin hash"),
    ):
        if item is not None and _HASH_RE.fullmatch(item) is None:
            raise ValueError(f"{name} must be lowercase SHA-256")
    if not value.source_boot_id:
        raise ValueError("firmware-LB source boot ID must be non-empty")
    if (
        value.source_wall_time_utc.tzinfo is None
        or value.source_wall_time_utc.utcoffset()
        != timezone.utc.utcoffset(value.source_wall_time_utc)
    ):
        raise ValueError("firmware-LB source wall time must be UTC")
    if value.source_monotonic_ns < 0:
        raise ValueError("firmware-LB source monotonic time must be nonnegative")
    if value.lb_sequence is None and value.lb_sample_hash is not None:
        raise ValueError("firmware-LB marker hash requires marker sequence")
