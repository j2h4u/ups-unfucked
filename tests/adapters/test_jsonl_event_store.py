"""Direct sealed-event history and terminal outbox checks."""

import os
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.adapters.jsonl_errors import EventCorruptionError, EventPathError
from src.adapters.jsonl_event_store import JsonlEventStore
from src.application.storage_values import EventRecord, EventStart, TerminalOutcomeRecord


def _event(
    number: int, epoch: str
) -> tuple[str, str, EventStart, EventRecord, TerminalOutcomeRecord]:
    blackout_id = uuid.UUID(int=number + 100, version=4).hex
    segment_id = uuid.UUID(int=number + 10_000, version=4).hex
    started = datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(minutes=number)
    start = started.isoformat(timespec="microseconds").replace("+00:00", "Z")
    ended = (
        (started + timedelta(seconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    return (
        blackout_id,
        segment_id,
        EventStart(
            blackout_id,
            segment_id,
            "boot",
            start,
            number * 10_000_000_000,
            {"battery_epoch_id": epoch},
        ),
        EventRecord(
            "end",
            "boot",
            ended,
            number * 10_000_000_000 + 1_000_000_000,
            {"termination": "restored"},
            "physical",
        ),
        TerminalOutcomeRecord(
            "boot",
            ended,
            number * 10_000_000_000 + 2_000_000_000,
            {"disposition": "recorded_only", "comparison_mode": "none"},
        ),
    )


def _seal(store: JsonlEventStore, number: int, epoch: str) -> str:
    blackout_id, _segment_id, start, end, outcome = _event(number, epoch)
    handle = store.open(start)
    handle = store.append(handle, end)
    store.seal(handle, outcome)
    return blackout_id


def test_history_directly_projects_sealed_events_newest_first_and_filters_epoch(
    tmp_path: Path,
) -> None:
    epoch_a = uuid.UUID(int=3, version=4).hex
    epoch_b = uuid.UUID(int=4, version=4).hex
    with JsonlEventStore(tmp_path) as store:
        first = _seal(store, 1, epoch_a)
        second = _seal(store, 2, epoch_b)
        third = _seal(store, 3, epoch_a)
        (tmp_path / "events" / "notes.txt").write_text("ignored", encoding="utf-8")

        history = store.history_tail(32)
        epoch = store.history_tail_for_epoch(epoch_a, 32)
        scan = store.history_scan_for_epoch(epoch_a)

    assert [item.blackout_id for item in history] == [third, second, first]
    assert [item.blackout_id for item in epoch.summaries] == [third, first]
    assert epoch.overflow_count == 0
    assert scan.scan_complete is True
    assert [item.blackout_id for item in scan.summaries] == [third, first]


def test_history_ignores_active_evt_until_sealed(tmp_path: Path) -> None:
    epoch = uuid.UUID(int=3, version=4).hex
    with JsonlEventStore(tmp_path) as store:
        _blackout_id, _segment_id, start, _end, _outcome = _event(1, epoch)
        store.open(start)
        assert store.history_tail(32) == ()


def test_history_only_ignores_registry_active_writable_event(tmp_path: Path) -> None:
    epoch = uuid.UUID(int=3, version=4).hex
    with JsonlEventStore(tmp_path) as store:
        _blackout_id, _segment_id, start, _end, _outcome = _event(1, epoch)
        store.open(start)
        assert store.history_tail(32) == ()
        orphan_id = uuid.UUID(int=999, version=4).hex
        orphan = tmp_path / "events" / f"evt-20260801T000000.000Z-{orphan_id}.jsonl"
        orphan.write_bytes(b"not-json\n")
        os.chmod(orphan, stat.S_IRUSR | stat.S_IWUSR)
        with pytest.raises(EventCorruptionError, match="unexpected permissions"):
            store.history_tail(32)


@pytest.mark.parametrize("kind", ["corrupt", "symlink"])
def test_history_fails_closed_for_owned_evt_path_damage(tmp_path: Path, kind: str) -> None:
    (tmp_path / "events").mkdir(parents=True)
    os.chmod(tmp_path / "events", stat.S_IRWXU)
    blackout_id = uuid.UUID(int=101, version=4).hex
    event_path = tmp_path / "events" / f"evt-20260801T000000.000Z-{blackout_id}.jsonl"
    if kind == "corrupt":
        event_path.write_bytes(b"not-json\n")
        os.chmod(event_path, stat.S_IRUSR)
    else:
        target = tmp_path / "target"
        target.write_bytes(b"not-json\n")
        event_path.symlink_to(target)
    with JsonlEventStore(tmp_path) as store:
        with pytest.raises((EventCorruptionError, EventPathError)):
            store.history_tail(1)


def test_seal_publishes_one_fifo_report_notice_without_history_projection(tmp_path: Path) -> None:
    epoch = uuid.UUID(int=3, version=4).hex
    with JsonlEventStore(tmp_path) as store:
        blackout_id = _seal(store, 1, epoch)
        pending = store.report_outbox.report_outbox_pending(8)

    assert len(pending) == 1
    assert pending[0].blackout_id == blackout_id
    assert not (tmp_path / "events" / "index.jsonl").exists()
    assert not (tmp_path / "events" / "event-catalog.jsonl").exists()
