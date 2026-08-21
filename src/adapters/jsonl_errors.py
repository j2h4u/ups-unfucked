"""Explicit errors raised by the JSONL persistence boundary."""

from src.application.errors import (
    StoragePortCorruption,
    StoragePortError,
)


class EventStoreError(StoragePortError):
    """Base class for explicit storage-boundary failures."""


class EventValidationError(EventStoreError):
    """A value cannot be represented by the strict v2 schema."""


class EventPathError(EventStoreError):
    """A storage path is unsafe or has unexpected permissions/type."""


class EventCorruptionError(StoragePortCorruption, EventStoreError):
    """Evidence contains non-tail corruption and science must fail closed."""
