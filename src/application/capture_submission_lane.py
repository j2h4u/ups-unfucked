"""Typed writer submissions for normal blackout capture operations."""

from collections.abc import Callable

from src.application.active_capture_session import ActiveCaptureSession
from src.application.capture_storage_commands import CaptureStart
from src.application.capture_writer import (
    CaptureCommand,
    CaptureCommandKind,
    CaptureWriter,
    RecoveryDisposition,
)
from src.domain.values import PhysicalObservation

FailureCallback = Callable[[Exception], bool | RecoveryDisposition]


class CaptureSubmissionLane:
    """Translate lifecycle intent into bounded sole-writer commands."""

    def __init__(self, session: ActiveCaptureSession, writer: CaptureWriter) -> None:
        self._session = session
        self._writer = writer

    def start(
        self,
        start: CaptureStart,
        *,
        on_durable: Callable[[], None],
        on_failure: FailureCallback,
    ) -> bool:
        def execute() -> None:
            self._session.start(start)
            on_durable()

        return self._submit(CaptureCommandKind.START, start.blackout_id, execute, on_failure)

    def observe(
        self,
        blackout_id: str,
        observation: PhysicalObservation,
        *,
        on_failure: FailureCallback,
    ) -> bool:
        return self._submit(
            CaptureCommandKind.OBSERVATION,
            blackout_id,
            lambda: self._session.append_observation(blackout_id, observation),
            on_failure,
        )

    def gap_and_observe(
        self,
        blackout_id: str,
        observation: PhysicalObservation,
        reason: str,
        *,
        on_failure: FailureCallback,
    ) -> bool:
        return self._submit(
            CaptureCommandKind.GAP,
            blackout_id,
            lambda: self._session.gap_and_observe(blackout_id, observation, reason),
            on_failure,
        )

    def gap(
        self,
        blackout_id: str,
        observation: PhysicalObservation,
        reason: str,
        *,
        on_failure: FailureCallback,
    ) -> bool:
        return self._submit(
            CaptureCommandKind.GAP,
            blackout_id,
            lambda: self._session.append_gap(blackout_id, observation, reason),
            on_failure,
        )

    def end(
        self,
        blackout_id: str,
        observation: PhysicalObservation,
        termination: str,
        *,
        on_durable: Callable[[], None],
        on_failure: FailureCallback,
    ) -> bool:
        def execute() -> None:
            self._session.end(blackout_id, observation, termination)
            on_durable()

        return self._submit(CaptureCommandKind.END, blackout_id, execute, on_failure)

    def mark_capture_unhealthy(self, reason: str) -> None:
        """Publish a bounded capture-lane health latch from boundary recovery."""
        self._writer.mark_capture_unhealthy(reason)

    def _submit(
        self,
        kind: CaptureCommandKind,
        scope_id: str,
        execute: Callable[[], None],
        on_failure: FailureCallback,
    ) -> bool:
        return self._writer.submit(
            CaptureCommand(
                kind=kind,
                execute=execute,
                scope_id=scope_id,
                recover_failure=on_failure,
            )
        )
