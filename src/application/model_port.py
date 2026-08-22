"""Read-only model boundary used by the safety loop."""

from typing import Protocol

from src.domain.values import FrozenModelSnapshot


class ModelSnapshotPort(Protocol):
    """Expose only the immutable snapshot required for safety calculation."""

    def current_snapshot(self) -> FrozenModelSnapshot: ...
