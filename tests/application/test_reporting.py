"""Bounded reporting orchestration tests."""

import pytest

from src.application.reporting import MAX_REBUILD_BYTES, MAX_REBUILD_FILES, reporting_tick
from src.application.storage_values import StorageHealth


class ReportingStore:
    def __init__(self, *, index_available: bool, durability_lag_s: float = 0.0) -> None:
        self.index_available = index_available
        self.durability_lag_s = durability_lag_s
        self.tail_limits: list[int] = []

    def storage_health(self, *, queued_observations=None, consumed_step_budget_remaining=None):
        return StorageHealth(
            capture_available=True,
            active_phase=None,
            queued_observations=queued_observations,
            durability_lag_s=self.durability_lag_s,
            index_available=self.index_available,
            rebuild_generation=None,
            rebuild_in_progress=not self.index_available,
            rebuild_files_done=0,
            rebuild_files_target=0,
            rebuild_files_remaining=0,
            rebuild_last_progress_utc=None,
            rebuild_stalled=False,
            consumed_step_budget_remaining=consumed_step_budget_remaining,
            event_count=0,
            total_bytes=0,
            free_bytes=1,
            alarm=None,
            bounded_error=None,
        )

    def index_tail(self, limit):
        self.tail_limits.append(limit)
        return ()


def test_idle_sealed_storage_has_no_durability_lag() -> None:
    store = ReportingStore(index_available=True, durability_lag_s=999.0)

    snapshot = reporting_tick(store)

    assert snapshot.health.durability_lag_s == 0.0


def test_reporting_query_is_bounded_to_32_events():
    store = ReportingStore(index_available=True)

    snapshot = reporting_tick(store, event_limit=32, consumed_step_budget_remaining=200)

    assert store.tail_limits == [32]
    assert snapshot.maintenance is None
    assert snapshot.health.consumed_step_budget_remaining == 200
    with pytest.raises(ValueError, match="between 0 and 32"):
        reporting_tick(store, event_limit=33)


def test_unavailable_index_returns_bounded_writer_request_without_scanning():
    store = ReportingStore(index_available=False)

    snapshot = reporting_tick(store)

    assert store.tail_limits == []
    assert snapshot.events == ()
    assert snapshot.maintenance is not None
    assert snapshot.maintenance.max_files == MAX_REBUILD_FILES
    assert snapshot.maintenance.max_bytes == MAX_REBUILD_BYTES
