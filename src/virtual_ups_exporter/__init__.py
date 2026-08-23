"""Atomic virtual-UPS safety publication."""

from __future__ import annotations

import math
import os
import signal
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Callable

from src.application.publication_freshness import (
    PublicationFreshness,
    PublicationFreshnessState,
    PublicationFreshnessTracker,
)
from src.application.safety import SafetyCalculation, SafetyPublication
from src.domain.values import (
    FrozenModelSnapshot,
    PhysicalObservation,
)
from src.virtual_ups_exporter.atomic_publication_cleanup import cleanup_atomic_publication

MAX_VIRTUAL_UPS_BYTES = 16 * 1024
MAX_NUT_VALUE_TEXT = 512
# Standalone exporter default; the composition root derives the production
# value from shutdown policy and NUT transport timing.
MAX_PUBLICATION_AGE_SEC = 30.0
PUBLICATION_DEADLINE_SEC = 0.8


class SafetyPublicationError(RuntimeError):
    """The complete virtual safety state could not be published atomically."""


@dataclass(frozen=True, slots=True)
class PollPublicationContext:
    observation: PhysicalObservation
    snapshot: FrozenModelSnapshot
    calculation: SafetyCalculation
    poll_latency_ms: float


@dataclass(frozen=True, slots=True)
class _ApparentTransitionSag:
    observed_utc: str
    voltage_drop_v: float
    load_delta_percent: float


