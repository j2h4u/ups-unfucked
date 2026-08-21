"""Composition root and one-second safety publication loop."""

import argparse
import logging
import math
import signal
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from src.adapters.minimal_jsonl import MinimalJsonlEventStore
from src.adapters.model_owner import ModelOwner
from src.adapters.nut_telemetry import NutTelemetry
from src.alerter import JournaldHealthAlertSink, JournaldReportSink
from src.application.assessment_worker import AssessmentWorker
from src.application.background_coordinator import (
    BackgroundCoordinator,
    BackgroundDependencies,
    BackgroundSettings,
)
from src.application.capture_blackout import BlackoutCapture, RuntimeErrorBoundary
from src.application.capture_writer import CaptureCommandKind, CaptureWriter
from src.application.degraded_startup import DeferredEventStore, EventStorageUnavailable
from src.application.errors import StoragePortConflict, StoragePortError
from src.application.history_query import HistoryReadPort
from src.application.model_port import ModelSnapshotPort
from src.application.ports import (
    AssessmentQueryEventStorePort,
    CloseablePort,
    PhysicalTelemetryPort,
    StartupRecoveryEventStorePort,
)
from src.application.publication_freshness import telemetry_loss_grace_s
from src.application.recharge_capture import RechargeCapture
from src.application.reporting_scheduler import (
    ReportingScheduler,
    ReportingSchedulerDependencies,
)
from src.application.safety import (
    SafetyCalculation,
    SafetyInputs,
    SafetyLatch,
    SafetyPublication,
    calculate_safety,
    conservative_safety_kind,
    make_safety_publication,
)
from src.application.safety_oracle import no_later_lb_oracle
from src.application.startup_recovery import StartupRecovery, recover_startup_metadata
from src.application.storage_values import EventProjection, EventRef, RecoveredCapture
from src.domain.lifecycle import classify_physical_observation, is_capture_candidate
from src.domain.readiness import ReadinessState, initial_readiness_state, update_readiness
from src.domain.values import (
    BlackoutKind,
    ChargeReadiness,
    FrozenModelSnapshot,
    PhysicalObservation,
)
from src.ema_filter import EMAFilter
from src.monitor_config import Config, ConfigError, configure_logging, load_config
from src.nut_client import NUTClient
from src.virtual_ups_exporter import (
    PollPublicationContext,
    SafetyPublicationError,
    VirtualUpsExporter,
)

try:
    from systemd.daemon import notify as sd_notify  # pyright: ignore[reportMissingImports]
except ImportError:

    def sd_notify(_status: str) -> None:
        return None


logger = logging.getLogger("ups-battery-monitor")

STICKY_RECOVERY_DEADLINE_SEC = 5.0
SHUTDOWN_CAPTURE_FLUSH_TIMEOUT_SEC = 5.0
RECONCILIATION_START_UTC = "0001-01-01T00:00:00Z"
RECONCILIATION_END_UTC = "9999-12-31T23:59:59.999999Z"


class InvalidObservationBoundary(RuntimeErrorBoundary):
    """A physical outage was observed but cannot enter the safety calculation."""

    def __init__(
        self,
        error: RuntimeErrorBoundary,
        *,
        observation: PhysicalObservation,
        snapshot: FrozenModelSnapshot,
        readiness: ChargeReadiness | None,
        physical_kind: BlackoutKind,
    ) -> None:
        super().__init__(str(error))
        self.observation = observation
        self.snapshot = snapshot
        self.readiness = readiness
        self.physical_kind = physical_kind


class PollPublisher(Protocol):
    def stage(self, context: PollPublicationContext) -> None: ...

    def publish(self, publication: SafetyPublication) -> None: ...

    def record_error(self, error: BaseException) -> None: ...

    def record_channel_error(self, channel: str, error: BaseException | str) -> None: ...

    def clear_channel_error(self, channel: str) -> None: ...

    def invalidate_output(self) -> None: ...

    def handle_poll_failure(
        self,
        error: BaseException,
        *,
        now: float | None = None,
    ) -> object: ...

    @property
    def watchdog_healthy(self) -> bool: ...


class RuntimeModelPort(ModelSnapshotPort, CloseablePort, Protocol):
    """Safety snapshot owner with an explicit runtime lifecycle."""


