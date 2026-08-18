"""Consumer-specific, raw-linked load-sag assessment.

This module deliberately evaluates only the existing ``LoadStepObservation``
objects.  It does not re-estimate a step, select a learning cohort, write a
model, or introduce a generic evidence/assessment dispatch mechanism.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from typing import Any

from src.domain.fragments import (
    AnchorKind,
    DischargeFragmentProfile,
    LoadStepObservation,
    ObservationOrigin,
)
from src.domain.values import StepQuality

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_STEP_REFS = 128


class LoadSagDisposition(StrEnum):
    """The load-sag consumer's bounded outcome."""

    ADMITTED = "admitted"
    DIAGNOSTIC = "diagnostic"
    REFUSED = "refused"


class LoadSagReason(StrEnum):
    """Closed per-step and assessment-level refusal reasons."""

    NON_NATURAL_ORIGIN = "non_natural_origin"
    ORIGIN_MISMATCH = "origin_mismatch"
    BATTERY_EPOCH_MISMATCH = "battery_epoch_mismatch"
    POLICY_REVISION_MISMATCH = "policy_revision_mismatch"
    EVALUATOR_REVISION_MISMATCH = "evaluator_revision_mismatch"
    SOURCE_PROFILE_HASH_MISMATCH = "source_profile_hash_mismatch"
    SOURCE_SLICE_HASH_MISMATCH = "source_slice_hash_mismatch"
    RAW_HASH_MISMATCH = "raw_hash_mismatch"
    STEP_NOT_QUALIFYING = "step_not_qualifying"
    DAMAGE_OVERLAPS_STEP = "damage_overlaps_step"
    MISSING_SOURCE_STEP = "missing_source_step"
    ASSESSMENT_BUDGET_EXHAUSTED = "assessment_budget_exhausted"


