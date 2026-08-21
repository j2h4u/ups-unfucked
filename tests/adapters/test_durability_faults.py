"""Fault-injection coverage for the durable adapter boundaries.

These tests deliberately exercise the filesystem edges directly and through a
real event-store startup/recovery cycle.  The assertions are about convergence
after a failed durable boundary, rather than a coverage percentage.
"""

import errno
import os
from pathlib import Path

import pytest

from src.adapters import jsonl_filesystem
from src.adapters import model_state_persistence as model_files
from src.adapters.jsonl_errors import EventPersistenceError
from src.adapters.jsonl_event_store import JsonlEventStore
from src.application.storage_values import EventStart, PreparingCaptureRef

BLACKOUT_ID = "00000000000040008000000000000011"
SEGMENT_ID = "00000000000040008000000000000012"


def _start() -> EventStart:
    return EventStart(
        BLACKOUT_ID,
        SEGMENT_ID,
        "boot-a",
        "2026-08-17T00:00:00.000000Z",
        1_000_000_000,
        {"battery_epoch_id": "00000000000040008000000000000013"},
    )


def _fd_name(fd: int) -> str:
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}")).name
    except OSError:
        return ""


def test_event_write_all_handles_partial_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    real_write = os.write
    calls = 0

    def partial_write(fd: int, data: bytes | bytearray | memoryview) -> int:
        nonlocal calls
        if _fd_name(fd).startswith("evt-"):
            calls += 1
            size = min(3, len(data))
            return real_write(fd, data[:size])
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", partial_write)
    with JsonlEventStore(tmp_path) as store:
        handle = store.open(_start())
        event_path = tmp_path / "events" / handle.path_token
        assert handle.next_seq == 1
        assert event_path.read_bytes().count(b"\n") == 1

    assert calls > 1


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        ("eintr", "durable append failed"),
        ("zero", "durable append failed"),
    ],
)
def test_event_write_failure_leaves_preparing_capture_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_error: str,
):
    real_write = os.write
    injected = False

    def failing_write(fd: int, data: bytes | bytearray | memoryview) -> int:
        nonlocal injected
        if _fd_name(fd).startswith("evt-") and not injected:
            injected = True
            if failure == "eintr":
                raise OSError(errno.EINTR, "injected interrupted write")
            return 0
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", failing_write)
    with JsonlEventStore(tmp_path) as store:
        with pytest.raises(EventPersistenceError, match=expected_error):
            store.open(_start())
        assert isinstance(store.work_registry().capture, PreparingCaptureRef)

        monkeypatch.setattr(os, "write", real_write)
        recovered = store.recover_startup()
        assert recovered is not None
        event_path = tmp_path / "events" / recovered.path_token
        assert event_path.read_bytes().count(b"\n") == 1


def test_atomic_replace_orders_write_sync_rename_and_directory_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    with JsonlEventStore(tmp_path) as store:
        destination = tmp_path / "events" / "projection.jsonl"
        destination.write_bytes(b"old\n")
        events: list[str] = []
        real_write = os.write
        real_fdatasync = os.fdatasync
        real_replace = os.replace
        real_fsync = os.fsync

        def record_write(fd: int, data: bytes | bytearray | memoryview) -> int:
            events.append("write")
            return real_write(fd, data)

        def record_fdatasync(fd: int) -> None:
            events.append("fdatasync")
            real_fdatasync(fd)

        def record_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
            events.append("rename")
            real_replace(source, target)

        def record_fsync(fd: int) -> None:
            events.append("dir-fsync")
            real_fsync(fd)

        monkeypatch.setattr(os, "write", record_write)
        monkeypatch.setattr(os, "fdatasync", record_fdatasync)
        monkeypatch.setattr(os, "replace", record_replace)
        monkeypatch.setattr(os, "fsync", record_fsync)

        store._filesystem.atomic_replace(destination, b"new\n", mode=0o600)

        assert destination.read_bytes() == b"new\n"
        assert events.index("write") < events.index("fdatasync")
        assert events.index("fdatasync") < events.index("rename")
        assert events.index("rename") < events.index("dir-fsync")


def test_model_atomic_write_flush_failure_preserves_original_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "model.json"
    target.write_bytes(b"old model\n")

    def fail_flush(_fd: int) -> None:
        raise OSError(errno.EIO, "injected model flush failure")

    monkeypatch.setattr(model_files.os, "fdatasync", fail_flush)
    with pytest.raises(OSError, match="injected model flush failure"):
        model_files.atomic_write_model(target, "new model\n")

    assert target.read_bytes() == b"old model\n"
    assert not tuple(tmp_path.glob("*.tmp"))


