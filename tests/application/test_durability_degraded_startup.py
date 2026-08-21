"""Degraded-startup recovery keeps safety alive without inventing evidence."""

from pathlib import Path

import pytest

from src.adapters.jsonl_event_store import JsonlEventStore
from src.application.degraded_startup import (
    DeferredEventStore,
    DegradedEventStore,
    EventStorageUnavailable,
)


class _Delegate:
    """Minimal activated delegate for the forwarding boundary."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_degraded_store_rejects_all_scientific_mutations_and_queries_empty() -> None:
    store = DegradedEventStore("registry\ncontains\tcorruption")

    assert store.work_registry().capture is None
    assert store.work_registry().pending_processing == ()
    assert store.history_tail(32) == ()
    assert not store.history_scan_for_epoch("epoch").scan_complete
    assert store.storage_health().alarm == "startup_degraded"
    assert "registry contains corruption" == store.reason

    with pytest.raises(EventStorageUnavailable, match="startup_degraded"):
        store.recover_startup()


def test_deferred_store_activates_real_store_after_startup_recovery_failure(
    tmp_path: Path,
) -> None:
    deferred = DeferredEventStore("active registry is torn")
    assert not deferred.storage_health().capture_available

    real_store = JsonlEventStore(tmp_path)
    deferred.activate(real_store)
    deferred.degrade("late stale error")

    assert deferred.storage_health().capture_available
    assert deferred.work_registry().capture is None
    deferred.close()
    assert real_store._owned_lock_fd is None


def test_deferred_store_activation_is_idempotent_but_cannot_switch_delegate() -> None:
    deferred = DeferredEventStore("storage unavailable")
    first = _Delegate()
    second = _Delegate()

    deferred.activate(first)  # type: ignore[arg-type]
    deferred.activate(first)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="already activated"):
        deferred.activate(second)  # type: ignore[arg-type]
    deferred.degrade("must not replace active delegate")
    deferred.close()
    assert first.closed
    assert not second.closed