@dataclass(frozen=True, slots=True)
class PollResult:
    observation: PhysicalObservation
    snapshot: FrozenModelSnapshot
    calculation: SafetyCalculation
    publication: SafetyPublication
    capture_accepted: bool


@dataclass(frozen=True, slots=True)
class RuntimeDependencies:
    """Composition-only dependencies for the safety daemon."""

    telemetry: PhysicalTelemetryPort
    model: RuntimeModelPort
    publisher: PollPublisher
    capture: BlackoutCapture
    writer: CaptureWriter
    coordinator: BackgroundCoordinator
    store: CloseablePort
    recovered_capture: RecoveredCapture | None = None
    recharge: RechargeCapture | None = None


@dataclass(frozen=True, slots=True)
class RuntimeClocks:
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    notify: Callable[[str], object] = sd_notify


class MonitorDaemon:
    """Orchestrate one immutable-snapshot safety poll at a fixed cadence."""

    def __init__(
        self,
        config: Config,
        dependencies: RuntimeDependencies,
        *,
        ema_filter: EMAFilter | None = None,
        clocks: RuntimeClocks = RuntimeClocks(),
        sticky_recovery_deadline_s: float = STICKY_RECOVERY_DEADLINE_SEC,
    ) -> None:
        if not math.isfinite(sticky_recovery_deadline_s) or sticky_recovery_deadline_s <= 0.0:
            raise ValueError("sticky_recovery_deadline_s must be positive and finite")
        self.config = config
        self._telemetry = dependencies.telemetry
        self._model = dependencies.model
        self._publisher = dependencies.publisher
        self._capture = dependencies.capture
        self._writer = dependencies.writer
        self._coordinator = dependencies.coordinator
        self._store = dependencies.store
        self._recovered_capture = dependencies.recovered_capture
        self._recharge = dependencies.recharge
        self._ema = ema_filter or EMAFilter(
            window_sec=config.ema_window_sec,
            poll_interval_sec=config.polling_interval,
        )
        self._monotonic_clock = clocks.monotonic
        self._sleeper = clocks.sleep
        self._notifier = clocks.notify
        self._sticky_recovery_deadline_s = sticky_recovery_deadline_s
        self._sticky_recovery_deadline: float | None = None
        self._recovery_requested = False
        # An open capture proves only unfinished durable work. The registry has
        # no evidence that virtual LB was ever published, so startup must
        # recompute safety from the first current observation.
        self._latch = SafetyLatch()
        self._readiness: ReadinessState = initial_readiness_state()
        self._running = False
        self._started = False
        self._closed = False
        self._fatal_error: SafetyPublicationError | None = None
        self._poll_sequence = 0
        self._consecutive_errors = 0
        self._last_observation: PhysicalObservation | None = None
        self._restart_reconciliation_done = False

    def start(self) -> None:
        if self._started:
            return
        self._writer.start()
        self._coordinator.start()
        self._started = True
        self._running = True

    def poll_once(self) -> PollResult:
        """Read, freeze, calculate, publish, then hand off capture in that order."""
        started = self._monotonic_clock()
        observation = self._telemetry.read()
        snapshot = self._model.current_snapshot()
        physical_kind = classify_physical_observation(observation)
        try:
            _validate_observation(observation)
        except RuntimeErrorBoundary as error:
            readiness = None
            if is_capture_candidate(observation):
                self._readiness, readiness = update_readiness(self._readiness, observation)
            raise InvalidObservationBoundary(
                error,
                observation=observation,
                snapshot=snapshot,
                readiness=readiness,
                physical_kind=physical_kind,
            ) from error
        voltage = observation.battery_voltage_v
        load = observation.load_percent
        assert voltage is not None and load is not None
        self._ema.add_sample(voltage, load)
        filtered_voltage = self._ema.voltage
        filtered_load = self._ema.load
        if filtered_voltage is None or filtered_load is None:
            raise RuntimeErrorBoundary("EMA did not produce a complete safety input")

        safety_kind = self._safety_kind(physical_kind)
        calculation = calculate_safety(
            inputs=SafetyInputs(
                voltage_v=filtered_voltage,
                load_percent=filtered_load,
                blackout_kind=safety_kind,
                shutdown_threshold_minutes=self.config.shutdown_minutes,
                previous_latch=self._latch,
            ),
            snapshot=snapshot,
        )
        publication = make_safety_publication(observation, calculation)
        context = PollPublicationContext(
            observation=observation,
            snapshot=snapshot,
            calculation=calculation,
            poll_sequence=self._poll_sequence,
            poll_latency_ms=max(0.0, (self._monotonic_clock() - started) * 1000.0),
        )
        self._publisher.stage(context)
        self._publisher.publish(publication)
        self._publisher.clear_channel_error("poll")

        self._latch = calculation.next_latch
        self._readiness, readiness = update_readiness(self._readiness, observation)
        self._coordinator.after_first_safety_publication()
        self._capture_recharge(observation, physical_kind)
        capture_accepted = self._capture_after_publication(
            observation,
            snapshot,
            readiness,
            physical_kind,
        )
        self._last_observation = observation
        self._poll_sequence += 1
        self._consecutive_errors = 0
        self._coordinator.record_poll_error_count(0)
        if self._recovery_requested:
            self._fatal_error = SafetyPublicationError(
                "sticky recovery deadline exceeded after fresh OL; restarting safety process"
            )
            self.request_stop()
        return PollResult(observation, snapshot, calculation, publication, capture_accepted)

    def _capture_recharge(
        self, observation: PhysicalObservation, physical_kind: BlackoutKind
    ) -> None:
        recharge = self._recharge
        if recharge is None or not self._coordinator.capture_enabled:
            return
        if physical_kind == BlackoutKind.ONLINE:
            restored_blackout_id = recharge.consume_pending_restoration()
            if restored_blackout_id is not None:
                accepted = recharge.begin(observation, preceding_blackout_id=restored_blackout_id)
                if accepted:
                    recharge.acknowledge_pending_restoration(restored_blackout_id)
                    self._restart_reconciliation_done = True
                return
            if not self._restart_reconciliation_done:
                projections = _restart_recharge_projections(self._store)
                if recharge.reconcile_restart(observation, projections):
                    self._restart_reconciliation_done = True
            recharge.observe(observation)
            return
        if is_capture_candidate(observation):
            recharge.supersede_by_blackout(
                observation,
                blackout_id=self._capture.active_blackout_id,
            )

    def _safety_kind(self, physical_kind: BlackoutKind) -> BlackoutKind:
        safety_kind = conservative_safety_kind(physical_kind)
        waiting_for_durable_end = (
            self._recovered_capture is not None or self._capture.has_unacknowledged_capture
        )
        if safety_kind != BlackoutKind.ONLINE:
            self._sticky_recovery_deadline = None
            return safety_kind
        if not (self._latch.virtual_lb and waiting_for_durable_end):
            self._sticky_recovery_deadline = None
            return safety_kind
        now = self._monotonic_clock()
        if not math.isfinite(now):
            raise RuntimeErrorBoundary("sticky recovery clock is not finite")
        if self._sticky_recovery_deadline is None:
            self._sticky_recovery_deadline = now + self._sticky_recovery_deadline_s
        if now < self._sticky_recovery_deadline:
            return BlackoutKind.BLACKOUT_REAL
        reason = "sticky recovery deadline exceeded"
        self._capture.expire_sticky_recovery(reason)
        self._publisher.record_channel_error("capture", TimeoutError(reason))
        self._publisher.record_channel_error("storage", TimeoutError(reason))
        self._recovery_requested = True
        self._sticky_recovery_deadline = None
        return safety_kind

    def _capture_after_publication(
        self,
        observation: PhysicalObservation,
        snapshot: FrozenModelSnapshot,
        readiness: ChargeReadiness,
        physical_kind: BlackoutKind,
    ) -> bool:
        if not self._coordinator.capture_enabled:
            self._capture.note_capture_unavailable(observation, snapshot, readiness)
            return False
        if self._recovered_capture is None:
            self._recovered_capture = self._coordinator.take_recovered_capture()
        recovered = self._recovered_capture
        if recovered is not None:
            if physical_kind == BlackoutKind.ONLINE:
                accepted = self._capture.close_recovered_capture(recovered)
                if accepted:
                    self._recovered_capture = None
                return accepted
            self._capture.attach_recovered_capture(
                recovered.handle,
                boot_id=recovered.last_boot_id,
            )
            self._recovered_capture = None
        return self._capture.accept_after_safety_publish(
            observation,
            safety_snapshot=snapshot,
            charge_readiness=readiness,
        )

    def run(self) -> None:
        """Poll once per monotonic second; secondary failures never create a second path."""
        self.start()
        next_deadline = self._monotonic_clock()
        ready_sent = False
        try:
            while self._running:
                keep_running, published = self._run_poll_iteration()
                if not keep_running:
                    break
                if published:
                    if not ready_sent:
                        self._notifier("READY=1")
                        ready_sent = True
                if (
                    self._running
                    and not self._recovery_requested
                    and self._publisher.watchdog_healthy
                ):
                    self._notifier("WATCHDOG=1")
                next_deadline += float(self.config.polling_interval)
                now = self._monotonic_clock()
                if next_deadline <= now:
                    next_deadline = now
                else:
                    self._sleeper(next_deadline - now)
        finally:
            self.shutdown()
        if self._fatal_error is not None:
            raise self._fatal_error

    def _run_poll_iteration(self) -> tuple[bool, bool]:
        fatal_startup_error = self._coordinator.fatal_startup_error
        if fatal_startup_error is not None:
            raise fatal_startup_error
        try:
            self.poll_once()
        except InvalidObservationBoundary as exc:
            return self._handle_invalid_observation(exc)
        except SafetyPublicationError as exc:
            self._handle_publication_failure(exc)
            return False, False
        except (
            OSError,
            ConnectionError,
            TimeoutError,
            ValueError,
            RuntimeErrorBoundary,
        ) as exc:
            return self._handle_poll_failure(exc)
        return True, True

    def _handle_poll_failure(self, error: BaseException) -> tuple[bool, bool]:
        self._consecutive_errors += 1
        self._coordinator.record_poll_error_count(self._consecutive_errors)
        self._publisher.record_channel_error("poll", error)
        logger.warning(
            "Safety poll unavailable: %s",
            error,
            extra={"event_type": "poll_unavailable"},
        )
        try:
            self._publisher.handle_poll_failure(error, now=self._monotonic_clock())
        except SafetyPublicationError as publication_error:
            self._handle_publication_failure(publication_error, fail_safe=True)
            return False, False
        return True, False

    def _handle_invalid_observation(
        self,
        error: InvalidObservationBoundary,
    ) -> tuple[bool, bool]:
        """Run fail-safe freshness handling before retaining a physical outage."""
        continued, _ = self._handle_poll_failure(error)
        if not continued:
            return False, False
        self._last_observation = error.observation
        if is_capture_candidate(error.observation) and error.readiness is not None:
            self._capture.note_capture_unavailable(
                error.observation,
                error.snapshot,
                error.readiness,
            )
        return True, False

    def _handle_publication_failure(
        self,
        error: SafetyPublicationError,
        *,
        fail_safe: bool = False,
    ) -> None:
        self._consecutive_errors += 1
        self._coordinator.record_poll_error_count(self._consecutive_errors)
        self._publisher.record_channel_error("poll", error)
        self._fatal_error = error
        self.request_stop()
        try:
            self._publisher.invalidate_output()
        except (OSError, SafetyPublicationError) as cleanup_error:
            logger.critical(
                "Safety output invalidation failed: %s",
                cleanup_error,
                exc_info=True,
            )
        event_type = "safety_fail_safe_failed" if fail_safe else "safety_publication_failed"
        logger.critical(
            "Safety publication failed; watchdog will not be refreshed: %s",
            error,
            exc_info=True,
            extra={"event_type": event_type},
        )

    def request_stop(self) -> None:
        self._running = False

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._running = False
        if self._recovery_requested:
            # The writer may still be inside an uninterruptible filesystem
            # syscall.  Let process/systemd recovery reclaim it; do not attempt
            # cancellation or a blocking join on this safety-failure path.
            self._notifier("STOPPING=1")
            return
        shutdown_error = self._shutdown_components()

        # A fatal safety publication error is the actionable root cause.  Do
        # not replace it with a best-effort cleanup error from this method.
        if self._fatal_error is None and shutdown_error is not None:
            raise shutdown_error

    def _shutdown_components(self) -> BaseException | None:
        """Attempt every cleanup step and return the first cleanup failure."""
        first_error: BaseException | None = None
        steps = (
            ("Background coordinator", self._coordinator.stop),
            ("Shutdown capture boundary", self._shutdown_capture_boundary),
            ("Shutdown recharge boundary", self._shutdown_recharge_boundary),
            ("Capture writer", lambda: self._writer.stop(drain=True)),
            ("Event store", self._store.close),
            ("Model owner", self._close_model_owner),
            ("Shutdown notification", lambda: self._notifier("STOPPING=1")),
        )
        for label, action in steps:
            first_error = self._attempt_shutdown_step(label, action, first_error)
        return first_error

    def _shutdown_recharge_boundary(self) -> None:
        if self._recharge is None or self._last_observation is None:
            return
        if self._recharge.service_stop(self._last_observation):
            return
        if self._writer.wait_for_lifecycle_capacity(SHUTDOWN_CAPTURE_FLUSH_TIMEOUT_SEC):
            if self._recharge.service_stop(self._last_observation):
                return
        raise RuntimeError("shutdown recharge boundary could not be queued")

    def _attempt_shutdown_step(
        self,
        label: str,
        action: Callable[[], object],
        first_error: BaseException | None,
    ) -> BaseException | None:
        try:
            action()
        except BaseException as error:
            if first_error is not None:
                logger.error("Additional shutdown cleanup failed: %s", error, exc_info=True)
            else:
                first_error = error
            logger.error("%s shutdown failed", label, exc_info=True)
        return first_error

    def _shutdown_capture_boundary(self) -> None:
        if self._last_observation is None:
            return
        accepted = self._capture.service_stop(self._last_observation)
        if accepted:
            return
        capacity = self._writer.wait_for_lifecycle_capacity(SHUTDOWN_CAPTURE_FLUSH_TIMEOUT_SEC)
        if not capacity or not self._capture.service_stop(self._last_observation):
            raise RuntimeError("shutdown capture boundary could not be queued")

    def _close_model_owner(self) -> None:
        self._model.close()


