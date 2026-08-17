"""Thread-safe durable operations for one active blackout event."""

from threading import Lock

from src.application.capture_storage_commands import (
    CaptureStart,
    GapAppend,
    append_end,
    append_gap,
    append_observation,
    append_recovered_end,
    start_capture,
)
from src.application.capture_writer import CaptureCommandKind
from src.application.errors import DurableCaptureTerminalError
from src.application.ports import CaptureRecoveryEventStorePort
from src.application.storage_values import (
    CaptureCloseState,
    EventHandle,
    RecoveredObservation,
)
from src.domain.values import PhysicalObservation


class ActiveCaptureSession:
    """Own the durable handle and serialize its storage-level transitions."""

    def __init__(self, store: CaptureRecoveryEventStorePort) -> None:
        self._store = store
        self._lock = Lock()
        self._handle: EventHandle | None = None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._handle is not None

    def attach(self, handle: EventHandle) -> None:
        with self._lock:
            if self._handle is not None:
                raise RuntimeError("previous blackout capture is still durable and active")
            self._handle = handle

    def clear(self) -> None:
        with self._lock:
            self._handle = None

    def start(self, start: CaptureStart) -> None:
        with self._lock:
            if self._handle is not None:
                raise RuntimeError("previous blackout capture is still durable and active")
        opened = start_capture(self._store, start)
        self.attach(opened)

    def append_observation(self, blackout_id: str, observation: PhysicalObservation) -> None:
        handle = self._require(blackout_id)
        appended = append_observation(self._store, handle, observation)
        self._replace(appended)

    def append_gap(
        self,
        blackout_id: str,
        observation: PhysicalObservation | RecoveredObservation,
        reason: str,
        *,
        failed_command: CaptureCommandKind | None = None,
        error_type: str | None = None,
    ) -> None:
        handle = self._require(blackout_id)
        appended = append_gap(
            self._store,
            handle,
            GapAppend(observation, reason, failed_command, error_type),
        )
        self._replace(appended)

    def gap_and_observe(
        self, blackout_id: str, observation: PhysicalObservation, reason: str
    ) -> None:
        self.append_gap(blackout_id, observation, reason)
        self.append_observation(blackout_id, observation)

    def end(
        self,
        blackout_id: str,
        observation: PhysicalObservation | RecoveredObservation,
        termination: str,
    ) -> None:
        try:
            append_end(self._store, self._require(blackout_id), observation, termination)
        except DurableCaptureTerminalError:
            # The adapter has already sealed and unregistered this event.  The
            # in-memory handle must not be offered to generic damaged-capture
            # recovery, which would append against a terminal file.
            self.clear()
            raise
        self.clear()

    def end_recovered(self, blackout_id: str, observation: RecoveredObservation) -> None:
        append_recovered_end(self._store, self._require(blackout_id), observation)
        self.clear()

    def close_damaged(
        self,
        blackout_id: str,
        command_kind: CaptureCommandKind,
        observation: PhysicalObservation | RecoveredObservation,
        exc: Exception,
    ) -> None:
        durable_state, durable_handle = self._durable_close_state(blackout_id)
        if durable_state is CaptureCloseState.OUTCOME:
            self.clear()
            return
        if durable_handle is not None:
            self._replace(durable_handle)
        if durable_state is CaptureCloseState.END:
            if durable_handle is None:
                raise RuntimeError("durable END has no reconstructed event handle")
            self._store.checkpoint_processing(durable_handle, "capture_damaged")
            self.clear()
            return
        self.append_gap(
            blackout_id,
            observation,
            f"{command_kind.value}_execution_failure",
            failed_command=command_kind,
            error_type=type(exc).__name__,
        )
        ended = append_end(
            self._store,
            self._require(blackout_id),
            observation,
            "capture_damaged",
        )
        self._store.checkpoint_processing(ended, "capture_damaged")
        self.clear()

    def _durable_close_state(
        self,
        blackout_id: str,
    ) -> tuple[CaptureCloseState, EventHandle | None]:
        """Read the adapter-owned registry tail for a suffix-only retry."""
        current = self._current_handle(blackout_id)
        reconciliation = self._store.reconcile_damaged_close(blackout_id, current)
        return reconciliation.state, reconciliation.handle

    def clear_capture_health(self) -> None:
        self._store.acknowledge_capture_recovery()

    def _current_handle(self, blackout_id: str) -> EventHandle | None:
        with self._lock:
            handle = self._handle
        if handle is None or handle.blackout_id != blackout_id:
            return None
        return handle

    def _replace(self, handle: EventHandle) -> None:
        with self._lock:
            self._handle = handle

    def _require(self, blackout_id: str) -> EventHandle:
        with self._lock:
            handle = self._handle
        if handle is None:
            raise RuntimeError("blackout capture has no durable start")
        if handle.blackout_id != blackout_id:
            raise RuntimeError("capture command belongs to a different blackout")
        return handle
