"""Classify raw UPS observations for the safety and telemetry paths."""

from src.domain.values import BlackoutKind, PhysicalObservation


def classify_physical_observation(
    observation: PhysicalObservation,
    *,
    attributed_kind: BlackoutKind | None = None,
) -> BlackoutKind:
    """Classify one poll, treating raw UPS flags as noisy battery evidence.

    ``CAL`` is not proof of a self-test: this function only accepts an explicit
    causal attribution supplied by the daemon.  Without it, every battery
    episode is conservatively a real blackout.
    """
    flags = frozenset(observation.raw_status.split())
    if {"CAL", "OB"}.intersection(flags):
        if attributed_kind == BlackoutKind.BLACKOUT_TEST:
            return attributed_kind
        return BlackoutKind.BLACKOUT_REAL
    if "OL" in flags:
        return BlackoutKind.ONLINE
    return BlackoutKind.UNKNOWN


def raw_lb_observed(observation: PhysicalObservation) -> bool:
    """Expose firmware LB as a diagnostic only; this function makes no safety decision."""
    return "LB" in observation.raw_status.split()
