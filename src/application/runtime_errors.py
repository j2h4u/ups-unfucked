"""Small runtime boundary shared by the safety loop."""

from __future__ import annotations


class RuntimeErrorBoundary(RuntimeError):
    """A runtime invariant must degrade the current poll safely."""
