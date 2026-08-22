"""Composition root and one-second safety publication loop."""

import argparse
import logging
import math
import shlex
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from src.adapters.model_owner import ModelOwner
from src.adapters.nut_telemetry import NutTelemetry
from src.adapters.telemetry_jsonl import TelemetryJsonlWriter
from src.adapters.telemetry_jsonl import read as read_telemetry
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
from src.domain.model_feedback import propose_model_feedback
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

QUICK_SELF_TEST_ADDRESS = "cyberpower@localhost"
QUICK_SELF_TEST_COMMAND = "test.battery.start.quick"
NUT_COMMAND_CONFIG_PATH = Path("/etc/nut/upsmon.conf")
QUICK_SELF_TEST_COOLDOWN = timedelta(days=14)


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

    def apply_ir_k(self, value: float) -> tuple[float, float] | None: ...


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
        self._last_self_test_check_date: date | None = None
        self._first_valid_observation_monotonic_ns: int | None = None
        self._feedback_replayed = False

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
        if self._first_valid_observation_monotonic_ns is None:
            self._first_valid_observation_monotonic_ns = observation.monotonic_ns
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
        completed = self._write_telemetry(observation, physical_kind)
        self._run_model_feedback(completed)
        self._maybe_run_quick_self_test(observation, physical_kind)
        return PollResult(observation, snapshot, calculation, publication)

    def _maybe_run_quick_self_test(
        self, observation: PhysicalObservation, physical_kind: BlackoutKind
    ) -> None:
        """Try the quick test at most once per UTC day, without touching safety."""
        now = _utc(observation.wall_time_utc)
        if not self._self_test_horizon_elapsed(observation):
            return
        check_date = now.date()
        if self._last_self_test_check_date == check_date:
            return
        self._last_self_test_check_date = check_date
        if physical_kind != BlackoutKind.ONLINE or observation.battery_pct != 100.0:
            return
        telemetry_path = self.config.model_dir / "events" / "telemetry.jsonl"
        try:
            if _has_recent_quick_test_or_blackout(telemetry_path, now):
                return
        except (OSError, RuntimeError, ValueError):
            logger.warning("Quick UPS self-test skipped: telemetry is invalid")
            return
        try:
            _run_quick_self_test()
        except (OSError, RuntimeError, subprocess.SubprocessError):
            logger.warning("Quick UPS self-test skipped: NUT command failed")

    def _self_test_horizon_elapsed(self, observation: PhysicalObservation) -> bool:
        first = self._first_valid_observation_monotonic_ns
        if first is None:
            return False
        horizon_ns = self.config.ema_window_sec * 1_000_000_000
        return observation.monotonic_ns - first >= horizon_ns

    def _write_telemetry(
        self, observation: PhysicalObservation, physical_kind: BlackoutKind
    ) -> tuple[dict[str, object], ...] | None:
        try:
            self._telemetry_writer.write(observation, physical_kind)
        except OSError as error:
            logger.warning("Telemetry sample unavailable: %s", error)
            self._publisher.record_channel_error("storage", error)
            return None
        else:
            self._publisher.clear_channel_error("storage")
            return self._telemetry_writer.take_completed_episode()

    def _run_model_feedback(self, completed: tuple[dict[str, object], ...] | None) -> None:
        """Apply small post-publication improvements; failures are diagnostic only."""
        try:
            if not self._feedback_replayed:
                self._feedback_replayed = True
                path = self.config.model_dir / "events" / "telemetry.jsonl"
                if path.exists():
                    self._apply_feedback(_closed_episodes(read_telemetry(path).records))
            elif completed is not None:
                self._apply_feedback((completed,))
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            logger.warning("Model feedback unavailable: %s", error)
            self._publisher.record_channel_error("feedback", error)
        else:
            self._publisher.clear_channel_error("feedback")

    def _apply_feedback(self, episodes: tuple[tuple[dict[str, object], ...], ...]) -> None:
        event_kinds = self._telemetry_writer.event_kinds()
        for episode in episodes:
            event_at = _event_start(episode)
            if event_at is None or event_kinds.get(event_at) in {"self_test", "model_update"}:
                continue
            proposal = propose_model_feedback(episode, self._model.current_snapshot())
            if proposal is None:
                continue
            field = "physics.ir_compensation.k_volts_per_percent"
            changed = self._model.apply_ir_k(proposal.to_value)
            if changed is None:
                continue
            self._telemetry_writer.record_model_update(
                at=str(episode[-1]["at"]),
                event_at=event_at,
                evidence_at=proposal.evidence_at,
                changes={field: changed},
                reason=proposal.reason,
            )
            event_kinds[event_at] = "model_update"

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


def _closed_episodes(
    records: Sequence[dict[str, object]],
) -> tuple[tuple[dict[str, object], ...], ...]:
    completed: list[tuple[dict[str, object], ...]] = []
    active: list[dict[str, object]] | None = None
    for record in records:
        flags = str(record.get("status", "")).split()
        if "OB" in flags or "CAL" in flags:
            active = active or []
            active.append(record)
        elif active is not None and "OL" in flags:
            active.append(record)
            completed.append(tuple(active))
            active = None
    return tuple(completed)


def _event_start(records: Sequence[Mapping[str, object]]) -> str | None:
    for record in records:
        flags = str(record.get("status", "")).split()
        at = record.get("at")
        if ("OB" in flags or "CAL" in flags) and isinstance(at, str):
            return at
    return None


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _has_recent_quick_test_or_blackout(path: Path, now: datetime) -> bool:
    if not path.exists():
        return False
    records = read_telemetry(path).records
    cutoff = now - QUICK_SELF_TEST_COOLDOWN
    for record in records:
        status = record["status"]
        if not isinstance(status, str) or not {"OB", "CAL"}.intersection(status.split()):
            continue
        at = record["at"]
        if not isinstance(at, str):
            raise ValueError("telemetry timestamp is invalid")
        timestamp = datetime.fromisoformat(at.removesuffix("Z") + "+00:00")
        if timestamp >= cutoff:
            return True
    return False


def _run_quick_self_test() -> None:
    username, password = _read_nut_command_credentials()
    try:
        result = subprocess.run(
            [
                "upscmd",
                "-u",
                username,
                "-p",
                password,
                QUICK_SELF_TEST_ADDRESS,
                QUICK_SELF_TEST_COMMAND,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("NUT command could not be started") from error
    if result.returncode != 0:
        raise RuntimeError("NUT command returned an error")
    logger.info("Quick UPS self-test started")


def _read_nut_command_credentials() -> tuple[str, str]:
    try:
        lines = NUT_COMMAND_CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RuntimeError("NUT command credentials are unavailable") from error
    for line in lines:
        try:
            fields = shlex.split(line, comments=True)
        except ValueError as error:
            raise RuntimeError("NUT command configuration is malformed") from error
        if not fields or fields[0] != "MONITOR":
            continue
        if len(fields) < 5 or not fields[3] or not fields[4]:
            raise RuntimeError("NUT command credentials are malformed")
        return fields[3], fields[4]
    raise RuntimeError("NUT command credentials are unavailable")


if __name__ == "__main__":
    main()
