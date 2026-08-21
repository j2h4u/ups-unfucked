"""Safety-first handoff from physical observations to per-blackout storage."""

import uuid
from collections.abc import Callable
from threading import Lock
from typing import TypeGuard

from src.application.active_capture_session import ActiveCaptureSession
from src.application.capture_boundary_recovery import (
    BoundaryRetryCallbacks,
    BoundaryRetryRequest,
    CaptureBoundaryRecovery,
)
from src.application.capture_storage_commands import CaptureStart
from src.application.capture_submission_lane import CaptureSubmissionLane
from src.application.capture_terminal_retry import TerminalCloseRetryLane
from src.application.capture_writer import (
    CaptureCommand,
    CaptureCommandKind,
    CaptureWriter,
    RecoveryDisposition,
)
from src.application.errors import DurableCaptureTerminalError
from src.application.ports import CaptureRecoveryEventStorePort
from src.application.prestart_loss import (
    OverflowDeliveryReservation,
    PrestartLossTracker,
    PrestartRecoveryCallbacks,
    PrestartRecoveryLane,
    PrestartRecoveryResult,
    PrestartRetention,
)
from src.application.storage_values import (
    EventHandle,
    RecoveredCapture,
    RecoveredObservation,
)
from src.domain.lifecycle import (
    UNKNOWN_PRELUDE_GAP_REASON,
    LifecycleSignal,
    LifecycleState,
    advance_lifecycle,
    is_capture_candidate,
    is_unknown_outage_candidate,
)
from src.domain.values import (
    BlackoutKind,
    ChargeReadiness,
    FrozenModelSnapshot,
    PhysicalObservation,
)


class RuntimeErrorBoundary(RuntimeError):
    """A capture bookkeeping invariant must degrade the poll handoff."""


