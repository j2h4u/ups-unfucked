"""Pure rolling full-charge readiness policy."""

from dataclasses import dataclass
from math import isfinite

from src.domain.reasons import ReadinessReason, order_reasons
from src.domain.values import ChargeReadiness, PhysicalObservation

REQUIRED_ONLINE_S = 12.0 * 60.0 * 60.0
TRAILING_WINDOW_S = 30.0 * 60.0
MAX_ACQUISITION_GAP_S = 25.0
MIN_READY_VOLTAGE_V = 13.0
MAX_READY_VOLTAGE_V = 14.5
MAX_TRAILING_SPAN_V = 0.30


@dataclass(frozen=True, slots=True)
class ReadinessState:
    boot_id: str | None
    continuous_online_start_ns: int | None
    last_monotonic_ns: int | None
    trailing_voltage_points: tuple[tuple[int, float], ...]
    last_reset_reason: ReadinessReason | None


def initial_readiness_state() -> ReadinessState:
    return ReadinessState(None, None, None, (), None)


def update_readiness(
    state: ReadinessState,
    observation: PhysicalObservation,
) -> tuple[ReadinessState, ChargeReadiness]:
    """Advance readiness and return the fact to freeze for this observation.

    On the first battery observation, the returned fact describes the completed
    online charging interval; the internal state is still reset immediately for
    the next physical episode.
    """
    reset_reason = _reset_reason(state, observation)
    voltage = observation.battery_voltage_v
    if reset_reason is not None or voltage is None:
        capture_snapshot = None
        if _is_first_battery_observation(state, observation, reset_reason):
            assert state.last_monotonic_ns is not None
            capture_snapshot = _snapshot(state, state.last_monotonic_ns)
        new_state = _restart_or_empty(observation, reset_reason)
        return new_state, capture_snapshot or _snapshot(new_state, observation.monotonic_ns)

    start_ns = state.continuous_online_start_ns
    if start_ns is None:
        start_ns = observation.monotonic_ns
    cutoff_ns = observation.monotonic_ns - int(TRAILING_WINDOW_S * 1_000_000_000)
    trailing = tuple(
        point
        for point in (*state.trailing_voltage_points, (observation.monotonic_ns, voltage))
        if point[0] >= cutoff_ns
    )
    if _voltage_span(trailing) > MAX_TRAILING_SPAN_V:
        new_state = ReadinessState(
            observation.boot_id,
            observation.monotonic_ns,
            observation.monotonic_ns,
            ((observation.monotonic_ns, voltage),),
            ReadinessReason.VOLTAGE_SPAN_TOO_WIDE,
        )
        return new_state, _snapshot(new_state, observation.monotonic_ns)

    new_state = ReadinessState(
        observation.boot_id,
        start_ns,
        observation.monotonic_ns,
        trailing,
        None,
    )
    return new_state, _snapshot(new_state, observation.monotonic_ns)


def _reset_reason(
    state: ReadinessState,
    observation: PhysicalObservation,
) -> ReadinessReason | None:
    flags = frozenset(observation.raw_status.split())
    if state.boot_id is not None and state.boot_id != observation.boot_id:
        reason = ReadinessReason.BOOT_CHANGED
    elif "CAL" in flags:
        reason = ReadinessReason.CALIBRATION_ACTIVE
    elif "OL" not in flags or "OB" in flags:
        reason = ReadinessReason.NOT_ONLINE
    elif observation.battery_voltage_v is None or not isfinite(observation.battery_voltage_v):
        reason = ReadinessReason.VOLTAGE_UNAVAILABLE
    elif not MIN_READY_VOLTAGE_V <= observation.battery_voltage_v <= MAX_READY_VOLTAGE_V:
        reason = ReadinessReason.VOLTAGE_OUT_OF_RANGE
    elif state.last_monotonic_ns is not None:
        gap_s = (observation.monotonic_ns - state.last_monotonic_ns) / 1_000_000_000
        reason = (
            ReadinessReason.ACQUISITION_GAP
            if gap_s <= 0.0 or gap_s > MAX_ACQUISITION_GAP_S
            else None
        )
    else:
        reason = None
    return reason


def _restart_or_empty(
    observation: PhysicalObservation,
    reason: ReadinessReason | None,
) -> ReadinessState:
    voltage = observation.battery_voltage_v
    eligible_voltage = (
        voltage is not None
        and isfinite(voltage)
        and MIN_READY_VOLTAGE_V <= voltage <= MAX_READY_VOLTAGE_V
    )
    flags = frozenset(observation.raw_status.split())
    eligible = "OL" in flags and "OB" not in flags and "CAL" not in flags and eligible_voltage
    if not eligible or voltage is None:
        return ReadinessState(None, None, None, (), reason)
    return ReadinessState(
        observation.boot_id,
        observation.monotonic_ns,
        observation.monotonic_ns,
        ((observation.monotonic_ns, voltage),),
        reason,
    )


def _is_first_battery_observation(
    state: ReadinessState,
    observation: PhysicalObservation,
    reset_reason: ReadinessReason | None,
) -> bool:
    flags = frozenset(observation.raw_status.split())
    gap_s = (
        None
        if state.last_monotonic_ns is None
        else (observation.monotonic_ns - state.last_monotonic_ns) / 1_000_000_000
    )
    return (
        reset_reason == ReadinessReason.NOT_ONLINE
        and "OB" in flags
        and state.boot_id == observation.boot_id
        and gap_s is not None
        and 0.0 < gap_s <= MAX_ACQUISITION_GAP_S
    )


def _snapshot(state: ReadinessState, monotonic_ns: int) -> ChargeReadiness:
    if state.continuous_online_start_ns is None:
        duration_s = 0.0
    else:
        duration_s = max(0.0, (monotonic_ns - state.continuous_online_start_ns) / 1_000_000_000)
    span = _voltage_span(state.trailing_voltage_points) if state.trailing_voltage_points else None
    reasons: list[ReadinessReason] = []
    if state.last_reset_reason is not None:
        reasons.append(state.last_reset_reason)
    if duration_s < REQUIRED_ONLINE_S:
        reasons.append(ReadinessReason.INSUFFICIENT_CONTINUOUS_ONLINE_TIME)
    ready = duration_s >= REQUIRED_ONLINE_S and span is not None and span <= MAX_TRAILING_SPAN_V
    return ChargeReadiness(ready, duration_s, span, order_reasons(tuple(reasons)))


def _voltage_span(points: tuple[tuple[int, float], ...]) -> float:
    voltages = tuple(point[1] for point in points)
    return max(voltages) - min(voltages)
