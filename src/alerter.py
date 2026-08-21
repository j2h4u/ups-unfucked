"""Bounded operator alerts for domain-authored reports and capture/storage health."""

from __future__ import annotations

import logging
from collections.abc import Callable

from src.application.report_reconstruction import canonical_report_bytes
from src.application.storage_values import CaptureQueueHealth, StorageHealth
from src.domain.reasons import DeclineReason
from src.domain.values import DeclineVerdict, PlainLanguageReport, ReserveCohortStatus

logger = logging.getLogger("ups-battery-monitor")
MAX_ALERT_TEXT = 512


class JournaldReportSink:
    """Publish one bounded plain-language blackout report to the process log."""

    def __init__(self, *, on_report: Callable[[PlainLanguageReport], None] | None = None) -> None:
        self._on_report = on_report

    def publish(self, report: PlainLanguageReport) -> None:
        lines = tuple(canonical_report_bytes(report).decode("utf-8").splitlines())
        logger.info(
            " | ".join(lines),
            extra={
                "event_type": "blackout_report",
                "blackout_id": report.blackout_id[:128],
                "disposition": report.disposition.value,
                "report_line_count": len(lines),
            },
        )
        if self._on_report is not None:
            self._on_report(report)


class JournaldHealthAlertSink:
    """Publish bounded storage, capture, and decline alerts to journald."""

    def publish(
        self,
        capture: CaptureQueueHealth,
        storage: StorageHealth,
        decline: tuple[ReserveCohortStatus, ...],
    ) -> None:
        self._publish_capture(capture)
        self._publish_storage(storage)
        self._publish_decline(decline)

    @staticmethod
    def _publish_storage(health: StorageHealth) -> None:
        if health.capture_available and health.alarm is None:
            return
        reason = health.alarm or "capture_unavailable"
        bounded_reason = _bounded(reason)
        logger.warning(
            "Blackout evidence storage is degraded: %s",
            bounded_reason,
            extra={
                "event_type": "storage_health_alert",
                "storage_alarm": bounded_reason,
                "capture_available": health.capture_available,
                "active_phase": health.active_phase or "none",
            },
        )

    @staticmethod
    def _publish_capture(health: CaptureQueueHealth) -> None:
        degraded = (
            not health.capture_available
            or health.observation_overflow_count > 0
            or health.lifecycle_overflow_count > 0
            or health.discarded_command_count > 0
        )
        if not degraded:
            return
        reason = health.bounded_error
        if reason is None or not reason.strip():
            reason = "storage_unavailable"
        bounded_reason = _bounded(reason)
        logger.warning(
            "Blackout evidence capture writer is degraded: %s",
            bounded_reason,
            extra={
                "event_type": "capture_queue_health_alert",
                "capture_queue_error": bounded_reason,
                "capture_available": health.capture_available,
                "lifecycle_queued": health.lifecycle_queued,
                "observations_queued": health.observations_queued,
                "observation_overflow_count": health.observation_overflow_count,
                "lifecycle_overflow_count": health.lifecycle_overflow_count,
                "discarded_command_count": health.discarded_command_count,
            },
        )

    @staticmethod
    def _publish_decline(statuses: tuple[ReserveCohortStatus, ...]) -> None:
        for status in statuses:
            if DeclineReason.EVIDENCE_STORAGE_CORRUPT in status.reasons.values:
                logger.warning(
                    "Battery decline evidence storage is corrupt for %s",
                    status.metric,
                    extra={
                        "event_type": "decline_evidence_storage_corrupt",
                        "metric": status.metric,
                        "verdict": status.verdict.value,
                    },
                )
                continue
            if status.verdict != DeclineVerdict.POSSIBLE_DECLINE:
                continue
            logger.warning(
                "Battery evidence reports possible decline for %s; baseline=%s recent=%s",
                status.metric,
                status.baseline,
                status.recent,
                extra={
                    "event_type": "possible_battery_decline",
                    "metric": status.metric,
                    "verdict": status.verdict.value,
                    "event_count": len(status.event_ids),
                },
            )


def _bounded(value: object) -> str:
    return " ".join(str(value).split())[:MAX_ALERT_TEXT]