class _BlackoutCaptureRecovery:
    """Own failure transitions so callbacks cannot bypass the capture aggregate."""

    def __init__(self, capture: "BlackoutCapture") -> None:
        self._capture = capture

    def recover_failed_start(self, start: CaptureStart) -> bool | RecoveryDisposition:
        capture = self._capture
        blackout_id = start.blackout_id
        try:
            recovered = capture._store.recover_startup()
        except Exception:
            recovered = None
        if recovered is None or recovered.blackout_id != blackout_id:
            with capture._submission_lock:
                retention = capture._prestart_loss.retain(start)
                self.reset_prestart_submission_locked(start.observation)
            if retention is PrestartRetention.AGGREGATED:
                return RecoveryDisposition.TERMINAL_SUCCESS
            capture._writer.clear_failed_scope(blackout_id)
            return False
        with capture._submission_lock:
            if (
                capture._lifecycle_state != LifecycleState.PREPARING
                or capture._submitted_blackout_id != blackout_id
                or capture._session.active
            ):
                raise RuntimeError("recovered START does not match submitted capture")
            capture._session.attach(recovered.handle)
            if capture._sticky_recovery_expired:
                capture._lifecycle_state = advance_lifecycle(
                    capture._lifecycle_state, None, LifecycleSignal.STICKY_RECOVERY_TIMEOUT
                ).state_after
                return False
            capture._lifecycle_state = advance_lifecycle(
                capture._lifecycle_state,
                None,
                LifecycleSignal.CAPTURE_PREPARED,
            ).state_after
        return True

    def recover_active_failure(
        self,
        blackout_id: str,
        command_kind: CaptureCommandKind,
        observation: PhysicalObservation | RecoveredObservation,
        exc: Exception,
    ) -> bool | RecoveryDisposition:
        capture = self._capture
        if isinstance(exc, DurableCaptureTerminalError):
            self.finish_durable_terminal_scope()
            return RecoveryDisposition.TERMINAL_SUCCESS
        try:
            capture._session.close_damaged(blackout_id, command_kind, observation, exc)
        except Exception:
            capture._terminal_recovery.arm(blackout_id, command_kind, observation, exc)
            self._mark_capture_failure(observation)
            raise
        self._mark_capture_failure(observation)
        self.finish_terminal_scope()
        return False

    def _mark_capture_failure(
        self, observation: PhysicalObservation | RecoveredObservation
    ) -> None:
        capture = self._capture
        physical = observation if isinstance(observation, PhysicalObservation) else None
        with capture._submission_lock:
            capture._lifecycle_state = advance_lifecycle(
                capture._lifecycle_state,
                physical,
                LifecycleSignal.CAPTURE_FAILURE,
            ).state_after

    def finish_terminal_retry(self) -> None:
        capture = self._capture
        with capture._submission_lock:
            capture._unknown_prelude_pending = False
            capture._submitted_boot_id = None
            capture._submitted_blackout_id = None
            capture._lifecycle_state = advance_lifecycle(
                capture._lifecycle_state,
                capture._last_observation,
                LifecycleSignal.TERMINAL_RESET,
            ).state_after

    def finish_terminal_scope(self) -> None:
        capture = self._capture
        with capture._submission_lock:
            capture._unknown_prelude_pending = False
            blackout_id = capture._submitted_blackout_id
            capture._submitted_boot_id = None
            capture._submitted_blackout_id = None
            capture._session.clear()
            capture._prestart_loss.mark_damaged(_observation_kind(capture._last_observation))
            capture._lifecycle_state = advance_lifecycle(
                capture._lifecycle_state, None, LifecycleSignal.TERMINAL_RESET
            ).state_after
        if blackout_id is not None:
            capture._writer.release_capture_scope(blackout_id)

    def finish_durable_terminal_scope(self) -> None:
        capture = self._capture
        with capture._submission_lock:
            capture._unknown_prelude_pending = False
            blackout_id = capture._submitted_blackout_id
            capture._submitted_boot_id = None
            capture._submitted_blackout_id = None
            capture._session.clear()
            capture._lifecycle_state = advance_lifecycle(
                capture._lifecycle_state,
                capture._last_observation,
                LifecycleSignal.CAPTURE_END_DURABLE,
            ).state_after
        if blackout_id is not None:
            capture._writer.release_capture_scope(blackout_id)

    def close_partial_prestart(
        self,
        recovered: RecoveredCapture,
        observation: PhysicalObservation,
        exc: Exception,
        overflow_delivery: OverflowDeliveryReservation | None,
    ) -> None:
        capture = self._capture
        with capture._submission_lock:
            capture._session.attach(recovered.handle)
            if capture._lifecycle_state == LifecycleState.PREPARING:
                capture._lifecycle_state = advance_lifecycle(
                    capture._lifecycle_state,
                    observation,
                    LifecycleSignal.CAPTURE_PREPARED,
                ).state_after
            capture._prestart_loss.release_overflow_delivery(overflow_delivery)
            capture._prestart_loss.mark_durable()
        self.recover_active_failure(
            recovered.blackout_id, CaptureCommandKind.START, observation, exc
        )

    def flush_prestart_on_stop(
        self, pending: CaptureStart, observation: PhysicalObservation
    ) -> bool:
        capture = self._capture
        overflow_delivery = capture._prestart_loss.reserve_overflow_delivery()
        accepted = capture._prestart_recovery.submit(
            pending,
            capture._prestart_loss.pending_boundary or observation,
            termination="service_stop",
            overflow_delivery=overflow_delivery,
        )
        if not accepted:
            capture._prestart_loss.release_overflow_delivery(overflow_delivery)
        return accepted

    def end_recovered(self, recovered: RecoveredCapture) -> None:
        capture = self._capture
        observation = recovered.last_observation
        capture._session.end_recovered(recovered.handle.blackout_id, observation)
        with capture._submission_lock:
            capture._unknown_prelude_pending = False
            capture._lifecycle_state = advance_lifecycle(
                capture._lifecycle_state,
                None,
                LifecycleSignal.CAPTURE_END_DURABLE,
            ).state_after
        capture._on_power_restored(recovered.handle.blackout_id)

    def mark_end_durable(
        self, observation: PhysicalObservation, blackout_id: str | None = None
    ) -> None:
        capture = self._capture
        with capture._submission_lock:
            capture._unknown_prelude_pending = False
            capture._lifecycle_state = advance_lifecycle(
                capture._lifecycle_state,
                observation,
                LifecycleSignal.CAPTURE_END_DURABLE,
            ).state_after
        if blackout_id is not None:
            capture._on_power_restored(blackout_id)

    def mark_end_submitted(self, state: LifecycleState) -> None:
        capture = self._capture
        capture._unknown_prelude_pending = False
        capture._submitted_boot_id = None
        capture._submitted_blackout_id = None
        capture._lifecycle_state = state

    def accept_prestart_result(
        self, result: PrestartRecoveryResult, observation: PhysicalObservation
    ) -> None:
        capture = self._capture
        with capture._submission_lock:
            _settle_prestart_overflow(capture._prestart_loss, result)
            capture._prestart_loss.mark_durable()
            if result.handle is None:
                capture._unknown_prelude_pending = False
                capture._session.clear()
                capture._submitted_boot_id = None
                capture._submitted_blackout_id = None
                signal = LifecycleSignal.CAPTURE_END_DURABLE
            else:
                capture._session.attach(result.handle)
                if result.current_kind != BlackoutKind.UNKNOWN:
                    capture._unknown_prelude_pending = False
                signal = LifecycleSignal.CAPTURE_PREPARED
            capture._lifecycle_state = advance_lifecycle(
                capture._lifecycle_state, observation, signal
            ).state_after

    def reset_prestart_submission(
        self,
        observation: PhysicalObservation,
        overflow_delivery: OverflowDeliveryReservation | None,
    ) -> None:
        capture = self._capture
        with capture._submission_lock:
            capture._prestart_loss.release_overflow_delivery(overflow_delivery)
            self.reset_prestart_submission_locked(observation)

    def reset_prestart_submission_locked(self, observation: PhysicalObservation) -> None:
        capture = self._capture
        capture._submitted_boot_id = None
        capture._submitted_blackout_id = None
        capture._lifecycle_state = advance_lifecycle(
            capture._lifecycle_state, observation, LifecycleSignal.START_REJECTED
        ).state_after


