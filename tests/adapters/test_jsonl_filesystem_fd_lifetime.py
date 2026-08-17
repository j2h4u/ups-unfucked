"""Descriptor cleanup tests for JSONL filesystem open primitives."""

import errno
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest

from src.adapters import jsonl_filesystem
from src.adapters.jsonl_errors import EventPathError, EventPersistenceError
from src.adapters.jsonl_filesystem import JsonlFilesystem

Primitive = Literal["create", "existing", "append-create", "append-existing"]
Failure = Literal["fstat", "fchmod"]
ErrorKind = Literal["os", "store"]


def _invoke(filesystem: JsonlFilesystem, path: Path, primitive: Primitive) -> int:
    if primitive == "create":
        return filesystem._create_regular_file(path, mode=0o600)
    if primitive == "existing":
        return filesystem._open_existing(path, writable=False)
    return filesystem._open_append_or_create(path, mode=0o600)


def _prepare_target(tmp_path: Path, primitive: Primitive) -> tuple[Path, bytes]:
    path = tmp_path / "event.jsonl"
    if primitive in {"existing", "append-existing"}:
        baseline = b"existing evidence\n"
        path.write_bytes(baseline)
        return path, baseline
    return path, b""


def _make_failure(
    error_kind: ErrorKind,
    failure: Failure,
) -> tuple[OSError | EventPathError, type[EventPersistenceError] | type[EventPathError]]:
    if error_kind == "os":
        return OSError(errno.EIO, f"injected {failure} failure"), EventPersistenceError
    return EventPathError(f"injected {failure} store failure"), EventPathError


def _patch_open_close_tracking(
    monkeypatch: pytest.MonkeyPatch,
    opened: list[int],
    closed: list[int],
    close_error: OSError | None = None,
) -> Callable[[int], os.stat_result]:
    real_fstat = os.fstat
    real_open = os.open
    real_close = os.close

    def tracking_open(path_value: Path, flags: int, mode: int = 0o777) -> int:
        fd = real_open(path_value, flags, mode)
        opened.append(fd)
        return fd

    def tracking_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)
        if close_error is not None:
            raise close_error

    monkeypatch.setattr(jsonl_filesystem.os, "open", tracking_open)
    monkeypatch.setattr(jsonl_filesystem.os, "close", tracking_close)
    return real_fstat


def _patch_fd_tracking(
    monkeypatch: pytest.MonkeyPatch,
    failure: Failure,
    injected: OSError | EventPathError,
    opened: list[int],
    closed: list[int],
) -> Callable[[int], os.stat_result]:
    real_fstat = _patch_open_close_tracking(monkeypatch, opened, closed)
    real_fchmod = os.fchmod

    def failing_fstat(fd: int) -> os.stat_result:
        if fd in opened:
            raise injected
        return real_fstat(fd)

    def failing_fchmod(fd: int, mode: int) -> None:
        if fd in opened:
            raise injected
        real_fchmod(fd, mode)

    if failure == "fstat":
        monkeypatch.setattr(jsonl_filesystem.os, "fstat", failing_fstat)
    else:
        monkeypatch.setattr(jsonl_filesystem.os, "fchmod", failing_fchmod)
    return real_fstat


@pytest.mark.parametrize(
    ("primitive", "error_kind", "failure"),
    [
        (primitive, error_kind, failure)
        for failure in ("fstat", "fchmod")
        for primitive in (
            ("create", "existing", "append-create", "append-existing")
            if failure == "fstat"
            else ("create", "append-create", "append-existing")
        )
        for error_kind in ("os", "store")
    ],
    ids=lambda value: str(value),
)
def test_open_validation_failure_closes_fd_once_and_preserves_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primitive: Primitive,
    error_kind: ErrorKind,
    failure: Failure,
) -> None:
    path, baseline = _prepare_target(tmp_path, primitive)
    filesystem = JsonlFilesystem(
        tmp_path,
        fault_hook=None,
        monotonic_clock_ns=lambda: 0,
    )
    opened: list[int] = []
    closed: list[int] = []
    injected, expected_error = _make_failure(error_kind, failure)
    real_fstat = _patch_fd_tracking(monkeypatch, failure, injected, opened, closed)

    with pytest.raises(expected_error) as raised:
        _invoke(filesystem, path, primitive)

    assert len(opened) == 1
    assert closed == opened
    with pytest.raises(OSError):
        real_fstat(opened[0])
    assert path.is_file()
    assert path.read_bytes() == baseline
    if error_kind == "os":
        last_error = filesystem._last_error_value()
        assert last_error is not None
        assert "injected" in last_error
        assert raised.value.__cause__ is injected
    else:
        assert filesystem._last_error_value() is None
        assert raised.value is injected


def test_writer_lock_nonregular_path_closes_fd_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "monitor.lock"
    lock_path.write_bytes(b"")
    filesystem = JsonlFilesystem(tmp_path, fault_hook=None, monotonic_clock_ns=lambda: 0)
    opened: list[int] = []
    closed: list[int] = []
    real_fstat = _patch_open_close_tracking(monkeypatch, opened, closed)
    real_isreg = jsonl_filesystem.stat.S_ISREG

    def reject_regular_file(mode: int) -> bool:
        return False if opened else real_isreg(mode)

    monkeypatch.setattr(jsonl_filesystem.stat, "S_ISREG", reject_regular_file)
    with pytest.raises(EventPathError, match="writer lock"):
        filesystem._acquire_writer_lock()

    assert len(opened) == 1
    assert closed == opened
    with pytest.raises(OSError):
        real_fstat(opened[0])
    assert lock_path.read_bytes() == b""


def test_atomic_replace_close_error_keeps_primary_and_cleans_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "projection.jsonl"
    destination.write_bytes(b"old\n")
    filesystem = JsonlFilesystem(tmp_path, fault_hook=None, monotonic_clock_ns=lambda: 0)
    opened: list[int] = []
    closed: list[int] = []
    primary = OSError(errno.EIO, "injected durable append failure")
    close_error = OSError(errno.EPERM, "injected close reporting failure")
    real_fstat = _patch_open_close_tracking(
        monkeypatch,
        opened,
        closed,
        close_error=close_error,
    )
    real_fdatasync = os.fdatasync

    def failing_fdatasync(fd: int) -> None:
        if fd in opened:
            raise primary
        real_fdatasync(fd)

    monkeypatch.setattr(jsonl_filesystem.os, "fdatasync", failing_fdatasync)
    with pytest.raises(EventPersistenceError) as raised:
        filesystem.atomic_replace(destination, b"new\n", mode=0o600)

    assert raised.value.__cause__ is primary
    assert any("descriptor cleanup failed" in note for note in raised.value.__notes__)
    assert len(opened) == 1
    assert closed == opened
    with pytest.raises(OSError):
        real_fstat(opened[0])
    assert destination.read_bytes() == b"old\n"
    assert not tuple(tmp_path.glob(".projection.jsonl.tmp-*"))
