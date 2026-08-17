"""Pending physical-boundary recovery for the active capture lane."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from src.application.capture_submission_lane import CaptureSubmissionLane, FailureCallback
from src.application.capture_writer import CaptureCommandKind
from src.application.prestart_loss import PrestartLossTracker
from src.domain.lifecycle import UNKNOWN_PRELUDE_GAP_REASON
from src.domain.values import (
    BlackoutKind,
    ChargeReadiness,
    FrozenModelSnapshot,
    PhysicalObservation,
)


@dataclass(frozen=True, slots=True)
class BoundaryRetryRequest:
    blackout_id: str
    observation: PhysicalObservation
    snapshot: FrozenModelSnapshot
    readiness: ChargeReadiness
    kind: BlackoutKind


@dataclass(frozen=True, slots=True)
class BoundaryRetryCallbacks:
    on_end_durable: Callable[[PhysicalObservation], None]
    on_end_submitted: Callable[[], None]
    on_failure: Callable[[CaptureCommandKind], FailureCallback]


class CaptureBoundaryRecovery:
    """Prevent rejected END/GAP submissions from being bypassed by later polls."""

    def __init__(
        self,
        submissions: CaptureSubmissionLane,
        prestart_loss: PrestartLossTracker,
    ) -> None:
        self._submissions = submissions
        self._prestart_loss = prestart_loss
        self._pending_end: PhysicalObservation | None = None
        self._pending_gap: tuple[PhysicalObservation, str] | None = None

    def retain_during_processing(
        self,
        observation: PhysicalObservation,
        snapshot: FrozenModelSnapshot,
        readiness: ChargeReadiness,
        kind: BlackoutKind,
    ) -> bool:
        if kind not in {
            BlackoutKind.BLACKOUT_REAL,
            BlackoutKind.BLACKOUT_TEST,
            BlackoutKind.ONLINE,
        }:
            return True
        retained = self._retain_start(observation, snapshot, readiness)
        if not retained and self._prestart_loss.overflowed:
            self._submissions.mark_capture_unhealthy("prestart_boundary_overflow")
        return kind == BlackoutKind.ONLINE

    def retry_before_active_observation(
        self,
        request: BoundaryRetryRequest,
        callbacks: BoundaryRetryCallbacks,
    ) -> bool | None:
        if self._pending_gap is not None:
            gap_observation, reason = self._pending_gap
            accepted = self._submissions.gap(
                request.blackout_id,
                gap_observation,
                reason,
                on_failure=callbacks.on_failure(CaptureCommandKind.GAP),
            )
            if accepted:
                self._pending_gap = None
            return False
        if self._pending_end is None:
            return None
        restored = self._pending_end
        if request.kind in {BlackoutKind.BLACKOUT_REAL, BlackoutKind.BLACKOUT_TEST}:
            self._retain_start(request.observation, request.snapshot, request.readiness)
        accepted = self._submissions.end(
            request.blackout_id,
            restored,
            "power_restored",
            on_durable=lambda: callbacks.on_end_durable(restored),
            on_failure=callbacks.on_failure(CaptureCommandKind.END),
        )
        if accepted:
            self._pending_end = None
            callbacks.on_end_submitted()
        return accepted if request.kind == BlackoutKind.ONLINE else False

    def defer_end(self, observation: PhysicalObservation) -> None:
        if self._pending_end is None:
            self._pending_end = observation

    def record_observation_loss(
        self,
        blackout_id: str,
        observation: PhysicalObservation,
        reason: str,
        *,
        on_failure: FailureCallback,
    ) -> None:
        if self._pending_gap is None:
            self._pending_gap = (observation, reason)
        gap_observation, pending_reason = self._pending_gap
        accepted = self._submissions.gap(
            blackout_id,
            gap_observation,
            pending_reason,
            on_failure=on_failure,
        )
        if accepted:
            self._pending_gap = None

    def retry_pending_gap(
        self,
        blackout_id: str,
        *,
        on_failure: FailureCallback,
    ) -> bool | None:
        """Retry a retained GAP before a terminal boundary is submitted.

        ``None`` means there is no retained GAP.  Once a GAP is accepted by
        the submission lane, its state is cleared; a writer execution failure
        remains the writer/recovery lane's responsibility.
        """
        if self._pending_gap is None:
            return None
        gap_observation, reason = self._pending_gap
        accepted = self._submissions.gap(
            blackout_id,
            gap_observation,
            reason,
            on_failure=on_failure,
        )
        if accepted:
            self._pending_gap = None
        return accepted

    def retry_before_service_stop(
        self,
        blackout_id: str,
        observation: PhysicalObservation,
        *,
        unknown_prelude_pending: bool,
        on_failure: Callable[[CaptureCommandKind], FailureCallback],
    ) -> bool:
        """Submit all retained GAP barriers before allowing a service-stop END."""
        pending_gap_result = self.retry_pending_gap(
            blackout_id,
            on_failure=on_failure(CaptureCommandKind.GAP),
        )
        if pending_gap_result is False:
            return False
        if not unknown_prelude_pending:
            return True
        return self._submissions.gap(
            blackout_id,
            observation,
            UNKNOWN_PRELUDE_GAP_REASON,
            on_failure=on_failure(CaptureCommandKind.GAP),
        )

    def _retain_start(
        self,
        observation: PhysicalObservation,
        snapshot: FrozenModelSnapshot,
        readiness: ChargeReadiness,
    ) -> bool:
        return self._prestart_loss.note(
            observation,
            snapshot,
            readiness,
            blackout_id=uuid.uuid4().hex,
            segment_id=uuid.uuid4().hex,
        )