class BlackoutCapture:
    """Convert the physical OL/OB lifecycle into serialized writer commands."""

    def __init__(
        self,
        store: CaptureRecoveryEventStorePort,
        writer: CaptureWriter,
        *,
        monotonic_clock: Callable[[], float] | None = None,
        on_power_restored: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._writer = writer
        self._monotonic_clock = monotonic_clock or writer.monotonic_clock
        self._submission_lock = Lock()
        self._lifecycle_state = LifecycleState.IDLE
        self._submitted_boot_id: str | None = None
        self._submitted_blackout_id: str | None = None
        self._session = ActiveCaptureSession(store)
        self._capture_recovery = _BlackoutCaptureRecovery(self)
        self._terminal_recovery = TerminalCloseRetryLane(
            writer,
            self._session,
            self.finish_terminal_retry,
            monotonic_clock=self._monotonic_clock,
        )
        self._submissions = CaptureSubmissionLane(self._session, writer)
        self._sticky_recovery_expired = False
        self._last_observation: PhysicalObservation | None = None
        self._unknown_prelude_pending = False
        self._prestart_loss = PrestartLossTracker()
        self._on_power_restored = on_power_restored or (lambda _blackout_id: None)
        recovery_callbacks = PrestartRecoveryCallbacks(
            self._capture_recovery.accept_prestart_result,
            self._capture_recovery.reset_prestart_submission,
            self._capture_recovery.close_partial_prestart,
        )
        self._prestart_recovery = PrestartRecoveryLane(store, writer, recovery_callbacks)
        self._boundary_recovery = CaptureBoundaryRecovery(self._submissions, self._prestart_loss)

    def recover_failed_start(self, start: CaptureStart) -> bool | RecoveryDisposition:
        return self._capture_recovery.recover_failed_start(start)

    def recover_active_failure(
        self,
        blackout_id: str,
        command_kind: CaptureCommandKind,
        observation: PhysicalObservation | RecoveredObservation,
        exc: Exception,
    ) -> bool | RecoveryDisposition:
        return self._capture_recovery.recover_active_failure(
            blackout_id, command_kind, observation, exc
        )

    def finish_terminal_retry(self) -> None:
        self._capture_recovery.finish_terminal_retry()

    def finish_terminal_scope(self) -> None:
        self._capture_recovery.finish_terminal_scope()

    def finish_durable_terminal_scope(self) -> None:
        self._capture_recovery.finish_durable_terminal_scope()

    def mark_end_durable(self, observation: PhysicalObservation) -> None:
        self._capture_recovery.mark_end_durable(observation)

    def note_capture_unavailable(
        self,
        observation: PhysicalObservation,
        snapshot: FrozenModelSnapshot,
        readiness: ChargeReadiness,
    ) -> None:
        with self._submission_lock:
            self._last_observation = observation
            if self._lifecycle_state == LifecycleState.CAPTURE_DAMAGED:
                self._terminal_recovery.retain_boundary(
                    self._prestart_loss,
                    observation,
                    snapshot,
                    readiness,
                )
                return
            if self._lifecycle_state != LifecycleState.IDLE:
                return
            self._prestart_loss.note(
                observation,
                snapshot,
                readiness,
                blackout_id=uuid.uuid4().hex,
                segment_id=uuid.uuid4().hex,
            )

    @property
    def has_unacknowledged_capture(self) -> bool:
        """Return memory-only durability state used by sticky virtual-LB clearing."""
        with self._submission_lock:
            return self._lifecycle_state in {
                LifecycleState.PREPARING,
                LifecycleState.CAPTURING,
                LifecycleState.PROCESSING,
            }

    @property
    def active_blackout_id(self) -> str | None:
        """Expose the immutable active identity for linked post-restoration capture."""
        with self._submission_lock:
            return self._submitted_blackout_id

    def expire_sticky_recovery(self, reason: str) -> bool:
        """Stop waiting for blocked storage and disable learning for its event."""
        with self._submission_lock:
            if self._sticky_recovery_expired:
                return False
            if self._lifecycle_state not in {
                LifecycleState.PREPARING,
                LifecycleState.CAPTURING,
                LifecycleState.PROCESSING,
            }:
                return False
            scope_id = self._submitted_blackout_id
            self._sticky_recovery_expired = True
            self._lifecycle_state = advance_lifecycle(
                self._lifecycle_state,
                self._last_observation,
                LifecycleSignal.STICKY_RECOVERY_TIMEOUT,
            ).state_after
        self._writer.mark_capture_unhealthy(reason, scope_id=scope_id)
        return True

    def attach_recovered_capture(self, handle: EventHandle, *, boot_id: str) -> None:
        if not boot_id:
            raise ValueError("recovered capture boot_id must be non-empty")
        if handle.next_seq < 1 or len(handle.last_record_sha256) != 64:
            raise ValueError("recovered capture handle is invalid")
        with self._submission_lock:
            if self._lifecycle_state == LifecycleState.CAPTURE_DAMAGED:
                raise RuntimeErrorBoundary(
                    "blackout capture is unavailable after terminal recovery failure"
                )
            if self._lifecycle_state != LifecycleState.IDLE or self._session.active:
                raise RuntimeErrorBoundary("blackout capture is already active")
            self._lifecycle_state = advance_lifecycle(
                LifecycleState.IDLE, None, LifecycleSignal.RECOVERED_CAPTURE_ATTACH
            ).state_after
            self._submitted_boot_id = boot_id
            self._submitted_blackout_id = handle.blackout_id
            self._session.attach(handle)

    def close_recovered_capture(self, recovered: RecoveredCapture) -> bool:
        """End a startup-OL capture at its last durable pre-restart observation."""
        handle = recovered.handle
        with self._submission_lock:
            if (
                self._sticky_recovery_expired
                or self._lifecycle_state == LifecycleState.CAPTURE_DAMAGED
            ):
                return False
            if self._lifecycle_state != LifecycleState.IDLE or self._session.active:
                raise RuntimeErrorBoundary("blackout capture is already active")
            self._lifecycle_state = advance_lifecycle(
                LifecycleState.IDLE, None, LifecycleSignal.RECOVERED_CAPTURE_ATTACH
            ).state_after
            self._submitted_boot_id = recovered.last_boot_id
            self._submitted_blackout_id = handle.blackout_id
            self._session.attach(handle)
            accepted = self._writer.submit(
                CaptureCommand(
                    kind=CaptureCommandKind.END,
                    execute=lambda: self._capture_recovery.end_recovered(recovered),
                    scope_id=handle.blackout_id,
                    recover_failure=lambda exc: self.recover_active_failure(
                        handle.blackout_id,
                        CaptureCommandKind.END,
                        recovered.last_observation,
                        exc,
                    ),
                )
            )
            if accepted:
                self._submitted_boot_id = None
                self._submitted_blackout_id = None
                self._lifecycle_state = advance_lifecycle(
                    self._lifecycle_state, None, LifecycleSignal.CAPTURE_END_SUBMITTED
                ).state_after
            else:
                self._session.clear()
                self._submitted_boot_id = None
                self._submitted_blackout_id = None
                self._lifecycle_state = advance_lifecycle(
                    self._lifecycle_state, None, LifecycleSignal.START_REJECTED
                ).state_after
            return accepted

    def accept_after_safety_publish(
        self,
        observation: PhysicalObservation,
        *,
        safety_snapshot: FrozenModelSnapshot,
        charge_readiness: ChargeReadiness,
    ) -> bool:
        """Submit capture work without performing filesystem I/O on the poll."""
        with self._submission_lock:
            transition = advance_lifecycle(
                self._lifecycle_state, observation, LifecycleSignal.OBSERVATION
            )
            kind = transition.blackout_kind
            self._last_observation = observation
            if (
                self._sticky_recovery_expired
                or self._lifecycle_state == LifecycleState.CAPTURE_DAMAGED
            ):
                if not self._sticky_recovery_expired:
                    self._terminal_recovery.retain_boundary(
                        self._prestart_loss,
                        observation,
                        safety_snapshot,
                        charge_readiness,
                    )
                    self._terminal_recovery.submit_if_due()
                return False
            if self._prestart_loss.blocks(kind):
                return False
            if self._lifecycle_state == LifecycleState.IDLE and self._prestart_loss.pending:
                return self._submit_prestart_recovery(observation)
            if self._lifecycle_state == LifecycleState.IDLE:
                return self._start_if_blackout(
                    observation, safety_snapshot, charge_readiness, transition.state_after, kind
                )
            if self._lifecycle_state == LifecycleState.PROCESSING:
                return self._boundary_recovery.retain_during_processing(
                    observation, safety_snapshot, charge_readiness, kind
                )
            return self._accept_active(
                observation,
                safety_snapshot,
                charge_readiness,
                kind,
                transition.state_after,
            )

    def _start_if_blackout(
        self,
        observation: PhysicalObservation,
        snapshot: FrozenModelSnapshot,
        readiness: ChargeReadiness,
        next_state: LifecycleState,
        kind: BlackoutKind,
    ) -> bool:
        if kind not in {BlackoutKind.BLACKOUT_REAL, BlackoutKind.BLACKOUT_TEST}:
            if not is_capture_candidate(observation):
                return True
        start = CaptureStart(uuid.uuid4().hex, uuid.uuid4().hex, observation, snapshot, readiness)
        self._unknown_prelude_pending = is_unknown_outage_candidate(observation)
        self._lifecycle_state = next_state
        self._submitted_boot_id = observation.boot_id
        self._submitted_blackout_id = start.blackout_id
        accepted = self._submissions.start(
            start,
            on_durable=lambda: self._start_durable(start),
            on_failure=lambda _exc: self.recover_failed_start(start),
        )
        if not accepted:
            self._prestart_loss.retain(start)
            self._capture_recovery.reset_prestart_submission_locked(observation)
        return accepted

    def _accept_active(
        self,
        observation: PhysicalObservation,
        snapshot: FrozenModelSnapshot,
        readiness: ChargeReadiness,
        kind: BlackoutKind,
        next_state: LifecycleState,
    ) -> bool:
        blackout_id = _required_blackout_id(self._submitted_blackout_id)

        def failure(command: CaptureCommandKind):
            return lambda exc: self.recover_active_failure(blackout_id, command, observation, exc)

        barrier_result = self._boundary_recovery.retry_before_active_observation(
            BoundaryRetryRequest(blackout_id, observation, snapshot, readiness, kind),
            BoundaryRetryCallbacks(
                lambda restored: self._capture_recovery.mark_end_durable(restored, blackout_id),
                lambda: self._capture_recovery.mark_end_submitted(LifecycleState.PROCESSING),
                failure,
            ),
        )
        if barrier_result is not None:
            return barrier_result

        prelude_result = self._submit_unknown_prelude_boundary(
            blackout_id,
            observation,
            kind,
            failure(CaptureCommandKind.GAP),
        )
        if prelude_result is not None:
            return prelude_result

        if kind == BlackoutKind.ONLINE:
            accepted = self._submissions.end(
                blackout_id,
                observation,
                "power_restored",
                on_durable=lambda: self._capture_recovery.mark_end_durable(
                    observation, blackout_id
                ),
                on_failure=failure(CaptureCommandKind.END),
            )
            if accepted:
                self._capture_recovery.mark_end_submitted(next_state)
            else:
                self._boundary_recovery.defer_end(observation)
            return accepted
        if observation.boot_id != self._submitted_boot_id:
            accepted = self._submissions.gap_and_observe(
                blackout_id,
                observation,
                "boot_changed",
                on_failure=failure(CaptureCommandKind.GAP),
            )
            if accepted:
                self._submitted_boot_id = observation.boot_id
            else:
                self._boundary_recovery.record_observation_loss(
                    blackout_id,
                    observation,
                    "boot_change_queue_overflow",
                    on_failure=failure(CaptureCommandKind.GAP),
                )
            return accepted
        accepted = self._submissions.observe(
            blackout_id,
            observation,
            on_failure=failure(CaptureCommandKind.OBSERVATION),
        )
        if not accepted:
            self._boundary_recovery.record_observation_loss(
                blackout_id,
                observation,
                "observation_queue_overflow",
                on_failure=failure(CaptureCommandKind.GAP),
            )
        return accepted

    def _submit_unknown_prelude_boundary(
        self,
        blackout_id: str,
        observation: PhysicalObservation,
        kind: BlackoutKind,
        on_failure: Callable[[Exception], bool | RecoveryDisposition],
    ) -> bool | None:
        if not self._unknown_prelude_pending or kind == BlackoutKind.UNKNOWN:
            return None
        same_boot_non_online = (
            observation.boot_id == self._submitted_boot_id and kind != BlackoutKind.ONLINE
        )
        if same_boot_non_online:
            accepted = self._submissions.gap_and_observe(
                blackout_id,
                observation,
                UNKNOWN_PRELUDE_GAP_REASON,
                on_failure=on_failure,
            )
            if accepted:
                self._unknown_prelude_pending = False
            return accepted
        accepted = self._submissions.gap(
            blackout_id,
            observation,
            UNKNOWN_PRELUDE_GAP_REASON,
            on_failure=on_failure,
        )
        if not accepted:
            return False
        self._unknown_prelude_pending = False
        return None

    def service_stop(self, observation: PhysicalObservation) -> bool:
        with self._submission_lock:
            if self._lifecycle_state == LifecycleState.CAPTURE_DAMAGED:
                return True
            pending = self._prestart_loss.pending
            if _should_flush_prestart_on_stop(
                pending, self._lifecycle_state
            ) and not _prestart_recovery_in_flight(
                pending, self._lifecycle_state, self._submitted_blackout_id
            ):
                return self._capture_recovery.flush_prestart_on_stop(pending, observation)
            if self._lifecycle_state == LifecycleState.PROCESSING:
                return True
            if self._lifecycle_state not in {
                LifecycleState.PREPARING,
                LifecycleState.CAPTURING,
            }:
                return True
            blackout_id = _required_blackout_id(self._submitted_blackout_id)
            unknown_prelude_pending = self._unknown_prelude_pending
            if not self._boundary_recovery.retry_before_service_stop(
                blackout_id,
                observation,
                unknown_prelude_pending=unknown_prelude_pending,
                on_failure=lambda command: (
                    lambda exc: self.recover_active_failure(blackout_id, command, observation, exc)
                ),
            ):
                return False
            if unknown_prelude_pending:
                self._unknown_prelude_pending = False
            accepted = self._submissions.end(
                blackout_id,
                observation,
                "service_stop",
                on_durable=lambda: self._capture_recovery.mark_end_durable(observation),
                on_failure=lambda exc: self.recover_active_failure(
                    blackout_id, CaptureCommandKind.END, observation, exc
                ),
            )
            if accepted:
                self._submitted_boot_id = None
                self._submitted_blackout_id = None
                self._lifecycle_state = advance_lifecycle(
                    self._lifecycle_state, observation, LifecycleSignal.SERVICE_STOP
                ).state_after
            return accepted

    def _start_durable(self, start: CaptureStart) -> None:
        with self._submission_lock:
            if (
                self._sticky_recovery_expired
                or self._lifecycle_state == LifecycleState.CAPTURE_DAMAGED
            ):
                signal = LifecycleSignal.STICKY_RECOVERY_TIMEOUT
            else:
                signal = LifecycleSignal.CAPTURE_PREPARED
            self._lifecycle_state = advance_lifecycle(
                self._lifecycle_state, start.observation, signal
            ).state_after

    def _submit_prestart_recovery(self, observation: PhysicalObservation) -> bool:
        start = self._prestart_loss.pending
        if start is None:
            raise RuntimeErrorBoundary("pre-start recovery has no retained start")
        boundary = self._prestart_loss.pending_boundary or observation
        boundary_kind = _observation_kind(boundary)
        self._lifecycle_state = advance_lifecycle(
            LifecycleState.IDLE, start.observation, LifecycleSignal.OBSERVATION
        ).state_after
        self._submitted_boot_id = boundary.boot_id
        self._submitted_blackout_id = start.blackout_id
        overflow_delivery = self._prestart_loss.reserve_overflow_delivery()
        accepted = self._prestart_recovery.submit(
            start,
            boundary,
            overflow_delivery=overflow_delivery,
        )
        if accepted and boundary_kind == BlackoutKind.ONLINE:
            self._lifecycle_state = advance_lifecycle(
                self._lifecycle_state,
                boundary,
                LifecycleSignal.CAPTURE_END_SUBMITTED,
            ).state_after
        elif not accepted:
            self._prestart_loss.release_overflow_delivery(overflow_delivery)
            self._submitted_boot_id = None
            self._submitted_blackout_id = None
            self._lifecycle_state = advance_lifecycle(
                self._lifecycle_state, observation, LifecycleSignal.START_REJECTED
            ).state_after
        return accepted


def _required_blackout_id(value: str | None) -> str:
    if value is None:
        raise RuntimeErrorBoundary("active capture has no submitted blackout id")
    return value


def _observation_kind(observation: PhysicalObservation | None) -> BlackoutKind:
    if observation is None:
        return BlackoutKind.UNKNOWN
    return advance_lifecycle(
        LifecycleState.IDLE, observation, LifecycleSignal.OBSERVATION
    ).blackout_kind


def _should_flush_prestart_on_stop(
    pending: CaptureStart | None, state: LifecycleState
) -> TypeGuard[CaptureStart]:
    return pending is not None and state in {LifecycleState.IDLE, LifecycleState.PROCESSING}


def _prestart_recovery_in_flight(
    pending: CaptureStart | None,
    state: LifecycleState,
    submitted_blackout_id: str | None,
) -> bool:
    return (
        pending is not None
        and state is LifecycleState.PROCESSING
        and submitted_blackout_id == pending.blackout_id
    )


def _settle_prestart_overflow(
    tracker: PrestartLossTracker,
    result: PrestartRecoveryResult,
) -> None:
    if result.durable_overflow_reservation is not None:
        tracker.acknowledge_overflow_delivery(result.durable_overflow_reservation)
    else:
        tracker.release_overflow_delivery(result.attempted_overflow_reservation)
