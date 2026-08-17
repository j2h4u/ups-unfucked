"""Narrow filesystem primitives shared by JSONL collaborators."""

import errno
import fcntl
import hashlib
import os
import stat
import uuid
from collections.abc import Callable
from pathlib import Path

from src.adapters.jsonl_errors import (
    EventConflictError,
    EventPathError,
    EventPersistenceError,
    EventStoreError,
)
from src.adapters.jsonl_record_codec import _bounded_error, _validate_path_token


def _ensure_private_directory(path: Path, *, create: bool) -> None:
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise EventPathError(
            f"cannot prepare private directory {path}: {_bounded_error(exc)}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise EventPathError(f"storage directory is unsafe: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise EventPathError(f"storage directory permissions are broader than 0700: {path}")


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_exact_fd(fd: int, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            raise OSError("unexpected EOF")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _unlink_owned_temporary(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISREG(info.st_mode) and path.name.startswith(".") and ".tmp-" in path.name:
        path.unlink()


def _cleanup_owned_temporary(path: Path, error: BaseException) -> None:
    """Best-effort cleanup that never masks the failed durability boundary."""
    try:
        _unlink_owned_temporary(path)
    except OSError as cleanup_error:
        error.add_note(f"temporary cleanup failed: {_bounded_error(cleanup_error)}")


def _close_open_fd(fd: int | None, error: BaseException) -> None:
    """Close a failed-open descriptor without masking its primary error."""
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError as close_error:
        error.add_note(f"descriptor cleanup failed: {_bounded_error(close_error)}")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise EventPersistenceError(f"cannot hash {path.name}: {_bounded_error(exc)}") from exc
    return digest.hexdigest()


class JsonlFilesystem:
    """Cohesive filesystem lane for the JSONL adapter family.

    ``atomic_replace`` and ``sync_storage_directory`` are the public
    cross-lane durability seams. Other underscored methods remain intentional
    adapter-family friend primitives rather than a broad public API.
    """

    def __init__(
        self,
        root: Path,
        *,
        fault_hook: Callable[[str], None] | None,
        monotonic_clock_ns: Callable[[], int],
    ) -> None:
        self._root = root
        self._events = root / "events"
        self._fault_hook = fault_hook
        self._monotonic_clock_ns = monotonic_clock_ns
        self._last_error: str | None = None
        self._pending_durability_started_ns: int | None = None
        self._catalog_reserver: Callable[[str], None] | None = None

    def _events_path(self) -> Path:
        return self._events

    def _last_error_value(self) -> str | None:
        return self._last_error

    def _clear_error(self) -> None:
        """Clear the current capture latch after a verified recovery transaction."""
        self._last_error = None

    def _begin_durability_window(self) -> None:
        self._pending_durability_started_ns = self._monotonic_clock_ns()

    def _end_durability_window(self) -> None:
        self._pending_durability_started_ns = None

    def _attach_catalog_reserver(self, reserver: Callable[[str], None]) -> None:
        self._catalog_reserver = reserver

    def _ensure_layout(self) -> None:
        _ensure_private_directory(self._root, create=True)
        created = not self._events.exists()
        _ensure_private_directory(self._events, create=True)
        if created:
            self.sync_storage_directory(self._root)

    def _event_path(self, path_token: str) -> Path:
        _validate_path_token(path_token)
        return self._events / path_token

    def _acquire_writer_lock(self) -> int:
        lock_path = self._root / "monitor.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        try:
            fd = os.open(lock_path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise EventPathError("writer lock is not a regular file")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except EventStoreError as exc:
            _close_open_fd(fd, exc)
            raise
        except OSError as exc:
            _close_open_fd(fd, exc)
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise EventConflictError("another event-store writer owns monitor.lock") from exc
            raise EventPersistenceError(
                f"cannot acquire writer lock: {_bounded_error(exc)}"
            ) from exc

    @staticmethod
    def _validate_lock_fd(fd: int) -> None:
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise EventPathError("supplied writer lock descriptor is not a regular file")
        except OSError as exc:
            raise EventPathError("supplied writer lock descriptor is invalid") from exc

    def _create_regular_file(self, path: Path, *, mode: int) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        try:
            if self._catalog_reserver is not None and path.name.startswith("evt-"):
                self._catalog_reserver(path.name)
                self._trip("after_catalog_reserve")
            fd = os.open(path, flags, mode)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise EventPathError(f"new path is not a regular file: {path.name}")
            os.fchmod(fd, mode)
            return fd
        except FileExistsError as exc:
            _close_open_fd(fd, exc)
            raise EventConflictError(
                f"exclusive create target already exists: {path.name}"
            ) from exc
        except EventStoreError as exc:
            _close_open_fd(fd, exc)
            raise
        except OSError as exc:
            _close_open_fd(fd, exc)
            self._record_error(exc)
            raise EventPersistenceError(
                f"cannot create {path.name}: {_bounded_error(exc)}"
            ) from exc

    def _open_existing(self, path: Path, *, writable: bool) -> int:
        flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0)
        if writable:
            flags |= os.O_APPEND
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        try:
            fd = os.open(path, flags)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise EventPathError(f"path is not a regular file: {path.name}")
            return fd
        except EventStoreError as exc:
            _close_open_fd(fd, exc)
            raise
        except OSError as exc:
            _close_open_fd(fd, exc)
            self._record_error(exc)
            raise EventPersistenceError(f"cannot open {path.name}: {_bounded_error(exc)}") from exc

    def _open_append_or_create(self, path: Path, *, mode: int) -> int:
        common_flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        common_flags |= getattr(os, "O_NOFOLLOW", 0)
        created = False
        fd: int | None = None
        try:
            try:
                fd = os.open(path, common_flags | os.O_CREAT | os.O_EXCL, mode)
                created = True
            except FileExistsError:
                fd = os.open(path, common_flags)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise EventPathError(f"projection path is not regular: {path.name}")
            os.fchmod(fd, mode)
            if created:
                self.sync_storage_directory(path.parent)
            return fd
        except EventStoreError as exc:
            _close_open_fd(fd, exc)
            raise
        except OSError as exc:
            _close_open_fd(fd, exc)
            self._record_error(exc)
            raise EventPersistenceError(
                f"cannot open projection {path.name}: {_bounded_error(exc)}"
            ) from exc

    def _append_and_sync_fd(self, fd: int, data: bytes) -> None:
        self._begin_durability_window()
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fdatasync(fd)
            self._end_durability_window()
        except OSError as exc:
            self._end_durability_window()
            self._record_error(exc)
            raise EventPersistenceError(f"durable append failed: {_bounded_error(exc)}") from exc

    def atomic_replace(self, destination: Path, data: bytes, *, mode: int) -> None:
        temporary = destination.parent / f".{destination.name}.tmp-{uuid.uuid4().hex}"
        fd: int | None = None
        try:
            fd = self._create_regular_file(temporary, mode=mode)
            self._append_and_sync_fd(fd, data)
            assert fd is not None
            try:
                os.close(fd)
            except OSError:
                fd = None
                raise
            fd = None
            os.replace(temporary, destination)
            self.sync_storage_directory(destination.parent)
        except EventStoreError as exc:
            _close_open_fd(fd, exc)
            _cleanup_owned_temporary(temporary, exc)
            raise
        except OSError as exc:
            _close_open_fd(fd, exc)
            self._record_error(exc)
            error = EventPersistenceError(f"atomic replacement failed: {_bounded_error(exc)}")
            _cleanup_owned_temporary(temporary, error)
            raise error from exc

    def _durability_lag_s(self) -> float | None:
        if self._pending_durability_started_ns is None:
            return 0.0
        return max(
            0.0,
            (self._monotonic_clock_ns() - self._pending_durability_started_ns) / 1_000_000_000,
        )

    def _trip(self, stage: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(stage)

    def sync_storage_directory(self, path: Path) -> None:
        try:
            _sync_directory(path)
        except OSError as exc:
            self._record_error(exc)
            raise EventPersistenceError(f"directory sync failed: {_bounded_error(exc)}") from exc

    def _record_error(self, error: BaseException | str) -> None:
        bounded = _bounded_error(error)
        self._last_error = bounded
