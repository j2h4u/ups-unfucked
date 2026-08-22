"""Conservative, proposal-only learning from one natural blackout.

The UPS does not expose battery current or a terminal discharge measurement.  A
natural event can nevertheless contain one useful, independent observation: a
load step and the corresponding battery-voltage step.  This module turns that
observation into a small proposal for the empirical load-sag coefficient.  It
does not mutate a model and intentionally ignores model-derived percentage and
runtime fields.
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
_MIN_VOLTAGE_STEP = 0.15
_MIN_LOAD_STEP = 15.0
_PLATEAU_POINTS = 5
_MAX_PLATEAU_STD = 2.0
_MAX_GAP_S = 2.5


@dataclass(frozen=True, slots=True)
class ModelFeedbackProposal:
    """One bounded proposal; applying it belongs to the model owner."""

    to_value: float
    reason: str
    evidence_at: str


def propose_model_feedback(
    rows: Sequence[Mapping[str, Any]], snapshot: FrozenModelSnapshot
) -> ModelFeedbackProposal | None:
    """Propose a bounded ``ir_k_v_per_pp`` correction from one closed blackout.

    Only raw ``battery_v``, ``load_pct``, ``status`` and ``at`` are used.  The
    episode must contain a real (non-CAL) OB interval followed by a return to
    OL, with one stable load step of at least 15 percentage points.  A missing,
    malformed, self-test, censored, or otherwise weak episode returns ``None``.
    """
    event = _natural_event(rows)
    if event is None or not _valid_snapshot(snapshot):
        return None
    candidate = _load_step(event)
    if candidate is None:
        return None
    estimated_k, evidence_at = candidate
    current_k = snapshot.ir_k_v_per_pp
    if estimated_k >= current_k or abs(estimated_k - current_k) < 0.0005:
        return None
    delta = max(-_MAX_STEP, estimated_k - current_k)
    if abs(delta) < 0.0005:
        return None
    target_k = current_k + delta
    return ModelFeedbackProposal(
        to_value=target_k,
        reason=(
            "natural blackout load step: raw battery-voltage sag supports a "
            "bounded empirical load-sag correction"
        ),
        evidence_at=evidence_at,
    )


def _natural_event(rows: Sequence[Mapping[str, Any]]) -> list[tuple[datetime, float, float]] | None:
    start = next((index for index, row in enumerate(rows) if _is_ob(row)), None)
    if start is None:
        return None
    end = next((index for index in range(start + 1, len(rows)) if not _is_ob(rows[index])), None)
    if end is None or end <= start:
        return None
    if "OL" not in str(rows[end].get("status", "")).split():
        return None
    event_rows = rows[start:end]
    if any(_is_self_test(row) for row in event_rows):
        return None
    parsed: list[tuple[datetime, float, float]] = []
    for row in event_rows:
        if _has_lb(row):
            continue
        parsed_row = _raw_sample(row)
        if parsed_row is None:
            return None
        parsed.append(parsed_row)
    return parsed if len(parsed) >= _PLATEAU_POINTS * 2 + 1 else None


def _load_step(
    event: list[tuple[datetime, float, float]],
) -> tuple[float, str] | None:
    for index in range(_PLATEAU_POINTS, len(event) - _PLATEAU_POINTS + 1):
        pre = event[index - _PLATEAU_POINTS : index]
        post = event[index : index + _PLATEAU_POINTS]
        if not _contiguous((*pre, *post)):
            continue
        pre_load = median(point[2] for point in pre)
        post_load = median(point[2] for point in post)
        if not _stable_load(pre, post, pre_load, post_load):
            continue
        delta_load = post_load - pre_load
        if abs(delta_load) < _MIN_LOAD_STEP or not (0.0 < pre_load <= 50.0):
            continue
        if not (0.0 < post_load <= 50.0):
            continue
        pre_slope, pre_intercept = _line_at_transition(pre, event[index][0])
        post_slope, post_intercept = _line_at_transition(post, event[index][0])
        if max(abs(pre_slope), abs(post_slope)) > 0.002:
            continue
        delta_voltage = post_intercept - pre_intercept
        estimated_k = -delta_voltage / delta_load
        if abs(delta_voltage) < _MIN_VOLTAGE_STEP or not (_MIN_K <= estimated_k <= _MAX_K):
            continue
        if delta_voltage * delta_load >= 0.0:
            continue
        return estimated_k, event[index][0].isoformat(timespec="seconds").replace("+00:00", "Z")
    return None


def _stable_load(
    pre: list[tuple[datetime, float, float]],
    post: list[tuple[datetime, float, float]],
    pre_load: float,
    post_load: float,
) -> bool:
    return (
        max(
            _population_std(point[2] for point in pre),
            _population_std(point[2] for point in post),
        )
        <= _MAX_PLATEAU_STD
        and abs(post_load - pre_load) >= _MIN_LOAD_STEP
    )


def _line_at_transition(
    points: list[tuple[datetime, float, float]], transition: datetime
) -> tuple[float, float]:
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


def _raw_sample(row: Mapping[str, Any]) -> tuple[datetime, float, float] | None:
    at = row.get("at")
    voltage = row.get("battery_v")
    load = row.get("load_pct")
    if not isinstance(at, str):
        return None
    if not _finite_number(voltage) or not _finite_number(load):
        return None
    try:
        timestamp = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        return None
    timestamp = timestamp.astimezone(timezone.utc)
    voltage = float(voltage)
    load = float(load)
    if not (8.0 <= voltage <= 15.0 and 0.0 <= load <= 100.0):
        return None
    return timestamp, voltage, load


def _finite_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _contiguous(points: tuple[tuple[datetime, float, float], ...]) -> bool:
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


def _is_ob(row: Mapping[str, Any]) -> bool:
    return "OB" in str(row.get("status", "")).split()


def _is_self_test(row: Mapping[str, Any]) -> bool:
    status = str(row.get("status", "")).split()
    input_v = row.get("input_v")
    return "CAL" in status or (
        isinstance(input_v, (int, float))
        and not isinstance(input_v, bool)
        and math.isfinite(input_v)
        and input_v >= 100.0
    )


def _has_lb(row: Mapping[str, Any]) -> bool:
    return "LB" in str(row.get("status", "")).split()


def _valid_snapshot(snapshot: FrozenModelSnapshot) -> bool:
    value = snapshot.ir_k_v_per_pp
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and _MIN_K <= value <= _MAX_K
    )
