"""Dependency-inversion ports for blackout application use cases."""

from typing import Protocol, runtime_checkable

from src.application.storage_values import (
    CaptureCloseReconciliation,
    CaptureQueueHealth,
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
from src.domain.values import PhysicalObservation, PlainLanguageReport, ReserveCohortStatus


class PhysicalTelemetryPort(Protocol):
    """Read the next immutable physical UPS observation."""

    def read(self) -> PhysicalObservation: ...


class CloseablePort(Protocol):
    """Own exactly one runtime resource lifecycle operation."""

    def close(self) -> None: ...


class ReportSinkPort(Protocol):
    """Publish a domain-authored plain-language blackout report."""

    def publish(self, report: PlainLanguageReport) -> None: ...


class HealthAlertPort(Protocol):
    """Publish bounded operator alerts derived from application health state."""

    def publish(
        self,
        capture: CaptureQueueHealth,
        storage: StorageHealth,
        decline: tuple[ReserveCohortStatus, ...],
    ) -> None: ...


class CaptureEventStorePort(Protocol):
    """Capture lane: append physical evidence and recover its active handle."""

    def open(self, start: EventStart) -> EventHandle: ...

    def append(self, handle: EventHandle, record: EventRecord) -> EventHandle: ...

    def recover_startup(self) -> RecoveredCapture | None: ...

    def checkpoint_processing(self, handle: EventHandle, frozen_stage: str) -> None: ...


@runtime_checkable
class CaptureRecoveryEventStorePort(CaptureEventStorePort, Protocol):
    """Capture close reconciliation owned by a durable adapter."""

    def reconcile_damaged_close(
        self,
        blackout_id: str,
        current_handle: EventHandle | None,
    ) -> CaptureCloseReconciliation: ...

    def project(self, ref: EventHandle) -> EventProjection: ...

    def acknowledge_capture_recovery(self) -> None: ...


class AssessmentQueryEventStorePort(Protocol):
    """Assessment lane: read one projection and bounded cohort history."""

    def project(self, ref: EventRef | EventHandle | SealedEventRef) -> EventProjection: ...

    def history_tail_for_epoch(self, battery_epoch_id: str, limit: int) -> EpochHistoryTail: ...


class ReportOutboxEventStorePort(Protocol):
    """Optional durable report-delivery capability layered on reporting reads."""

    def report_outbox_pending(self, limit: int) -> tuple[ReportNoticeIdentity, ...]: ...

    def acknowledge_report_notice(self, notice: ReportNoticeIdentity) -> None: ...


class ProcessingRejectionEventStorePort(Protocol):
    """Optional close-lane refusal capability for malformed projections."""

    def reject_processing(
        self,
        processing: ProcessingRef,
        reason: InfrastructureReason,
    ) -> SealedEventRef: ...


class AssessmentCloseEventStorePort(Protocol):
    """Close lane: append derived facts and seal one terminal outcome."""

    def append(self, handle: EventHandle, record: EventRecord) -> EventHandle: ...

    def seal(self, handle: EventHandle, outcome: TerminalOutcomeRecord) -> SealedEventRef: ...

    def checkpoint_processing(self, handle: EventHandle, frozen_stage: str) -> None: ...


class AssessmentTerminalEventStorePort(
    AssessmentCloseEventStorePort, ProcessingRejectionEventStorePort, Protocol
):
    """Close lane including deterministic infrastructure rejection."""


class StartupRecoveryEventStorePort(Protocol):
    """Startup lane: inspect durable work without projecting event contents."""

    def recover_startup(self) -> RecoveredCapture | None: ...

    def work_registry(self) -> WorkRegistry: ...


class ReportingEventStorePort(Protocol):
    """Reporting lane: bounded health and sealed event-history reads only."""

    def storage_health(
        self,
        *,
        queued_observations: int | None = None,
        consumed_step_budget_remaining: int | None = None,
    ) -> StorageHealth: ...

    def history_tail(self, limit: int) -> tuple[EventSummary, ...]: ...

    def history_tail_for_epoch(self, battery_epoch_id: str, limit: int) -> EpochHistoryTail: ...

    def history_scan_for_epoch(self, battery_epoch_id: str) -> EpochHistoryScan: ...

    def project(self, ref: EventRef | EventHandle | SealedEventRef) -> EventProjection: ...
