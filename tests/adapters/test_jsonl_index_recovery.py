"""Bounded index recovery and work-registry idempotency proofs."""

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.adapters.jsonl_errors import ProjectionUnavailableError
from src.adapters.jsonl_event_store import JsonlEventStore
from src.application.storage_values import EventRecord, EventStart, TerminalOutcomeRecord


class InjectedCrash(RuntimeError):
    """Simulated process death after one durable boundary."""


def _seal_event(store: JsonlEventStore, number: int) -> str:
    blackout_id = uuid.UUID(int=number + 1000, version=4).hex
    segment_id = uuid.UUID(int=number + 2000, version=4).hex
    started = datetime(2026, 8, 16, tzinfo=timezone.utc) + timedelta(minutes=number)
    start = started.isoformat(timespec="microseconds").replace("+00:00", "Z")
    ended = (
        (started + timedelta(seconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    handle = store.open(
        EventStart(
            blackout_id,
            segment_id,
            "boot-a",
            start,
            number * 10_000_000_000,
            {"battery_epoch_id": uuid.UUID(int=3, version=4).hex},
        )
    )
    handle = store.append(
        handle,
        EventRecord(
            "end",
            "boot-a",
            ended,
            number * 10_000_000_000 + 1_000_000_000,
            {"termination": "power_restored"},
            "physical",
        ),
    )
    store.seal(
        handle,
        TerminalOutcomeRecord(
            "boot-a",
            ended,
            number * 10_000_000_000 + 1_000_000_001,
            {"disposition": "recorded_only", "comparison_mode": "none", "duration_s": 1.0},
        ),
    )
    return blackout_id


def _rebuild_until_ready(store: JsonlEventStore, *, max_files: int = 1) -> None:
    for _ in range(256):
        if store.maintenance.rebuild_index_tick(max_files=max_files, max_bytes=4 * 1024 * 1024):
            return
    raise AssertionError("bounded rebuild did not become ready")


def test_merge_verification_rejects_equal_length_earlier_corruption(tmp_path: Path) -> None:
    armed = False

    def crash(stage: str) -> None:
        if armed and stage == "after_rebuild_merge_verify_cursor":
            raise InjectedCrash(stage)

    store = JsonlEventStore(tmp_path, fault_hook=crash)
    for number in range(3):
        _seal_event(store, number)
    (tmp_path / "events" / "index.jsonl").unlink()

    armed = True
    with pytest.raises(InjectedCrash):
        _rebuild_until_ready(store)
    store.close()

    merge_path = tmp_path / "events" / "index.rebuild.merged.jsonl"
    lines = merge_path.read_bytes().splitlines(keepends=True)
    assert len(lines) == 3
    assert len(lines[0]) == len(lines[1])
    merge_path.write_bytes(lines[1] + lines[0] + lines[2])

    with JsonlEventStore(tmp_path) as restarted:
        with pytest.raises(ProjectionUnavailableError, match="cumulative digest"):
            _rebuild_until_ready(restarted)


@pytest.mark.parametrize(
    "cleanup_stage",
    (
        "after_rebuild_cleanup_rebuild",
        "after_rebuild_cleanup_merge",
        "after_rebuild_cleanup_delta",
        "before_rebuild_cleanup_cursor",
    ),
)
def test_cleanup_cursor_is_last_and_restart_removes_orphans(
    tmp_path: Path, cleanup_stage: str
) -> None:
    armed = False

    def crash(stage: str) -> None:
        if armed and stage == cleanup_stage:
            raise InjectedCrash(stage)

    store = JsonlEventStore(tmp_path, fault_hook=crash)
    expected_id = _seal_event(store, 0)
    (tmp_path / "events" / "index.jsonl").unlink()
    _rebuild_until_ready(store, max_files=8)
    armed = True
    with pytest.raises(InjectedCrash, match=cleanup_stage):
        store.maintenance.promote_index_rebuild()
    store.close()

    with JsonlEventStore(tmp_path) as restarted:
        restarted.maintenance.promote_index_rebuild()
        assert restarted.index_tail(1)[0].blackout_id == expected_id
        events = tmp_path / "events"
        assert not (events / "index-rebuild.cursor.json").exists()
        assert not (events / "index.rebuild.in-progress.jsonl").exists()
        assert not (events / "index.rebuild.merged.jsonl").exists()
        assert not (events / "index-rebuild.delta.jsonl").exists()
        assert not (events / "index-rebuild.catalog.cursor.json").exists()


def test_cursorless_orphans_are_removed_only_with_authoritative_index(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        expected_id = _seal_event(store, 0)
        events = tmp_path / "events"
        for name in (
            "index.rebuild.in-progress.jsonl",
            "index.rebuild.merged.jsonl",
            "index-rebuild.delta.jsonl",
            "index-rebuild.catalog.cursor.json",
        ):
            (events / name).write_bytes(b"orphan\n")
        assert store.maintenance.rebuild_index_tick(max_files=1, max_bytes=4 * 1024 * 1024) is False
        assert store.index_tail(1)[0].blackout_id == expected_id
        assert not any(
            (events / name).exists()
            for name in (
                "index.rebuild.in-progress.jsonl",
                "index.rebuild.merged.jsonl",
                "index-rebuild.delta.jsonl",
                "index-rebuild.catalog.cursor.json",
            )
        )


def test_sealed_replay_does_not_scan_growing_index(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        expected_id = _seal_event(store, 0)
        events = tmp_path / "events"
        index_path = events / "index.jsonl"
        for number in range(1, 40):
            _seal_event(store, number)
        assert index_path.stat().st_size > 4 * 1024
        store._registry._remove_processing_or_capture_ref(blackout_id=expected_id)
        assert store.index_tail(1)[0].blackout_id != expected_id
