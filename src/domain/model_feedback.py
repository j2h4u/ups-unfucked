"""Conservative, proposal-only learning from one natural blackout.

The UPS does not expose battery current or a terminal discharge measurement.  A
natural event can nevertheless contain useful, independent observations: a
load step and the corresponding battery-voltage step, or a sustained discharge
curve.  This module turns those observations into bounded model proposals.  It
does not mutate a model and intentionally ignores model-derived percentage and
runtime fields.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, TypeGuard

from src.battery_math.lut import soc_from_voltage
from src.battery_math.peukert import peukert_runtime_hours
from src.domain.values import FrozenModelSnapshot

_MIN_K = 0.005
_MAX_K = 0.040
_MAX_STEP = 0.002
_MIN_VOLTAGE_STEP = 0.15
_MIN_LOAD_STEP = 15.0
_PLATEAU_POINTS = 5
_MAX_PLATEAU_STD = 2.0
_MAX_GAP_S = 2.5
_MAX_CURVE_CADENCE_MULTIPLIER = 1.5
_IR_FIELD = "physics.ir_compensation.k_volts_per_percent"
_SOH_FIELD = "soh"
_MIN_SOH = 0.05
_MIN_CURVE_LOAD = 5.0
_MAX_CURVE_LOAD = 50.0
_MIN_CURVE_DURATION_S = 300.0
_SETTLING_S = 60.0
_ANCHOR_S = 30.0
_MIN_CURVE_VOLTAGE_DROP = 0.2
_MIN_CURVE_SOC_DROP = 0.15
_VOLTAGE_QUANTUM_HALF_V = 0.05
_MIN_SOH_DROP = 0.02
_MAX_SOH_STEP = 0.05


@dataclass(frozen=True, slots=True)
class ModelFeedbackProposal:
    """One bounded proposal; applying it belongs to the model owner."""

    to_value: float
    reason: str
    evidence_at: str
    field: str = _IR_FIELD


def propose_model_feedback(
    rows: Sequence[Mapping[str, Any]], snapshot: FrozenModelSnapshot
) -> ModelFeedbackProposal | None:
    """Propose a bounded ``ir_k_v_per_pp`` correction from one closed blackout.

    Only raw ``battery_v``, ``load_pct``, ``status`` and ``at`` are used.  The
    episode must contain a real (non-CAL) OB interval followed by a return to
    OL, with one stable load step of at least 15 percentage points.  If that
    path is unavailable, a qualifying raw, possibly censored discharge curve
    can produce a separate SoH proposal.  Weak evidence returns ``None``.
    """
    if not _valid_snapshot(snapshot):
        return None
    event = _natural_event(rows)
    if event is not None:
        candidate = _load_step(event)
        if candidate is not None:
            estimated_k, evidence_at = candidate
            current_k = snapshot.ir_k_v_per_pp
            if estimated_k < current_k and abs(estimated_k - current_k) >= 0.0005:
                delta = max(-_MAX_STEP, estimated_k - current_k)
                if abs(delta) >= 0.0005:
                    return ModelFeedbackProposal(
                        to_value=current_k + delta,
                        reason=(
                            "natural blackout load step: raw battery-voltage sag supports a "
                            "bounded empirical load-sag correction"
                        ),
                        evidence_at=evidence_at,
                        field=_IR_FIELD,
                    )
    return propose_soh_feedback(rows, snapshot)


def propose_soh_feedback(
    rows: Sequence[Mapping[str, Any]], snapshot: FrozenModelSnapshot
) -> ModelFeedbackProposal | None:
    """Propose a bounded SoH decrease from a natural, possibly censored curve.

    The estimate is deliberately independent of model-derived percentage and
    runtime fields.  It uses only the frozen physics snapshot and raw voltage,
    load, input-voltage, status, and timestamp samples.
    """
    if not _valid_soh_snapshot(snapshot):
        return None
    event = _soh_event(rows)
    if event is None:
        return None
    estimate = _soh_curve_estimate(event, snapshot)
    if estimate is None:
        return None
    return _soh_proposal(estimate, snapshot.soh)


def _soh_proposal(
    estimate: tuple[float, float | None, str], current_soh: float
) -> ModelFeedbackProposal | None:
    upper_soh, survival_floor, evidence_at = estimate
    if upper_soh > current_soh - _MIN_SOH_DROP:
        return None
    target_soh = max(_MIN_SOH, current_soh - _MAX_SOH_STEP, upper_soh)
    if survival_floor is not None and target_soh < survival_floor:
        return None
    if target_soh >= current_soh:
        return None
    return ModelFeedbackProposal(
        to_value=target_soh,
        reason=(
            "natural blackout discharge curve: raw voltage trajectory supports "
            "a bounded downward state-of-health correction"
        ),
        evidence_at=evidence_at,
        field=_SOH_FIELD,
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
    if any(_is_cal(row) for row in event_rows):
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


def _is_cal(row: Mapping[str, Any]) -> bool:
    status = str(row.get("status", "")).split()
    return "CAL" in status


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


def _valid_soh_snapshot(snapshot: FrozenModelSnapshot) -> bool:
    return _valid_snapshot(snapshot) and (
        _finite_positive(snapshot.rated_capacity_ah)
        and _finite_positive(snapshot.nominal_voltage_v)
        and _finite_positive(snapshot.nominal_power_watts)
        and _finite_positive(snapshot.peukert_exponent)
        and _MIN_SOH <= snapshot.soh <= 1.0
        and _finite_number(snapshot.ir_reference_load_percent)
        and bool(snapshot.lut)
    )


def _soh_event(
    rows: Sequence[Mapping[str, Any]],
) -> list[tuple[datetime, float, float, float, bool]] | None:
    """Parse the longest usable OB prefix, retaining raw LB evidence."""
    start = next((index for index, row in enumerate(rows) if _is_ob(row)), None)
    if start is None:
        return None
    parsed: list[tuple[datetime, float, float, float, bool]] = []
    for row in rows[start:]:
        if not _is_ob(row):
            break
        if _is_cal(row):
            return None
        sample = _curve_sample(row)
        if sample is None:
            return None
        parsed.append(sample)
    return parsed if len(parsed) >= 2 else None


def _curve_sample(row: Mapping[str, Any]) -> tuple[datetime, float, float, float, bool] | None:
    raw = _raw_sample(row)
    input_v = row.get("input_v")
    if raw is None or not _finite_number(input_v):
        return None
    return (*raw, float(input_v), _has_lb(row))


def _soh_curve_estimate(
    event: list[tuple[datetime, float, float, float, bool]],
    snapshot: FrozenModelSnapshot,
) -> tuple[float, float | None, str] | None:
    anchors = _curve_anchors(event, snapshot)
    if anchors is None:
        return None
    start_voltage, end_voltage, start_index, end_at = anchors
    if not _curve_q_is_valid(event[start_index:], snapshot):
        return None
    conservative_delta_soc = _soc_delta(
        start_voltage - _VOLTAGE_QUANTUM_HALF_V,
        end_voltage + _VOLTAGE_QUANTUM_HALF_V,
        snapshot,
    )
    if conservative_delta_soc < _MIN_CURVE_SOC_DROP:
        return None
    q = _curve_q(event[start_index:], snapshot)
    if q <= 0.0:
        return None
    upper_soh = q / conservative_delta_soc
    if not math.isfinite(upper_soh):
        return None
    survival_floor = _survival_floor(event[start_index:], start_voltage, q, snapshot)
    return upper_soh, survival_floor, end_at.isoformat(timespec="seconds").replace("+00:00", "Z")


def _curve_anchors(
    event: list[tuple[datetime, float, float, float, bool]],
    snapshot: FrozenModelSnapshot,
) -> tuple[float, float, int, datetime] | None:
    onset = _settled_onset(event)
    if onset is None:
        return None
    onset_index, settling_end = onset
    end_time = event[-1][0]
    if (
        end_time - event[onset_index][0]
    ).total_seconds() < _MIN_CURVE_DURATION_S or not _curve_loads_valid(event[onset_index:]):
        return None
    start_window = _window(
        event, settling_end, settling_end + timedelta(seconds=_ANCHOR_S), snapshot
    )
    end_window = _window(event, end_time - timedelta(seconds=_ANCHOR_S), end_time, snapshot)
    if start_window is None or end_window is None:
        return None
    _start_at, start_voltage, start_index = start_window
    end_at, end_voltage, _ = end_window
    if start_index >= len(event) - 1 or start_voltage - end_voltage < _MIN_CURVE_VOLTAGE_DROP:
        return None
    if _soc_delta(start_voltage, end_voltage, snapshot) < _MIN_CURVE_SOC_DROP:
        return None
    return start_voltage, end_voltage, start_index, end_at


def _settled_onset(
    event: list[tuple[datetime, float, float, float, bool]],
) -> tuple[int, datetime] | None:
    for index, sample in enumerate(event):
        if not _input_low(sample[3]):
            continue
        settling_end = sample[0].timestamp() + _SETTLING_S
        settled = all(
            _input_low(candidate[3])
            for candidate in event[index:]
            if candidate[0].timestamp() <= settling_end
        )
        if settled and event[-1][0].timestamp() >= settling_end:
            return index, datetime.fromtimestamp(settling_end, timezone.utc)
    return None


def _window(
    event: list[tuple[datetime, float, float, float, bool]],
    start: datetime,
    end: datetime,
    snapshot: FrozenModelSnapshot,
) -> tuple[datetime, float, int] | None:
    indices = [index for index, sample in enumerate(event) if start <= sample[0] <= end]
    if not indices:
        return None
    return (
        event[indices[-1]][0],
        median(
            event[index][1]
            + snapshot.ir_k_v_per_pp * (event[index][2] - snapshot.ir_reference_load_percent)
            for index in indices
        ),
        indices[0],
    )


def _curve_loads_valid(event: list[tuple[datetime, float, float, float, bool]]) -> bool:
    return all(_MIN_CURVE_LOAD <= sample[2] <= _MAX_CURVE_LOAD for sample in event)


def _curve_q_is_valid(
    event: list[tuple[datetime, float, float, float, bool]], snapshot: FrozenModelSnapshot
) -> bool:
    if not _curve_cadence_is_valid(event):
        return False
    return all(
        0.0 < (current[0] - previous[0]).total_seconds()
        and peukert_runtime_hours(
            (previous[2] + current[2]) / 2.0,
            snapshot.rated_capacity_ah,
            snapshot.peukert_exponent,
            snapshot.nominal_voltage_v,
            snapshot.nominal_power_watts,
        )
        > 0.0
        for previous, current in zip(event, event[1:], strict=False)
    )


def _curve_cadence_is_valid(
    event: list[tuple[datetime, float, float, float, bool]],
) -> bool:
    intervals = tuple(
        (current[0] - previous[0]).total_seconds()
        for previous, current in zip(event, event[1:], strict=False)
    )
    if not intervals or any(interval <= 0.0 for interval in intervals):
        return False
    cadence = median(intervals)
    return all(interval <= cadence * _MAX_CURVE_CADENCE_MULTIPLIER for interval in intervals)


def _curve_q(
    event: list[tuple[datetime, float, float, float, bool]], snapshot: FrozenModelSnapshot
) -> float:
    return sum(
        (current[0] - previous[0]).total_seconds()
        / 60.0
        / peukert_runtime_hours(
            (previous[2] + current[2]) / 2.0,
            snapshot.rated_capacity_ah,
            snapshot.peukert_exponent,
            snapshot.nominal_voltage_v,
            snapshot.nominal_power_watts,
        )
        / 60.0
        for previous, current in zip(event, event[1:], strict=False)
    )


def _soc_delta(start_voltage: float, end_voltage: float, snapshot: FrozenModelSnapshot) -> float:
    return soc_from_voltage(start_voltage, snapshot.lut) - soc_from_voltage(
        end_voltage, snapshot.lut
    )


def _survival_floor(
    event: list[tuple[datetime, float, float, float, bool]],
    start_voltage: float,
    q: float,
    snapshot: FrozenModelSnapshot,
) -> float | None:
    if not any(sample[4] for sample in event):
        return None
    start_soc = soc_from_voltage(start_voltage + _VOLTAGE_QUANTUM_HALF_V, snapshot.lut)
    if start_soc <= 0.0:
        return None
    return q / start_soc


def _input_low(value: float) -> bool:
    return math.isfinite(value) and value < 100.0


def _finite_positive(value: object) -> bool:
    return _finite_number(value) and float(value) > 0.0
