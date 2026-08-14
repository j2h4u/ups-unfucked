"""Focused durability and replay tests for the discharge journal."""

import json
import logging
import os
import stat

import pytest

from src.discharge_journal import (
    DischargeJournal,
    EventCursor,
    JournalCorruptionError,
    JournalEnd,
    JournalError,
    JournalPathError,
    JournalSample,
    JournalStart,
)


def _start(journal: DischargeJournal):
    return journal.start_event(JournalStart({"status": "OB DISCHRG", "input_voltage": 0}))


def test_append_and_replay_projection(tmp_path):
    path = tmp_path / "state" / "discharge-events-v1.jsonl"
    journal = DischargeJournal(path, boot_id="boot-a")
    cursor = _start(journal)
    cursor = journal.append_sample(cursor, JournalSample({"voltage": 12.5, "load": 20.0}))
    journal.close_event(cursor, JournalEnd({"reason": "power_restored"}))

    projection = journal.replay()
    assert projection.open_event_id is None
    assert len(projection.events) == 1
    event = next(iter(projection.events.values()))
    assert [record.seq for record in event.records] == [0, 1, 2]
    assert event.samples[0].payload["voltage"] == 12.5
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    journal.close()


def test_close_reopen_preserves_exact_single_projection_bytes(tmp_path):
    path = tmp_path / "state" / "discharge-events-v1.jsonl"
    journal = DischargeJournal(path, boot_id="boot-a")
    cursor = _start(journal)
    cursor = journal.append_sample(cursor, JournalSample({"voltage": 12.5}))
    journal.close_event(cursor, JournalEnd({"reason": "shutdown_requested"}))
    journal.close()
    persisted = path.read_bytes()
    assert persisted.count(b"\n") == 3

    reopened = DischargeJournal(path, boot_id="boot-b")
    projection = reopened.replay()
    assert path.read_bytes() == persisted
    event = next(iter(projection.events.values()))
    assert [record.seq for record in event.records] == [0, 1, 2]
    assert [record.record_type for record in projection.records] == ["start", "sample", "end"]
    reopened.close()


def test_closed_applied_record_is_replayable_and_idempotent(tmp_path):
    path = tmp_path / "journal"
    journal = DischargeJournal(path, boot_id="boot-a")
    cursor = _start(journal)
    journal.close_event(cursor, JournalEnd({"reason": "power_restored"}))
    event_id = next(iter(journal.replay().events))

    journal.mark_applied(event_id, "hash-a", "recorded_only")
    journal.mark_applied(event_id, "hash-a", "recorded_only")

    event = journal.replay().events[event_id]
    assert event.applied is not None
    assert event.applied.seq == 2
    assert event.applied.payload["model_hash"] == "hash-a"
    assert event.applied.payload["disposition"] == "recorded_only"
    with pytest.raises(JournalError):
        journal.mark_applied(event_id, "hash-b", "recorded_only")
    with pytest.raises(JournalError):
        journal.mark_applied(event_id, "hash-a", "applied")


def test_historical_marker_without_disposition_is_not_rewritten(tmp_path):
    """An old terminal marker remains raw evidence under the strict new API."""
    journal = DischargeJournal(tmp_path / "journal", boot_id="boot-a")
    cursor = _start(journal)
    journal.close_event(cursor, JournalEnd({"reason": "power_restored"}))
    event_id = next(iter(journal.replay().events))
    journal._append(
        EventCursor(event_id, 2, "boot-a", True),
        "applied",
        {"model_hash": "hash-a"},
    )
    before = journal.path.read_bytes()

    with pytest.raises(JournalError):
        journal.mark_applied(event_id, "hash-a", "recorded_only")

    assert journal.path.read_bytes() == before
    assert "disposition" not in journal.replay().events[event_id].applied.payload


def test_observed_duration_uses_closed_end_duration_for_one_sample_event(tmp_path):
    """A short closed event retains its monotonic end duration even with one sample."""
    journal = DischargeJournal(tmp_path / "journal", boot_id="boot-a")
    cursor = _start(journal)
    cursor = journal.append_sample(cursor, JournalSample({"timestamp": 100.0}))
    journal.close_event(cursor, JournalEnd({"observed_duration_sec": 1.25}))
    event_id = next(iter(journal.replay().events))

    assert journal.observed_duration(event_id) == pytest.approx(1.25)


