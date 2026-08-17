"""Bounded reporting queries and immutable maintenance requests."""

from dataclasses import dataclass, replace

from src.application.ports import ReportingEventStorePort
from src.application.storage_values import EventSummary, StorageHealth

MAX_REPORT_EVENTS = 32
MAX_REBUILD_FILES = 32
MAX_REBUILD_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class IndexMaintenanceRequest:
    max_files: int = MAX_REBUILD_FILES
    max_bytes: int = MAX_REBUILD_BYTES


@dataclass(frozen=True, slots=True)
class ReportingSnapshot:
    health: StorageHealth
    events: tuple[EventSummary, ...]
    maintenance: IndexMaintenanceRequest | None


def reporting_tick(
    store: ReportingEventStorePort,
    *,
    event_limit: int = MAX_REPORT_EVENTS,
    queued_observations: int | None = None,
    consumed_step_budget_remaining: int | None = None,
) -> ReportingSnapshot:
    """Read one bounded reporting view; a writer may execute the returned request."""
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
    events = store.index_tail(event_limit) if health.index_available and event_limit else ()
    maintenance = None if health.index_available else IndexMaintenanceRequest()
    return ReportingSnapshot(health, events, maintenance)
