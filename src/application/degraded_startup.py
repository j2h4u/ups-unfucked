"""Small storage boundary used when scientific startup cannot be recovered.

The safety loop does not need an event repository to publish the current UPS
state.  This object deliberately implements only the bounded read operations
used by the health reporter; all scientific mutations remain unavailable.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import Protocol, cast

from src.application.errors import StoragePortError
from src.application.ports import (
    AssessmentCloseEventStorePort,
    AssessmentQueryEventStorePort,
    CaptureRecoveryEventStorePort,
    CloseablePort,
    ProcessingRejectionEventStorePort,
    ReportingEventStorePort,
    ReportOutboxEventStorePort,
    StartupRecoveryEventStorePort,
)
from src.application.storage_values import (
    CaptureCloseReconciliation,
    CaptureCloseState,
    EpochHistoryScan,
    EpochHistoryTail,
    EventHandle,
    EventProjection,
    EventRecord,
    EventRef,
    EventStart,
    EventSummary,
    ProcessingRef,
    RecoveredCapture,
    ReportNoticeIdentity,
    SealedEventRef,
    StorageHealth,
    TerminalOutcomeRecord,
    WorkRegistry,
)
from src.domain.reasons import InfrastructureReason


class EventStorageUnavailable(StoragePortError):
    """Scientific event storage is unavailable for this process lifetime."""


class _DeferredStoreDelegate(
    CaptureRecoveryEventStorePort,
    AssessmentQueryEventStorePort,
    AssessmentCloseEventStorePort,
    StartupRecoveryEventStorePort,
    ReportingEventStorePort,
    ProcessingRejectionEventStorePort,
    CloseablePort,
    Protocol,
):
    """Complete capability required only by the forwarding startup facade."""

    @property
    def report_outbox(self) -> ReportOutboxEventStorePort: ...

    def sealed_event_projections(
        self,
        start_utc: str,
        end_utc: str,
        *,
        event_kind: str = "blackout",
    ) -> tuple[EventProjection, ...]: ...

    def sealed_recharge_projections_for_blackouts(
        self,
        blackout_ids: Collection[str],
    ) -> tuple[EventProjection, ...]: ...


class DegradedEventStore:
    """No-capture store that keeps safety/reporting construction deterministic."""

    acknowledge_capture_recovery: Callable[[], None]

    def __init__(self, reason: BaseException | str) -> None:
        self.reason = _bounded_reason(reason)
        self._closed = False
        self.acknowledge_capture_recovery = lambda: None
        self.report_outbox: ReportOutboxEventStorePort = self

    def close(self) -> None:
        """Close the no-op boundary without attempting any filesystem work."""
        self._closed = True

    def open(self, start: EventStart) -> EventHandle:
        del start
        raise EventStorageUnavailable(f"startup_degraded: {self.reason}")

    def append(self, handle: EventHandle, record: EventRecord) -> EventHandle:
        del handle, record
        raise EventStorageUnavailable(f"startup_degraded: {self.reason}")

    def seal(self, handle: EventHandle, outcome: TerminalOutcomeRecord) -> SealedEventRef:
        del handle, outcome
        raise EventStorageUnavailable(f"startup_degraded: {self.reason}")

    def reject_processing(
        self,
        processing: ProcessingRef,
        reason: InfrastructureReason,
    ) -> SealedEventRef:
        del processing, reason
        raise EventStorageUnavailable(f"startup_degraded: {self.reason}")

    def project(self, ref: EventRef | EventHandle | SealedEventRef) -> EventProjection:
        del ref
        raise EventStorageUnavailable(f"startup_degraded: {self.reason}")

    def recover_startup(self) -> RecoveredCapture | None:
        raise EventStorageUnavailable(f"startup_degraded: {self.reason}")

    def sealed_event_projections(
        self,
        start_utc: str,
        end_utc: str,
        *,
        event_kind: str = "blackout",
    ) -> tuple[EventProjection, ...]:
        del start_utc, end_utc, event_kind
        return ()

    def sealed_recharge_projections_for_blackouts(
        self,
        blackout_ids: Collection[str],
    ) -> tuple[EventProjection, ...]:
        del blackout_ids
        return ()

    def checkpoint_processing(self, handle: EventHandle, frozen_stage: str) -> None:
        del handle, frozen_stage
        raise EventStorageUnavailable(f"startup_degraded: {self.reason}")

    def reconcile_damaged_close(
        self,
        blackout_id: str,
        current_handle: EventHandle | None,
    ) -> CaptureCloseReconciliation:
        del blackout_id
        return CaptureCloseReconciliation(CaptureCloseState.UNKNOWN, current_handle)

    def work_registry(self) -> WorkRegistry:
        """Expose an empty registry so no event can be invented in degraded mode."""
        return WorkRegistry(None, ())

    def storage_health(
        self,
        *,
        queued_observations: int | None = None,
        consumed_step_budget_remaining: int | None = None,
    ) -> StorageHealth:
        """Return a latched, operator-visible storage alarm."""
        return StorageHealth(
            capture_available=False,
            active_phase=None,
            queued_observations=queued_observations,
            durability_lag_s=None,
            consumed_step_budget_remaining=consumed_step_budget_remaining,
            event_count=0,
            total_bytes=0,
            free_bytes=0,
            alarm="startup_degraded",
            bounded_error=self.reason,
        )

    def history_tail(self, limit: int) -> tuple[EventSummary, ...]:
        """Return no sealed science while storage is unavailable."""
        del limit
        return ()

    def history_tail_for_epoch(self, battery_epoch_id: str, limit: int) -> EpochHistoryTail:
        """Return an incomplete empty cohort instead of querying storage."""
        del battery_epoch_id, limit
        return EpochHistoryTail((), 0, False)

    def history_scan_for_epoch(self, battery_epoch_id: str) -> EpochHistoryScan:
        """Return an incomplete empty decline cohort instead of querying storage."""
        del battery_epoch_id
        return EpochHistoryScan((), False)

    def report_outbox_pending(self, limit: int) -> tuple[ReportNoticeIdentity, ...]:
        del limit
        return ()

    def acknowledge_report_notice(self, notice: ReportNoticeIdentity) -> None:
        del notice
        raise EventStorageUnavailable(f"startup_degraded: {self.reason}")


class DeferredEventStore:
    """Safety-first facade that activates one real store after the first poll."""

    def __init__(self, reason: BaseException | str) -> None:
        self._fallback = DegradedEventStore(reason)
        self._delegate: _DeferredStoreDelegate | None = None
        self.report_outbox: ReportOutboxEventStorePort = _DeferredReportOutbox(self)

    def activate(self, store: _DeferredStoreDelegate) -> None:
        if self._delegate is not None and self._delegate is not store:
            raise RuntimeError("deferred event store was already activated")
        self._delegate = store

    def degrade(self, reason: BaseException | str) -> None:
        if self._delegate is None:
            self._fallback = DegradedEventStore(reason)

    def close(self) -> None:
        self._target().close()

    def open(self, start: EventStart) -> EventHandle:
        return self._target().open(start)

    def append(self, handle: EventHandle, record: EventRecord) -> EventHandle:
        return self._target().append(handle, record)

    def seal(self, handle: EventHandle, outcome: TerminalOutcomeRecord) -> SealedEventRef:
        return self._target().seal(handle, outcome)

    def reject_processing(
        self,
        processing: ProcessingRef,
        reason: InfrastructureReason,
    ) -> SealedEventRef:
        return self._target().reject_processing(processing, reason)

    def project(self, ref: EventRef | EventHandle | SealedEventRef) -> EventProjection:
        return cast(AssessmentQueryEventStorePort, self._target()).project(ref)

    def recover_startup(self):
        return self._target().recover_startup()

    def work_registry(self) -> WorkRegistry:
        return self._target().work_registry()

    def sealed_event_projections(
        self,
        start_utc: str,
        end_utc: str,
        *,
        event_kind: str = "blackout",
    ) -> tuple[EventProjection, ...]:
        return self._target().sealed_event_projections(
            start_utc,
            end_utc,
            event_kind=event_kind,
        )

    def sealed_recharge_projections_for_blackouts(
        self,
        blackout_ids: Collection[str],
    ) -> tuple[EventProjection, ...]:
        return self._target().sealed_recharge_projections_for_blackouts(blackout_ids)

    def checkpoint_processing(self, handle: EventHandle, frozen_stage: str) -> None:
        self._target().checkpoint_processing(handle, frozen_stage)

    def reconcile_damaged_close(
        self,
        blackout_id: str,
        current_handle: EventHandle | None,
    ) -> CaptureCloseReconciliation:
        return self._target().reconcile_damaged_close(blackout_id, current_handle)

    def acknowledge_capture_recovery(self) -> None:
        self._target().acknowledge_capture_recovery()

    def history_tail(self, limit: int) -> tuple[EventSummary, ...]:
        return self._target().history_tail(limit)

    def history_tail_for_epoch(self, battery_epoch_id: str, limit: int) -> EpochHistoryTail:
        return self._target().history_tail_for_epoch(battery_epoch_id, limit)

    def history_scan_for_epoch(self, battery_epoch_id: str) -> EpochHistoryScan:
        return self._target().history_scan_for_epoch(battery_epoch_id)

    def storage_health(
        self,
        *,
        queued_observations: int | None = None,
        consumed_step_budget_remaining: int | None = None,
    ) -> StorageHealth:
        return self._target().storage_health(
            queued_observations=queued_observations,
            consumed_step_budget_remaining=consumed_step_budget_remaining,
        )

    def _target(self) -> _DeferredStoreDelegate:
        return cast(_DeferredStoreDelegate, self._delegate or self._fallback)


class _DeferredReportOutbox:
    """Forward report delivery to whichever storage target is currently active."""

    def __init__(self, owner: DeferredEventStore) -> None:
        self._owner = owner

    def report_outbox_pending(self, limit: int) -> tuple[ReportNoticeIdentity, ...]:
        return self._owner._target().report_outbox.report_outbox_pending(limit)

    def acknowledge_report_notice(self, notice: ReportNoticeIdentity) -> None:
        self._owner._target().report_outbox.acknowledge_report_notice(notice)


def _bounded_reason(reason: BaseException | str) -> str:
    text = " ".join(str(reason).split())
    if not text:
        text = "scientific event storage unavailable"
    if isinstance(reason, BaseException):
        text = f"{type(reason).__name__}: {text}"
    return text[:512]
