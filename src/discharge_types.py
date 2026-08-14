"""Immutable contracts shared by discharge capture and model application.

The collector owns lifecycle transitions, while the handler owns scientific eligibility
and model application.  Keeping this contract independent prevents either side from
silently dropping evidence metadata at the boundary.
"""

from dataclasses import dataclass
from typing import Literal

ApplicationStatus = Literal["applied", "already_applied", "skipped"]


@dataclass(frozen=True)
class CompletedDischarge:
    """A closed discharge event presented to the scientific application boundary."""

    event_id: str | None
    lifecycle: str
    evidence_class: str
    voltages: tuple[float, ...]
    times: tuple[float, ...]
    loads: tuple[float, ...]
    model_processing_eligible: bool
    eligibility_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ModelApplicationResult:
    """Outcome of attempting to apply one completed event to the battery model."""

    event_id: str | None
    status: ApplicationStatus
    model_hash: str | None
    eligibility_reasons: tuple[str, ...]

    @property
    def applied(self) -> bool:
        """Whether this call committed an authoritative model update."""
        return self.status == "applied"

    @property
    def already_applied(self) -> bool:
        """Whether the event was previously committed and was deduplicated."""
        return self.status == "already_applied"

    @property
    def skipped(self) -> bool:
        """Whether eligibility rejected authoritative application."""
        return self.status == "skipped"
