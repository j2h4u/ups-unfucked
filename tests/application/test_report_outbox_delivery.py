"""Durable report-delivery retries and metric-specific history cohorts."""

from __future__ import annotations

import errno
import os
import uuid
from pathlib import Path
from threading import Event, Thread

import pytest

from src.adapters.jsonl_errors import (
    EventConflictError,
    EventCorruptionError,
    EventPathError,
    EventPersistenceError,
)
from src.adapters.jsonl_event_store import JsonlEventStore
from src.adapters.jsonl_report_outbox import JsonlReportOutbox, ReportNotice
from src.application import decline_reporting
from src.application.storage_values import ReportNoticeIdentity


def _segment_token(number: int, blackout_id: str) -> str:
    return f"evt-20260816T00{number // 60:02d}{number % 60:02d}.000Z-{blackout_id}.jsonl"


class _FailingSink:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.delivered: list[tuple[str, str, str]] = []

    def publish_notice(self, notice: ReportNotice) -> None:
        if self.failures:
            self.failures -= 1
            raise OSError("injected report sink outage")
        self.delivered.append(notice.identity)


def _is_report_outbox_fd(fd: int) -> bool:
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}")).name == "report-outbox.jsonl"
    except OSError:
        return False


def _notice_values(number: int = 0) -> dict[str, str]:
    blackout_id = uuid.UUID(int=number + 1000, version=4).hex
    return {
        "blackout_id": blackout_id,
        "segment_filename": _segment_token(number, blackout_id),
        "locator_sha256": f"{number + 1:064x}",
        "index_head_sha256": f"{number + 1001:064x}",
    }


def _identity(notice: ReportNotice) -> ReportNoticeIdentity:
    return ReportNoticeIdentity(notice.blackout_id, notice.segment_filename, notice.locator_sha256)


def _assert_fds_closed(fds: list[int]) -> None:
    assert fds
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


class _OutboxRace:
    def __init__(
        self, outbox: JsonlReportOutbox, first: ReportNotice, values: dict[str, str]
    ) -> None:
        self.outbox = outbox
        self.first = first
        self.values = values
        self.real_write = os.write
        self.append_entered = Event()
        self.release_append = Event()
        self.pending_started = Event()
        self.pending_finished = Event()
        self.append_errors: list[BaseException] = []
        self.pending_errors: list[BaseException] = []
        self.pending_results: list[tuple[ReportNotice, ...]] = []

    def paused_partial_append(self, fd: int, raw: bytes) -> None:
        self.real_write(fd, raw[:5])
        self.append_entered.set()
        if not self.release_append.wait(5):
            raise AssertionError("append pause was not released")
        raise EventPersistenceError("injected paused partial append")

    def append_worker(self) -> None:
        try:
            self.outbox.append(**self.values)
        except BaseException as exc:  # pragma: no cover - assertion below
            self.append_errors.append(exc)

    def pending_ack_worker(self) -> None:
        self.pending_started.set()
        try:
            self.pending_results.append(self.outbox.pending(limit=2))
            self.outbox.acknowledge(self.first)
        except BaseException as exc:  # pragma: no cover - assertion below
            self.pending_errors.append(exc)
        finally:
            self.pending_finished.set()