def test_observed_duration_uses_per_boot_spans_for_gapped_event(tmp_path):
    """Reboot-gapped events remain duration sums, not the end marker estimate."""
    journal = DischargeJournal(tmp_path / "journal", boot_id="boot-a")
    cursor = _start(journal)
    cursor = journal.append_sample(cursor, JournalSample({"timestamp": 100.0}))
    cursor = journal.append_sample(cursor, JournalSample({"timestamp": 110.0}))
    cursor = journal.append_reboot_gap(cursor, new_boot_id="boot-b")
    cursor = journal.append_sample(cursor, JournalSample({"timestamp": 200.0}))
    cursor = journal.append_sample(cursor, JournalSample({"timestamp": 215.0}))
    journal.close_event(cursor, JournalEnd({"observed_duration_sec": 999.0}))
    event_id = next(iter(journal.replay().events))

    assert journal.observed_duration(event_id) == pytest.approx(25.0)


def test_open_event_cannot_be_marked_applied(tmp_path):
    journal = DischargeJournal(tmp_path / "journal")
    event_id = _start(journal).event_id
    with pytest.raises(JournalError, match="still open"):
        journal.mark_applied(event_id, "hash-a", "recorded_only")


def test_rejects_symlink_and_non_regular_target(tmp_path):
    target = tmp_path / "target"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "journal"
    link.symlink_to(target)
    with pytest.raises(JournalPathError):
        DischargeJournal(link)

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(JournalPathError):
        DischargeJournal(directory)


def test_rejects_duplicate_or_non_monotonic_sequence(tmp_path):
    path = tmp_path / "journal"
    journal = DischargeJournal(path, boot_id="boot-a")
    cursor = _start(journal)
    journal.append_sample(cursor, JournalSample({"n": 1}))
    journal.close()
    with path.open("a", encoding="utf-8") as stream:
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[1])
        stream.write(json.dumps(record) + "\n")
    # Constructor replay is intentionally fail-closed; this direct invocation
    # keeps the test focused on the persisted duplicate.
    with pytest.raises(JournalCorruptionError):
        DischargeJournal(path)


def test_torn_final_line_is_recovered_with_warning(tmp_path, caplog):
    path = tmp_path / "journal"
    journal = DischargeJournal(path, boot_id="boot-a")
    _start(journal)
    journal.close()
    with path.open("ab") as stream:
        stream.write(b'{"schema_version":1,"record_type":"sample"')
    with caplog.at_level(logging.WARNING):
        reopened = DischargeJournal(path, boot_id="boot-b")
    projection = reopened.replay()
    assert projection.torn_tail_recovered
    assert len(projection.records) == 1
    assert "torn final" in caplog.text


def test_complete_json_without_newline_is_torn_tail(tmp_path, caplog):
    path = tmp_path / "journal"
    journal = DischargeJournal(path, boot_id="boot-a")
    _start(journal)
    journal.close()
    record = {
        "schema_version": 1,
        "record_type": "sample",
        "event_id": "tail-event",
        "seq": 0,
        "boot_id": "boot-a",
        "wall_time_utc": "2026-08-14T00:00:00Z",
        "monotonic_ns": 1,
        "payload": {},
    }
    with path.open("ab") as stream:
        stream.write(json.dumps(record).encode("utf-8"))
    with caplog.at_level(logging.WARNING):
        reopened = DischargeJournal(path, boot_id="boot-b")
    projection = reopened.replay()
    assert projection.torn_tail_recovered
    assert len(projection.records) == 1
    assert "unterminated final" in caplog.text


def test_torn_tail_is_truncated_before_append_and_reopen(tmp_path):
    """Recovery persists the valid prefix so a later append remains valid JSONL."""
    path = tmp_path / "journal"
    journal = DischargeJournal(path, boot_id="boot-a")
    _start(journal)
    journal.close()
    with path.open("ab") as stream:
        stream.write(b'{"schema_version":1,"record_type":"sample"')

    reopened = DischargeJournal(path, boot_id="boot-b")
    assert path.read_bytes().endswith(b"\n")
    projection = reopened.replay()
    event_id = projection.open_event_id
    assert event_id is not None
    cursor = reopened.resume_event(event_id)
    cursor = reopened.append_sample(cursor, JournalSample({"voltage": 12.4}))
    reopened.close_event(cursor, JournalEnd({"reason": "power_restored"}))
    reopened.close()

    final = DischargeJournal(path, boot_id="boot-c")
    event = next(iter(final.replay().events.values()))
    assert [record.record_type for record in event.records] == ["start", "sample", "end"]
    final.close()


