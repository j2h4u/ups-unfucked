"""Consumer-specific curve admission over independently assessable slices."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from math import isfinite

from src.domain.forward_comparison import compare_admitted_observations
from src.domain.fragment_policy import DEFAULT_DISCHARGE_FRAGMENT_POLICY
from src.domain.fragments import DischargeFragmentProfile, DischargeSlice, ObservationOrigin
from src.domain.values import (
    ComparisonMode,
    ForwardComparison,
    FrozenModelSnapshot,
    NumericSummary,
)


class CurveDisposition(StrEnum):
    ADMITTED = "admitted"
    DIAGNOSTIC_ONLY = "diagnostic_only"
    REFUSED = "refused"


class CurveReason(StrEnum):
    MISSING_RAW_SAMPLES = "missing_raw_samples"
    NON_NATURAL_ORIGIN = "non_natural_origin"
    SNAPSHOT_EPOCH_MISMATCH = "snapshot_epoch_mismatch"
    POLICY_REVISION_MISMATCH = "policy_revision_mismatch"
    INSUFFICIENT_COVERAGE = "insufficient_coverage"
    ACQUISITION_GAP = "acquisition_gap"
    NON_MONOTONIC_TIMELINE = "non_monotonic_timeline"
    INVALID_VOLTAGE = "invalid_voltage"
    VOLTAGE_SPAN_TOO_SMALL = "voltage_span_too_small"
    QUANTISATION_INSUFFICIENT = "quantisation_insufficient"
    FORWARD_COMPARISON_REFUSED = "forward_comparison_refused"


@dataclass(frozen=True, slots=True)
class CurvePolicy:
    """Versioned local curve-admission and bounded-result policy."""

    revision: str = "curve-assessment-v1"
    fragment_policy_revision: str = DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision
    evaluator_revision: str = "forward-comparison-v1"
    min_coverage_ratio: float = DEFAULT_DISCHARGE_FRAGMENT_POLICY.min_coverage_ratio
    max_gap_s: float = DEFAULT_DISCHARGE_FRAGMENT_POLICY.normal_gap_s
    min_voltage_span_v: float = DEFAULT_DISCHARGE_FRAGMENT_POLICY.min_voltage_span_v
    min_voltage_quanta: int = DEFAULT_DISCHARGE_FRAGMENT_POLICY.min_voltage_quanta
    max_reasons: int = 8
    max_raw_sample_count: int = DEFAULT_DISCHARGE_FRAGMENT_POLICY.max_physical_samples

    def __post_init__(self) -> None:
        if not self.revision or not self.fragment_policy_revision or not self.evaluator_revision:
            raise ValueError("curve policy revisions must be non-empty")
        if not 0.0 < self.min_coverage_ratio <= 1.0:
            raise ValueError("curve coverage threshold must be in (0, 1]")
        if not isfinite(self.max_gap_s) or self.max_gap_s <= 0.0:
            raise ValueError("curve gap threshold must be positive")
        if not isfinite(self.min_voltage_span_v) or self.min_voltage_span_v <= 0.0:
            raise ValueError("curve voltage-span threshold must be positive")
        if self.min_voltage_quanta <= 0 or self.max_reasons <= 0 or self.max_raw_sample_count <= 0:
            raise ValueError("curve policy bounds must be positive")


DEFAULT_CURVE_POLICY = CurvePolicy()


def _validate_assessment_identity(assessment: CurveAssessment) -> None:
    for value, name in (
        (assessment.policy_revision, "curve policy revision"),
        (assessment.evaluator_revision, "curve evaluator revision"),
        (assessment.profile_series_id, "profile series ID"),
        (assessment.slice_id, "slice ID"),
        (assessment.blackout_id, "blackout ID"),
        (assessment.physical_episode_id, "physical episode ID"),
        (assessment.battery_epoch_id, "battery epoch ID"),
        (assessment.segment_id, "segment ID"),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be non-empty")
    for value, name in (
        (assessment.profile_series_id, "profile series ID"),
        (assessment.slice_id, "slice ID"),
    ):
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"{name} must be lowercase SHA-256")


def _validate_assessment_scope(assessment: CurveAssessment) -> None:
    _validate_origin_scope(assessment)
    _validate_disposition_scope(assessment)


def _validate_origin_scope(assessment: CurveAssessment) -> None:
    if assessment.observation_origin is ObservationOrigin.UAT and not assessment.uat_intent_id:
        raise ValueError("UAT curve assessment requires intent ID")
    if assessment.observation_origin is not ObservationOrigin.UAT and assessment.uat_intent_id:
        raise ValueError("UAT intent ID is only valid for UAT")


def _validate_disposition_scope(assessment: CurveAssessment) -> None:
    if assessment.disposition is CurveDisposition.REFUSED and not assessment.reasons:
        raise ValueError("refused curve assessment requires a reason")
    natural = assessment.observation_origin is ObservationOrigin.NATURAL
    if natural and assessment.disposition is CurveDisposition.DIAGNOSTIC_ONLY:
        raise ValueError("natural curve assessment cannot be diagnostic-only")
    if not natural and assessment.disposition is CurveDisposition.ADMITTED:
        raise ValueError("non-natural curve assessment cannot be admitted")
    _validate_natural_reasons(assessment, natural)


def _validate_natural_reasons(assessment: CurveAssessment, natural: bool) -> None:
    comparison_only = all(
        item is CurveReason.FORWARD_COMPARISON_REFUSED for item in assessment.reasons
    )
    if natural and assessment.disposition is CurveDisposition.REFUSED and comparison_only:
        raise ValueError("forward comparison refusal does not refuse the curve")
    if natural and assessment.disposition is CurveDisposition.ADMITTED and not comparison_only:
        raise ValueError("admitted curve has a local refusal reason")


def _validate_assessment_counts(assessment: CurveAssessment) -> None:
    if (
        min(
            assessment.raw_sample_count,
            assessment.reason_overflow_count,
            assessment.profile_issue_overflow_count,
        )
        < 0
    ):
        raise ValueError("curve counts must be nonnegative")
    source_time = assessment.source_wall_time_utc
    if source_time is not None and (
        source_time.tzinfo is None or source_time.utcoffset() != timezone.utc.utcoffset(source_time)
    ):
        raise ValueError("curve source wall time must be UTC")
    if assessment.source_monotonic_ns < 0:
        raise ValueError("curve source monotonic time must be nonnegative")


@dataclass(frozen=True, slots=True)
class CurveAssessment:
    """A bounded answer for one slice; it never gates other consumers."""

    policy_revision: str
    evaluator_revision: str
    profile_series_id: str
    slice_id: str
    blackout_id: str
    physical_episode_id: str
    battery_epoch_id: str
    segment_id: str
    observation_origin: ObservationOrigin
    uat_intent_id: str | None
    disposition: CurveDisposition
    raw_sample_count: int
    raw_first_sample_hash: str | None
    raw_last_sample_hash: str | None
    raw_ordered_hashes_sha256: str | None
    raw_span_digests: tuple[str, ...]
    coverage_ratio: float
    max_gap_s: float
    voltage_span_v: float | None
    voltage_quantum_v: float | None
    reasons: tuple[CurveReason, ...]
    reason_overflow_count: int
    profile_issue_overflow_count: int
    first_unprofiled_raw_hash: str | None
    comparison: ForwardComparison | None
    source_boot_id: str = ""
    source_wall_time_utc: datetime | None = None
    source_monotonic_ns: int = 0

    def __post_init__(self) -> None:
        _validate_assessment_identity(self)
        _validate_assessment_scope(self)
        _validate_assessment_counts(self)


def assess_curve(
    profile: DischargeFragmentProfile,
    snapshot: FrozenModelSnapshot,
    policy: CurvePolicy = DEFAULT_CURVE_POLICY,
) -> tuple[CurveAssessment, ...]:
    """Assess every slice independently using only local raw facts."""
    if not isinstance(profile, DischargeFragmentProfile):
        raise TypeError("curve profile must be DischargeFragmentProfile")
    if not isinstance(snapshot, FrozenModelSnapshot):
        raise TypeError("curve snapshot must be FrozenModelSnapshot")
    return tuple(_assess_slice(profile, item, snapshot, policy) for item in profile.slices)


def _assess_slice(
    profile: DischargeFragmentProfile,
    item: DischargeSlice,
    snapshot: FrozenModelSnapshot,
    policy: CurvePolicy,
) -> CurveAssessment:
    samples = item.samples
    reasons: list[CurveReason] = []
    if item.origin is not ObservationOrigin.NATURAL:
        reasons.append(CurveReason.NON_NATURAL_ORIGIN)
    if item.policy_revision != policy.fragment_policy_revision:
        reasons.append(CurveReason.POLICY_REVISION_MISMATCH)
    if item.battery_epoch_id != snapshot.battery_epoch_id:
        reasons.append(CurveReason.SNAPSHOT_EPOCH_MISMATCH)
    metrics = _sample_metrics(samples, policy)
    reasons.extend(metrics.reasons)
    comparison: ForwardComparison | None = None
    natural_local = item.origin is ObservationOrigin.NATURAL and not reasons
    if natural_local:
        comparison = compare_admitted_observations(
            tuple(sample.observation for sample in samples),
            snapshot,
        )
        if comparison.mode is ComparisonMode.NONE:
            reasons.append(CurveReason.FORWARD_COMPARISON_REFUSED)
    disposition = (
        CurveDisposition.REFUSED
        if item.origin is ObservationOrigin.NATURAL
        and reasons
        and not _only_comparison_refusal(reasons)
        else CurveDisposition.DIAGNOSTIC_ONLY
        if item.origin is not ObservationOrigin.NATURAL
        else CurveDisposition.ADMITTED
    )
    bounded_reasons, overflow = _bound_reasons(reasons, policy.max_reasons)
    first, last, digest, span_digests = _raw_identity(item)
    source = item.spans[0]
    return CurveAssessment(
        policy.revision,
        policy.evaluator_revision,
        profile.series_id,
        item.slice_id,
        item.blackout_id,
        item.physical_episode_id,
        item.battery_epoch_id,
        item.segment_id,
        item.origin,
        item.uat_intent_id,
        disposition,
        len(samples),
        first,
        last,
        digest,
        span_digests,
        metrics.coverage_ratio,
        metrics.max_gap_s,
        metrics.voltage_span_v,
        metrics.voltage_quantum_v,
        bounded_reasons,
        overflow,
        profile.issue_overflow_count,
        profile.first_unprofiled_raw_hash,
        comparison,
        source.boot_id,
        source.first_wall_time_utc,
        source.first_monotonic_ns,
    )


def _only_comparison_refusal(reasons: list[CurveReason]) -> bool:
    return bool(reasons) and all(item is CurveReason.FORWARD_COMPARISON_REFUSED for item in reasons)


@dataclass(frozen=True, slots=True)
class _SampleMetrics:
    duration_s: float
    coverage_ratio: float
    max_gap_s: float
    voltage_span_v: float | None
    voltage_quantum_v: float | None
    voltage_summary: NumericSummary
    load_summary: NumericSummary
    reasons: tuple[CurveReason, ...]


def _sample_metrics(samples: tuple, policy: CurvePolicy) -> _SampleMetrics:
    if not samples:
        return _SampleMetrics(
            0.0,
            0.0,
            0.0,
            None,
            None,
            _empty_summary(),
            _empty_summary(),
            (CurveReason.MISSING_RAW_SAMPLES,),
        )
    duration, coverage, max_gap, timeline_reasons = _timeline_metrics(samples, policy)
    span, quantum, voltage_summary, load_summary, electrical_reasons = _electrical_metrics(
        samples, policy
    )
    return _SampleMetrics(
        duration,
        coverage,
        max_gap,
        span,
        quantum or None,
        voltage_summary,
        load_summary,
        (*timeline_reasons, *electrical_reasons),
    )


def _timeline_metrics(
    samples: tuple, policy: CurvePolicy
) -> tuple[float, float, float, tuple[CurveReason, ...]]:
    gaps = tuple(
        (right.observation.monotonic_ns - left.observation.monotonic_ns) / 1_000_000_000
        for left, right in zip(samples, samples[1:], strict=False)
    )
    reasons: list[CurveReason] = []
    if any(
        right.sequence != left.sequence + 1
        for left, right in zip(samples, samples[1:], strict=False)
    ):
        reasons.append(CurveReason.INSUFFICIENT_COVERAGE)
    if any(gap <= 0.0 for gap in gaps):
        reasons.append(CurveReason.NON_MONOTONIC_TIMELINE)
    max_gap = max(gaps, default=0.0)
    if max_gap > policy.max_gap_s:
        reasons.append(CurveReason.ACQUISITION_GAP)
    coverage = len(samples) / (samples[-1].sequence - samples[0].sequence + 1)
    if coverage < policy.min_coverage_ratio:
        reasons.append(CurveReason.INSUFFICIENT_COVERAGE)
    duration = (samples[-1].observation.monotonic_ns - samples[0].observation.monotonic_ns) / 1e9
    return duration, coverage, max_gap, tuple(reasons)


def _electrical_metrics(
    samples: tuple, policy: CurvePolicy
) -> tuple[float | None, float, NumericSummary, NumericSummary, tuple[CurveReason, ...]]:
    voltages = tuple(item.observation.battery_voltage_v for item in samples)
    loads = tuple(item.observation.load_percent for item in samples)
    valid_voltages = tuple(value for value in voltages if value is not None and isfinite(value))
    valid_loads = tuple(value for value in loads if value is not None and isfinite(value))
    span = max(valid_voltages) - min(valid_voltages) if valid_voltages else None
    quantum = max(
        (item.observation.voltage_token_quantum_v or 0.0 for item in samples), default=0.0
    )
    reasons: list[CurveReason] = []
    if len(valid_voltages) != len(samples) or len(valid_loads) != len(samples):
        reasons.append(CurveReason.INVALID_VOLTAGE)
    if span is None or span < policy.min_voltage_span_v:
        reasons.append(CurveReason.VOLTAGE_SPAN_TOO_SMALL)
    if quantum <= 0.0 or (span is not None and span < policy.min_voltage_quanta * quantum):
        reasons.append(CurveReason.QUANTISATION_INSUFFICIENT)
    return span, quantum, _summary(valid_voltages), _summary(valid_loads), tuple(reasons)


def _raw_identity(
    item: DischargeSlice,
) -> tuple[str | None, str | None, str | None, tuple[str, ...]]:
    if item.samples:
        hashes = tuple(sample.canonical_hash for sample in item.samples)
        return (
            hashes[0],
            hashes[-1],
            sha256("".join(hashes).encode("ascii")).hexdigest(),
            tuple(span.ordered_sample_hashes_sha256 for span in item.spans),
        )
    return None, None, None, tuple(span.ordered_sample_hashes_sha256 for span in item.spans)


def _summary(values: tuple[float, ...]) -> NumericSummary:
    if not values:
        return _empty_summary()
    mean = sum(values) / len(values)
    return NumericSummary(
        min(values),
        max(values),
        mean,
        (sum((value - mean) ** 2 for value in values) / len(values)) ** 0.5,
    )


def _empty_summary() -> NumericSummary:
    return NumericSummary(None, None, None, None)


def _bound_reasons(reasons: list[CurveReason], limit: int) -> tuple[tuple[CurveReason, ...], int]:
    ordered = tuple(dict.fromkeys(reasons))
    return ordered[:limit], max(0, len(ordered) - limit)