@dataclass(frozen=True, slots=True)
class LoadSagPolicy:
    """Concrete v1 consumer policy matching the current step identifier."""

    revision: str = "load-sag-v1"
    evaluator_revision: str = "ir-load-step-v1"
    min_step_delta_pp: float = 15.0
    min_initial_movement_pp: float = 10.0
    max_settling_disagreement: float = 0.15
    max_load_stddev_pp: float = 2.0
    max_slope_v_per_s: float = 0.002
    max_gap_s: float = 2.5
    max_step_references: int = _MAX_STEP_REFS

    def __post_init__(self) -> None:
        for value, name in (
            (self.revision, "load-sag policy revision"),
            (self.evaluator_revision, "load-sag evaluator revision"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        for value, name in (
            (self.min_step_delta_pp, "minimum step delta"),
            (self.min_initial_movement_pp, "minimum initial movement"),
            (self.max_settling_disagreement, "maximum settling disagreement"),
            (self.max_load_stddev_pp, "maximum load standard deviation"),
            (self.max_slope_v_per_s, "maximum slope"),
            (self.max_gap_s, "maximum gap"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
            ):
                raise ValueError(f"{name} must be finite")
        if (
            isinstance(self.max_step_references, bool)
            or not isinstance(self.max_step_references, int)
            or self.max_step_references <= 0
        ):
            raise ValueError("maximum step references must be positive")
        if self.max_gap_s <= 0.0 or self.max_slope_v_per_s <= 0.0:
            raise ValueError("load-sag timing and slope limits must be positive")


DEFAULT_LOAD_SAG_POLICY = LoadSagPolicy()
SUPPORTED_LOAD_SAG_POLICY_REVISIONS = frozenset({DEFAULT_LOAD_SAG_POLICY.revision})


def resolve_load_sag_policy(revision: str) -> LoadSagPolicy:
    """Resolve one exact consumer policy revision."""
    if revision == DEFAULT_LOAD_SAG_POLICY.revision:
        return DEFAULT_LOAD_SAG_POLICY
    raise ValueError(f"unknown load-sag policy revision: {revision}")


@dataclass(frozen=True, slots=True)
class LoadSagAssessmentContext:
    """Raw scope and evaluator identity supplied by the assessment caller."""

    battery_epoch_id: str
    policy_revision: str
    evaluator_revision: str
    origin: ObservationOrigin
    fragment_policy_revision: str
    expected_profile_hash: str | None = None
    expected_slice_hashes: tuple[str, ...] | None = None
    expected_step_hashes: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        _require_id(self.battery_epoch_id, "battery epoch ID")
        _require_id(self.policy_revision, "policy revision")
        _require_id(self.evaluator_revision, "evaluator revision")
        _require_id(self.fragment_policy_revision, "fragment policy revision")
        if not isinstance(self.origin, ObservationOrigin):
            try:
                object.__setattr__(self, "origin", ObservationOrigin(self.origin))
            except (TypeError, ValueError) as exc:
                raise ValueError("origin must be a supported ObservationOrigin") from exc
        _validate_optional_hash(self.expected_profile_hash, "expected profile hash")
        _validate_optional_hashes(self.expected_slice_hashes, "expected slice hashes")
        _validate_optional_hashes(self.expected_step_hashes, "expected step hashes")


@dataclass(frozen=True, slots=True)
class LoadSagStepRefusal:
    """A bounded refusal retaining the exact step and raw contributors."""

    step_record_hash: str
    parent_slice_hash: str
    contributing_sample_hashes: tuple[str, ...]
    reason: LoadSagReason

    def __post_init__(self) -> None:
        _require_hash(self.step_record_hash, "step record hash")
        _require_hash(self.parent_slice_hash, "parent slice hash")
        _validate_hashes(self.contributing_sample_hashes, "contributing sample hashes")
        if not isinstance(self.reason, LoadSagReason):
            try:
                object.__setattr__(self, "reason", LoadSagReason(self.reason))
            except (TypeError, ValueError) as exc:
                raise ValueError("unknown load-sag refusal reason") from exc


@dataclass(frozen=True, slots=True)
class LoadSagAssessment:
    """Immutable consumer result with exact source identities and bounded refs."""

    policy_revision: str
    fragment_policy_revision: str
    evaluator_revision: str
    battery_epoch_id: str
    origin: ObservationOrigin
    disposition: LoadSagDisposition
    source_profile_hash: str
    source_blackout_id: str
    source_physical_episode_id: str
    source_segment_id: str
    source_boot_id: str
    source_first_wall_time_utc: datetime
    source_first_monotonic_ns: int
    source_slice_hashes: tuple[str, ...]
    source_step_hashes: tuple[str, ...]
    admitted_steps: tuple[LoadStepObservation, ...]
    refusals: tuple[LoadSagStepRefusal, ...]
    step_count: int
    step_overflow_count: int = 0
    refusal_overflow_count: int = 0
    first_unprofiled_step_hash: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.policy_revision, "assessment policy revision")
        _require_id(self.fragment_policy_revision, "assessment fragment policy revision")
        _require_id(self.evaluator_revision, "assessment evaluator revision")
        _require_id(self.battery_epoch_id, "assessment battery epoch ID")
        _require_id(self.source_blackout_id, "source blackout ID")
        _require_id(self.source_physical_episode_id, "source physical episode ID")
        _require_id(self.source_segment_id, "source segment ID")
        _require_id(self.source_boot_id, "source boot ID")
        _require_hash(self.source_profile_hash, "source profile hash")
        _validate_wall_time(self.source_first_wall_time_utc)
        _validate_nonnegative_int(self.source_first_monotonic_ns, "source first monotonic time")
        _validate_hashes(self.source_slice_hashes, "source slice hashes")
        _validate_hashes(self.source_step_hashes, "source step hashes")
        if not isinstance(self.origin, ObservationOrigin):
            raise TypeError("assessment origin must be ObservationOrigin")
        if not isinstance(self.disposition, LoadSagDisposition):
            raise TypeError("assessment disposition must be LoadSagDisposition")
        if not isinstance(self.admitted_steps, tuple) or any(
            not isinstance(step, LoadStepObservation) for step in self.admitted_steps
        ):
            raise TypeError("admitted steps must be a tuple of LoadStepObservation")
        if not isinstance(self.refusals, tuple) or any(
            not isinstance(item, LoadSagStepRefusal) for item in self.refusals
        ):
            raise TypeError("refusals must be a tuple of LoadSagStepRefusal")
        _validate_nonnegative_int(self.step_count, "assessment step count")
        _validate_nonnegative_int(self.step_overflow_count, "step overflow count")
        _validate_nonnegative_int(self.refusal_overflow_count, "refusal overflow count")
        if (
            self.step_count
            != len(self.admitted_steps) + len(self.refusals) + self.step_overflow_count
        ):
            raise ValueError("step count does not match retained steps and overflow")
        if self.first_unprofiled_step_hash is not None:
            _require_hash(self.first_unprofiled_step_hash, "first unprofiled step hash")
        if self.step_overflow_count and self.first_unprofiled_step_hash is None:
            raise ValueError("step overflow requires first unprofiled step hash")
        if not self.step_overflow_count and self.first_unprofiled_step_hash is not None:
            raise ValueError("first unprofiled step hash requires step overflow")
        _validate_assessment_disposition(self)


def assess_load_sag(
    profile: DischargeFragmentProfile,
    context: LoadSagAssessmentContext,
    policy: LoadSagPolicy,
) -> LoadSagAssessment:
    """Assess each raw-linked step independently for the load-sag consumer."""
    if not isinstance(profile, DischargeFragmentProfile):
        raise TypeError("profile must be DischargeFragmentProfile")
    if not isinstance(context, LoadSagAssessmentContext):
        raise TypeError("context must be LoadSagAssessmentContext")
    if not isinstance(policy, LoadSagPolicy):
        raise TypeError("policy must be LoadSagPolicy")
    if policy != resolve_load_sag_policy(policy.revision):
        raise ValueError("load-sag policy values do not match its revision")
    source = _source_metadata(profile)
    source_steps = profile.load_steps
    step_limit = policy.max_step_references
    retained_steps = source_steps[:step_limit]
    overflow_count = max(0, len(source_steps) - step_limit)
    first_overflow = source_steps[step_limit].step_record_hash if overflow_count else None
    global_reason = _context_reason(profile, context, policy, source)
    admitted: list[LoadStepObservation] = []
    refusals: list[LoadSagStepRefusal] = []
    for step in retained_steps:
        reason = global_reason or _step_reason(step, profile, context)
        if reason is None:
            admitted.append(step)
        else:
            refusals.append(_refusal(step, reason))
    disposition = _disposition(context.origin, tuple(admitted), tuple(refusals), global_reason)
    return LoadSagAssessment(
        policy_revision=context.policy_revision,
        fragment_policy_revision=context.fragment_policy_revision,
        evaluator_revision=context.evaluator_revision,
        battery_epoch_id=context.battery_epoch_id,
        origin=context.origin,
        disposition=disposition,
        source_profile_hash=source[0],
        source_blackout_id=source[1],
        source_physical_episode_id=source[2],
        source_segment_id=source[3],
        source_boot_id=source[4],
        source_first_wall_time_utc=source[5],
        source_first_monotonic_ns=source[6],
        source_slice_hashes=tuple(item.slice_id for item in profile.slices),
        source_step_hashes=tuple(step.step_record_hash for step in source_steps),
        admitted_steps=tuple(admitted),
        refusals=tuple(refusals),
        step_count=len(source_steps),
        step_overflow_count=overflow_count,
        refusal_overflow_count=max(0, len(refusals) - policy.max_step_references),
        first_unprofiled_step_hash=first_overflow,
    )


def assessment_payload(assessment: LoadSagAssessment) -> dict[str, Any]:
    """Return the exact bounded payload accepted by the dedicated codec."""
    if not isinstance(assessment, LoadSagAssessment):
        raise TypeError("assessment must be LoadSagAssessment")
    return {
        "assessment_schema": "load-sag-assessment-v1",
        "policy_revision": assessment.policy_revision,
        "fragment_policy_revision": assessment.fragment_policy_revision,
        "evaluator_revision": assessment.evaluator_revision,
        "battery_epoch_id": assessment.battery_epoch_id,
        "observation_origin": assessment.origin.value,
        "disposition": assessment.disposition.value,
        "source_profile_hash": assessment.source_profile_hash,
        "source_blackout_id": assessment.source_blackout_id,
        "source_physical_episode_id": assessment.source_physical_episode_id,
        "source_segment_id": assessment.source_segment_id,
        "source_boot_id": assessment.source_boot_id,
        "source_first_wall_time_utc": _canonical_wall_time(assessment.source_first_wall_time_utc),
        "source_first_monotonic_ns": assessment.source_first_monotonic_ns,
        "source_slice_hashes": list(assessment.source_slice_hashes),
        "source_step_hashes": list(assessment.source_step_hashes),
        "admitted_steps": [_step_ref(step) for step in assessment.admitted_steps],
        "refusals": [
            [
                item.step_record_hash,
                item.parent_slice_hash,
                list(item.contributing_sample_hashes),
                item.reason.value,
            ]
            for item in assessment.refusals
        ],
        "step_count": assessment.step_count,
        "step_overflow_count": assessment.step_overflow_count,
        "refusal_overflow_count": assessment.refusal_overflow_count,
        "first_unprofiled_step_hash": assessment.first_unprofiled_step_hash,
    }


def assessment_from_payload(
    profile: DischargeFragmentProfile,
    payload: dict[str, Any],
    *,
    policy: LoadSagPolicy,
) -> LoadSagAssessment:
    """Reconstruct an assessment by resolving every step reference in ``profile``."""
    if not isinstance(payload, dict):
        raise ValueError("load-sag payload must be an object")
    if not isinstance(policy, LoadSagPolicy):
        raise TypeError("policy must be LoadSagPolicy")
    resolved_policy = resolve_load_sag_policy(policy.revision)
    if policy != resolved_policy:
        raise ValueError("load-sag policy values do not match its revision")
    _validate_payload_shape(payload)
    source = _source_metadata(profile)
    _validate_payload_source(payload, profile, source, resolved_policy)
    profile_steps = {step.step_record_hash: step for step in profile.load_steps}
    admitted = tuple(
        _resolve_step_ref(item, profile_steps, profile) for item in payload["admitted_steps"]
    )
    refusals = tuple(_resolve_refusal(item, profile_steps, profile) for item in payload["refusals"])
    return LoadSagAssessment(
        policy_revision=payload["policy_revision"],
        fragment_policy_revision=payload["fragment_policy_revision"],
        evaluator_revision=payload["evaluator_revision"],
        battery_epoch_id=payload["battery_epoch_id"],
        origin=ObservationOrigin(payload["observation_origin"]),
        disposition=LoadSagDisposition(payload["disposition"]),
        source_profile_hash=payload["source_profile_hash"],
        source_blackout_id=source[1],
        source_physical_episode_id=source[2],
        source_segment_id=source[3],
        source_boot_id=source[4],
        source_first_wall_time_utc=source[5],
        source_first_monotonic_ns=source[6],
        source_slice_hashes=tuple(payload["source_slice_hashes"]),
        source_step_hashes=tuple(payload["source_step_hashes"]),
        admitted_steps=admitted,
        refusals=refusals,
        step_count=payload["step_count"],
        step_overflow_count=payload["step_overflow_count"],
        refusal_overflow_count=payload["refusal_overflow_count"],
        first_unprofiled_step_hash=payload["first_unprofiled_step_hash"],
    )


def _validate_payload_shape(payload: dict[str, Any]) -> None:
    if set(payload) != set(assessment_payload_fields()):
        raise ValueError("load-sag payload fields are not exact")
    _validate_payload_primitives(payload)


def _validate_payload_source(
    payload: dict[str, Any],
    profile: DischargeFragmentProfile,
    source: tuple[str, str, str, str, str, datetime, int],
    policy: LoadSagPolicy,
) -> None:
    expected_steps = tuple(step.step_record_hash for step in profile.load_steps)
    _validate_payload_identity(payload, profile, source, expected_steps)
    _validate_payload_policy(payload, profile, policy)
    _validate_payload_context(payload, profile, source, expected_steps)


def _validate_payload_identity(
    payload: dict[str, Any],
    profile: DischargeFragmentProfile,
    source: tuple[str, str, str, str, str, datetime, int],
    expected_steps: tuple[str, ...],
) -> None:
    if payload["source_profile_hash"] != source[0]:
        raise ValueError("source profile hash does not match supplied profile")
    if tuple(payload["source_slice_hashes"]) != tuple(item.slice_id for item in profile.slices):
        raise ValueError("source slice hashes do not match supplied profile")
    if tuple(payload["source_step_hashes"]) != expected_steps:
        raise ValueError("source step hashes do not match supplied profile")


def _validate_payload_policy(
    payload: dict[str, Any], profile: DischargeFragmentProfile, policy: LoadSagPolicy
) -> None:
    if payload["policy_revision"] != policy.revision:
        raise ValueError("payload policy revision is unsupported")
    if payload["fragment_policy_revision"] != profile.policy_revision:
        raise ValueError("payload fragment policy revision does not match profile")
    if payload["evaluator_revision"] != policy.evaluator_revision:
        raise ValueError("payload evaluator revision does not match policy")


def _validate_payload_context(
    payload: dict[str, Any],
    profile: DischargeFragmentProfile,
    source: tuple[str, str, str, str, str, datetime, int],
    expected_steps: tuple[str, ...],
) -> None:
    actual_source = (
        payload["source_blackout_id"],
        payload["source_physical_episode_id"],
        payload["source_segment_id"],
        payload["source_boot_id"],
        payload["source_first_monotonic_ns"],
    )
    if actual_source != (source[1], source[2], source[3], source[4], source[6]):
        raise ValueError("payload source scope does not match supplied profile")
    if payload["source_first_wall_time_utc"] != _canonical_wall_time(source[5]):
        raise ValueError("payload source wall time does not match supplied profile")
    if payload["battery_epoch_id"] != profile.slices[0].battery_epoch_id:
        raise ValueError("payload battery epoch does not match supplied profile")
    if payload["observation_origin"] != profile.slices[0].origin.value:
        raise ValueError("payload origin does not match supplied profile")
    if (
        payload["first_unprofiled_step_hash"] is not None
        and payload["first_unprofiled_step_hash"] not in expected_steps
    ):
        raise ValueError("first unprofiled step hash is absent from source profile")


def source_profile_hash(profile: DischargeFragmentProfile) -> str:
    """Derive the stable source identity used by the assessment and codec."""
    if not isinstance(profile, DischargeFragmentProfile):
        raise TypeError("profile must be DischargeFragmentProfile")
    fields = (
        profile.policy_revision,
        profile.series_id,
        *(anchor.canonical_hash for anchor in profile.anchors),
        *(item.slice_id for item in profile.slices),
        *(step.step_record_hash for step in profile.load_steps),
    )
    return sha256("|".join(fields).encode("utf-8")).hexdigest()


def assessment_payload_fields() -> tuple[str, ...]:
    return (
        "assessment_schema",
        "policy_revision",
        "fragment_policy_revision",
        "evaluator_revision",
        "battery_epoch_id",
        "observation_origin",
        "disposition",
        "source_profile_hash",
        "source_blackout_id",
        "source_physical_episode_id",
        "source_segment_id",
        "source_boot_id",
        "source_first_wall_time_utc",
        "source_first_monotonic_ns",
        "source_slice_hashes",
        "source_step_hashes",
        "admitted_steps",
        "refusals",
        "step_count",
        "step_overflow_count",
        "refusal_overflow_count",
        "first_unprofiled_step_hash",
    )


def _source_metadata(
    profile: DischargeFragmentProfile,
) -> tuple[str, str, str, str, str, datetime, int]:
    if not profile.slices:
        raise ValueError("load-sag assessment requires at least one source slice")
    first = profile.slices[0]
    span = first.spans[0]
    return (
        source_profile_hash(profile),
        first.blackout_id,
        first.physical_episode_id,
        first.segment_id,
        span.boot_id,
        span.first_wall_time_utc,
        span.first_monotonic_ns,
    )


def _context_reason(
    profile: DischargeFragmentProfile,
    context: LoadSagAssessmentContext,
    policy: LoadSagPolicy,
    source: tuple[str, str, str, str, str, datetime, int],
) -> LoadSagReason | None:
    source_reason = _expected_source_reason(profile, context, source)
    if source_reason is not None:
        return source_reason
    revision_reason = _expected_revision_reason(profile, context, policy)
    if revision_reason is not None:
        return revision_reason
    return _expected_scope_reason(profile, context)


def _expected_source_reason(
    profile: DischargeFragmentProfile,
    context: LoadSagAssessmentContext,
    source: tuple[str, str, str, str, str, datetime, int],
) -> LoadSagReason | None:
    if context.expected_profile_hash is not None and context.expected_profile_hash != source[0]:
        return LoadSagReason.SOURCE_PROFILE_HASH_MISMATCH
    actual_slice_hashes = tuple(item.slice_id for item in profile.slices)
    actual_step_hashes = tuple(step.step_record_hash for step in profile.load_steps)
    if (
        context.expected_slice_hashes is not None
        and context.expected_slice_hashes != actual_slice_hashes
    ):
        return LoadSagReason.SOURCE_SLICE_HASH_MISMATCH
    if (
        context.expected_step_hashes is not None
        and context.expected_step_hashes != actual_step_hashes
    ):
        return LoadSagReason.RAW_HASH_MISMATCH
    return None


def _expected_revision_reason(
    profile: DischargeFragmentProfile,
    context: LoadSagAssessmentContext,
    policy: LoadSagPolicy,
) -> LoadSagReason | None:
    if context.policy_revision != policy.revision:
        return LoadSagReason.POLICY_REVISION_MISMATCH
    if context.fragment_policy_revision != profile.policy_revision:
        return LoadSagReason.POLICY_REVISION_MISMATCH
    if context.evaluator_revision != policy.evaluator_revision:
        return LoadSagReason.EVALUATOR_REVISION_MISMATCH
    return None


def _expected_scope_reason(
    profile: DischargeFragmentProfile, context: LoadSagAssessmentContext
) -> LoadSagReason | None:
    if not profile.slices or profile.slices[0].battery_epoch_id != context.battery_epoch_id:
        return LoadSagReason.BATTERY_EPOCH_MISMATCH
    if any(item.origin is not context.origin for item in profile.slices):
        return LoadSagReason.ORIGIN_MISMATCH
    return None


def _step_reason(
    step: LoadStepObservation,
    profile: DischargeFragmentProfile,
    context: LoadSagAssessmentContext,
) -> LoadSagReason | None:
    if context.origin is not ObservationOrigin.NATURAL:
        return LoadSagReason.NON_NATURAL_ORIGIN
    if step.estimate.quality is not StepQuality.QUALIFYING:
        return LoadSagReason.STEP_NOT_QUALIFYING
    if _damage_overlaps_step(step, profile):
        return LoadSagReason.DAMAGE_OVERLAPS_STEP
    return None


def _damage_overlaps_step(step: LoadStepObservation, profile: DischargeFragmentProfile) -> bool:
    parent = step.parent_slice
    by_hash = {sample.canonical_hash: sample for sample in parent.samples}
    times = tuple(
        by_hash[value].observation.monotonic_ns
        for value in step.contributing_sample_hashes
        if value in by_hash
    )
    if not times:
        return False
    first_time, last_time = min(times), max(times)
    return any(
        anchor.kind is AnchorKind.CORRUPTION
        and anchor.boot_id == parent.spans[0].boot_id
        and first_time <= anchor.monotonic_ns <= last_time
        for anchor in profile.anchors
    )


def _refusal(step: LoadStepObservation, reason: LoadSagReason) -> LoadSagStepRefusal:
    return LoadSagStepRefusal(
        step_record_hash=step.step_record_hash,
        parent_slice_hash=step.parent_slice.slice_id,
        contributing_sample_hashes=step.contributing_sample_hashes,
        reason=reason,
    )


def _disposition(
    origin: ObservationOrigin,
    admitted: tuple[LoadStepObservation, ...],
    refusals: tuple[LoadSagStepRefusal, ...],
    global_reason: LoadSagReason | None,
) -> LoadSagDisposition:
    if origin is not ObservationOrigin.NATURAL:
        return LoadSagDisposition.DIAGNOSTIC
    if admitted:
        return LoadSagDisposition.ADMITTED
    if global_reason is None and not refusals:
        return LoadSagDisposition.REFUSED
    return LoadSagDisposition.REFUSED


def _validate_assessment_disposition(value: LoadSagAssessment) -> None:
    if value.origin is not ObservationOrigin.NATURAL:
        if value.disposition is not LoadSagDisposition.DIAGNOSTIC or value.admitted_steps:
            raise ValueError(
                "non-natural load-sag assessments are diagnostic and admitted-step free"
            )
    elif value.disposition is LoadSagDisposition.DIAGNOSTIC:
        raise ValueError("natural load-sag assessment cannot be diagnostic")
    if value.disposition is LoadSagDisposition.ADMITTED and not value.admitted_steps:
        raise ValueError("admitted assessment requires at least one step")
    admitted_hashes = {step.step_record_hash for step in value.admitted_steps}
    refused_hashes = {item.step_record_hash for item in value.refusals}
    if len(admitted_hashes) != len(value.admitted_steps) or len(refused_hashes) != len(
        value.refusals
    ):
        raise ValueError("assessment step references must be unique")
    if admitted_hashes & refused_hashes:
        raise ValueError("a step cannot be both admitted and refused")


def _step_ref(step: LoadStepObservation) -> list[Any]:
    return [
        step.step_record_hash,
        step.parent_slice.slice_id,
        list(step.contributing_sample_hashes),
    ]


def _resolve_step_ref(
    value: Any,
    profile_steps: dict[str, LoadStepObservation],
    profile: DischargeFragmentProfile,
) -> LoadStepObservation:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("admitted step reference fields are not exact")
    step = profile_steps.get(value[0])
    if step is None or step.parent_slice.slice_id != value[1]:
        raise ValueError("admitted step reference is absent from profile")
    hashes = tuple(value[2])
    if hashes != step.contributing_sample_hashes:
        raise ValueError("admitted step raw hashes do not match profile")
    if step.parent_slice not in profile.slices:
        raise ValueError("admitted step parent is absent from profile")
    return step


def _resolve_refusal(
    value: Any,
    profile_steps: dict[str, LoadStepObservation],
    profile: DischargeFragmentProfile,
) -> LoadSagStepRefusal:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("refusal fields are not exact")
    result = LoadSagStepRefusal(
        step_record_hash=value[0],
        parent_slice_hash=value[1],
        contributing_sample_hashes=tuple(value[2]),
        reason=LoadSagReason(value[3]),
    )
    step = profile_steps.get(result.step_record_hash)
    if step is None or step.parent_slice.slice_id != result.parent_slice_hash:
        raise ValueError("refused step reference is absent from profile")
    if result.contributing_sample_hashes != step.contributing_sample_hashes:
        raise ValueError("refused step raw hashes do not match profile")
    if step.parent_slice not in profile.slices:
        raise ValueError("refused step parent is absent from profile")
    return result


def _validate_payload_primitives(payload: dict[str, Any]) -> None:
    if payload["assessment_schema"] != "load-sag-assessment-v1":
        raise ValueError("unsupported load-sag assessment schema")
    for name in (
        "policy_revision",
        "fragment_policy_revision",
        "evaluator_revision",
        "battery_epoch_id",
        "source_blackout_id",
        "source_physical_episode_id",
        "source_segment_id",
        "source_boot_id",
    ):
        _require_id(payload[name], name)
    _require_hash(payload["source_profile_hash"], "source profile hash")
    _validate_hashes(tuple(payload["source_slice_hashes"]), "source slice hashes")
    _validate_hashes(tuple(payload["source_step_hashes"]), "source step hashes")
    _validate_wall_text(payload["source_first_wall_time_utc"])
    _validate_nonnegative_int(payload["source_first_monotonic_ns"], "source first monotonic time")
    _validate_nonnegative_int(payload["step_count"], "step count")
    _validate_nonnegative_int(payload["step_overflow_count"], "step overflow count")
    _validate_nonnegative_int(payload["refusal_overflow_count"], "refusal overflow count")
    if payload["first_unprofiled_step_hash"] is not None:
        _require_hash(payload["first_unprofiled_step_hash"], "first unprofiled step hash")
    ObservationOrigin(payload["observation_origin"])
    LoadSagDisposition(payload["disposition"])
    if not isinstance(payload["admitted_steps"], list) or not isinstance(payload["refusals"], list):
        raise ValueError("step references must be arrays")


def _require_id(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_hash(value: Any, name: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be lowercase SHA-256 hex")


def _validate_hashes(values: tuple[str, ...], name: str) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{name} must be a tuple")
    for value in values:
        _require_hash(value, name)


def _validate_optional_hashes(values: tuple[str, ...] | None, name: str) -> None:
    if values is not None:
        _validate_hashes(values, name)


def _validate_optional_hash(value: Any, name: str) -> None:
    if value is not None:
        _require_hash(value, name)


def _validate_nonnegative_int(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def _validate_wall_time(value: Any) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise ValueError("wall time must be timezone-aware UTC")


def _canonical_wall_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_wall_text(value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError("source wall time must be canonical UTC text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("source wall time is invalid") from exc
    _validate_wall_time(parsed)