def build_daemon(
    config: Config,
    *,
    virtual_ups_path: Path,
    health_path: Path,
) -> MonitorDaemon:
    """Wire safety first; scientific recovery runs only after the first poll."""
    publication_age_s = telemetry_loss_grace_s(
        shutdown_minutes=config.shutdown_minutes,
        nut_timeout_s=config.nut_timeout,
        polling_interval_s=config.polling_interval,
    )
    config.model_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    model = ModelOwner.open_runtime(
        config.model_dir / "model.json",
        rated_capacity_ah=config.capacity_ah,
        safety_oracle=lambda before, after: no_later_lb_oracle(
            before,
            after,
            shutdown_threshold_minutes=config.shutdown_minutes,
        ),
    )
    exporter = VirtualUpsExporter(
        virtual_ups_path=virtual_ups_path,
        health_path=health_path,
        max_publication_age_s=publication_age_s,
    )
    writer = CaptureWriter(
        on_failure=lambda command, error: exporter.record_channel_error(
            "storage"
            if command.kind
            in {CaptureCommandKind.MODEL_COMMIT, CaptureCommandKind.RECOVERY_RECEIPT}
            else "capture",
            error,
        )
    )
    client = NUTClient(
        host=config.nut_host,
        port=config.nut_port,
        timeout=config.nut_timeout,
        ups_name=config.ups_name,
    )
    startup_error: BaseException = EventStorageUnavailable(
        "scientific event storage deferred until first safety publication"
    )
    deferred_store = DeferredEventStore(startup_error)
    recharge = RechargeCapture(deferred_store, writer)

    def load_store() -> StartupRecovery:
        try:
            candidate = MinimalJsonlEventStore(
                config.model_dir,
                writer_lock_fd=model.writer_lock_fd,
            )
        except StoragePortConflict:
            raise
        except (StoragePortError, OSError) as exc:
            deferred_store.degrade(exc)
            raise
        try:
            startup = recover_startup_metadata(candidate)
        except StoragePortConflict:
            candidate.close()
            raise
        except (StoragePortError, OSError) as exc:
            deferred_store.degrade(exc)
            candidate.close()
            raise
        recovered = startup.recovered_capture
        if recovered is not None and recovered.event_kind == "recharge":
            recharge.recover(recovered)
            startup = StartupRecovery(None, startup.pending_processing)
        deferred_store.activate(candidate)
        return startup

    store = deferred_store
    try:
        worker = AssessmentWorker(store, model)
        reporter = ReportingScheduler(
            ReportingSchedulerDependencies(
                store=store,
                model=model,
                writer=writer,
                publisher=exporter,
                health_alerts=JournaldHealthAlertSink(),
            )
        )
        report_sink = JournaldReportSink(on_report=exporter.record_report)
        startup = StartupRecovery(None, ())
        coordinator = BackgroundCoordinator(
            BackgroundDependencies(
                assessment_store=store,
                startup_store=store,
                reporting_store=store,
                commit_model=model,
                policy_model=model,
                worker=worker,
                writer=writer,
                reporter=reporter,
                report_sink=report_sink,
                # The minimal event files are the durable report source.  A
                # report is reconstructed from sealed history on close and at
                # startup; there is no second durable outbox in this cutover.
                report_outbox_store=None,
            ),
            startup,
            BackgroundSettings(
                reporting_interval_s=config.reporting_interval,
                on_error=exporter.record_error,
                on_storage_error=lambda error: exporter.record_channel_error("storage", error),
                on_report_error=lambda error: exporter.record_channel_error("report", error),
                on_background_recovered=lambda: exporter.clear_channel_error("background"),
                startup_loader=load_store,
                startup_error=startup_error,
                on_startup_degraded=exporter.record_startup_degraded,
                on_startup_recovered=exporter.clear_startup_degraded,
            ),
        )
        capture = BlackoutCapture(store, writer, on_power_restored=recharge.on_power_restored)
        return MonitorDaemon(
            config,
            RuntimeDependencies(
                telemetry=NutTelemetry(client),
                model=model,
                publisher=exporter,
                capture=capture,
                writer=writer,
                coordinator=coordinator,
                store=store,
                recharge=recharge,
            ),
        )
    except BaseException:
        try:
            store.close()
        finally:
            model.close()
        raise


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UPS battery safety monitor")
    parser.add_argument(
        "--virtual-ups-path",
        type=Path,
        default=Path("/run/ups-battery-monitor/ups-virtual.dev"),
    )
    parser.add_argument(
        "--health-path",
        type=Path,
        default=Path("/run/ups-battery-monitor/ups-health.json"),
    )
    return parser.parse_args(args)


