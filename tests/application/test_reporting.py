"""Bounded reporting orchestration tests."""

import pytest

from src.application.reporting import reporting_tick
from src.application.storage_values import StorageHealth


class ReportingStore:
    def __init__(self, *, durability_lag_s: float = 0.0) -> None:
        self.durability_lag_s = durability_lag_s
        self.tail_limits: list[int] = []

    def storage_health(self, *, queued_observations=None, consumed_step_budget_remaining=None):
        return StorageHealth(
            capture_available=True,
            active_phase=None,
            queued_observations=queued_observations,
            durability_lag_s=self.durability_lag_s,
            consumed_step_budget_remaining=consumed_step_budget_remaining,
            event_count=0,
            total_bytes=0,
            free_bytes=1,
            alarm=None,
            bounded_error=None,
        )

    def history_tail(self, limit):
        self.tail_limits.append(limit)
        return ()


def test_idle_sealed_storage_has_no_durability_lag() -> None:
    store = ReportingStore(durability_lag_s=999.0)

    snapshot = reporting_tick(store)

    assert snapshot.health.durability_lag_s == 0.0


def test_reporting_query_is_bounded_to_32_events():
    store = ReportingStore()

    snapshot = reporting_tick(store, event_limit=32, consumed_step_budget_remaining=200)

    assert store.tail_limits == [32]
    assert snapshot.health.consumed_step_budget_remaining == 200
    with pytest.raises(ValueError, match="between 0 and 32"):
        reporting_tick(store, event_limit=33)


def test_reporting_reads_history_when_storage_is_available():
    store = ReportingStore()

    snapshot = reporting_tick(store)

    assert store.tail_limits == [32]
    assert snapshot.events == ()
