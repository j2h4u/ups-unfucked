"""Bounded reporting and index-maintenance scheduling with no UPS commands."""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from src.application.capture_writer import (
    CaptureCommand,
    CaptureCommandKind,
    CaptureWriter,
)
from src.application.decline_reporting import decline_statuses
from src.application.model_port import ModelPolicyPort
from src.application.ports import (
    HealthAlertPort,
    MaintenanceEventStorePort,
    ReportingEventStorePort,
)
from src.application.reporting import ReportingSnapshot, reporting_tick
from src.application.storage_values import CaptureQueueHealth
from src.domain.values import ReserveCohortStatus

MAINTENANCE_MAX_FILES = 4
MAINTENANCE_MAX_BYTES = 512 * 1024
MAINTENANCE_WALL_BUDGET_S = 0.25


class HealthPublisher(Protocol):
    def publish_health(
        self,
        reporting: ReportingSnapshot,
        capture: CaptureQueueHealth,
        *,
        consecutive_errors: int,
        decline: tuple[ReserveCohortStatus, ...],
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ReportingSchedulerDependencies:
    """Explicit reporting and maintenance capabilities used by the scheduler."""

    store: ReportingEventStorePort
    maintenance: MaintenanceEventStorePort
    model: ModelPolicyPort
    writer: CaptureWriter
    publisher: HealthPublisher
    health_alerts: HealthAlertPort


class ReportingScheduler:
    """Compose bounded health and request lazy index work on the sole writer lane."""

    def __init__(
        self,
        dependencies: ReportingSchedulerDependencies,
    ) -> None:
        self._store = dependencies.store
        self._maintenance = dependencies.maintenance
        self._model = dependencies.model
        self._writer = dependencies.writer
        self._publisher = dependencies.publisher
        self._health_alerts = dependencies.health_alerts
        self._lock = Lock()
        self._maintenance_pending = False

    def tick(self, *, consecutive_errors: int) -> ReportingSnapshot:
        """Read fixed-size projections and enqueue, but never execute, repair work."""
        capture = self._writer.health()
        policy = self._model.policy_projection()
        consumed_remaining = max(
            0,
            policy.learning_policy.max_consumed_step_hashes - len(policy.consumed_step_hashes),
        )
        snapshot = reporting_tick(
            self._store,
            queued_observations=capture.observations_queued,
            consumed_step_budget_remaining=consumed_remaining,
        )
        if snapshot.health.index_available:
            epoch_scan = self._store.index_scan_for_decline_epoch(policy.snapshot.battery_epoch_id)
            decline = decline_statuses(
                self._store,
                epoch_scan.summaries,
                scan_complete=epoch_scan.scan_complete,
            )
        else:
            decline = decline_statuses(self._store, (), scan_complete=False)
        self._publisher.publish_health(
            snapshot,
            capture,
            consecutive_errors=max(0, consecutive_errors),
            decline=decline,
        )
        self._health_alerts.publish(capture, snapshot.health, decline)
        if snapshot.maintenance is not None:
            self._enqueue_maintenance(
                max_files=snapshot.maintenance.max_files,
                max_bytes=snapshot.maintenance.max_bytes,
            )
        return snapshot

    def _enqueue_maintenance(self, *, max_files: int, max_bytes: int) -> None:
        with self._lock:
            if self._maintenance_pending:
                return
            self._maintenance_pending = True

        def execute() -> None:
            started = time.monotonic()
            try:
                health = self._maintenance.storage_health()
                if not health.rebuild_in_progress:
                    self._maintenance.begin_index_rebuild()
                complete = self._maintenance.rebuild_index_tick(
                    max_files=min(max_files, MAINTENANCE_MAX_FILES),
                    max_bytes=min(max_bytes, MAINTENANCE_MAX_BYTES),
                )
                elapsed = time.monotonic() - started
                if elapsed > MAINTENANCE_WALL_BUDGET_S:
                    raise RuntimeError(
                        "maintenance_chunk_budget_exceeded "
                        f"elapsed_s={elapsed:.3f} budget_s={MAINTENANCE_WALL_BUDGET_S:.3f}"
                    )
                if complete:
                    self._maintenance.promote_index_rebuild()
            finally:
                with self._lock:
                    self._maintenance_pending = False

        accepted = self._writer.submit(
            CaptureCommand(
                kind=CaptureCommandKind.RECOVERY_RECEIPT,
                execute=execute,
            )
        )
        if not accepted:
            with self._lock:
                self._maintenance_pending = False
