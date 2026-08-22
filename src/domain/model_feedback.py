"""Proposal-only learning from independent natural-blackout observations.

The monitor extracts one IR load-step observation from a closed natural event
and persists it. Only a later cohort of independent observations can produce
a bounded proposal. A single noisy, quantized voltage sample must never mutate
the model.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Any, TypeGuard

from src.domain.values import FrozenModelSnapshot

_MIN_K = 0.005
_MAX_K = 0.040
_MAX_STEP = 0.002
_MIN_PROPOSAL_DECREMENT = 0.001
_MIN_VOLTAGE_STEP = 0.20
_MIN_LOAD_STEP = 15.0
_PLATEAU_DURATION_S = 20.0
_MAX_GAP_S = 15.0
_MAX_PLATEAU_STD = 2.0
_MAX_VOLTAGE_SLOPE_V_PER_S = 0.002
_MAX_COHORT_SPREAD_K = 0.0015
_MAX_OBSERVATION_UNCERTAINTY_K = 0.0035
_VOLTAGE_QUANTUM_HALF_V = 0.05
_IR_FIELD = "physics.ir_compensation.k_volts_per_percent"


@dataclass(frozen=True, slots=True)
class IRObservation:
    """A persisted, non-mutating estimate from one blackout event.

    ``uncertainty`` is in ``k`` units and represents one half-step of the
    UPS's 0.1 V display quantization divided by the observed load step. It is
    evidence metadata, not permission to widen the cohort conflict guard.
    """

    event_at: str
    estimate: float
    evidence_at: str
    uncertainty: float
    reason: str


@dataclass(frozen=True, slots=True)
class ModelFeedbackProposal:
    """One bounded proposal; applying it belongs to the model owner."""

    to_value: float
    reason: str
    evidence_at: str
    field: str = _IR_FIELD


RawSample = tuple[datetime, float, float]


def extract_ir_observation(
    rows: Sequence[Mapping[str, Any]],
    *,
    event_at: str | None = None,
) -> IRObservation | None:
    """Extract one IR candidate without proposing or mutating a model.

    The event must contain a real OB interval followed by OL, with no CAL
    samples. A load increase is accepted only when both voltage/load plateaus
    are stable for at least 20 seconds. The duration guard makes 1-second and
    roughly 11-second polling cadences equivalent; physical guards reject
    implausible ranges, slopes, and voltage/load directions.
    """

    event = _natural_event(rows)
    if event is None:
        return None
    candidate = _load_step(event)
    if candidate is None:
        return None
    estimate, evidence, uncertainty = candidate
    first_at = event[0][0].isoformat(timespec="seconds").replace("+00:00", "Z")
    normalized_event_at = _normalize_timestamp(event_at) if event_at is not None else first_at
    if normalized_event_at is None:
        return None
    return IRObservation(
        event_at=normalized_event_at,
        estimate=estimate,
        evidence_at=evidence,
        uncertainty=uncertainty,
        reason=(
            "natural blackout load step: raw quantized battery-voltage sag "
            "supports an independent IR observation"
        ),
    )


def propose_ir_cohort_feedback(
    saved_observations: Sequence[IRObservation | Mapping[str, Any]],
    new_observation: IRObservation | Mapping[str, Any] | None,
    snapshot: FrozenModelSnapshot,
) -> ModelFeedbackProposal | None:
    """Return one bounded IR proposal from saved observations plus a new one.

    At least three valid observations from at least two distinct ``event_at``
    values are required. Replayed event IDs count once, the estimate is the
    cohort median, and spread above 0.0015 k is conflict. A proposal is
    downward-only, requires a robust 0.001 k decrement, and is capped at
    0.002 k per cohort.
    """

    if not _valid_snapshot(snapshot):
        return None
    observations = _unique_observations(saved_observations, new_observation)
    if len(observations) < 3 or len({item.event_at for item in observations}) < 2:
        return None
    estimates = tuple(item.estimate for item in observations)
    if max(item.uncertainty for item in observations) > _MAX_OBSERVATION_UNCERTAINTY_K:
        return None
    if max(estimates) - min(estimates) > _MAX_COHORT_SPREAD_K:
        return None
    target = median(estimates)
    current = snapshot.ir_k_v_per_pp
    decrement = current - target
    if decrement < _MIN_PROPOSAL_DECREMENT:
        return None
    bounded_target = max(current - _MAX_STEP, target)
    evidence_at = max(item.evidence_at for item in observations)
    return ModelFeedbackProposal(
        to_value=bounded_target,
        evidence_at=evidence_at,
        reason=(
            "cohort of independent natural blackout IR observations: median estimate "
            "supports a bounded downward correction"
        ),
    )


def _unique_observations(
    saved: Sequence[IRObservation | Mapping[str, Any]],
    new: IRObservation | Mapping[str, Any] | None,
) -> tuple[IRObservation, ...]:
    unique: dict[str, IRObservation] = {}
    candidates = (*saved, new) if new is not None else saved
    for candidate in candidates:
        observation = _coerce_observation(candidate)
        if observation is not None:
            unique.setdefault(observation.event_at, observation)
    return tuple(unique.values())


def _coerce_observation(value: IRObservation | Mapping[str, Any]) -> IRObservation | None:
    if isinstance(value, IRObservation):
        candidate = value
    elif isinstance(value, Mapping):
        event_at = _normalize_timestamp(value.get("event_at"))
        evidence_at = _normalize_timestamp(value.get("evidence_at"))
        estimate = value.get("estimate")
        uncertainty = value.get("uncertainty")
        reason = value.get("reason")
        if (
            event_at is None
            or evidence_at is None
            or not _finite_number(estimate)
            or not _finite_number(uncertainty)
            or not isinstance(reason, str)
        ):
            return None
        candidate = IRObservation(
            event_at=event_at,
            estimate=float(estimate),
            evidence_at=evidence_at,
            uncertainty=float(uncertainty),
            reason=reason,
        )
    else:
        return None
    event_at = _normalize_timestamp(candidate.event_at)
    evidence_at = _normalize_timestamp(candidate.evidence_at)
    if event_at is None or evidence_at is None:
        return None
    if not _valid_estimate(candidate.estimate) or not _valid_uncertainty(candidate.uncertainty):
        return None
    if not candidate.reason:
        return None
    return IRObservation(
        event_at=event_at,
        estimate=float(candidate.estimate),
        evidence_at=evidence_at,
        uncertainty=float(candidate.uncertainty),
        reason=candidate.reason,
    )


def _natural_event(rows: Sequence[Mapping[str, Any]]) -> list[RawSample] | None:
    start = next((index for index, row in enumerate(rows) if _is_ob(row)), None)
    if start is None:
        return None
    end = next((index for index in range(start + 1, len(rows)) if not _is_ob(rows[index])), None)
    if end is None or "OL" not in str(rows[end].get("status", "")).split():
        return None
    event_rows = rows[start:end]
    if any(_is_cal(row) for row in event_rows):
        return None
    parsed: list[RawSample] = []
    for row in event_rows:
        if _has_lb(row):
            continue
        sample = _raw_sample(row)
        if sample is None:
            return None
        parsed.append(sample)
    return parsed if len(parsed) >= 3 else None


def _load_step(event: list[RawSample]) -> tuple[float, str, float] | None:
    for index in range(1, len(event)):
        transition = event[index][0]
        pre = _plateau_window(event[:index], transition, before=True)
        post = _plateau_window(event[index:], transition, before=False)
        if not _plateau_is_valid(pre, post, transition):
            continue
        pre_load = median(point[2] for point in pre)
        post_load = median(point[2] for point in post)
        delta_load = post_load - pre_load
        if abs(delta_load) < _MIN_LOAD_STEP or not (
            0.0 < pre_load <= 50.0 and 0.0 < post_load <= 50.0
        ):
            continue
        pre_slope, pre_intercept = _line_at_transition(pre, transition)
        post_slope, post_intercept = _line_at_transition(post, transition)
        if max(abs(pre_slope), abs(post_slope)) > _MAX_VOLTAGE_SLOPE_V_PER_S:
            continue
        delta_voltage = post_intercept - pre_intercept
        if delta_voltage * delta_load >= 0.0 or delta_voltage > -_MIN_VOLTAGE_STEP:
            continue
        estimate = -delta_voltage / delta_load
        if not _MIN_K <= estimate <= _MAX_K:
            continue
        evidence_at = transition.isoformat(timespec="seconds").replace("+00:00", "Z")
        return estimate, evidence_at, _VOLTAGE_QUANTUM_HALF_V / abs(delta_load)
    return None


def _plateau_window(
    points: list[RawSample], transition: datetime, *, before: bool
) -> list[RawSample]:
    selected: list[RawSample] = []
    candidates = reversed(points) if before else iter(points)
    for sample in candidates:
        selected.append(sample)
        span = (
            _seconds_between(sample[0], transition)
            if before
            else _seconds_between(transition, sample[0])
        )
        if span >= _PLATEAU_DURATION_S:
            break
    return list(reversed(selected)) if before else selected


def _plateau_is_valid(pre: list[RawSample], post: list[RawSample], transition: datetime) -> bool:
    if not pre or not post or not _contiguous((*pre, *post)):
        return False
    if _seconds_between(pre[0][0], transition) < _PLATEAU_DURATION_S:
        return False
    if _seconds_between(transition, post[-1][0]) < _PLATEAU_DURATION_S:
        return False
    return (
        max(_population_std(point[2] for point in pre), _population_std(point[2] for point in post))
        <= _MAX_PLATEAU_STD
    )


def _line_at_transition(points: list[RawSample], transition: datetime) -> tuple[float, float]:
    origin = transition.timestamp()
    xs = [point[0].timestamp() - origin for point in points]
    ys = [point[1] for point in points]
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    slope = (
        0.0
        if denominator == 0.0
        else sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True)) / denominator
    )
    return slope, y_mean - slope * x_mean


def _raw_sample(row: Mapping[str, Any]) -> RawSample | None:
    timestamp = _parse_timestamp(row.get("at"))
    voltage = row.get("battery_v")
    load = row.get("load_pct")
    if timestamp is None or not _finite_number(voltage) or not _finite_number(load):
        return None
    voltage = float(voltage)
    load = float(load)
    if not (8.0 <= voltage <= 15.0 and 0.0 <= load <= 100.0):
        return None
    return timestamp, voltage, load


def _finite_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_snapshot(snapshot: FrozenModelSnapshot) -> bool:
    value = snapshot.ir_k_v_per_pp
    return _finite_number(value) and _MIN_K <= value <= _MAX_K


def _valid_estimate(value: object) -> bool:
    return _finite_number(value) and _MIN_K <= value <= _MAX_K


def _valid_uncertainty(value: object) -> bool:
    return _finite_number(value) and value >= 0.0


def _contiguous(points: tuple[RawSample, ...]) -> bool:
    return all(
        0.0 < (current[0] - previous[0]).total_seconds() <= _MAX_GAP_S
        for previous, current in zip(points, points[1:], strict=False)
    )


def _population_std(values: Any) -> float:
    values = tuple(values)
    if not values:
        return math.inf
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def _seconds_between(left: datetime, right: datetime) -> float:
    return (right - left).total_seconds()


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        return None
    return timestamp.astimezone(timezone.utc)


def _normalize_timestamp(value: object) -> str | None:
    timestamp = _parse_timestamp(value)
    return timestamp.isoformat(timespec="seconds").replace("+00:00", "Z") if timestamp else None


def _is_ob(row: Mapping[str, Any]) -> bool:
    return "OB" in str(row.get("status", "")).split()


def _is_cal(row: Mapping[str, Any]) -> bool:
    return "CAL" in str(row.get("status", "")).split()


def _has_lb(row: Mapping[str, Any]) -> bool:
    return "LB" in str(row.get("status", "")).split()
