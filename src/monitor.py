"""Composition root and one-second safety publication loop."""

import argparse
import logging
import math
import signal
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from src.adapters.model_owner import ModelOwner
from src.adapters.nut_telemetry import NutTelemetry
from src.adapters.telemetry_jsonl import TelemetryJsonlWriter
from src.application.model_port import ModelSnapshotPort
from src.application.ports import CloseablePort, PhysicalTelemetryPort
from src.application.publication_freshness import telemetry_loss_grace_s
from src.application.runtime_errors import RuntimeErrorBoundary
from src.application.safety import (
    SafetyCalculation,
    SafetyInputs,
    SafetyLatch,
    SafetyPublication,
    calculate_safety,
    conservative_safety_kind,
    make_safety_publication,
)
from src.domain.lifecycle import classify_physical_observation
from src.domain.values import (
    BlackoutKind,
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


class InvalidObservationBoundary(RuntimeErrorBoundary):
    """A physical outage was observed but cannot enter the safety calculation."""

    def __init__(
        self,
        error: RuntimeErrorBoundary,
        *,
        observation: PhysicalObservation,
        snapshot: FrozenModelSnapshot,
        physical_kind: BlackoutKind,
    ) -> None:
        super().__init__(str(error))
        self.observation = observation
        self.snapshot = snapshot
        self.physical_kind = physical_kind


class PollPublisher(Protocol):
    def stage(self, context: PollPublicationContext) -> None: ...

    def publish(self, publication: SafetyPublication) -> None: ...

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


@dataclass(frozen=True, slots=True)
class RuntimeDependencies:
    """Composition-only dependencies for the safety daemon."""

    telemetry: PhysicalTelemetryPort
    model: RuntimeModelPort
    publisher: PollPublisher
    telemetry_writer: TelemetryJsonlWriter


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
    ) -> None:
        self.config = config
        self._telemetry = dependencies.telemetry
        self._model = dependencies.model
        self._publisher = dependencies.publisher
        self._telemetry_writer = dependencies.telemetry_writer
        self._ema = ema_filter or EMAFilter(
            window_sec=config.ema_window_sec,
            poll_interval_sec=config.polling_interval,
        )
        self._monotonic_clock = clocks.monotonic
        self._sleeper = clocks.sleep
        self._notifier = clocks.notify
        self._latch = SafetyLatch()
        self._running = False
        self._started = False
        self._closed = False
        self._fatal_error: SafetyPublicationError | None = None

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._running = True

    def poll_once(self) -> PollResult:
        """Read, calculate, publish safety, then append raw telemetry."""
        started = self._monotonic_clock()
        observation = self._telemetry.read()
        snapshot = self._model.current_snapshot()
        physical_kind = classify_physical_observation(observation)
        try:
            _validate_observation(observation)
        except RuntimeErrorBoundary as error:
            raise InvalidObservationBoundary(
                error,
                observation=observation,
                snapshot=snapshot,
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
            poll_latency_ms=max(0.0, (self._monotonic_clock() - started) * 1000.0),
        )
        self._publisher.stage(context)
        self._publisher.publish(publication)
        self._publisher.clear_channel_error("poll")

        self._latch = calculation.next_latch
        self._write_telemetry(observation, physical_kind)
        return PollResult(observation, snapshot, calculation, publication)

    def _write_telemetry(
        self, observation: PhysicalObservation, physical_kind: BlackoutKind
    ) -> None:
        try:
            self._telemetry_writer.write(observation, physical_kind)
        except OSError as error:
            logger.warning("Telemetry sample unavailable: %s", error)
            self._publisher.record_channel_error("storage", error)
        else:
            self._publisher.clear_channel_error("storage")

    def _safety_kind(self, physical_kind: BlackoutKind) -> BlackoutKind:
        safety_kind = conservative_safety_kind(physical_kind)
        return safety_kind

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
                ready_sent = self._notify_iteration(published, ready_sent)
                next_deadline = self._wait_for_next_poll(next_deadline)
        finally:
            self.shutdown()
        if self._fatal_error is not None:
            raise self._fatal_error

    def _notify_iteration(self, published: bool, ready_sent: bool) -> bool:
        if published and not ready_sent:
            self._notifier("READY=1")
            ready_sent = True
        if self._running and self._publisher.watchdog_healthy:
            self._notifier("WATCHDOG=1")
        return ready_sent

    def _wait_for_next_poll(self, next_deadline: float) -> float:
        next_deadline += float(self.config.polling_interval)
        now = self._monotonic_clock()
        if next_deadline <= now:
            return now
        self._sleeper(next_deadline - now)
        return next_deadline

    def _run_poll_iteration(self) -> tuple[bool, bool]:
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
        return True, False

    def _handle_publication_failure(
        self,
        error: SafetyPublicationError,
        *,
        fail_safe: bool = False,
    ) -> None:
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
        shutdown_error = self._shutdown_components()

        # A fatal safety publication error is the actionable root cause.  Do
        # not replace it with a best-effort cleanup error from this method.
        if self._fatal_error is None and shutdown_error is not None:
            raise shutdown_error

    def _shutdown_components(self) -> BaseException | None:
        """Attempt every cleanup step and return the first cleanup failure."""
        first_error: BaseException | None = None
        steps = (
            ("Model owner", self._close_model_owner),
            ("Shutdown notification", lambda: self._notifier("STOPPING=1")),
        )
        for label, action in steps:
            first_error = self._attempt_shutdown_step(label, action, first_error)
        return first_error

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

    def _close_model_owner(self) -> None:
        self._model.close()


def build_daemon(
    config: Config,
    *,
    virtual_ups_path: Path,
) -> MonitorDaemon:
    """Wire the safety loop and direct raw-telemetry side channel."""
    publication_age_s = telemetry_loss_grace_s(
        shutdown_minutes=config.shutdown_minutes,
        nut_timeout_s=config.nut_timeout,
        polling_interval_s=config.polling_interval,
    )
    config.model_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    model = ModelOwner.open_runtime(
        config.model_dir / "model.json",
        rated_capacity_ah=config.capacity_ah,
    )
    exporter = VirtualUpsExporter(
        virtual_ups_path=virtual_ups_path,
        max_publication_age_s=publication_age_s,
    )
    telemetry_writer = TelemetryJsonlWriter(
        config.model_dir,
        silent_window_sec=config.ema_window_sec,
    )
    client = NUTClient(
        host=config.nut_host,
        port=config.nut_port,
        timeout=config.nut_timeout,
        ups_name=config.ups_name,
    )
    try:
        return MonitorDaemon(
            config,
            RuntimeDependencies(
                telemetry=NutTelemetry(client),
                model=model,
                publisher=exporter,
                telemetry_writer=telemetry_writer,
            ),
        )
    except BaseException:
        model.close()
        raise


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UPS battery safety monitor")
    parser.add_argument(
        "--virtual-ups-path",
        type=Path,
        default=Path("/run/ups-battery-monitor/ups-virtual.dev"),
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


if __name__ == "__main__":
    main()
