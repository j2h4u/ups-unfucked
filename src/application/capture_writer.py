"""Small priority lane that keeps durable capture off the safety poll."""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Condition, Thread

from src.application.storage_values import CaptureQueueHealth

OBSERVATION_CAPACITY = 120
LIFECYCLE_CAPACITY = 8


class CaptureCommandKind(StrEnum):
    START = "start"
    END = "end"
    GAP = "gap"
    RECOVERY_RECEIPT = "recovery_receipt"
    MODEL_COMMIT = "model_commit"
    OBSERVATION = "observation"
    TERMINAL_RETRY = "terminal_retry"


class RecoveryDisposition(StrEnum):
    """Result of handling a failed writer command."""

    FAILED = "failed"
    RECOVERED = "recovered"
    TERMINAL_SUCCESS = "terminal_success"


@dataclass(frozen=True, slots=True)
class CaptureCommand:
    kind: CaptureCommandKind
    execute: Callable[[], None]
    scope_id: str | None = None
    recover_failure: Callable[[Exception], bool | RecoveryDisposition] | None = None

    @property
    def lifecycle_priority(self) -> bool:
        return self.kind != CaptureCommandKind.OBSERVATION


@dataclass(frozen=True, slots=True)
class _QueuedCommand:
    submission_order: int
    command: CaptureCommand
    submitted_monotonic: float


