"""Application-visible failures exposed by persistence ports."""


class StoragePortError(RuntimeError):
    """A persistence port cannot complete the requested operation."""


class StoragePortConflict(StoragePortError):
    """A persistence port found an incompatible durable state."""


class StoragePortCorruption(StoragePortError):
    """Durable evidence is structurally corrupt and cannot be assessed."""


class DurableCaptureTerminalError(StoragePortError):
    """The capture event reached a durable terminal outcome before raising."""
