"""Durable file mechanics shared by the model owner and offline transform.

The functions here publish complete model bytes, preserve no-clobber backups,
and provide the single nonblocking writer lock.  They do not decide whether
a scientific model change is allowed; that transaction remains in
``ModelOwner`` or the one-shot transform.
"""

import errno
import fcntl
import hashlib
import os
import stat
import tempfile
from pathlib import Path

MAX_MODEL_BYTES = 4 * 1024 * 1024


class ModelStateFileError(RuntimeError):
    """A model-owned file could not be safely read or durably published."""


class ModelStateLockHeld(ModelStateFileError):
    """Another writer owns the model directory lock."""


def persisted_hash(raw: bytes) -> str:
    """Return the SHA-256 receipt for exact persisted bytes."""
    return hashlib.sha256(raw).hexdigest()


def read_model_file(path: Path, *, error_type: type[Exception] = ModelStateFileError) -> bytes:
    """Read a bounded regular model file without following symlinks."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise error_type(f"cannot read model file {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise error_type(f"model path is not a regular file: {path}")
        if info.st_size > MAX_MODEL_BYTES:
            raise error_type(f"model file exceeds {MAX_MODEL_BYTES} bytes")
        return _read_model_bytes(fd, info.st_size, path, error_type)
    except OSError as exc:
        raise error_type(f"cannot read model file {path}: {exc}") from exc
    finally:
        os.close(fd)


def _read_model_bytes(
    fd: int,
    expected_size: int,
    path: Path,
    error_type: type[Exception],
) -> bytes:
    chunks: list[bytes] = []
    remaining = expected_size
    while remaining:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            raise error_type(f"short read from model file: {path}")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def atomic_write_model(path: str | Path, content: str, *, mode: int = 0o600) -> str:
    """Durably replace one model-owned file and return its exact content hash."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    content_hash = persisted_hash(content.encode("utf-8"))
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, delete=False, suffix=".tmp"
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fdatasync(temporary.fileno())
            os.fchmod(temporary.fileno(), mode)
        temporary_path.replace(target)
        sync_directory(target.parent)
    except Exception as exc:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                exc.add_note(f"temporary cleanup failed: {cleanup_error}")
        raise
    return content_hash


def sync_directory(path: Path) -> None:
    """Flush directory metadata after a rename or no-clobber link."""
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def acquire_writer_lock(lock_path: Path) -> int:
    """Acquire the sole nonblocking model writer lock."""
    if lock_path.is_symlink():
        raise ModelStateFileError(f"writer lock is a symlink: {lock_path}")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError as exc:
        if "fd" in locals():
            os.close(fd)
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
            raise ModelStateLockHeld(f"another writer owns {lock_path}") from exc
        raise ModelStateFileError(f"cannot acquire writer lock {lock_path}: {exc}") from exc


def release_writer_lock(fd: int) -> None:
    """Release and close a lock acquired by ``acquire_writer_lock``."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