class VirtualUpsExporter:
    """Publish one complete safety result without consulting mutable model state."""

    def __init__(
        self,
        *,
        virtual_ups_path: Path,
        max_publication_age_s: float = MAX_PUBLICATION_AGE_SEC,
        publication_deadline_s: float = PUBLICATION_DEADLINE_SEC,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(max_publication_age_s) or max_publication_age_s <= 0.0:
            raise ValueError("max_publication_age_s must be positive and finite")
        if not math.isfinite(publication_deadline_s) or publication_deadline_s <= 0.0:
            raise ValueError("publication_deadline_s must be positive and finite")
        self.virtual_ups_path = Path(virtual_ups_path)
        self._max_publication_age_s = max_publication_age_s
        self._publication_deadline_s = publication_deadline_s
        self._monotonic_clock = monotonic_clock
        self._lock = Lock()
        self._staged: PollPublicationContext | None = None
        self._last_publication: SafetyPublication | None = None
        self._last_online_observation: PhysicalObservation | None = None
        self._last_apparent_sag: _ApparentTransitionSag | None = None
        self._last_error: str | None = None
        self._poll_error: str | None = None
        self._storage_error: str | None = None
        self._freshness_tracker = PublicationFreshnessTracker(
            initial_monotonic=self._monotonic_clock(),
            initial_file_age_s=_existing_file_age_s(self.virtual_ups_path),
            max_age_s=self._max_publication_age_s,
        )

    def stage(self, context: PollPublicationContext) -> None:
        """Freeze the physical/model context consumed by the next publication."""
        if not math.isfinite(context.poll_latency_ms) or context.poll_latency_ms < 0.0:
            raise ValueError("poll_latency_ms must be finite and non-negative")
        with self._lock:
            self._staged = context

    def publish(self, publication: SafetyPublication) -> None:
        """Atomically replace the dummy-ups device file for one complete poll."""
        with self._lock:
            context = self._staged
            if context is None:
                raise SafetyPublicationError("safety publication has no staged physical context")
            try:
                self._validate_publication(context, publication)
                metrics = self._virtual_metrics(context, publication)
                payload = "".join(f"{key}: {_nut_value(value)}\n" for key, value in metrics.items())
                _write_with_deadline(
                    self.virtual_ups_path,
                    payload,
                    mode=0o644,
                    byte_limit=MAX_VIRTUAL_UPS_BYTES,
                    deadline_s=self._publication_deadline_s,
                )
            except (OSError, ValueError, SafetyPublicationError) as exc:
                self._mark_publication_failed(exc)
                if isinstance(exc, SafetyPublicationError):
                    raise
                raise SafetyPublicationError(
                    f"virtual UPS publication failed: {self._last_error}"
                ) from exc
            self._record_apparent_transition(context)
            self._last_publication = publication
            self._poll_error = None
            self._refresh_error_locked()
            self._freshness_tracker.record_success(self._monotonic_clock())
            self._staged = None

    def handle_poll_failure(
        self,
        error: BaseException,
        *,
        now: float | None = None,
    ) -> PublicationFreshness:
        """Apply grace then publish explicit low-battery fail-safe output."""
        with self._lock:
            self._poll_error = _bounded_error(error)
            self._refresh_error_locked()
            freshness = self._freshness_locked(self._monotonic_clock() if now is None else now)
            if freshness.state == PublicationFreshnessState.FRESH:
                self._freshness_tracker.mark_temporarily_unavailable()
                freshness = PublicationFreshness(
                    PublicationFreshnessState.TEMPORARILY_UNAVAILABLE,
                    freshness.age_s,
                    freshness.reason,
                    freshness.fail_safe_active,
                )
            if freshness.state == PublicationFreshnessState.TEMPORARILY_UNAVAILABLE:
                return freshness
            if not self._freshness_tracker.has_prior_publication and self._last_publication is None:
                # An unavailable NUT endpoint cannot distinguish an upsd
                # restart from a simultaneous blackout. Without one current
                # physical observation, keep the output absent and retry in
                # this process; synthetic LB would create a false shutdown.
                self._freshness_tracker.mark_stale(fail_safe_active=False)
                self._staged = None
                return self._freshness_locked(self._monotonic_clock() if now is None else now)
            reason = freshness.reason or self._last_error or "poll unavailable"
            payload = _fail_safe_payload(reason)
            try:
                _write_with_deadline(
                    self.virtual_ups_path,
                    payload,
                    mode=0o644,
                    byte_limit=MAX_VIRTUAL_UPS_BYTES,
                    deadline_s=self._publication_deadline_s,
                )
            except (OSError, ValueError, SafetyPublicationError) as exc:
                self._mark_publication_failed(exc)
                raise SafetyPublicationError(
                    f"fail-safe UPS publication failed: {self._last_error}"
                ) from exc
            self._freshness_tracker.mark_stale(fail_safe_active=True)
            self._staged = None
            return self._freshness_locked(self._monotonic_clock() if now is None else now)

    def freshness(self, *, now: float | None = None) -> PublicationFreshness:
        """Return the typed freshness state without performing I/O."""
        with self._lock:
            return self._freshness_locked(self._monotonic_clock() if now is None else now)

    @property
    def watchdog_healthy(self) -> bool:
        """Healthy watchdog is allowed only while the current safety path is fresh."""
        with self._lock:
            return self._freshness_tracker.state == PublicationFreshnessState.FRESH

    def record_channel_error(self, channel: str, error: BaseException | str) -> None:
        """Latch one bounded error channel without touching unrelated channels."""
        with self._lock:
            self._set_channel_error_locked(channel, _bounded_error(error))
            self._refresh_error_locked()

    def clear_channel_error(self, channel: str) -> None:
        """Clear only the named channel after its explicit recovery condition."""
        with self._lock:
            self._set_channel_error_locked(channel, None)
            self._refresh_error_locked()

    def _set_channel_error_locked(self, channel: str, value: str | None) -> None:
        match channel:
            case "poll":
                self._poll_error = value
            case "storage":
                self._storage_error = value
            case _:
                raise ValueError(f"unknown error channel: {channel}")

    def invalidate_output(self) -> None:
        """Remove an untrusted old output after a publication path failure."""
        with self._lock:
            _run_with_deadline(
                self._invalidate_output_locked,
                deadline_s=self._publication_deadline_s,
                operation_name="publication invalidation",
            )

    def _virtual_metrics(
        self,
        context: PollPublicationContext,
        publication: SafetyPublication,
    ) -> dict[str, object]:
        observation = context.observation
        calculation = context.calculation
        snapshot = context.snapshot
        metrics: dict[str, object] = {
            "ups.status": publication.virtual_status_token,
            "battery.runtime": max(0, round(calculation.runtime_minutes * 60.0)),
            "battery.charge": calculation.charge_percent,
            "battery.voltage": observation.battery_voltage_v,
            "ups.load": observation.load_percent,
            "input.voltage": observation.input_voltage_v,
            "battery.health": round(snapshot.soh * 100.0),
            "battery.load_sag.coefficient_v_per_load_percent": snapshot.ir_k_v_per_pp,
            "ups.raw.status": publication.raw_status,
            "ups.raw.lb_observed": publication.raw_lb_observed,
            "ups.safety.lb_source": (
                "none"
                if publication.virtual_lb_source is None
                else publication.virtual_lb_source.value
            ),
            "ups.safety.event_class": publication.event_class.value,
        }
        if self._last_apparent_sag is not None:
            metrics["battery.load_sag.apparent_transition_v"] = (
                self._last_apparent_sag.voltage_drop_v
            )
            metrics["battery.load_sag.apparent_transition_load_delta_percent"] = (
                self._last_apparent_sag.load_delta_percent
            )
        return {key: value for key, value in metrics.items() if value is not None}

    def _record_apparent_transition(self, context: PollPublicationContext) -> None:
        observation = context.observation
        if self._is_online(observation):
            self._last_online_observation = observation
            return
        apparent_sag = self._apparent_sag(observation)
        if apparent_sag is not None:
            self._last_apparent_sag = apparent_sag

    @staticmethod
    def _is_online(observation: PhysicalObservation) -> bool:
        flags = frozenset(observation.raw_status.split())
        return "OL" in flags and "OB" not in flags

    def _apparent_sag(self, observation: PhysicalObservation) -> _ApparentTransitionSag | None:
        if "OB" not in observation.raw_status.split():
            return None
        before = self._last_online_observation
        if before is None:
            return None
        values = _transition_values(before, observation)
        if values is None:
            return None
        before_voltage, voltage, before_load, load = values
        return _ApparentTransitionSag(
            observed_utc=observation.wall_time_utc.isoformat(),
            voltage_drop_v=before_voltage - voltage,
            load_delta_percent=load - before_load,
        )

    def _freshness_locked(self, now: float) -> PublicationFreshness:
        return self._freshness_tracker.evaluate(
            now,
            last_error=_bounded_text(self._last_error),
            has_current_publication=self._last_publication is not None,
        )

    def _mark_publication_failed(self, error: BaseException) -> None:
        self._poll_error = _bounded_error(error)
        self._refresh_error_locked()
        self._freshness_tracker.mark_stale(fail_safe_active=False)
        self._staged = None
        try:
            _run_with_deadline(
                self._invalidate_output_locked,
                deadline_s=self._publication_deadline_s,
                operation_name="publication invalidation",
            )
        except (OSError, SafetyPublicationError) as cleanup_error:
            self._poll_error = _bounded_error(cleanup_error)
            self._refresh_error_locked()

    def _refresh_error_locked(self) -> None:
        self._last_error = next(
            (
                value
                for value in (
                    self._poll_error,
                    self._storage_error,
                )
                if value is not None
            ),
            None,
        )

    def _invalidate_output_locked(self) -> None:
        path = self.virtual_ups_path
        if path.is_symlink():
            raise OSError("cannot invalidate symlinked virtual UPS output")
        if not path.exists():
            return
        if not path.is_file():
            raise OSError("cannot invalidate non-regular virtual UPS output")
        path.unlink()
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _validate_publication(
        context: PollPublicationContext,
        publication: SafetyPublication,
    ) -> None:
        calculation = context.calculation
        observation = context.observation
        if publication.virtual_status_token != calculation.virtual_status:
            raise SafetyPublicationError("publication status disagrees with staged calculation")
        if publication.modeled_runtime_minutes != calculation.runtime_minutes:
            raise SafetyPublicationError("publication runtime disagrees with staged calculation")
        if publication.raw_status != observation.raw_status:
            raise SafetyPublicationError("publication raw status disagrees with staged observation")


def _transition_values(
    before: PhysicalObservation,
    observation: PhysicalObservation,
) -> tuple[float, float, float, float] | None:
    before_voltage = before.battery_voltage_v
    voltage = observation.battery_voltage_v
    before_load = before.load_percent
    load = observation.load_percent
    if before_voltage is None or voltage is None or before_load is None or load is None:
        return None
    return before_voltage, voltage, before_load, load


def _nut_value(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("NUT metric must be finite")
        return format(value, ".9g")
    text = " ".join(str(value).replace(":", " ").split())
    if not text:
        raise ValueError("NUT metric must not be empty")
    return text[:MAX_NUT_VALUE_TEXT]


def _atomic_write_text(path: Path, text: str, *, mode: int, byte_limit: int) -> None:
    raw = text.encode("utf-8")
    if len(raw) > byte_limit:
        raise ValueError(f"publication exceeds {byte_limit} bytes")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("publication target is a symlink")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    temporary_identity: tuple[int, int] | None = None
    primary_error: BaseException | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        temporary_identity, validation_error = _inspect_publication_temporary(
            descriptor,
            temporary,
        )
        if validation_error is not None:
            raise validation_error
        os.fchmod(descriptor, mode)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short publication write")
            view = view[written:]
        os.fdatasync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_atomic_publication(
            temporary,
            descriptor,
            temporary_identity,
            primary_error,
        )


def _inspect_publication_temporary(
    descriptor: int,
    temporary: Path,
) -> tuple[tuple[int, int] | None, OSError | None]:
    """Inspect both ownership views, retaining one exact identity if proven."""
    descriptor_identity: tuple[int, int] | None = None
    path_identity: tuple[int, int] | None = None
    first_error: OSError | None = None

    try:
        descriptor_stat = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_stat.st_mode):
            raise OSError("publication temporary descriptor is not a regular file")
        descriptor_identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
    except OSError as exc:
        first_error = exc

    try:
        opened_stat = os.lstat(temporary)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise OSError("publication temporary target is not a regular file")
        path_identity = (opened_stat.st_dev, opened_stat.st_ino)
    except OSError as exc:
        if first_error is None:
            first_error = exc

    if (
        descriptor_identity is not None
        and path_identity is not None
        and descriptor_identity != path_identity
        and first_error is None
    ):
        first_error = OSError("publication temporary descriptor changed")

    identity = descriptor_identity if descriptor_identity is not None else path_identity
    return identity, first_error


def _write_with_deadline(
    path: Path,
    text: str,
    *,
    mode: int,
    byte_limit: int,
    deadline_s: float,
) -> None:
    """Bound the synchronous publication path on the poll thread."""
    _run_with_deadline(
        lambda: _atomic_write_text(path, text, mode=mode, byte_limit=byte_limit),
        deadline_s=deadline_s,
        operation_name="publication write",
    )


def _run_with_deadline(
    operation: Callable[[], None],
    *,
    deadline_s: float,
    operation_name: str,
) -> None:
    """Run one main-thread filesystem operation under a finite alarm boundary."""
    if threading.current_thread() is not threading.main_thread():
        raise SafetyPublicationError("bounded safety publication requires the process main thread")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer: tuple[float, float] | None = None
    previous_timer_started_at: float | None = None
    handler_installed = False
    operation_completed = False
    restore_timer: tuple[float, float] | None = None
    deliver_expired_one_shot = False

    def timeout_handler(_signum: int, _frame: object) -> None:
        if operation_completed:
            return
        raise TimeoutError(f"{operation_name} exceeded {deadline_s:.3f}s deadline")

    try:
        signal.signal(signal.SIGALRM, timeout_handler)
        handler_installed = True
        previous_timer = signal.setitimer(signal.ITIMER_REAL, deadline_s)
        previous_timer_started_at = time.monotonic()
        operation()
        operation_completed = True
    finally:
        try:
            if previous_timer is not None and previous_timer_started_at is not None:
                elapsed_s = max(0.0, time.monotonic() - previous_timer_started_at)
                remaining_s, interval_s, expired_one_shot = _adjust_previous_timer(
                    previous_timer,
                    elapsed_s,
                )
                signal.setitimer(signal.ITIMER_REAL, 0.0)
                if expired_one_shot:
                    deliver_expired_one_shot = operation_completed
                elif remaining_s > 0.0:
                    restore_timer = (remaining_s, interval_s)
        finally:
            if handler_installed:
                signal.signal(signal.SIGALRM, previous_handler)
        if restore_timer is not None:
            signal.setitimer(signal.ITIMER_REAL, *restore_timer)
        if deliver_expired_one_shot:
            os.kill(os.getpid(), signal.SIGALRM)


def _adjust_previous_timer(
    previous_timer: tuple[float, float],
    elapsed_s: float,
) -> tuple[float, float, bool]:
    """Return the pre-existing timer's next deadline after elapsed inner work."""
    previous_remaining_s, previous_interval_s = previous_timer
    if previous_remaining_s <= 0.0:
        return 0.0, 0.0, False
    if previous_interval_s <= 0.0:
        if elapsed_s < previous_remaining_s:
            return previous_remaining_s - elapsed_s, 0.0, False
        return 0.0, 0.0, elapsed_s >= previous_remaining_s
    if elapsed_s < previous_remaining_s:
        return previous_remaining_s - elapsed_s, previous_interval_s, False

    elapsed_after_first = elapsed_s - previous_remaining_s
    remainder_s = elapsed_after_first % previous_interval_s
    next_remaining_s = (
        previous_interval_s if remainder_s <= 0.0 else previous_interval_s - remainder_s
    )
    return next_remaining_s, previous_interval_s, False


def _fail_safe_payload(reason: str) -> str:
    metrics = {
        "ups.status": "OB DISCHRG LB",
        "battery.runtime": 0,
        "battery.charge": 0,
        "ups.safety.freshness": "stale_failed",
        "ups.safety.failure_reason": reason,
    }
    return "".join(f"{key}: {_nut_value(value)}\n" for key, value in metrics.items())


def _existing_file_age_s(path: Path) -> float | None:
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    return max(0.0, age) if math.isfinite(age) else None


def _bounded_text(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:MAX_NUT_VALUE_TEXT] or None


def _bounded_error(error: BaseException | str) -> str:
    if isinstance(error, BaseException):
        return f"{type(error).__name__}: {_bounded_text(error) or 'operation failed'}"[
            :MAX_NUT_VALUE_TEXT
        ]
    return _bounded_text(error) or "operation failed"