def test_report_outbox_survives_twenty_failures_and_restart_without_loss(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events"
    events.mkdir(mode=0o700)
    expected: list[tuple[str, str, str]] = []
    outbox = JsonlReportOutbox(events)
    for number in range(20):
        blackout_id = uuid.UUID(int=number + 100, version=4).hex
        segment = _segment_token(number, blackout_id)
        locator = f"{number + 1:064x}"
        index_head = f"{number + 101:064x}"
        notice = outbox.append(
            blackout_id=blackout_id,
            segment_filename=segment,
            locator_sha256=locator,
            index_head_sha256=index_head,
        )
        expected.append(notice.identity)

    with JsonlEventStore(tmp_path) as store:
        pending = store.report_outbox.report_outbox_pending(20)
        assert tuple(
            (item.blackout_id, item.segment_filename, item.locator_sha256) for item in pending
        ) == tuple(expected)

    sink = _FailingSink(20)
    for _ in range(20):
        restarted = JsonlReportOutbox(events)
        with pytest.raises(OSError, match="sink outage"):
            sink.publish_notice(restarted.pending(limit=20)[0])
        assert restarted.pending(limit=20)[0].identity == expected[0]

    restarted = JsonlReportOutbox(events)
    delivered = 0
    for notice in restarted.pending(limit=20):
        sink.publish_notice(notice)
        restarted.acknowledge(notice)
        delivered += 1
    assert delivered == 20
    assert sink.delivered == expected
    assert restarted.pending(limit=20) == ()
    assert JsonlReportOutbox(events).pending(limit=20) == ()


def test_report_outbox_acknowledges_exact_fifo_head_without_history_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = tmp_path / "events"
    events.mkdir(mode=0o700)
    outbox = JsonlReportOutbox(events)
    first = outbox.append(**_notice_values())
    second = outbox.append(**_notice_values(1))
    assert outbox.head() == first
    real_iter_from = outbox._iter_from

    def fail_history_scan(_offset: int):
        raise AssertionError("acknowledgement must not scan report history")

    monkeypatch.setattr(outbox, "_iter_from", fail_history_scan)
    outbox.acknowledge(first)
    monkeypatch.setattr(outbox, "_iter_from", real_iter_from)

    assert outbox.pending(limit=2) == (second,)


def test_event_store_report_facade_preserves_fifo_conflicts_and_restart_head(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events"
    events.mkdir(mode=0o700)
    seeded = JsonlReportOutbox(events)
    notices = tuple(seeded.append(**_notice_values(number)) for number in range(3))
    cursor_path = events / "report-outbox.cursor.json"

    with JsonlEventStore(tmp_path) as store:
        facade = store.report_outbox
        assert facade.report_outbox_pending(3) == tuple(_identity(item) for item in notices)
        before = cursor_path.read_bytes() if cursor_path.exists() else None
        with pytest.raises(EventConflictError, match="not pending"):
            facade.acknowledge_report_notice(_identity(notices[1]))
        assert (cursor_path.read_bytes() if cursor_path.exists() else None) == before

        facade.acknowledge_report_notice(_identity(notices[0]))
        after_first = cursor_path.read_bytes()
        with pytest.raises(EventConflictError, match="not pending"):
            facade.acknowledge_report_notice(_identity(notices[0]))
        assert cursor_path.read_bytes() == after_first
        unknown = ReportNoticeIdentity(
            notices[0].blackout_id,
            notices[0].segment_filename,
            f"{999:064x}",
        )
        with pytest.raises(EventConflictError, match="not pending"):
            facade.acknowledge_report_notice(unknown)
        assert cursor_path.read_bytes() == after_first

        facade.acknowledge_report_notice(_identity(notices[1]))
        assert facade.report_outbox_pending(3) == (_identity(notices[2]),)

    with JsonlEventStore(tmp_path) as restarted:
        assert restarted.report_outbox.report_outbox_pending(3) == (_identity(notices[2]),)
        restarted.report_outbox.acknowledge_report_notice(_identity(notices[2]))
        assert restarted.report_outbox.report_outbox_pending(3) == ()


def test_report_outbox_closes_fds_after_pending_break_and_decode_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = tmp_path / "events"
    events.mkdir(mode=0o700)
    outbox = JsonlReportOutbox(events)
    first = outbox.append(**_notice_values())
    outbox.append(**_notice_values(1))
    opened: list[int] = []
    real_open_read = outbox._open_read

    def track_open(*, writable: bool = False) -> int:
        fd = real_open_read(writable=writable)
        opened.append(fd)
        return fd

    monkeypatch.setattr(outbox, "_open_read", track_open)
    assert outbox.pending(limit=1) == (first,)
    _assert_fds_closed(opened)

    path = events / "report-outbox.jsonl"
    raw = path.read_bytes()
    first_end = raw.index(b"\n") + 1
    path.write_bytes(b"not-json\n" + raw[first_end:])
    opened.clear()
    with pytest.raises(EventCorruptionError, match="report outbox"):
        outbox.pending(limit=2)
    _assert_fds_closed(opened)


def test_report_outbox_closes_fds_for_head_and_ack_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = tmp_path / "events"
    events.mkdir(mode=0o700)
    outbox = JsonlReportOutbox(events)
    first = outbox.append(**_notice_values())
    second = outbox.append(**_notice_values(1))
    opened: list[int] = []
    real_open_read = outbox._open_read

    def track_open(*, writable: bool = False) -> int:
        fd = real_open_read(writable=writable)
        opened.append(fd)
        return fd

    monkeypatch.setattr(outbox, "_open_read", track_open)
    assert outbox.head() == first
    _assert_fds_closed(opened)

    opened.clear()
    with pytest.raises(EventConflictError, match="not FIFO"):
        outbox.acknowledge(second)
    _assert_fds_closed(opened)

    opened.clear()
    outbox.acknowledge(first)
    _assert_fds_closed(opened)


def test_report_outbox_write_all_handles_eintr_and_partial_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = tmp_path / "events"
    events.mkdir(mode=0o700)
    real_write = os.write
    calls = 0

    def interrupted_partial_write(fd: int, data: bytes | bytearray | memoryview) -> int:
        nonlocal calls
        if _is_report_outbox_fd(fd):
            calls += 1
            if calls == 1:
                raise OSError(errno.EINTR, "injected interrupted write")
            return real_write(fd, data[:3])
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", interrupted_partial_write)
    outbox = JsonlReportOutbox(events)
    notice = outbox.append(**_notice_values())

    assert calls > 2
    assert outbox.pending(limit=1) == (notice,)


def test_report_outbox_serializes_partial_append_and_pending_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = tmp_path / "events"
    events.mkdir(mode=0o700)
    outbox = JsonlReportOutbox(events)
    first = outbox.append(**_notice_values())
    race = _OutboxRace(outbox, first, _notice_values(1))
    real_append_sync = outbox._append_sync

    monkeypatch.setattr(outbox, "_append_sync", race.paused_partial_append)
    appender = Thread(target=race.append_worker)
    appender.start()
    assert race.append_entered.wait(2)

    reader = Thread(target=race.pending_ack_worker)
    reader.start()
    assert race.pending_started.wait(2)
    assert not race.pending_finished.wait(0.2)

    race.release_append.set()
    appender.join(2)
    reader.join(2)
    assert not appender.is_alive()
    assert not reader.is_alive()
    assert len(race.append_errors) == 1
    assert isinstance(race.append_errors[0], EventPersistenceError)
    assert race.pending_errors == []
    assert race.pending_results == [(first,)]

    monkeypatch.setattr(outbox, "_append_sync", real_append_sync)
    second = outbox.append(**race.values)
    assert outbox.pending(limit=2) == (second,)
    outbox.acknowledge(second)
    assert outbox.pending(limit=2) == ()


def test_report_outbox_zero_write_is_a_durable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = tmp_path / "events"
    events.mkdir(mode=0o700)
    real_write = os.write

    def zero_write(fd: int, data: bytes | bytearray | memoryview) -> int:
        if _is_report_outbox_fd(fd):
            return 0
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", zero_write)
    with pytest.raises(EventPersistenceError, match="cannot append report outbox"):
        JsonlReportOutbox(events).append(**_notice_values())


@pytest.mark.parametrize("failure", [errno.ENOSPC, errno.EIO])
def test_report_outbox_partial_write_failure_converges_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: int
) -> None:
    events = tmp_path / "events"
    events.mkdir(mode=0o700)
    values = _notice_values()
    real_write = os.write
    injected = False

    def partial_failure(fd: int, data: bytes | bytearray | memoryview) -> int:
        nonlocal injected
        if _is_report_outbox_fd(fd) and not injected:
            injected = True
            real_write(fd, data[:5])
            raise OSError(failure, "injected report outbox failure")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", partial_failure)
    with pytest.raises(EventPersistenceError, match="cannot append report outbox"):
        JsonlReportOutbox(events).append(**values)

    monkeypatch.setattr(os, "write", real_write)
    restarted = JsonlReportOutbox(events)
    notice = restarted.append(**values)

    assert restarted.pending(limit=2) == (notice,)
    assert events.joinpath("report-outbox.jsonl").read_bytes().count(b"\n") == 1


def test_report_outbox_fdatasync_failure_retries_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = tmp_path / "events"
    events.mkdir(mode=0o700)
    values = _notice_values()
    real_fdatasync = os.fdatasync
    injected = False

    def failing_fdatasync(fd: int) -> None:
        nonlocal injected
        if _is_report_outbox_fd(fd) and not injected:
            injected = True
            raise OSError(errno.EIO, "injected report outbox fdatasync failure")
        real_fdatasync(fd)

    monkeypatch.setattr(os, "fdatasync", failing_fdatasync)
    with pytest.raises(EventPersistenceError, match="cannot append report outbox"):
        JsonlReportOutbox(events).append(**values)

    monkeypatch.setattr(os, "fdatasync", real_fdatasync)
    restarted = JsonlReportOutbox(events)
    notice = restarted.append(**values)

    assert restarted.pending(limit=2) == (notice,)
    assert events.joinpath("report-outbox.jsonl").read_bytes().count(b"\n") == 1


def test_report_outbox_pending_repairs_torn_tail_after_restart(tmp_path: Path) -> None:
    events = tmp_path / "events"
    events.mkdir(mode=0o700)
    outbox = JsonlReportOutbox(events)
    notice = outbox.append(**_notice_values())
    fd = os.open(events / "report-outbox.jsonl", os.O_WRONLY | os.O_APPEND)
    try:
        os.write(fd, b'{"torn":')
        os.fdatasync(fd)
    finally:
        os.close(fd)

    restarted = JsonlReportOutbox(events)
    assert restarted.pending(limit=2) == (notice,)
    assert events.joinpath("report-outbox.jsonl").read_bytes().count(b"\n") == 1


def test_report_outbox_syncs_parent_directory_after_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = tmp_path / "events"
    events.mkdir(mode=0o700)
    real_fsync = os.fsync
    synced_paths: list[Path] = []

    def record_fsync(fd: int) -> None:
        try:
            synced_paths.append(Path(os.readlink(f"/proc/self/fd/{fd}")))
        except OSError:
            pass
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", record_fsync)
    JsonlReportOutbox(events).append(**_notice_values())

    assert events in synced_paths


@pytest.mark.parametrize("unsafe_kind", ["symlink", "directory"])
def test_report_outbox_rejects_unsafe_path(tmp_path: Path, unsafe_kind: str) -> None:
    events = tmp_path / "events"
    events.mkdir(mode=0o700)
    target = events / "report-outbox.jsonl"
    if unsafe_kind == "symlink":
        outside = tmp_path / "outside.jsonl"
        outside.write_bytes(b"must not be followed\n")
        target.symlink_to(outside)
    else:
        target.mkdir(mode=0o700)

    with pytest.raises((EventPathError, EventPersistenceError)):
        JsonlReportOutbox(events).append(**_notice_values())


def test_metric_specific_latest_six_are_not_crowded_out_by_other_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summaries = tuple(object() for _ in range(12))
    evidence = {
        summary: decline_reporting._EventDeclineEvidence(
            load_sag=(number,) if number < 6 else (),
            firmware=(),
            long_partial=(),
        )
        for number, summary in enumerate(summaries)
    }

    def load(_store, summary):
        return evidence[summary]

    monkeypatch.setattr(decline_reporting, "_event_decline_evidence", load)
    selected = decline_reporting._collect_decline_evidence(
        object(),
        summaries,
    )
    samples, available = decline_reporting._metric_samples(selected, "load_sag")

    assert available
    assert samples == tuple(range(6))