def test_unknown_schema_is_not_treated_as_torn_tail(tmp_path):
    path = tmp_path / "journal"
    journal = DischargeJournal(path)
    journal.close()
    record = {
        "schema_version": 99,
        "record_type": "start",
        "event_id": "event",
        "seq": 0,
        "boot_id": "boot",
        "wall_time_utc": "2026-08-14T00:00:00Z",
        "monotonic_ns": 1,
        "payload": {},
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(JournalCorruptionError):
        DischargeJournal(path)


def test_initial_replay_failure_closes_descriptor(tmp_path):
    path = tmp_path / "journal"
    journal = DischargeJournal(path)
    journal.close()
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(JournalCorruptionError):
        DischargeJournal(path)

    open_targets = []
    for fd_name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd_name}")
        except FileNotFoundError:
            continue
        if target == str(path):
            open_targets.append(fd_name)
    assert not open_targets


def test_middle_corruption_is_hard_error_and_unhealthy(tmp_path):
    path = tmp_path / "journal"
    journal = DischargeJournal(path, boot_id="boot-a")
    cursor = _start(journal)
    journal.append_sample(cursor, JournalSample({"n": 1}))
    journal.close()
    with path.open("ab") as stream:
        stream.write(b"not-json\n")
        stream.write(b"{}\n")
    with pytest.raises(JournalCorruptionError):
        DischargeJournal(path)


def test_reboot_gap_is_explicit_and_does_not_change_event_id(tmp_path):
    path = tmp_path / "journal"
    journal = DischargeJournal(path, boot_id="boot-a")
    cursor = _start(journal)
    cursor = journal.append_sample(cursor, JournalSample({"voltage": 12.4}))
    cursor = journal.append_reboot_gap(cursor, new_boot_id="boot-b")
    journal.append_sample(cursor, JournalSample({"voltage": 12.1}))
    projection = journal.replay()
    event = next(iter(projection.events.values()))
    assert len(event.reboot_gaps) == 1
    assert event.reboot_gaps[0].payload["from_boot_id"] == "boot-a"
    assert event.reboot_gaps[0].payload["to_boot_id"] == "boot-b"
    assert {sample.boot_id for sample in event.samples} == {"boot-a", "boot-b"}


def test_sync_calls_are_explicit(tmp_path, monkeypatch):
    path = tmp_path / "journal"
    calls: list[int] = []
    original = os.fdatasync
    monkeypatch.setattr(os, "fdatasync", lambda fd: calls.append(fd) or original(fd))
    journal = DischargeJournal(path)
    cursor = _start(journal)
    journal.append_sample(cursor, JournalSample({"ok": True}))
    assert len(calls) >= 3  # creation plus start and sample


def test_degraded_health_is_sticky_across_successful_replay(tmp_path):
    journal = DischargeJournal(tmp_path / "journal")
    _start(journal)
    journal.mark_degraded("persistence warning")

    projection = journal.replay()

    assert projection.open_event_id is not None
    assert journal.health.healthy is False
    assert journal.health.last_error == "persistence warning"
    with pytest.raises(JournalCorruptionError, match="persistence warning"):
        journal.append_sample(
            journal.resume_event(projection.open_event_id), JournalSample({"blocked": True})
        )
    with pytest.raises(JournalCorruptionError, match="persistence warning"):
        journal.start_event(JournalStart({"blocked": True}))


def test_total_byte_cap_rejects_before_read(tmp_path, monkeypatch):
    path = tmp_path / "journal"
    cap = 4096
    path.touch()
    with path.open("r+b") as stream:
        stream.truncate(cap + 1)

    read_called = False

    def unexpected_read(_fd, _count):
        nonlocal read_called
        read_called = True
        raise AssertionError("oversized journal was read")

    monkeypatch.setattr(os, "read", unexpected_read)
    with pytest.raises(JournalCorruptionError, match="total byte size"):
        DischargeJournal(path, max_line_bytes=256, max_bytes=cap)
    assert read_called is False
