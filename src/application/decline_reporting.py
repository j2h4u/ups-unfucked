"""Bounded recomputation of decline cohorts from current sealed raw evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from src.application.assessment_codec import project_observations
from src.application.errors import StoragePortCorruption
from src.application.ports import ReportingEventStorePort
from src.application.storage_values import EventProjection, EventRef, EventSummary
from src.domain import decline_policy as _decline_policy
from src.domain.decline import (
    FirmwareReserveSample,
    LoadSagTrendSample,
    LongPartialSample,
    assess_firmware_lb_reserve,
    assess_load_sag_trend,
    assess_long_partial_curve,
    storage_corruption_status,
)
from src.domain.evidence import terminal_science_policy
from src.domain.values import PhysicalObservation, ReserveCohortStatus

_EventContext = _decline_policy.DeclineEventContext
_firmware_sample = _decline_policy.firmware_sample
_integrated_load = _decline_policy.integrated_load
_load_sag_samples = _decline_policy.load_sag_samples
_long_partial_sample = _decline_policy.long_partial_sample
_stable_origin_window = _decline_policy.stable_origin_window
_voltage_at_horizon = _decline_policy.voltage_at_horizon


@dataclass(frozen=True, slots=True)
class _EventDeclineEvidence:
    load_sag: tuple[LoadSagTrendSample, ...] | None
    firmware: tuple[FirmwareReserveSample, ...] | None
    long_partial: tuple[LongPartialSample, ...] | None


@dataclass(frozen=True, slots=True)
class _DeclineSamples:
    load_sag: tuple[LoadSagTrendSample, ...]
    firmware: tuple[FirmwareReserveSample, ...]
    long_partial: tuple[LongPartialSample, ...]
    load_sag_available: bool
    firmware_available: bool
    long_partial_available: bool


_NO_DECLINE_EVIDENCE = _EventDeclineEvidence((), (), ())


def decline_statuses(
    store: ReportingEventStorePort,
    summaries: Sequence[EventSummary],
    *,
    scan_complete: bool = True,
) -> tuple[ReserveCohortStatus, ReserveCohortStatus, ReserveCohortStatus]:
    """Recompute each metric from its own latest-six raw-evidence cohort."""
    if not scan_complete:
        return _empty_decline_statuses()
    try:
        evidence = _collect_decline_evidence(store, summaries)
    except StoragePortCorruption:
        return _storage_corruption_statuses()
    samples = _decline_samples(evidence)
    return (
        _load_sag_status(samples),
        _firmware_status(samples),
        _long_partial_status(samples),
    )


def _empty_decline_statuses() -> tuple[
    ReserveCohortStatus, ReserveCohortStatus, ReserveCohortStatus
]:
    return (
        assess_load_sag_trend(()),
        assess_firmware_lb_reserve(()),
        assess_long_partial_curve(()),
    )


def _storage_corruption_statuses() -> tuple[
    ReserveCohortStatus, ReserveCohortStatus, ReserveCohortStatus
]:
    return (
        storage_corruption_status("load_sag_trend"),
        storage_corruption_status("firmware_lb_reserve_proxy"),
        storage_corruption_status("long_partial_curve"),
    )


def _decline_samples(evidence: tuple[_EventDeclineEvidence, ...]) -> _DeclineSamples:
    load_sag, load_sag_available = _metric_samples(evidence, "load_sag")
    firmware, firmware_available = _metric_samples(evidence, "firmware")
    long_partial, long_partial_available = _metric_samples(evidence, "long_partial")
    return _DeclineSamples(
        load_sag,
        firmware,
        long_partial,
        load_sag_available,
        firmware_available,
        long_partial_available,
    )


def _collect_decline_evidence(
    store: ReportingEventStorePort,
    summaries: Sequence[EventSummary],
) -> tuple[_EventDeclineEvidence, ...]:
    """Walk newest-first and stop each metric after six usable events."""
    counts = [0, 0, 0]
    active = [True, True, True]
    collected: list[_EventDeclineEvidence] = []
    for summary in reversed(summaries):
        if not any(active):
            break
        item = _event_decline_evidence(store, summary)
        collected.append(item)
        candidates = (item.load_sag, item.firmware, item.long_partial)
        for index, candidate in enumerate(candidates):
            if not active[index]:
                continue
            if candidate is None:
                active[index] = False
            elif candidate:
                counts[index] += 1
                active[index] = counts[index] < 6
    return tuple(collected)


def _metric_samples(
    evidence: tuple[_EventDeclineEvidence, ...],
    metric: Literal["load_sag", "firmware", "long_partial"],
) -> tuple[tuple[Any, ...], bool]:
    selected: list[tuple[Any, ...]] = []
    count = 0
    for item in evidence:
        if metric == "load_sag":
            candidate = item.load_sag
        elif metric == "firmware":
            candidate = item.firmware
        else:
            candidate = item.long_partial
        if candidate is None:
            return (), False
        selected.append(candidate)
        if candidate:
            count += 1
        if count == 6:
            break
    return tuple(sample for candidate in reversed(selected) for sample in candidate), True


def _load_sag_status(samples: _DeclineSamples) -> ReserveCohortStatus:
    return assess_load_sag_trend(samples.load_sag if samples.load_sag_available else ())


def _firmware_status(samples: _DeclineSamples) -> ReserveCohortStatus:
    return assess_firmware_lb_reserve(samples.firmware if samples.firmware_available else ())


def _long_partial_status(samples: _DeclineSamples) -> ReserveCohortStatus:
    return assess_long_partial_curve(samples.long_partial if samples.long_partial_available else ())


def _event_decline_evidence(
    store: ReportingEventStorePort,
    summary: EventSummary,
) -> _EventDeclineEvidence:
    try:
        projection = _sealed_science_projection(store, summary)
        if projection is None:
            return _NO_DECLINE_EVIDENCE
        start = projection.start
        if start is None:
            raise ValueError("sealed event is missing start")
        epoch = _required_string(start.payload.get("battery_epoch_id"))
        observations = project_observations(projection)
        if not observations:
            raise ValueError("sealed event has no observations")
        context = _EventContext(
            summary.blackout_id,
            start.segment_id,
            _utc(start.wall_time_utc),
            epoch,
        )
        ready = _ready_at_start(start.payload)
    except StoragePortCorruption:
        raise
    except Exception:
        return _EventDeclineEvidence(None, None, None)

    if _summary_has_damage(summary):
        load_sag = None
        long_partial = None
    else:
        try:
            load_sag = _load_sag_samples(
                context, _decline_policy.load_sag_observations(observations)
            )
        except (TypeError, ValueError):
            load_sag = None
        if not _decline_policy.natural_event(observations):
            long_partial = ()
        else:
            try:
                long_partial = (_long_partial_sample(context, ready, observations),)
            except (TypeError, ValueError):
                long_partial = None
    firmware = _firmware_evidence(
        context,
        ready,
        observations,
        provenance_gap_observed=_gap_before_selected_lb(projection, observations),
    )
    return _EventDeclineEvidence(load_sag, firmware, long_partial)


def _sealed_science_projection(
    store: ReportingEventStorePort,
    summary: EventSummary,
) -> EventProjection | None:
    projection = store.project(EventRef(summary.blackout_id, summary.segment_filename))
    if projection.outcome is None:
        raise ValueError("sealed decline candidate is missing its terminal outcome")
    if not _terminal_science_eligible(projection, summary.termination):
        return None
    return projection


def _terminal_science_eligible(projection: EventProjection, summary_termination: object) -> bool:
    """Require the same naturally completed terminal fact at both boundaries."""
    if projection.end is None:
        return False
    payload = projection.end.payload
    raw = payload.get("termination") if isinstance(payload, Mapping) else None
    projection_policy = terminal_science_policy(raw)
    summary_policy = terminal_science_policy(summary_termination)
    return (
        projection_policy.authorizes_science
        and summary_policy.authorizes_science
        and projection_policy.termination == summary_policy.termination
    )


def _summary_has_damage(summary: EventSummary) -> bool:
    return False


def _firmware_evidence(
    context: _EventContext,
    ready: bool,
    observations: tuple[PhysicalObservation, ...],
    *,
    provenance_gap_observed: bool,
) -> tuple[FirmwareReserveSample, ...] | None:
    if not any("LB" in item.raw_status.split() for item in observations):
        return ()
    if not _decline_policy.natural_prefix(
        observations,
        provenance_gap_observed=provenance_gap_observed,
    ):
        return ()
    try:
        sample = _firmware_sample(context, ready, observations)
    except (TypeError, ValueError):
        return None
    return None if sample is None else (sample,)


def _gap_before_selected_lb(
    projection: EventProjection,
    observations: tuple[PhysicalObservation, ...],
) -> bool:
    lb_index = _decline_policy.trusted_lb_observation_index(observations)
    if lb_index is None:
        return False
    if lb_index == 0:
        lb_seq = projection.start.seq if projection.start is not None else None
    else:
        record_index = lb_index - 1
        lb_seq = (
            projection.observations[record_index].seq
            if record_index < len(projection.observations)
            else None
        )
    return lb_seq is not None and any(gap.seq <= lb_seq for gap in projection.gaps)


def _ready_at_start(payload: Mapping[str, Any]) -> bool:
    readiness = _mapping(payload.get("charge_readiness"))
    return readiness.get("ready") is True


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("expected mapping")
    return value


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _required_string(value: object) -> str:
    result = _string(value)
    if result is None:
        raise ValueError("expected non-empty string")
    return result
