"""Pure physical classification and blackout lifecycle transitions."""

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

from src.domain.values import BlackoutKind, PhysicalObservation, TerminationFact

TEST_INPUT_VOLTAGE_THRESHOLD_V = 100.0
UNKNOWN_PRELUDE_GAP_REASON = "unknown_line_voltage_prelude"


class LifecycleState(StrEnum):
    IDLE = "idle"
    PREPARING = "preparing"
    CAPTURING = "capturing"
    PROCESSING = "processing"
    CAPTURE_DAMAGED = "capture_damaged"


class LifecycleSignal(StrEnum):
    OBSERVATION = "observation"
    CAPTURE_PREPARED = "capture_prepared"
    SERVICE_STOP = "service_stop"
    REBOOT_GAP = "reboot_gap"
    CAPTURE_FAILURE = "capture_failure"
    RECOVERED_CAPTURE_ATTACH = "recovered_capture_attach"
    CAPTURE_END_SUBMITTED = "capture_end_submitted"
    CAPTURE_END_DURABLE = "capture_end_durable"
    START_REJECTED = "start_rejected"
    TERMINAL_RESET = "terminal_reset"
    STICKY_RECOVERY_TIMEOUT = "sticky_recovery_timeout"


@dataclass(frozen=True, slots=True)
class LifecycleTransition:
    state_after: LifecycleState
    blackout_kind: BlackoutKind
    termination: TerminationFact | None
    raw_lb_observed: bool
    record_gap: bool


def classify_physical_observation(observation: PhysicalObservation) -> BlackoutKind:
    """Classify battery operation fail-closed when input voltage is absent or low."""
    flags = frozenset(observation.raw_status.split())
    if "OB" in flags or "CAL" in flags:
        if (
            observation.input_voltage_v is not None
            and observation.input_voltage_v >= TEST_INPUT_VOLTAGE_THRESHOLD_V
        ):
            return BlackoutKind.BLACKOUT_TEST
        return BlackoutKind.BLACKOUT_REAL
    if "OL" in flags:
        return BlackoutKind.ONLINE
    return BlackoutKind.UNKNOWN


def is_unknown_outage_candidate(observation: PhysicalObservation) -> bool:
    """Use independent line-voltage evidence to retain a conservative UNKNOWN.

    A missing, unusable, or low input-voltage reading is the same fail-closed
    boundary used to classify a physical ``OB``/``CAL`` observation as a real
    outage.  A healthy line reading keeps a communication-only UNKNOWN out of
    the event journal.
    """
    if classify_physical_observation(observation) != BlackoutKind.UNKNOWN:
        return False
    input_voltage = observation.input_voltage_v
    return (
        input_voltage is None
        or not isfinite(input_voltage)
        or input_voltage < TEST_INPUT_VOLTAGE_THRESHOLD_V
    )


def is_capture_candidate(observation: PhysicalObservation) -> bool:
    """Return whether one physical observation merits a durable event start."""
    kind = classify_physical_observation(observation)
    return kind in {
        BlackoutKind.BLACKOUT_REAL,
        BlackoutKind.BLACKOUT_TEST,
    } or is_unknown_outage_candidate(observation)


def raw_lb_observed(observation: PhysicalObservation) -> bool:
    """Expose firmware LB as a diagnostic only; this function makes no safety decision."""
    return "LB" in observation.raw_status.split()


def advance_lifecycle(
    state: LifecycleState,
    observation: PhysicalObservation | None,
    signal: LifecycleSignal,
) -> LifecycleTransition:
    """Return the deterministic lifecycle transition for one explicit signal."""
    kind = (
        BlackoutKind.UNKNOWN if observation is None else classify_physical_observation(observation)
    )
    control = _advance_control(state, signal)
    if control is None and signal == LifecycleSignal.OBSERVATION:
        after, termination, record_gap = _advance_observation(state, kind, observation)
    elif control is None:
        after, termination, record_gap = state, None, False
    else:
        after, termination, record_gap = control

    return LifecycleTransition(
        state_after=after,
        blackout_kind=kind,
        termination=termination,
        raw_lb_observed=False if observation is None else raw_lb_observed(observation),
        record_gap=record_gap,
    )


def _advance_control(
    state: LifecycleState,
    signal: LifecycleSignal,
) -> tuple[LifecycleState, TerminationFact | None, bool] | None:
    if signal == LifecycleSignal.REBOOT_GAP and state == LifecycleState.CAPTURING:
        return state, None, True
    rule = _CONTROL_RULES.get(signal)
    if rule is None or state not in rule[0]:
        return None
    return rule[1], rule[2], rule[3]


def _advance_observation(
    state: LifecycleState,
    kind: BlackoutKind,
    observation: PhysicalObservation | None,
) -> tuple[LifecycleState, TerminationFact | None, bool]:
    if (
        state == LifecycleState.IDLE
        and observation is not None
        and is_capture_candidate(observation)
    ):
        return LifecycleState.PREPARING, None, False
    if (
        state in {LifecycleState.PREPARING, LifecycleState.CAPTURING}
        and kind == BlackoutKind.ONLINE
    ):
        return LifecycleState.PROCESSING, TerminationFact.POWER_RESTORED, False
    return state, None, False


_CONTROL_RULES = {
    LifecycleSignal.CAPTURE_FAILURE: (
        frozenset(
            {
                LifecycleState.PREPARING,
                LifecycleState.CAPTURING,
                LifecycleState.PROCESSING,
            }
        ),
        LifecycleState.CAPTURE_DAMAGED,
        TerminationFact.CAPTURE_DAMAGED,
        False,
    ),
    LifecycleSignal.STICKY_RECOVERY_TIMEOUT: (
        frozenset(
            {
                LifecycleState.PREPARING,
                LifecycleState.CAPTURING,
                LifecycleState.PROCESSING,
            }
        ),
        LifecycleState.CAPTURE_DAMAGED,
        TerminationFact.CAPTURE_DAMAGED,
        False,
    ),
    LifecycleSignal.SERVICE_STOP: (
        frozenset({LifecycleState.PREPARING, LifecycleState.CAPTURING}),
        LifecycleState.PROCESSING,
        TerminationFact.SERVICE_STOP,
        False,
    ),
    LifecycleSignal.CAPTURE_PREPARED: (
        frozenset({LifecycleState.PREPARING}),
        LifecycleState.CAPTURING,
        None,
        False,
    ),
    LifecycleSignal.RECOVERED_CAPTURE_ATTACH: (
        frozenset({LifecycleState.IDLE}),
        LifecycleState.CAPTURING,
        None,
        False,
    ),
    LifecycleSignal.CAPTURE_END_SUBMITTED: (
        frozenset({LifecycleState.PREPARING, LifecycleState.CAPTURING}),
        LifecycleState.PROCESSING,
        None,
        False,
    ),
    LifecycleSignal.CAPTURE_END_DURABLE: (
        frozenset({LifecycleState.PROCESSING, LifecycleState.CAPTURE_DAMAGED}),
        LifecycleState.IDLE,
        None,
        False,
    ),
    LifecycleSignal.TERMINAL_RESET: (
        frozenset({LifecycleState.PROCESSING, LifecycleState.CAPTURE_DAMAGED}),
        LifecycleState.IDLE,
        None,
        False,
    ),
    LifecycleSignal.START_REJECTED: (
        frozenset({LifecycleState.PREPARING, LifecycleState.CAPTURING}),
        LifecycleState.IDLE,
        None,
        False,
    ),
}
