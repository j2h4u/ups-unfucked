"""Explicit errors raised by the JSONL persistence boundary."""

from src.application.errors import (
    DurableCaptureTerminalError,
    StoragePortConflict,
    StoragePortCorruption,
    StoragePortError,
)


class EventStoreError(StoragePortError):
    """Base class for explicit storage-boundary failures."""


class EventValidationError(EventStoreError):
    """A value cannot be represented by the strict v2 schema."""


class EventPathError(EventStoreError):
    """A storage path is unsafe or has unexpected permissions/type."""


class EventPersistenceError(EventStoreError):
    """A filesystem mutation or durability operation failed."""


class EventConflictError(StoragePortConflict, EventStoreError):
    """An idempotency key already exists with different canonical bytes."""


class EventCorruptionError(StoragePortCorruption, EventStoreError):
    """Evidence contains non-tail corruption and science must fail closed."""


class ProjectionUnavailableError(EventStoreError):
    """The rebuildable index cannot currently accept/query projections."""


class ProcessingBacklogFullError(DurableCaptureTerminalError, ProjectionUnavailableError):
    """All processing slots were occupied, so the event was durably rejected."""
