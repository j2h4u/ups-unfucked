"""Classify raw UPS observations for the safety and telemetry paths."""

from src.domain.values import BlackoutKind, PhysicalObservation


def classify_physical_observation(observation: PhysicalObservation) -> BlackoutKind:
    """Classify battery operation from raw status flags, independent of input voltage."""
    flags = frozenset(observation.raw_status.split())
    if "CAL" in flags:
        return BlackoutKind.BLACKOUT_TEST
    if "OB" in flags:
        return BlackoutKind.BLACKOUT_REAL
    if "OL" in flags:
        return BlackoutKind.ONLINE
    return BlackoutKind.UNKNOWN


def raw_lb_observed(observation: PhysicalObservation) -> bool:
    """Expose firmware LB as a diagnostic only; this function makes no safety decision."""
    return "LB" in observation.raw_status.split()
