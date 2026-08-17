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
from typing import Never

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
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != info.st_size:
            raise error_type(f"short read from model file: {path}")
        return raw
    except OSError as exc:
        raise error_type(f"cannot read model file {path}: {exc}") from exc
    finally:
        os.close(fd)


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


def ensure_verified_backup(path: Path, source_raw: bytes) -> None:
    """Install or verify an exact pre-transform backup without clobbering it."""
    if path.exists() or path.is_symlink():
        if read_model_file(path) != source_raw:
            raise ModelStateFileError("existing pre-transform backup conflicts with source model")
        return
    _install_backup_exclusively(path, source_raw)
    if read_model_file(path) != source_raw:
        raise ModelStateFileError("pre-transform backup verification failed")


def _install_backup_exclusively(path: Path, source_raw: bytes) -> None:
    """Publish source bytes without replacing a concurrently created backup."""
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(temporary_fd, 0o600)
        remaining = memoryview(source_raw)
        while remaining:
            written = os.write(temporary_fd, remaining)
            if written <= 0:
                raise ModelStateFileError("pre-transform backup write made no progress")
            remaining = remaining[written:]
        os.fdatasync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1
        try:
            os.link(temporary_path, path, follow_symlinks=False)
        except FileExistsError:
            if read_model_file(path) != source_raw:
                raise ModelStateFileError(
                    "existing pre-transform backup conflicts with source model"
                )
        else:
            sync_directory(path.parent)
    except OSError as exc:
        raise ModelStateFileError(f"cannot create pre-transform backup {path}: {exc}") from exc
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        temporary_path.unlink(missing_ok=True)


def restore_exact_source(
    path: Path,
    source_raw: bytes,
    *,
    backup: Path,
    failure: BaseException,
) -> Never:
    """Restore exact source bytes after a failed one-shot target publication."""
    try:
        current_raw = read_model_file(path)
        if current_raw != source_raw:
            atomic_write_model(path, source_raw.decode("utf-8"))
            if read_model_file(path) != source_raw:
                raise ModelStateFileError("restored source bytes differ from pre-transform source")
    except BaseException as rollback_error:
        error = ModelStateFileError(
            "target write/verification failed and automatic rollback failed; "
            f"verified source backup retained at {backup}"
        )
        error.add_note(f"original target failure: {failure}")
        raise error from rollback_error
    raise ModelStateFileError(
        f"target write/verification failed; exact source restored: {failure}"
    ) from failure


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
