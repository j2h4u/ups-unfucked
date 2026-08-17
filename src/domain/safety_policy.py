"""Canonical current virtual-UPS status policy for one unlatched poll."""

from dataclasses import dataclass

from src.domain.values import BlackoutKind

NUT_STATUS_ONLINE = "OL"
NUT_STATUS_DISCHARGING = "OB DISCHRG"
NUT_STATUS_LOW_BATTERY = "OB DISCHRG LB"
SAFETY_LB_FLOOR_MINUTES = 2


def validate_shutdown_threshold_minutes(value: object) -> int:
    """Return one runtime threshold that stays above the hard safety floor."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("shutdown_minutes must be a positive integer")
    if value <= SAFETY_LB_FLOOR_MINUTES:
        raise ValueError(
            "shutdown_minutes must be greater than the "
            f"{SAFETY_LB_FLOOR_MINUTES}-minute safety floor"
        )
    return value


@dataclass(frozen=True, slots=True)
class UnlatchedSafetyDecision:
    """Current status decision before application-owned event latching."""

    virtual_status: str
    modeled_lb: bool
    hard_floor_lb: bool


def decide_unlatched_safety_status(
    blackout_kind: BlackoutKind,
    runtime_minutes: float | None,
    shutdown_threshold_minutes: float,
) -> UnlatchedSafetyDecision:
    """Apply the sole current floor/status policy without retaining event state."""
    if blackout_kind == BlackoutKind.ONLINE:
        return UnlatchedSafetyDecision(NUT_STATUS_ONLINE, False, False)
    if blackout_kind == BlackoutKind.UNKNOWN:
        blackout_kind = BlackoutKind.BLACKOUT_REAL
    effective_runtime = 0.0 if runtime_minutes is None else runtime_minutes
    hard_floor_lb = effective_runtime < SAFETY_LB_FLOOR_MINUTES
    modeled_lb = (
        blackout_kind == BlackoutKind.BLACKOUT_REAL
        and effective_runtime < shutdown_threshold_minutes
    )
    status = NUT_STATUS_LOW_BATTERY if hard_floor_lb or modeled_lb else NUT_STATUS_DISCHARGING
    return UnlatchedSafetyDecision(status, modeled_lb, hard_floor_lb)
