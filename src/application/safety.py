"""One-snapshot safety calculation independent of capture and firmware LB."""

from dataclasses import dataclass
from enum import StrEnum

from src.battery_math.lut import soc_from_voltage
from src.battery_math.peukert import PeukertParameters, runtime_minutes
from src.domain import safety_policy
from src.domain.lifecycle import raw_lb_observed
from src.domain.values import BlackoutKind, FrozenModelSnapshot, PhysicalObservation

# Preserve the application boundary API while the authority lives inward.
NUT_STATUS_ONLINE = safety_policy.NUT_STATUS_ONLINE
NUT_STATUS_DISCHARGING = safety_policy.NUT_STATUS_DISCHARGING
NUT_STATUS_LOW_BATTERY = safety_policy.NUT_STATUS_LOW_BATTERY
SAFETY_LB_FLOOR_MINUTES = safety_policy.SAFETY_LB_FLOOR_MINUTES


class VirtualLbSource(StrEnum):
    MODELED_THRESHOLD = "modeled_threshold"
    HARD_FLOOR = "hard_floor"


@dataclass(frozen=True, slots=True)
class SafetyLatch:
    """Event-scoped virtual-LB memory; OL is the only clearing transition."""

    virtual_lb: bool = False
    source: VirtualLbSource | None = None


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    virtual_status: str
    modeled_lb: bool
    hard_floor_lb: bool
    next_latch: SafetyLatch


@dataclass(frozen=True, slots=True)
class SafetyInputs:
    voltage_v: float
    load_percent: float
    blackout_kind: BlackoutKind
    shutdown_threshold_minutes: int
    previous_latch: SafetyLatch = SafetyLatch()


@dataclass(frozen=True, slots=True)
class SafetyCalculation:
    soc: float
    charge_percent: int
    runtime_minutes: float
    virtual_status: str
    modeled_lb: bool
    hard_floor_lb: bool
    virtual_lb_source: VirtualLbSource | None
    next_latch: SafetyLatch
    event_class: BlackoutKind


@dataclass(frozen=True, slots=True)
class SafetyPublication:
    """Complete safety output; raw firmware LB is diagnostic-only."""

    virtual_status_token: str
    raw_status: str
    raw_lb_observed: bool
    event_class: BlackoutKind
    modeled_runtime_minutes: float
    modeled_lb: bool
    hard_floor_lb: bool
    virtual_lb_source: VirtualLbSource | None


def conservative_safety_kind(physical_kind: BlackoutKind) -> BlackoutKind:
    """Treat an unclassified physical status as a real blackout for safety.

    This is intentionally more conservative than Release A's stale-state fallback:
    the current modeled reserve still decides whether virtual LB is published.
    """
    if physical_kind == BlackoutKind.UNKNOWN:
        return BlackoutKind.BLACKOUT_REAL
    return physical_kind


def calculate_safety(
    *,
    inputs: SafetyInputs,
    snapshot: FrozenModelSnapshot,
) -> SafetyCalculation:
    """Calculate one complete publication from exactly one frozen snapshot.

    Raw physical status, including firmware ``LB``, is intentionally absent
    from this signature. It remains evidence and cannot influence virtual LB.
    """
    normalized_voltage = inputs.voltage_v + snapshot.ir_k_v_per_pp * (
        inputs.load_percent - snapshot.ir_reference_load_percent
    )
    soc = soc_from_voltage(normalized_voltage, snapshot.lut)
    remaining = runtime_minutes(
        soc,
        inputs.load_percent,
        PeukertParameters(
            capacity_ah=snapshot.rated_capacity_ah,
            soh=snapshot.soh,
            peukert_exponent=snapshot.peukert_exponent,
            nominal_voltage=snapshot.nominal_voltage_v,
            nominal_power_watts=snapshot.nominal_power_watts,
        ),
    )
    decision = safety_decision(
        inputs.blackout_kind,
        remaining,
        inputs.shutdown_threshold_minutes,
        inputs.previous_latch,
    )
    return SafetyCalculation(
        soc=soc,
        charge_percent=int(round(max(0.0, min(1.0, soc)) * 100.0)),
        runtime_minutes=remaining,
        virtual_status=decision.virtual_status,
        modeled_lb=decision.modeled_lb,
        hard_floor_lb=decision.hard_floor_lb,
        virtual_lb_source=decision.next_latch.source,
        next_latch=decision.next_latch,
        event_class=inputs.blackout_kind,
    )


def make_safety_publication(
    observation: PhysicalObservation,
    calculation: SafetyCalculation,
) -> SafetyPublication:
    """Join physical diagnostics to an already completed model-only decision."""
    return SafetyPublication(
        virtual_status_token=calculation.virtual_status,
        raw_status=observation.raw_status,
        raw_lb_observed=raw_lb_observed(observation),
        event_class=calculation.event_class,
        modeled_runtime_minutes=calculation.runtime_minutes,
        modeled_lb=calculation.modeled_lb,
        hard_floor_lb=calculation.hard_floor_lb,
        virtual_lb_source=calculation.virtual_lb_source,
    )


def safety_decision(
    blackout_kind: BlackoutKind,
    runtime_minutes_value: float | None,
    shutdown_threshold_minutes: int,
    previous_latch: SafetyLatch,
) -> SafetyDecision:
    if blackout_kind == BlackoutKind.ONLINE:
        return SafetyDecision(NUT_STATUS_ONLINE, False, False, SafetyLatch())
    if previous_latch.virtual_lb:
        modeled_lb = previous_latch.source == VirtualLbSource.MODELED_THRESHOLD
        hard_floor_lb = previous_latch.source == VirtualLbSource.HARD_FLOOR
        return SafetyDecision(
            NUT_STATUS_LOW_BATTERY,
            modeled_lb,
            hard_floor_lb,
            previous_latch,
        )
    current = safety_policy.decide_unlatched_safety_status(
        blackout_kind,
        runtime_minutes_value,
        shutdown_threshold_minutes,
    )
    hard_floor_lb = current.hard_floor_lb
    modeled_lb = current.modeled_lb
    source = (
        VirtualLbSource.HARD_FLOOR
        if hard_floor_lb
        else VirtualLbSource.MODELED_THRESHOLD
        if modeled_lb
        else None
    )
    if source is not None:
        latch = SafetyLatch(virtual_lb=True, source=source)
        return SafetyDecision(NUT_STATUS_LOW_BATTERY, modeled_lb, hard_floor_lb, latch)
    return SafetyDecision(current.virtual_status, modeled_lb, hard_floor_lb, SafetyLatch())
