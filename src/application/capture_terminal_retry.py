"""Serialized, bounded retry of a failed capture-damaged close."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from threading import Lock

from src.application.active_capture_session import ActiveCaptureSession
from src.application.capture_writer import (
    CaptureCommand,
    CaptureCommandKind,
    CaptureWriter,
)
from src.application.prestart_loss import PrestartLossTracker
from src.application.storage_values import RecoveredObservation
from src.domain.values import (
    ChargeReadiness,
    FrozenModelSnapshot,
    PhysicalObservation,
)

TERMINAL_RETRY_BASE_DELAY_S = 1.0
TERMINAL_RETRY_MAX_DELAY_S = 60.0


@dataclass(frozen=True, slots=True)
class TerminalCloseRetry:
    """Bounded in-memory retry for a failed capture-damage close."""

    blackout_id: str
    command_kind: CaptureCommandKind
    observation: PhysicalObservation | RecoveredObservation
    error: Exception
    attempt: int
    next_due_monotonic: float
    queued: bool = False


class TerminalCloseRetryLane:
    """Keep retry state off the safety poll while using the sole writer."""

    def __init__(
        self,
        writer: CaptureWriter,
        session: ActiveCaptureSession,
        on_success: Callable[[], None],
        *,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self._writer = writer
        self._session = session
        self._on_success = on_success
        self._clock = monotonic_clock or writer.monotonic_clock
        self._lock = Lock()
        self._retry: TerminalCloseRetry | None = None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._retry is not None

    def arm(
        self,
        blackout_id: str,
        command_kind: CaptureCommandKind,
        observation: PhysicalObservation | RecoveredObservation,
        exc: Exception,
    ) -> None:
        with self._lock:
            self._retry = TerminalCloseRetry(
                blackout_id,
                command_kind,
                observation,
                exc,
                0,
                self._clock() + TERMINAL_RETRY_BASE_DELAY_S,
            )

    def retain_boundary(
        self,
        tracker: PrestartLossTracker,
        observation: PhysicalObservation,
        snapshot: FrozenModelSnapshot,
        readiness: ChargeReadiness,
    ) -> None:
        if not self.active:
            return
        retained = tracker.note(
            observation,
            snapshot,
            readiness,
            blackout_id=uuid.uuid4().hex,
            segment_id=uuid.uuid4().hex,
        )
        if not retained and tracker.overflowed:
            self._writer.mark_capture_unhealthy("prestart_boundary_overflow")

    def submit_if_due(self) -> None:
        with self._lock:
            retry = self._retry
            if retry is None or retry.queued or self._clock() < retry.next_due_monotonic:
                return
            queued_retry = replace(retry, queued=True)
            self._retry = queued_retry
        self._writer.clear_failed_scope(retry.blackout_id)
        accepted = self._writer.submit(
            CaptureCommand(
                kind=CaptureCommandKind.TERMINAL_RETRY,
                execute=lambda: self._execute(queued_retry),
                scope_id=retry.blackout_id,
                recover_failure=lambda exc: self._failure(queued_retry, exc),
            )
        )
        if accepted:
            return
        self._writer.fail_scope(retry.blackout_id)
        with self._lock:
            self._retry = _advance_retry(retry, self._clock())

    def _execute(self, retry: TerminalCloseRetry) -> None:
        self._session.close_damaged(
            retry.blackout_id,
            retry.command_kind,
            retry.observation,
            retry.error,
        )
        self._session.clear()
        self._session.clear_capture_health()
        self._writer.release_capture_scope(retry.blackout_id)
        self._writer.restore_capture_health()
        with self._lock:
            self._retry = None
        self._on_success()

    def _failure(self, retry: TerminalCloseRetry, _exc: Exception) -> bool:
        self._writer.clear_failed_scope(retry.blackout_id)
        with self._lock:
            if self._retry is not None:
                self._retry = _advance_retry(
                    replace(self._retry, queued=False),
                    self._clock(),
                )
        return False


def _advance_retry(retry: TerminalCloseRetry, now: float) -> TerminalCloseRetry:
    attempt = min(retry.attempt + 1, 6)
    delay = min(TERMINAL_RETRY_MAX_DELAY_S, TERMINAL_RETRY_BASE_DELAY_S * (2**attempt))
    return replace(retry, attempt=attempt, next_due_monotonic=now + delay, queued=False)