class CaptureWriter:
    """Sole serialized execution lane for event and model writes."""

    def __init__(
        self,
        *,
        on_failure: Callable[[CaptureCommand, Exception], None] | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ):
        self._lifecycle: deque[_QueuedCommand] = deque()
        self._observations: deque[_QueuedCommand] = deque()
        self._maintenance: deque[_QueuedCommand] = deque()
        self._condition = Condition()
        self._running = False
        self._thread: Thread | None = None
        self._closed = False
        self._manual_draining = False
        self._failed_scopes: set[str] = set()
        self._next_submission_order = 0
        self._on_failure = on_failure
        self._capture_available = True
        self._observation_overflow_count = 0
        self._lifecycle_overflow_count = 0
        self._discarded_command_count = 0
        self._bounded_error: str | None = None
        self._active_scopes: set[str] = set()
        self._anonymous_capture_active = False
        self._monotonic_clock = monotonic_clock
        self._max_busy_time_s = 0.0
        self._last_failure_kind: str | None = None
        self._learning_enabled = True

    def submit(self, command: CaptureCommand) -> bool:
        """Enqueue without waiting; reserved lifecycle capacity is independent."""
        with self._condition:
            if self._closed:
                return False
            if (
                command.kind
                in {
                    CaptureCommandKind.MODEL_COMMIT,
                    CaptureCommandKind.RECOVERY_RECEIPT,
                }
                and not self._learning_enabled
            ):
                return False
            if command.scope_id is not None and command.scope_id in self._failed_scopes:
                return False
            queue = self._queue_for(command)
            capacity = OBSERVATION_CAPACITY if queue is self._observations else LIFECYCLE_CAPACITY
            if len(queue) >= capacity:
                self._capture_available = False
                if command.lifecycle_priority:
                    self._lifecycle_overflow_count += 1
                    self._bounded_error = "lifecycle_queue_overflow"
                else:
                    self._observation_overflow_count += 1
                    self._bounded_error = "observation_queue_overflow"
                return False
            queued = _QueuedCommand(
                self._next_submission_order,
                command,
                self._monotonic_clock(),
            )
            self._next_submission_order += 1
            queue.append(queued)
            if command.kind == CaptureCommandKind.START and command.scope_id is not None:
                # Reserve the scope at enqueue time.  A failed START remains
                # authoritative until its retained pre-start boundary closes.
                self._active_scopes.add(command.scope_id)
            self._condition.notify()
            return True

    def drain_one(self) -> bool:
        """Execute one priority command; exposed for deterministic orchestration tests."""
        with self._condition:
            if self._running:
                raise RuntimeError("cannot drain manually while capture writer thread is running")
            if self._manual_draining:
                raise RuntimeError("another manual capture drain is already running")
            queued = self._take_nowait()
            if queued is not None:
                self._manual_draining = True
        if queued is None:
            return False
        try:
            self._execute(queued.command)
        finally:
            with self._condition:
                self._manual_draining = False
                self._condition.notify_all()
        return True

    def start(self) -> None:
        with self._condition:
            if self._running:
                return
            if self._manual_draining:
                raise RuntimeError("cannot start while a manual capture drain is running")
            if self._closed:
                raise RuntimeError("capture writer cannot restart after stop")
            self._running = True
            self._thread = Thread(target=self._run, name="ups-capture-writer", daemon=True)
            self._thread.start()

    def stop(self, *, drain: bool = True) -> None:
        with self._condition:
            self._running = False
            self._closed = True
            self._capture_available = False
            if self._maintenance:
                self._discarded_command_count += len(self._maintenance)
                self._maintenance.clear()
            if not drain:
                self._discarded_command_count += len(self._lifecycle) + len(self._observations)
                self._lifecycle.clear()
                self._observations.clear()
                self._maintenance.clear()
                self._bounded_error = "capture_writer_stopped_without_drain"
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            if thread.is_alive():
                raise RuntimeError("capture writer did not stop")
        self._thread = None

    def wait_for_lifecycle_capacity(self, timeout_s: float) -> bool:
        """Wait boundedly for one shutdown-boundary command slot."""
        if timeout_s < 0.0:
            raise ValueError("timeout_s must not be negative")
        with self._condition:
            available = self._condition.wait_for(
                lambda: len(self._lifecycle) < LIFECYCLE_CAPACITY or self._closed,
                timeout=timeout_s,
            )
            return available and not self._closed

    def health(self) -> CaptureQueueHealth:
        with self._condition:
            return CaptureQueueHealth(
                capture_available=self._capture_available,
                lifecycle_queued=len(self._lifecycle),
                observations_queued=len(self._observations),
                observation_overflow_count=self._observation_overflow_count,
                lifecycle_overflow_count=self._lifecycle_overflow_count,
                discarded_command_count=self._discarded_command_count,
                bounded_error=self._bounded_error,
                maintenance_queued=len(self._maintenance),
                max_busy_time_s=self._max_busy_time_s,
                oldest_queue_age_s=self._oldest_queue_age_locked(),
                last_failure_kind=self._last_failure_kind,
            )

    @property
    def monotonic_clock(self) -> Callable[[], float]:
        """Expose the lane clock to application retry state."""
        return self._monotonic_clock

    def clear_failed_scope(self, scope_id: str) -> None:
        """Allow a bounded recovery command for a previously failed scope."""
        with self._condition:
            self._failed_scopes.discard(scope_id)
            self._condition.notify_all()

    def fail_scope(self, scope_id: str) -> None:
        """Keep a scope closed to ordinary submissions after enqueue failure."""
        with self._condition:
            self._failed_scopes.add(scope_id)

    def release_capture_scope(self, scope_id: str) -> None:
        """Forget writer-side activity after a terminal close or rejection."""
        with self._condition:
            self._release_scope_locked(scope_id)
            self._condition.notify_all()

    def restore_capture_health(self) -> None:
        """Clear the current capture latch while retaining historical failure fields."""
        with self._condition:
            if not self._closed:
                self._capture_available = True
                self._bounded_error = None
                self._condition.notify_all()

    def mark_capture_unhealthy(
        self,
        reason: BaseException | str,
        *,
        scope_id: str | None = None,
    ) -> None:
        """Latch capture/storage failure without interrupting an in-flight syscall.

        A safety poll may outlive the writer's filesystem operation.  Once the
        bounded recovery deadline expires, maintenance/model work must not run
        for that event even if the blocked operation eventually returns.  The
        writer thread is deliberately left alone; its owner decides when it is
        safe to join it.
        """
        with self._condition:
            self._capture_available = False
            self._learning_enabled = False
            self._last_failure_kind = "sticky_recovery_timeout"
            self._bounded_error = _bounded_error(reason)
            self._discarded_command_count += len(self._maintenance)
            self._maintenance.clear()
            if scope_id is not None:
                self._failed_scopes.add(scope_id)
                self._discard_scope_locked(scope_id)
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                while (
                    self._running
                    and not self._lifecycle
                    and not self._observations
                    and not self._maintenance
                ):
                    self._condition.wait()
                if (
                    not self._running
                    and not self._lifecycle
                    and not self._observations
                    and not self._maintenance
                ):
                    return
                queued = self._take_nowait()
                if queued is None:
                    # Maintenance is intentionally held behind an active
                    # capture.  Wait for the lifecycle transition instead of
                    # polling the lock at 100 Hz while a blackout is active.
                    self._condition.wait_for(
                        lambda: (
                            not self._running
                            or self._lifecycle
                            or self._observations
                            or (
                                self._maintenance
                                and self._learning_enabled
                                and not self._capture_active_locked()
                            )
                        )
                    )
            if queued is not None:
                self._execute(queued.command)

    def _take_nowait(self) -> _QueuedCommand | None:
        if self._lifecycle:
            # END/GAP are durable barriers for the observations already
            # accepted ahead of them.  Their slots remain reserved, but they
            # must not seal a file and then let an older observation append.
            if (
                self._observations
                and self._lifecycle[0].command.kind
                in {CaptureCommandKind.END, CaptureCommandKind.GAP}
                and self._observations[0].submission_order < self._lifecycle[0].submission_order
            ):
                return self._observations.popleft()
            return self._lifecycle.popleft()
        if self._observations:
            return self._observations.popleft()
        if self._maintenance and self._learning_enabled and not self._capture_active_locked():
            return self._maintenance.popleft()
        return None

    def _execute(self, command: CaptureCommand) -> None:
        started = self._monotonic_clock()
        succeeded = False
        try:
            command.execute()
            succeeded = True
        except Exception as exc:
            with self._condition:
                self._capture_available = False
                self._bounded_error = f"{type(exc).__name__}: {exc}"[:512]
                self._last_failure_kind = command.kind.value
                if command.scope_id is not None:
                    self._failed_scopes.add(command.scope_id)
            recovery = self._recover(command, exc)
            if command.scope_id is not None:
                with self._condition:
                    if recovery is not RecoveryDisposition.FAILED:
                        if recovery is RecoveryDisposition.TERMINAL_SUCCESS:
                            self._discard_scope_locked(command.scope_id)
                            self._release_scope_locked(command.scope_id)
                        else:
                            self._failed_scopes.discard(command.scope_id)
                    else:
                        self._discard_scope(command.scope_id)
            if (
                recovery is not RecoveryDisposition.TERMINAL_SUCCESS
                and self._on_failure is not None
            ):
                try:
                    self._on_failure(command, exc)
                except Exception as callback_exc:
                    self._append_bounded_error("failure observer", callback_exc)
        finally:
            elapsed = max(0.0, self._monotonic_clock() - started)
            with self._condition:
                self._max_busy_time_s = max(self._max_busy_time_s, elapsed)
                self._update_capture_state_locked(command, succeeded)
                self._condition.notify_all()

    def _queue_for(self, command: CaptureCommand) -> deque[_QueuedCommand]:
        if command.kind in {
            CaptureCommandKind.MODEL_COMMIT,
            CaptureCommandKind.RECOVERY_RECEIPT,
        }:
            return self._maintenance
        return self._lifecycle if command.lifecycle_priority else self._observations

    def _capture_active_locked(self) -> bool:
        return bool(self._active_scopes) or self._anonymous_capture_active

    def _update_capture_state_locked(self, command: CaptureCommand, succeeded: bool) -> None:
        if command.kind == CaptureCommandKind.START:
            if succeeded:
                if command.scope_id is None:
                    self._anonymous_capture_active = True
            return
        if command.kind != CaptureCommandKind.END or not succeeded:
            return
        if command.scope_id is None:
            self._anonymous_capture_active = False
        else:
            self._active_scopes.discard(command.scope_id)

    def _oldest_queue_age_locked(self) -> float:
        queued = (*self._lifecycle, *self._observations, *self._maintenance)
        if not queued:
            return 0.0
        oldest = min(item.submitted_monotonic for item in queued)
        return max(0.0, self._monotonic_clock() - oldest)

    def _recover(self, command: CaptureCommand, exc: Exception) -> RecoveryDisposition:
        if command.recover_failure is None:
            return RecoveryDisposition.FAILED
        try:
            result = command.recover_failure(exc)
            if isinstance(result, RecoveryDisposition):
                return result
            return RecoveryDisposition.RECOVERED if result else RecoveryDisposition.FAILED
        except Exception as recovery_exc:
            self._append_bounded_error("terminal_recovery_failed", recovery_exc)
            return RecoveryDisposition.FAILED

    def _discard_scope(self, scope_id: str) -> None:
        with self._condition:
            self._discard_scope_locked(scope_id)

    def _discard_scope_locked(self, scope_id: str) -> None:
        lifecycle = deque(item for item in self._lifecycle if item.command.scope_id != scope_id)
        observations = deque(
            item for item in self._observations if item.command.scope_id != scope_id
        )
        self._discarded_command_count += (
            len(self._lifecycle) - len(lifecycle) + len(self._observations) - len(observations)
        )
        self._lifecycle = lifecycle
        self._observations = observations

    def _release_scope_locked(self, scope_id: str) -> None:
        self._active_scopes.discard(scope_id)
        self._failed_scopes.discard(scope_id)

    def _append_bounded_error(self, label: str, exc: Exception) -> None:
        with self._condition:
            detail = f"{label} {type(exc).__name__}: {exc}"
            if self._bounded_error is None:
                self._bounded_error = detail[:512]
            else:
                self._bounded_error = f"{self._bounded_error}; {detail}"[:512]


def _bounded_error(error: BaseException | str) -> str:
    if isinstance(error, BaseException):
        return f"{type(error).__name__}: {error}"[:512]
    return str(error)[:512] or "capture writer unhealthy"
