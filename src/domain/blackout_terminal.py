"""Immutable v3 terminal facts for one bounded blackout aggregate."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from src.domain.fragments import (
    ObservationOrigin,
)

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ID_BYTES = 128


class BlackoutTermination(StrEnum):
    """Closed physical reasons for ending one blackout aggregate."""

    POWER_RESTORED = "power_restored"
    SERVICE_STOP = "service_stop"
    CLOSED_RESTART_GAP = "closed_restart_gap"
    SAFE_SHUTDOWN_RESTARTED = "safe_shutdown_restarted"
    CAPTURE_DAMAGED = "capture_damaged"
    AGGREGATE_BUDGET_EXHAUSTED = "aggregate_budget_exhausted"


class BudgetKind(StrEnum):
    """The bounded resource that forced an aggregate rollover."""

    BYTES = "bytes"
    SEGMENT_REFS = "segment_refs"


class ContinuationKind(StrEnum):
    """How a terminal aggregate is linked to its successor."""

    SIZE_ROLLOVER = "size_rollover"
    REBOOT_GAP = "reboot_gap"


@dataclass(frozen=True, slots=True)
class BlackoutEnd:
    """A scoped, immutable terminal boundary for a v3 blackout chain."""

    blackout_id: str
    physical_episode_id: str
    battery_epoch_id: str
    segment_id: str
    termination: BlackoutTermination
    observation_origin: ObservationOrigin
    wall_time_utc: datetime
    monotonic_ns: int
    boot_id: str
    terminal_anchor_record_hash: str | None = None
    budget_kind: BudgetKind | None = None
    continued_by: str | None = None
    continuation_kind: ContinuationKind | None = None
    uat_intent_id: str | None = None

    def __post_init__(self) -> None:
        _validate_ids(self)
        _validate_utc(self.wall_time_utc)
        _validate_nonnegative(self.monotonic_ns, "terminal monotonic time")
        _validate_enum(self.termination, BlackoutTermination, "termination")
        _validate_enum(self.observation_origin, ObservationOrigin, "observation origin")
        _validate_optional_hash(self.terminal_anchor_record_hash, "terminal anchor record hash")
        _validate_enum(self.budget_kind, BudgetKind, "budget kind", optional=True)
        _validate_enum(self.continuation_kind, ContinuationKind, "continuation kind", optional=True)
        _validate_optional_id(self.continued_by, "continued-by blackout ID")
        _validate_optional_id(self.uat_intent_id, "UAT intent ID")
        _validate_terminal_shape(self)


def _validate_ids(value: BlackoutEnd) -> None:
    for item, name in (
        (value.blackout_id, "blackout ID"),
        (value.physical_episode_id, "physical episode ID"),
        (value.battery_epoch_id, "battery epoch ID"),
        (value.segment_id, "segment ID"),
        (value.boot_id, "terminal boot ID"),
    ):
        _validate_id(item, name)


def _validate_terminal_shape(value: BlackoutEnd) -> None:
    _validate_origin_shape(value)
    _validate_budget_shape(value)
    _validate_successor_shape(value)


def _validate_origin_shape(value: BlackoutEnd) -> None:
    if value.observation_origin is ObservationOrigin.UAT and not value.uat_intent_id:
        raise ValueError("UAT blackout end requires intent ID")
    if value.observation_origin is not ObservationOrigin.UAT and value.uat_intent_id is not None:
        raise ValueError("UAT intent ID is only valid for UAT origin")


def _validate_budget_shape(value: BlackoutEnd) -> None:
    budgeted = value.termination is BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED
    if budgeted != (value.budget_kind is not None):
        raise ValueError("only budget-exhausted ends may carry a budget kind")
    has_anchor = value.terminal_anchor_record_hash is not None
    if budgeted == has_anchor:
        raise ValueError(
            "aggregate budget exhaustion is the only terminal matrix case without an anchor"
        )


def _validate_successor_shape(value: BlackoutEnd) -> None:
    budgeted = value.termination is BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED
    linked = value.continued_by is not None
    if linked != (value.continuation_kind is not None):
        raise ValueError("continued-by and continuation kind must be supplied together")
    if value.termination is BlackoutTermination.CLOSED_RESTART_GAP:
        if value.continuation_kind is not ContinuationKind.REBOOT_GAP:
            raise ValueError("closed restart gap requires a reboot-gap successor")
    elif value.continuation_kind is ContinuationKind.REBOOT_GAP:
        raise ValueError("reboot-gap continuation requires a closed restart gap")
    if budgeted:
        if value.continuation_kind is not ContinuationKind.SIZE_ROLLOVER:
            raise ValueError("budget exhaustion requires a size-rollover successor")
    elif value.continuation_kind is ContinuationKind.SIZE_ROLLOVER:
        raise ValueError("size rollover requires aggregate budget exhaustion")


def _validate_id(value: object, field: str) -> None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > _MAX_ID_BYTES:
        raise ValueError(f"{field} must be a non-empty UTF-8 ID of at most 128 bytes")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} must not contain control characters")


def _validate_optional_id(value: object, field: str) -> None:
    if value is not None:
        _validate_id(value, field)


def _validate_utc(value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("terminal wall time must be timezone-aware UTC")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("terminal wall time must be UTC")


def _validate_nonnegative(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")


def _validate_optional_hash(value: object, field: str) -> None:
    if value is not None and (not isinstance(value, str) or _HASH_RE.fullmatch(value) is None):
        raise ValueError(f"{field} must be lowercase SHA-256 or None")


def _validate_enum(
    value: object, enum_type: type[StrEnum], field: str, *, optional: bool = False
) -> None:
    if optional and value is None:
        return
    if not isinstance(value, enum_type):
        raise TypeError(f"{field} must be {enum_type.__name__}")
