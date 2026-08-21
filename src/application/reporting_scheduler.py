"""Bounded reporting scheduling with no UPS commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.application.capture_writer import CaptureWriter
from src.application.decline_reporting import decline_statuses
from src.application.model_port import ModelPolicyPort
from src.application.ports import (
    HealthAlertPort,
    ReportingEventStorePort,
)
from src.application.reporting import ReportingSnapshot, reporting_tick
from src.application.storage_values import CaptureQueueHealth
from src.domain.values import ReserveCohortStatus


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
    """Explicit reporting capabilities used by the scheduler."""

    store: ReportingEventStorePort
    model: ModelPolicyPort
    writer: CaptureWriter
    publisher: HealthPublisher
    health_alerts: HealthAlertPort


class ReportingScheduler:
    """Compose bounded health and history reads on the reporting lane."""

    def __init__(
        self,
        dependencies: ReportingSchedulerDependencies,
    ) -> None:
        self._store = dependencies.store
        self._model = dependencies.model
        self._writer = dependencies.writer
        self._publisher = dependencies.publisher
        self._health_alerts = dependencies.health_alerts

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
        epoch_scan = self._store.history_scan_for_epoch(policy.snapshot.battery_epoch_id)
        decline = decline_statuses(
            self._store,
            epoch_scan.summaries,
            scan_complete=epoch_scan.scan_complete,
        )
        self._publisher.publish_health(
            snapshot,
            capture,
            consecutive_errors=max(0, consecutive_errors),
            decline=decline,
        )
        self._health_alerts.publish(capture, snapshot.health, decline)
        return snapshot