def main() -> None:
    configure_logging()
    arguments = parse_args()
    try:
        config = load_config()
        daemon = build_daemon(
            config,
            virtual_ups_path=arguments.virtual_ups_path,
            health_path=arguments.health_path,
        )
    except (ConfigError, OSError, RuntimeError, ValueError) as exc:
        logger.critical("Monitor startup failed: %s", exc, exc_info=True)
        raise SystemExit(1) from exc

    def stop(_signum: int, _frame: object) -> None:
        daemon.request_stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    daemon.run()


def _validate_observation(observation: PhysicalObservation) -> None:
    voltage = observation.battery_voltage_v
    load = observation.load_percent
    if voltage is None or not math.isfinite(voltage) or voltage <= 0.0:
        raise RuntimeErrorBoundary("battery voltage is missing or invalid")
    if load is None or not math.isfinite(load) or not 0.0 <= load <= 100.0:
        raise RuntimeErrorBoundary("UPS load is missing or invalid")


def _restart_recharge_projections(store: CloseablePort) -> tuple[EventProjection, ...]:
    """Read sealed and bounded pending ordinary events for restart handoff."""
    history = cast(HistoryReadPort, store)
    projections = list(
        history.sealed_event_projections(
            RECONCILIATION_START_UTC,
            RECONCILIATION_END_UTC,
            event_kind="blackout",
        )
    )
    projections.extend(
        history.sealed_event_projections(
            RECONCILIATION_START_UTC,
            RECONCILIATION_END_UTC,
            event_kind="recharge",
        )
    )
    startup = cast(StartupRecoveryEventStorePort, store)
    query = cast(AssessmentQueryEventStorePort, store)
    for pending in startup.work_registry().pending_processing:
        projection = query.project(EventRef(pending.blackout_id, pending.final_path_token))
        if projection.event_kind in {"blackout", "recharge"}:
            projections.append(projection)
    return tuple(projections)


if __name__ == "__main__":
    main()
