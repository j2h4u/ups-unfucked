"""Application-visible failures exposed by persistence ports."""


class StoragePortError(RuntimeError):
    """A persistence port cannot complete the requested operation."""


class StoragePortCorruption(StoragePortError):
    """Durable evidence is structurally corrupt and cannot be assessed."""
