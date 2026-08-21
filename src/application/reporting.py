"""Bounded reporting queries over sealed event history."""

from dataclasses import dataclass, replace

from src.application.ports import ReportingEventStorePort
from src.application.storage_values import EventSummary, StorageHealth

MAX_REPORT_EVENTS = 32


@dataclass(frozen=True, slots=True)
class ReportingSnapshot:
    health: StorageHealth
    events: tuple[EventSummary, ...]


def reporting_tick(
    store: ReportingEventStorePort,
    *,
    event_limit: int = MAX_REPORT_EVENTS,
    queued_observations: int | None = None,
    consumed_step_budget_remaining: int | None = None,
) -> ReportingSnapshot:
    """Read one bounded newest-first reporting view."""
    if not 0 <= event_limit <= MAX_REPORT_EVENTS:
        raise ValueError(f"event_limit must be between 0 and {MAX_REPORT_EVENTS}")
    health = store.storage_health(
        queued_observations=queued_observations,
        consumed_step_budget_remaining=consumed_step_budget_remaining,
    )
    if health.active_phase is None and (queued_observations is None or queued_observations == 0):
        # The adapter tracks the most recent fsync age, while this projection
        # reports accepted-but-undurable work.  A sealed idle store has none.
        health = replace(health, durability_lag_s=0.0)
    events = store.history_tail(event_limit) if event_limit else ()
    return ReportingSnapshot(health, events)
