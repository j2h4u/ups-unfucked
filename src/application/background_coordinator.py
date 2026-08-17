"""Background assessment, durable close, reporting, and startup recovery."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Condition, Thread

from src.application.assessment_codec import ProjectionInputError
from src.application.assessment_worker import AssessmentWorker, CloseRequest
from src.application.capture_writer import CaptureCommand, CaptureCommandKind, CaptureWriter
from src.application.close_blackout import close_blackout
from src.application.errors import StoragePortConflict, StoragePortCorruption, StoragePortError
from src.application.model_port import ModelCommitPort, ModelPolicyPort
from src.application.ports import (
    AssessmentTerminalEventStorePort,
    ReportingEventStorePort,
    ReportOutboxEventStorePort,
    ReportSinkPort,
    StartupRecoveryEventStorePort,
)
from src.application.report_reconstruction import (
    ReportReconstructionError,
    reconstruct_latest_report,
    reconstruct_report_for_event,
)
from src.application.reporting_scheduler import ReportingScheduler
from src.application.startup_recovery import (
    StartupRecovery,
    defer_processing_after_first_publication,
)
from src.application.storage_values import RecoveredCapture, ReportNoticeIdentity
from src.domain.reasons import InfrastructureReason
from src.domain.values import PlainLanguageReport

BACKGROUND_INTERVAL_SEC = 0.25
ASSESSMENT_RETRY_INTERVAL_SEC = 10.0


@dataclass(frozen=True, slots=True)
class BackgroundDependencies:
    """Application services and their explicitly bounded background ports."""

    assessment_store: AssessmentTerminalEventStorePort
    startup_store: StartupRecoveryEventStorePort
    reporting_store: ReportingEventStorePort
    commit_model: ModelCommitPort
    policy_model: ModelPolicyPort
    worker: AssessmentWorker
    writer: CaptureWriter
    reporter: ReportingScheduler
    report_sink: ReportSinkPort
    report_outbox_store: ReportOutboxEventStorePort | None = None


@dataclass(frozen=True, slots=True)
class BackgroundSettings:
    """Clock and failure callbacks for one coordinator instance."""

    reporting_interval_s: float
    monotonic_clock: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], datetime] | None = None
    on_error: Callable[[BaseException], None] | None = None
    on_storage_error: Callable[[BaseException], None] | None = None
    on_report_error: Callable[[BaseException], None] | None = None
    on_background_recovered: Callable[[], None] | None = None
    startup_loader: Callable[[], StartupRecovery] | None = None
    startup_error: BaseException | None = None
    startup_retry_interval_s: float = 10.0
    on_startup_degraded: Callable[[BaseException], None] | None = None
    on_startup_recovered: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class _CloseNotice:
    blackout_id: str
    segment_filename: str


class BackgroundCoordinator:
    """Run bounded assessment/reporting while the writer owns all mutations."""

    def __init__(
        self,
        dependencies: BackgroundDependencies,
        startup: StartupRecovery,
        settings: BackgroundSettings,
    ) -> None:
        if settings.reporting_interval_s <= 0.0:
            raise ValueError("reporting_interval_s must be positive")
        if settings.startup_retry_interval_s <= 0.0:
            raise ValueError("startup_retry_interval_s must be positive")
        self._assessment_store = dependencies.assessment_store
        self._startup_store = dependencies.startup_store
        self._reporting_store = dependencies.reporting_store
        self._model = dependencies.commit_model
        self._policy_model = dependencies.policy_model
        self._worker = dependencies.worker
        self._writer = dependencies.writer
        self._startup = startup
        self._reporter = dependencies.reporter
        self._report_sink = dependencies.report_sink
        self._report_outbox_store = dependencies.report_outbox_store
        self._reporting_interval_s = settings.reporting_interval_s
        self._monotonic_clock = settings.monotonic_clock
        self._wall_clock = settings.wall_clock or (lambda: datetime.now(timezone.utc))
        self._on_error = settings.on_error
        self._on_storage_error = settings.on_storage_error
        self._on_report_error = settings.on_report_error
        self._on_background_recovered = settings.on_background_recovered
        self._startup_loader = settings.startup_loader
        self._startup_retry_interval_s = settings.startup_retry_interval_s
        self._on_startup_degraded = settings.on_startup_degraded
        self._on_startup_recovered = settings.on_startup_recovered
        self._condition = Condition()
        self._running = False
        self._ready = False
        self._activated = False
        self._startup_loaded = settings.startup_loader is None
        self._startup_degraded = settings.startup_error is not None
        self._fatal_startup_error: StoragePortConflict | None = None
        self._next_startup_retry = 0.0
        self._thread: Thread | None = None
        self._scheduled: set[str] = set()
        self._close_in_flight = False
        self._notices: deque[_CloseNotice] = deque(maxlen=256)
        self._report_restored = False
        self._consecutive_errors = 0
        self._next_reporting = 0.0
        self._next_assessment_retry = 0.0
        if settings.startup_error is not None and self._on_startup_degraded is not None:
            self._on_startup_degraded(settings.startup_error)

    def start(self) -> None:
        with self._condition:
            if self._running:
                return
            self._running = True
            self._thread = Thread(
                target=self._run,
                name="ups-assessment-worker",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._condition:
            self._running = False
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            if thread.is_alive():
                raise RuntimeError("background coordinator did not stop")
        self._thread = None

    def after_first_safety_publication(self) -> None:
        """Activate deferred recovery using a memory-only poll-thread handoff."""
        with self._condition:
            self._ready = True
            self._condition.notify_all()

    def record_poll_error_count(self, count: int) -> None:
        with self._condition:
            self._consecutive_errors = max(0, count)

    @property
    def capture_enabled(self) -> bool:
        """Capture/learning stays off while startup storage is degraded."""
        with self._condition:
            return self._startup_loaded and not self._startup_degraded

    @property
    def fatal_startup_error(self) -> StoragePortConflict | None:
        with self._condition:
            return self._fatal_startup_error

    def take_recovered_capture(self) -> RecoveredCapture | None:
        """Return one recovery handoff after the background loader completes."""
        with self._condition:
            recovered = self._startup.recovered_capture
            if recovered is None:
                return None
            self._startup = StartupRecovery(None, self._startup.pending_processing)
            return recovered

    def run_one(self) -> None:
        """Execute one deterministic background iteration for tests."""
        self._activate_if_ready()
        if self._startup_degraded:
            now = self._monotonic_clock()
            if now >= self._next_reporting:
                self._reporter.tick(consecutive_errors=self._consecutive_errors)
                self._next_reporting = now + self._reporting_interval_s
            return
        if not self._activated:
            return
        self._restore_latest_report()
        self._publish_notices()
        self._discover_processing()
        self._prepare_one()
        now = self._monotonic_clock()
        if now >= self._next_reporting:
            self._reporter.tick(consecutive_errors=self._consecutive_errors)
            self._next_reporting = now + self._reporting_interval_s

    def _run(self) -> None:
        while True:
            with self._condition:
                if not self._running:
                    return
                if not self._ready:
                    self._condition.wait(timeout=BACKGROUND_INTERVAL_SEC)
                    continue
            try:
                self.run_one()
                if self._on_background_recovered is not None:
                    self._on_background_recovered()
            except StoragePortConflict as exc:
                with self._condition:
                    self._fatal_startup_error = exc
                    self._running = False
                self._record_error(exc)
            except Exception as exc:
                self._record_error(exc)
            with self._condition:
                if self._running:
                    self._condition.wait(timeout=BACKGROUND_INTERVAL_SEC)

    def _activate_if_ready(self) -> None:
        with self._condition:
            ready = self._ready
        if not ready:
            return
        if self._startup_loader is not None and (
            not self._startup_loaded or self._startup_degraded
        ):
            self._load_startup_metadata()
        if self._startup_degraded:
            return
        if self._activated:
            return
        accepted = defer_processing_after_first_publication(self._startup, self._worker)
        for processing in self._startup.pending_processing[:accepted]:
            self._scheduled.add(processing.blackout_id)
        self._activated = True

    def _load_startup_metadata(self) -> None:
        loader = self._startup_loader
        if loader is None:
            self._startup_loaded = True
            return
        now = self._monotonic_clock()
        if self._startup_degraded and now < self._next_startup_retry:
            return
        try:
            startup = loader()
        except StoragePortConflict:
            raise
        except Exception as exc:
            self._startup_loaded = True
            self._startup_degraded = True
            self._next_startup_retry = now + self._startup_retry_interval_s
            if self._on_startup_degraded is not None:
                self._on_startup_degraded(exc)
            self._record_error(exc)
            return
        self._startup = startup
        self._startup_loaded = True
        if self._startup_degraded and self._on_startup_recovered is not None:
            self._on_startup_recovered()
        self._startup_degraded = False
        self._activated = False

    def _discover_processing(self) -> None:
        registry = self._startup_store.work_registry()
        for processing in registry.pending_processing:
            blackout_id = processing.blackout_id
            if blackout_id in self._scheduled:
                continue
            if self._worker.defer(CloseRequest(processing)):
                self._scheduled.add(blackout_id)

    def _prepare_one(self) -> None:
        with self._condition:
            if self._close_in_flight:
                return
        now = self._monotonic_clock()
        if now < self._next_assessment_retry:
            return
        request = self._worker.peek_pending()
        if request is None:
            return
        try:
            prepared = self._worker.prepare(request)
        except StoragePortConflict:
            raise
        except (ProjectionInputError, StoragePortCorruption):
            self._submit_invalid_rejection(request)
            return
        except Exception:
            self._next_assessment_retry = now + ASSESSMENT_RETRY_INTERVAL_SEC
            raise
        self._next_assessment_retry = 0.0
        blackout_id = prepared.request.processing.blackout_id

        def execute() -> None:
            try:
                close_blackout(self._assessment_store, self._model, prepared)
            except Exception:
                with self._condition:
                    self._next_assessment_retry = (
                        self._monotonic_clock() + ASSESSMENT_RETRY_INTERVAL_SEC
                    )
                    self._close_in_flight = False
                    self._condition.notify_all()
                raise
            with self._condition:
                self._worker.discard_pending(request)
                self._scheduled.discard(blackout_id)
                self._notices.append(
                    _CloseNotice(
                        prepared.request.processing.blackout_id,
                        prepared.request.processing.final_path_token,
                    )
                )
                self._close_in_flight = False
                self._condition.notify_all()

        with self._condition:
            self._close_in_flight = True
        accepted = self._writer.submit(
            CaptureCommand(kind=CaptureCommandKind.MODEL_COMMIT, execute=execute)
        )
        if not accepted:
            with self._condition:
                self._close_in_flight = False

    def _submit_invalid_rejection(self, request: CloseRequest) -> None:
        blackout_id = request.processing.blackout_id

        def execute() -> None:
            try:
                sealed = self._assessment_store.reject_processing(
                    request.processing,
                    InfrastructureReason.CAPTURE_DAMAGED,
                )
            except Exception:
                with self._condition:
                    self._next_assessment_retry = (
                        self._monotonic_clock() + ASSESSMENT_RETRY_INTERVAL_SEC
                    )
                    self._close_in_flight = False
                    self._condition.notify_all()
                raise
            with self._condition:
                self._worker.discard_pending(request)
                self._scheduled.discard(blackout_id)
                self._notices.append(_CloseNotice(sealed.blackout_id, sealed.final_path_token))
                self._close_in_flight = False
                self._condition.notify_all()

        with self._condition:
            self._close_in_flight = True
        accepted = self._writer.submit(
            CaptureCommand(kind=CaptureCommandKind.MODEL_COMMIT, execute=execute)
        )
        if not accepted:
            with self._condition:
                self._close_in_flight = False

    def _publish_notices(self) -> None:
        while True:
            durable_notice = self._next_durable_notice()
            with self._condition:
                if durable_notice is None and not self._notices:
                    return
                notice = (
                    _CloseNotice(durable_notice.blackout_id, durable_notice.segment_filename)
                    if durable_notice is not None
                    else self._notices[0]
                )
            try:
                report = self._report(notice)
            except Exception as exc:
                self._record_report_error(exc)
                return
            try:
                self._report_sink.publish(report)
            except Exception as exc:
                self._record_report_error(exc)
                return
            if durable_notice is not None:
                try:
                    self._acknowledge_durable_notice(durable_notice)
                except Exception as exc:
                    self._record_report_error(exc)
                    return
            with self._condition:
                if self._notices and self._notices[0].blackout_id == notice.blackout_id:
                    self._notices.popleft()

    def _next_durable_notice(self) -> ReportNoticeIdentity | None:
        if self._report_outbox_store is None:
            return None
        pending = self._report_outbox_store.report_outbox_pending(1)
        return pending[0] if pending else None

    def _acknowledge_durable_notice(self, notice: ReportNoticeIdentity) -> None:
        if self._report_outbox_store is None:
            raise ReportReconstructionError("durable report outbox acknowledgement is unavailable")
        self._report_outbox_store.acknowledge_report_notice(notice)

    def _report(self, notice: _CloseNotice) -> PlainLanguageReport:
        consumed, budget = self._evidence_budget()
        report = reconstruct_report_for_event(
            self._reporting_store,
            blackout_id=notice.blackout_id,
            segment_filename=notice.segment_filename,
            consumed_evidence_budget_remaining=max(0, budget - consumed),
        )
        if report is None:
            raise ReportReconstructionError("sealed outcome is not available for reporting")
        return report

    def _restore_latest_report(self) -> None:
        if self._report_restored:
            return
        try:
            consumed, budget = self._evidence_budget()
            report = reconstruct_latest_report(
                self._reporting_store,
                consumed_evidence_budget_remaining=max(0, budget - consumed),
            )
        except Exception as exc:
            self._record_report_error(exc)
            return
        if report is not None:
            try:
                self._report_sink.publish(report)
            except Exception as exc:
                self._record_report_error(exc)
                return
        self._report_restored = True

    def _evidence_budget(self) -> tuple[int, int]:
        projection = self._policy_model.policy_projection()
        return (
            len(projection.consumed_step_hashes),
            projection.learning_policy.max_consumed_step_hashes,
        )

    def _record_error(self, error: BaseException) -> None:
        if isinstance(error, StoragePortError) and self._on_storage_error is not None:
            self._on_storage_error(error)
        elif self._on_error is not None:
            self._on_error(error)

    def _record_report_error(self, error: BaseException) -> None:
        if self._on_report_error is not None:
            self._on_report_error(error)
