"""Classify raw UPS observations for the safety and telemetry paths."""

from src.domain.values import BlackoutKind, PhysicalObservation

TEST_INPUT_VOLTAGE_THRESHOLD_V = 100.0


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


def raw_lb_observed(observation: PhysicalObservation) -> bool:
    """Expose firmware LB as a diagnostic only; this function makes no safety decision."""
    return "LB" in observation.raw_status.split()
