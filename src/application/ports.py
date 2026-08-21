"""Dependency-inversion ports for blackout application use cases."""

from typing import Protocol

from src.domain.values import PhysicalObservation


class PhysicalTelemetryPort(Protocol):
    """Read the next immutable physical UPS observation."""

    def read(self) -> PhysicalObservation: ...


class CloseablePort(Protocol):
    """Own exactly one runtime resource lifecycle operation."""

    def close(self) -> None: ...
