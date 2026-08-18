"""Closed error taxonomy for the private JSONL v3 durability boundary."""

from __future__ import annotations

import errno
from collections.abc import Mapping

from src.application.errors import StoragePortConflict, StoragePortCorruption, StoragePortError

MAX_OS_ERROR_BYTES = 96


class V3StorageError(StoragePortError):
    """Base class for failures at the v3 storage boundary."""

    reason_code = "v3-storage-error"

    def __init__(self, message: str = "v3 storage operation failed") -> None:
        # Messages can be constructed from syscall input.  Keep the public
        # payload closed even when a caller supplies a path, token, or empty
        # text; detailed exception text remains available only in chaining.
        del message
        self.reason = self.reason_code[:MAX_OS_ERROR_BYTES] or "v3-storage-error"
        self.cleanup_failed = False
        super().__init__(self.reason)


class V3WriterOwnershipError(V3StorageError):
    """The ModelOwner writer lease is absent, stale, or held by another writer."""

    reason_code = "writer-ownership"


class V3PathError(V3StorageError):
    """A path violates the owner-only v3 filesystem contract."""

    reason_code = "unsafe-path"


class V3ValidationError(V3StorageError):
    """An adapter input or private schema is not valid."""

    reason_code = "validation"


class V3AppendConflict(StoragePortConflict, V3StorageError):
    """A retry has the same identity but different bytes or cursor."""

    reason_code = "append-conflict"


class V3CapacityError(V3StorageError):
    """A bounded construction cannot accept another record before rollover."""

    reason_code = "capacity"


class V3PersistenceError(V3StorageError):
    """A write, sync, rename, or readback boundary failed."""

    reason_code = "persistence"


class V3CorruptionError(StoragePortCorruption, V3StorageError):
    """Durable bytes violate an integrity or schema invariant."""

    reason_code = "corruption"


class V3ProjectionUnavailable(V3StorageError):
    """A rebuildable v3 projection is absent, stale, or damaged."""

    reason_code = "projection-unavailable"


class V3TransactionClosed(V3StorageError):
    reason_code = "transaction-closed"


class V3PathBindingConflict(V3StorageError):
    reason_code = "path-binding-conflict"


class V3FileNotFound(V3PathError):
    reason_code = "file-not-found"


def bounded_os_error(error: BaseException | str) -> str:
    """Render only a closed errno/type code; never include OS text or paths."""
    if isinstance(error, OSError):
        code = errno.errorcode.get(error.errno or 0, "EUNKNOWN")
        return f"{code}:os-error"
    if isinstance(error, V3StorageError):
        return error.reason_code
    error_type = type(error).__name__
    return error_type[:MAX_OS_ERROR_BYTES] or "unknown-error"


def reason_payload(error: V3StorageError) -> Mapping[str, str]:
    """Return the bounded representation suitable for health/application layers."""
    return {"reason_code": error.reason_code, "reason": error.reason}