def test_model_atomic_write_cleanup_failure_adds_note_to_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "model.json"
    target.write_bytes(b"old model\n")
    real_unlink = Path.unlink

    def fail_flush(_fd: int) -> None:
        raise OSError(errno.EIO, "injected model flush failure")

    def fail_cleanup(path: Path, *args, **kwargs) -> None:
        if path.parent == tmp_path and path.suffix == ".tmp":
            raise OSError(errno.EPERM, "injected temporary cleanup failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(model_files.os, "fdatasync", fail_flush)
    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    with pytest.raises(OSError, match="injected model flush failure") as error:
        model_files.atomic_write_model(target, "new model\n")

    assert any("temporary cleanup failed" in note for note in error.value.__notes__)
    assert target.read_bytes() == b"old model\n"
    for temporary in tmp_path.glob("*.tmp"):
        real_unlink(temporary)


def test_atomic_replace_rename_failure_cleans_owned_temporary_and_preserves_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    with JsonlEventStore(tmp_path) as store:
        destination = tmp_path / "events" / "projection.jsonl"
        destination.write_bytes(b"old\n")
        real_replace = os.replace
        real_unlink = Path.unlink
        unlinked: list[Path] = []

        def failing_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
            if Path(target) == destination:
                raise OSError(errno.EIO, "injected rename failure")
            real_replace(source, target)

        def record_unlink(path: Path, *args, **kwargs) -> None:
            if path.parent == destination.parent and path.name.startswith(".projection.jsonl.tmp-"):
                unlinked.append(path)
            real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "replace", failing_replace)
        monkeypatch.setattr(Path, "unlink", record_unlink)
        with pytest.raises(EventPersistenceError, match="atomic replacement"):
            store._filesystem.atomic_replace(destination, b"new\n", mode=0o600)

        assert destination.read_bytes() == b"old\n"
        assert len(unlinked) == 1
        assert not tuple(destination.parent.glob(".projection.jsonl.tmp-*"))


def test_atomic_replace_cleanup_failure_does_not_mask_rename_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    with JsonlEventStore(tmp_path) as store:
        destination = tmp_path / "events" / "projection.jsonl"
        destination.write_bytes(b"old\n")
        real_replace = os.replace
        real_cleanup = jsonl_filesystem._unlink_owned_temporary
        temporary_paths: list[Path] = []

        def failing_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
            if Path(target) == destination:
                raise OSError(errno.EIO, "injected rename failure")
            real_replace(source, target)

        def failing_cleanup(path: Path) -> None:
            temporary_paths.append(path)
            raise OSError(errno.ENOSPC, "injected cleanup failure")

        monkeypatch.setattr(os, "replace", failing_replace)
        monkeypatch.setattr(jsonl_filesystem, "_unlink_owned_temporary", failing_cleanup)
        with pytest.raises(EventPersistenceError, match="atomic replacement") as error:
            store._filesystem.atomic_replace(destination, b"new\n", mode=0o600)

        assert destination.read_bytes() == b"old\n"
        assert len(temporary_paths) == 1
        assert temporary_paths[0].exists()
        assert any("temporary cleanup failed" in note for note in error.value.__notes__)

        monkeypatch.setattr(jsonl_filesystem, "_unlink_owned_temporary", real_cleanup)
        real_cleanup(temporary_paths[0])
        assert not temporary_paths[0].exists()


def test_atomic_replace_directory_sync_failure_converges_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    with JsonlEventStore(tmp_path) as store:
        destination = tmp_path / "events" / "projection.jsonl"
        destination.write_bytes(b"old\n")
        real_fsync = os.fsync
        failed = False

        def failing_directory_fsync(fd: int) -> None:
            nonlocal failed
            if not failed and _fd_name(fd) == "events":
                failed = True
                raise OSError(errno.EIO, "injected directory sync failure")
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", failing_directory_fsync)
        with pytest.raises(EventPersistenceError, match="directory sync failed"):
            store._filesystem.atomic_replace(destination, b"new\n", mode=0o600)
        assert destination.read_bytes() == b"new\n"

        monkeypatch.setattr(os, "fsync", real_fsync)
        store._filesystem.atomic_replace(destination, b"newer\n", mode=0o600)
        assert destination.read_bytes() == b"newer\n"


def test_model_backup_write_all_handles_partial_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    backup = tmp_path / "model.pretransform.json"
    source = b'{"schema_version": 2}\n'
    real_write = os.write

    def partial_write(fd: int, data: bytes | bytearray | memoryview) -> int:
        size = min(2, len(data))
        return real_write(fd, data[:size])

    monkeypatch.setattr(os, "write", partial_write)
    model_files.ensure_verified_backup(backup, source)
    assert backup.read_bytes() == source
    assert not tuple(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("failure", ["eintr", "zero"])
def test_model_backup_write_failures_clean_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
):
    backup = tmp_path / "model.pretransform.json"
    source = b'{"schema_version": 2}\n'

    def failing_write(fd: int, data: bytes | bytearray | memoryview) -> int:
        if failure == "eintr":
            raise OSError(errno.EINTR, "injected interrupted backup write")
        return 0

    monkeypatch.setattr(os, "write", failing_write)
    with pytest.raises(
        model_files.ModelStateFileError,
        match="(?:cannot create pre-transform backup|pre-transform backup write made no progress)",
    ):
        model_files.ensure_verified_backup(backup, source)
    assert not backup.exists()
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_restore_exact_source_reports_rollback_failure_and_retains_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "model.json"
    backup = tmp_path / "model.pretransform.json"
    source = b'{"source": true}\n'
    target.write_bytes(b'{"changed": true}\n')
    backup.write_bytes(source)

    def failed_restore(*args, **kwargs):
        raise OSError(errno.EIO, "injected rollback write failure")

    monkeypatch.setattr(model_files, "atomic_write_model", failed_restore)
    with pytest.raises(model_files.ModelStateFileError, match="rollback failed") as error:
        model_files.restore_exact_source(
            target,
            source,
            backup=backup,
            failure=RuntimeError("target publication failed"),
        )

    assert "verified source backup retained" in str(error.value)
    assert target.read_bytes() == b'{"changed": true}\n'
    assert backup.read_bytes() == source
