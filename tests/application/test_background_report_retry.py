"""Report delivery remains retryable after the durable close succeeds."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from src.application.assessment_codec import ProjectionInputError
from src.application.assessment_worker import CloseRequest
from src.application.background_coordinator import (
    BackgroundCoordinator,
    BackgroundDependencies,
    BackgroundSettings,
    _CloseNotice,
)
from src.application.capture_writer import CaptureWriter
from src.application.startup_recovery import StartupRecovery
from src.application.storage_values import ProcessingRef, SealedEventRef
from src.domain.reasons import InfrastructureReason
from src.domain.values import PlainLanguageReport, TerminalDisposition


class _FailOnceSink:
    def __init__(self) -> None:
        self.attempts = 0
        self.published: list[PlainLanguageReport] = []

    def publish(self, report: PlainLanguageReport) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise OSError("transient report sink failure")
        self.published.append(report)


def test_transient_report_sink_failure_keeps_notice_for_automatic_retry(
    monkeypatch,
) -> None:
    sink = _FailOnceSink()
    errors: list[BaseException] = []
    writer = CaptureWriter()
    coordinator = BackgroundCoordinator(
        BackgroundDependencies(
            assessment_store=cast(Any, object()),
            startup_store=cast(Any, object()),
            reporting_store=cast(Any, object()),
            commit_model=cast(Any, object()),
            policy_model=cast(Any, object()),
            worker=cast(Any, object()),
            writer=writer,
            reporter=cast(Any, object()),
            report_sink=sink,
        ),
        StartupRecovery(None, ()),
        BackgroundSettings(reporting_interval_s=60.0, on_report_error=errors.append),
    )
    report = PlainLanguageReport(
        blackout_id="blackout-a",
        disposition=TerminalDisposition.RECORDED_ONLY,
        lines=("No model change",),
        generated_utc=datetime(2026, 8, 16, tzinfo=timezone.utc),
    )
    notice = _CloseNotice("a" * 32, "event.jsonl")
    coordinator._notices.append(notice)
    monkeypatch.setattr(coordinator, "_report", lambda _notice: report)

    coordinator._publish_notices()
    assert tuple(coordinator._notices) == (notice,)
    assert len(errors) == 1

    coordinator._publish_notices()
    assert not coordinator._notices
    assert sink.published == [report]
    writer.stop(drain=True)


def test_invalid_assessment_is_rejected_once_on_writer_lane() -> None:
    processing = ProcessingRef("a" * 32, ("b" * 32,), "event.jsonl", "end_durable")
    request = CloseRequest(processing)

    class Worker:
        discarded = False
        prepare_calls = 0

        def peek_pending(self):
            return request

        def prepare(self, _request):
            self.prepare_calls += 1
            raise ProjectionInputError("invalid durable assessment")

        def discard_pending(self, discarded):
            assert discarded is request
            self.discarded = True

    class Store:
        rejected = []

        def reject_processing(self, rejected, reason):
            self.rejected.append((rejected, reason))
            return SealedEventRef(
                rejected.blackout_id, rejected.segment_ids, rejected.final_path_token
            )

    worker = Worker()
    store = Store()
    writer = CaptureWriter()
    coordinator = BackgroundCoordinator(
        BackgroundDependencies(
            assessment_store=cast(Any, store),
            startup_store=cast(Any, object()),
            reporting_store=cast(Any, object()),
            commit_model=cast(Any, object()),
            policy_model=cast(Any, object()),
            worker=cast(Any, worker),
            writer=writer,
            reporter=cast(Any, object()),
            report_sink=cast(Any, object()),
        ),
        StartupRecovery(None, ()),
        BackgroundSettings(reporting_interval_s=60.0),
    )
    coordinator._scheduled.add(processing.blackout_id)

    coordinator._prepare_one()
    assert not worker.discarded
    assert store.rejected == []
    coordinator._prepare_one()
    assert worker.prepare_calls == 1

    assert writer.drain_one()
    assert worker.discarded
    assert store.rejected == [(processing, InfrastructureReason.CAPTURE_DAMAGED)]
    assert processing.blackout_id not in coordinator._scheduled
    assert tuple(coordinator._notices) == (
        _CloseNotice(processing.blackout_id, processing.final_path_token),
    )
    writer.stop(drain=True)


def test_failed_close_stays_at_queue_head_before_newer_event() -> None:
    first = CloseRequest(ProcessingRef("a" * 32, ("b" * 32,), "first.jsonl", "end_durable"))
    second = CloseRequest(ProcessingRef("d" * 32, ("e" * 32,), "second.jsonl", "end_durable"))

    class Worker:
        pending = [first, second]
        prepared_ids: list[str] = []

        def peek_pending(self):
            return self.pending[0] if self.pending else None

        def prepare(self, request):
            self.prepared_ids.append(request.processing.blackout_id)
            raise ProjectionInputError("invalid durable assessment")

        def discard_pending(self, request):
            assert self.pending[0] is request
            self.pending.pop(0)

    class Store:
        attempts = 0

        def reject_processing(self, processing, _reason):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("transient terminal write failure")
            return SealedEventRef(
                processing.blackout_id,
                processing.segment_ids,
                processing.final_path_token,
            )

    now = [0.0]
    worker = Worker()
    store = Store()
    writer = CaptureWriter(monotonic_clock=lambda: now[0])
    coordinator = BackgroundCoordinator(
        BackgroundDependencies(
            assessment_store=cast(Any, store),
            startup_store=cast(Any, object()),
            reporting_store=cast(Any, object()),
            commit_model=cast(Any, object()),
            policy_model=cast(Any, object()),
            worker=cast(Any, worker),
            writer=writer,
            reporter=cast(Any, object()),
            report_sink=cast(Any, object()),
        ),
        StartupRecovery(None, ()),
        BackgroundSettings(reporting_interval_s=60.0, monotonic_clock=lambda: now[0]),
    )

    coordinator._prepare_one()
    assert writer.drain_one()
    assert worker.pending == [first, second]

    now[0] = 11.0
    coordinator._prepare_one()
    assert writer.drain_one()
    assert worker.pending == [second]
    assert worker.prepared_ids == [first.processing.blackout_id] * 2

    coordinator._prepare_one()
    assert worker.prepared_ids[-1] == second.processing.blackout_id
    writer.stop(drain=False)
