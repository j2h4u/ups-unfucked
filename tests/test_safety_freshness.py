"""Deterministic safety freshness and publication failure fixtures."""

from __future__ import annotations

import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

import pytest

from src.application.publication_freshness import (
    PublicationFreshnessState,
    PublicationFreshnessTracker,
    telemetry_loss_grace_s,
)
from src.application.safety import SafetyInputs, calculate_safety, make_safety_publication
from src.battery_math.lut import LutPoint
from src.domain.values import BlackoutKind, FrozenModelSnapshot, PhysicalObservation
from src.virtual_ups_exporter import (
    PollPublicationContext,
    SafetyPublicationError,
    VirtualUpsExporter,
    _adjust_previous_timer,
    _atomic_write_text,
    _run_with_deadline,
)
from src.virtual_ups_exporter.atomic_publication_cleanup import cleanup_atomic_publication


def _snapshot() -> FrozenModelSnapshot:
    return FrozenModelSnapshot(
        7.2,
        12.0,
        510.0,
        1.0,
        1.2,
        0.012,
        0.0,
        (LutPoint(13.7, 1.0), LutPoint(10.8, 0.0)),
    )


def _observation(status: str = "OL") -> PhysicalObservation:
    return PhysicalObservation(
        "boot-a",
        1_000_000_000,
        datetime(2026, 8, 16, tzinfo=timezone.utc),
        status,
        "13.30",
        13.3,
        0.01,
        20.0,
        230.0,
    )


def _publish_online(exporter: VirtualUpsExporter) -> None:
    observation = _observation()
    snapshot = _snapshot()
    calculation = calculate_safety(
        inputs=SafetyInputs(13.3, 20.0, BlackoutKind.ONLINE, 5),
        snapshot=snapshot,
    )
    exporter.stage(PollPublicationContext(observation, snapshot, calculation, 1.0))
    exporter.publish(make_safety_publication(observation, calculation))


def test_poll_loss_uses_grace_then_explicit_lb_fail_safe(tmp_path: Path) -> None:
    now = [0.0]
    exporter = VirtualUpsExporter(
        virtual_ups_path=tmp_path / "ups.dev",
        max_publication_age_s=2.0,
        monotonic_clock=lambda: now[0],
    )
    _publish_online(exporter)

    now[0] = 1.0
    temporary = exporter.handle_poll_failure(ConnectionError("NUT down"))
    assert temporary.state == PublicationFreshnessState.TEMPORARILY_UNAVAILABLE
    assert "ups.status: OL" in (tmp_path / "ups.dev").read_text()

    now[0] = 2.1
    stale = exporter.handle_poll_failure(ConnectionError("NUT still down"))
    assert stale.state == PublicationFreshnessState.STALE_FAILED
    assert stale.fail_safe_active
    assert "ups.status: OB DISCHRG LB" in (tmp_path / "ups.dev").read_text()
    assert "ups.safety.freshness: stale_failed" in (tmp_path / "ups.dev").read_text()
    assert not exporter.watchdog_healthy


@pytest.mark.parametrize(
    ("transport_budget_s", "expected_grace_s"),
    ((29.999, 30.0), (30.0, 30.0), (30.001, 30.001)),
)
def test_telemetry_loss_grace_has_exact_transport_floor_boundary(
    transport_budget_s: float,
    expected_grace_s: float,
) -> None:
    nut_timeout_s = (transport_budget_s - 2.0) / 5.0

    assert telemetry_loss_grace_s(
        shutdown_minutes=5,
        nut_timeout_s=nut_timeout_s,
        polling_interval_s=1.0,
    ) == pytest.approx(expected_grace_s)


@pytest.mark.parametrize(
    ("shutdown_minutes", "nut_timeout_s", "polling_interval_s", "error_fragment"),
    (
        (True, 2.0, 1.0, "shutdown"),
        (5, float("nan"), 1.0, "nut_timeout"),
        (5, 2.0, 0.0, "polling_interval"),
    ),
)
def test_telemetry_loss_grace_rejects_unsafe_inputs(
    shutdown_minutes: int,
    nut_timeout_s: float,
    polling_interval_s: float,
    error_fragment: str,
) -> None:
    with pytest.raises(ValueError, match=error_fragment):
        telemetry_loss_grace_s(
            shutdown_minutes=shutdown_minutes,
            nut_timeout_s=nut_timeout_s,
            polling_interval_s=polling_interval_s,
        )


def test_steady_state_loss_keeps_last_output_until_derived_deadline(
    tmp_path: Path,
) -> None:
    now = [0.0]
    exporter = VirtualUpsExporter(
        virtual_ups_path=tmp_path / "ups.dev",
        max_publication_age_s=30.0,
        monotonic_clock=lambda: now[0],
    )
    _publish_online(exporter)

    now[0] = 29.999
    temporary = exporter.handle_poll_failure(ConnectionError("NUT restart"))
    assert temporary.state == PublicationFreshnessState.TEMPORARILY_UNAVAILABLE
    assert "ups.status: OL" in (tmp_path / "ups.dev").read_text()

    now[0] = 30.001
    stale = exporter.handle_poll_failure(ConnectionError("NUT remains down"))
    assert stale.state == PublicationFreshnessState.STALE_FAILED
    assert stale.fail_safe_active
    assert "ups.status: OB DISCHRG LB" in (tmp_path / "ups.dev").read_text()


def test_steady_state_recovery_before_derived_deadline_restores_fresh(
    tmp_path: Path,
) -> None:
    now = [0.0]
    exporter = VirtualUpsExporter(
        virtual_ups_path=tmp_path / "ups.dev",
        max_publication_age_s=30.0,
        monotonic_clock=lambda: now[0],
    )
    _publish_online(exporter)

    now[0] = 29.999
    temporary = exporter.handle_poll_failure(ConnectionError("NUT restart"))
    assert temporary.state == PublicationFreshnessState.TEMPORARILY_UNAVAILABLE
    _publish_online(exporter)

    freshness = exporter.freshness(now=now[0])
    assert freshness.state == PublicationFreshnessState.FRESH
    assert not freshness.fail_safe_active


def test_freshness_tracker_cold_start_expires_without_physical_publication() -> None:
    tracker = PublicationFreshnessTracker(
        initial_monotonic=10.0,
        initial_file_age_s=None,
        max_age_s=30.0,
    )

    waiting = tracker.evaluate(39.999, last_error=None, has_current_publication=False)
    assert waiting.state == PublicationFreshnessState.TEMPORARILY_UNAVAILABLE
    assert waiting.reason == "no_physical_publication"

    expired = tracker.evaluate(40.0, last_error=None, has_current_publication=False)
    assert expired.state == PublicationFreshnessState.STALE_FAILED
    assert expired.reason == "no_physical_publication_before_cold_start_deadline"


def test_freshness_tracker_tracks_previous_file_and_current_recovery() -> None:
    tracker = PublicationFreshnessTracker(
        initial_monotonic=10.0,
        initial_file_age_s=2.0,
        max_age_s=30.0,
    )

    current = tracker.evaluate(10.0, last_error="NUT down", has_current_publication=False)
    assert current.state == PublicationFreshnessState.TEMPORARILY_UNAVAILABLE
    assert current.age_s == pytest.approx(2.0)
    assert current.reason == "NUT down"

    tracker.record_success(12.0)
    fresh = tracker.evaluate(12.0, last_error=None, has_current_publication=True)
    assert fresh.state == PublicationFreshnessState.FRESH
    assert fresh.age_s == 0.0

    stale = tracker.evaluate(42.1, last_error=None, has_current_publication=True)
    assert stale.state == PublicationFreshnessState.STALE_FAILED
    assert stale.reason == "publication_age_exceeded"


def test_freshness_tracker_rejects_nonfinite_clock() -> None:
    tracker = PublicationFreshnessTracker(
        initial_monotonic=0.0,
        initial_file_age_s=None,
        max_age_s=30.0,
    )

    with pytest.raises(ValueError, match="clock must be finite"):
        tracker.evaluate(float("nan"), last_error=None, has_current_publication=False)


def test_no_file_cold_start_repeated_loss_stays_unavailable_without_synthetic_lb(
    tmp_path: Path,
) -> None:
    now = [0.0]
    output = tmp_path / "ups.dev"
    exporter = VirtualUpsExporter(
        virtual_ups_path=output,
        max_publication_age_s=2.0,
        monotonic_clock=lambda: now[0],
    )

    temporary = exporter.handle_poll_failure(ConnectionError("NUT unavailable"))
    assert temporary.state == PublicationFreshnessState.TEMPORARILY_UNAVAILABLE
    assert temporary.reason == "ConnectionError: NUT unavailable"
    assert not output.exists()
    assert not exporter.watchdog_healthy

    now[0] = 2.1
    stale = exporter.handle_poll_failure(ConnectionError("NUT still unavailable"))
    assert stale.state == PublicationFreshnessState.STALE_FAILED
    assert not stale.fail_safe_active

    now[0] = 30.0
    repeated = exporter.handle_poll_failure(ConnectionError("NUT remains unavailable"))
    assert repeated.state == PublicationFreshnessState.STALE_FAILED
    assert not repeated.fail_safe_active
    assert not output.exists()
    assert not exporter.watchdog_healthy


def test_stale_previous_output_is_replaced_by_explicit_fail_safe(tmp_path: Path) -> None:
    output = tmp_path / "ups.dev"
    output.write_text("ups.status: OL\n", encoding="utf-8")
    old = time.time() - 30.0
    os.utime(output, (old, old))
    exporter = VirtualUpsExporter(
        virtual_ups_path=output,
        max_publication_age_s=2.0,
        monotonic_clock=lambda: 0.0,
    )

    stale = exporter.handle_poll_failure(ConnectionError("NUT unavailable"))
    assert stale.state == PublicationFreshnessState.STALE_FAILED
    assert stale.fail_safe_active
    contents = output.read_text(encoding="utf-8")
    assert "ups.status: OB DISCHRG LB" in contents
    assert "ups.status: OL" not in contents


def test_fresh_recovery_clears_only_publication_failure(tmp_path: Path) -> None:
    now = [0.0]
    exporter = VirtualUpsExporter(
        virtual_ups_path=tmp_path / "ups.dev",
        max_publication_age_s=1.0,
        monotonic_clock=lambda: now[0],
    )
    _publish_online(exporter)
    now[0] = 1.1
    exporter.handle_poll_failure(ConnectionError("NUT down"))

    _publish_online(exporter)

    freshness = exporter.freshness(now=now[0])
    assert freshness.state == PublicationFreshnessState.FRESH
    assert not freshness.fail_safe_active
    assert exporter.watchdog_healthy
    assert "ups.status: OL" in (tmp_path / "ups.dev").read_text()


def test_failed_fail_safe_invalidates_old_output(tmp_path: Path, monkeypatch) -> None:
    now = [0.0]
    exporter = VirtualUpsExporter(
        virtual_ups_path=tmp_path / "ups.dev",
        max_publication_age_s=1.0,
        monotonic_clock=lambda: now[0],
    )
    _publish_online(exporter)
    now[0] = 1.1

    def fail_write(*_args, **_kwargs):
        raise OSError("EIO")

    monkeypatch.setattr("src.virtual_ups_exporter._atomic_write_text", fail_write)
    with pytest.raises(SafetyPublicationError):
        exporter.handle_poll_failure(OSError("NUT unavailable"))
    assert not (tmp_path / "ups.dev").exists()
    assert not exporter.watchdog_healthy


def test_blocked_publication_is_detected_by_deadline(tmp_path: Path, monkeypatch) -> None:
    exporter = VirtualUpsExporter(
        virtual_ups_path=tmp_path / "ups.dev",
        publication_deadline_s=0.02,
    )

    def blocked_write(*_args, **_kwargs):
        time.sleep(1.0)

    monkeypatch.setattr("src.virtual_ups_exporter._atomic_write_text", blocked_write)
    with pytest.raises(SafetyPublicationError, match="deadline"):
        _publish_online(exporter)
    assert not (tmp_path / "ups.dev").exists()


def test_alarm_during_atomic_cleanup_leaves_no_owned_temp(tmp_path: Path, monkeypatch) -> None:
    temporary = tmp_path / ".ups.dev.injected.tmp"
    temporary.write_bytes(b"payload\n")
    info = temporary.stat()
    identity = (info.st_dev, info.st_ino)
    original_unlink = Path.unlink
    injected = False

    def alarm_on_owned_temp(path: Path, *, missing_ok: bool = False) -> None:
        nonlocal injected
        if path == temporary and not injected:
            injected = True
            os.kill(os.getpid(), signal.SIGALRM)
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", alarm_on_owned_temp)
    with pytest.raises(TimeoutError, match="publication write.*deadline"):
        _run_with_deadline(
            lambda: cleanup_atomic_publication(temporary, None, identity, None),
            deadline_s=0.2,
            operation_name="publication write cleanup",
        )

    assert injected
    assert not temporary.exists()


def test_alarm_during_cleanup_preserves_primary_write_failure(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "ups.dev"
    original_unlink = Path.unlink

    def alarm_on_owned_temp(path: Path, *, missing_ok: bool = False) -> None:
        if path.parent == tmp_path and path.name.startswith(f".{output.name}."):
            os.kill(os.getpid(), signal.SIGALRM)
        original_unlink(path, missing_ok=missing_ok)

    def fail_sync(_descriptor: int) -> None:
        raise OSError("EIO during publication sync")

    monkeypatch.setattr(Path, "unlink", alarm_on_owned_temp)
    monkeypatch.setattr("src.virtual_ups_exporter.os.fdatasync", fail_sync)
    with pytest.raises(OSError, match="EIO during publication sync"):
        _atomic_write_text(output, "payload\n", mode=0o644, byte_limit=1024)

    assert not output.exists()
    assert not tuple(tmp_path.glob(f".{output.name}.*.tmp"))


def test_atomic_publication_keeps_temporary_private_until_replace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "ups.dev"
    create_modes: list[int] = []
    real_open = os.open

    def tracking_open(*args, **kwargs) -> int:
        if len(args) >= 3:
            create_modes.append(args[2])
        return real_open(*args, **kwargs)

    monkeypatch.setattr("src.virtual_ups_exporter.os.open", tracking_open)

    _atomic_write_text(output, "payload\n", mode=0o644, byte_limit=1024)

    assert create_modes == [0o600]
    assert output.stat().st_mode & 0o777 == 0o644


def test_fstat_failure_closes_fd_and_removes_owned_temp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "ups.dev"
    output.write_text("original\n", encoding="utf-8")
    unrelated = tmp_path / ".unrelated.tmp"
    unrelated.write_bytes(b"keep\n")
    opened: list[tuple[Path, int]] = []
    closed: list[int] = []
    fstat_calls: list[int] = []
    lstat_paths: list[Path] = []
    real_open = os.open
    real_close = os.close
    real_lstat = os.lstat

    def tracking_open(*args, **kwargs) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.append((Path(args[0]), descriptor))
        return descriptor

    def tracking_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    def failing_fstat(descriptor: int):
        fstat_calls.append(descriptor)
        raise OSError("injected temporary fstat failure")

    def tracking_lstat(path: str | Path, *args, **kwargs):
        lstat_paths.append(Path(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr("src.virtual_ups_exporter.os.open", tracking_open)
    monkeypatch.setattr("src.virtual_ups_exporter.os.close", tracking_close)
    monkeypatch.setattr("src.virtual_ups_exporter.os.lstat", tracking_lstat)
    monkeypatch.setattr("src.virtual_ups_exporter.os.fstat", failing_fstat)

    with pytest.raises(OSError, match="injected temporary fstat failure"):
        _atomic_write_text(output, "replacement\n", mode=0o644, byte_limit=1024)

    assert len(opened) == 1
    temporary, descriptor = opened[0]
    assert fstat_calls == [descriptor]
    assert temporary in lstat_paths
    assert closed == [descriptor]
    assert not temporary.exists()
    assert output.read_text(encoding="utf-8") == "original\n"
    assert unrelated.read_bytes() == b"keep\n"


def test_first_lstat_failure_closes_fd_and_removes_owned_temp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "ups.dev"
    output.write_text("original\n", encoding="utf-8")
    opened: list[tuple[Path, int]] = []
    closed: list[int] = []
    lstat_calls: list[Path] = []
    failed = False
    primary_error = OSError("injected temporary lstat failure")
    real_open = os.open
    real_close = os.close
    real_lstat = os.lstat

    def tracking_open(*args, **kwargs) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.append((Path(args[0]), descriptor))
        return descriptor

    def tracking_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    def failing_first_lstat(path: str | Path, *args, **kwargs):
        nonlocal failed
        path_obj = Path(path)
        lstat_calls.append(path_obj)
        if (
            not args
            and not kwargs
            and path_obj.parent == tmp_path
            and path_obj.name.startswith(f".{output.name}.")
            and not failed
        ):
            failed = True
            raise primary_error
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr("src.virtual_ups_exporter.os.open", tracking_open)
    monkeypatch.setattr("src.virtual_ups_exporter.os.close", tracking_close)
    monkeypatch.setattr("src.virtual_ups_exporter.os.lstat", failing_first_lstat)

    with pytest.raises(OSError) as raised:
        _atomic_write_text(output, "replacement\n", mode=0o644, byte_limit=1024)

    assert raised.value is primary_error
    assert len(opened) == 1
    temporary, descriptor = opened[0]
    assert temporary in lstat_calls
    assert closed == [descriptor]
    assert output.read_text(encoding="utf-8") == "original\n"
    assert not temporary.exists()
    assert not tuple(tmp_path.glob(f".{output.name}.*.tmp"))


def test_both_identity_inspections_fail_and_leave_temp_unlinked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "ups.dev"
    output.write_text("original\n", encoding="utf-8")
    unrelated = tmp_path / ".unrelated.tmp"
    unrelated.write_bytes(b"keep\n")
    opened: list[tuple[Path, int]] = []
    closed: list[int] = []
    real_open = os.open
    real_close = os.close
    real_lstat = os.lstat

    def tracking_open(*args, **kwargs) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.append((Path(args[0]), descriptor))
        return descriptor

    def tracking_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    def failing_fstat(_descriptor: int) -> None:
        raise OSError("injected temporary fstat failure")

    def failing_lstat(path: str | Path, *args, **kwargs):
        path_obj = Path(path)
        if (
            not args
            and not kwargs
            and path_obj.parent == tmp_path
            and path_obj.name.startswith(f".{output.name}.")
        ):
            raise OSError("injected temporary lstat failure")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr("src.virtual_ups_exporter.os.open", tracking_open)
    monkeypatch.setattr("src.virtual_ups_exporter.os.close", tracking_close)
    monkeypatch.setattr("src.virtual_ups_exporter.os.fstat", failing_fstat)
    monkeypatch.setattr("src.virtual_ups_exporter.os.lstat", failing_lstat)

    with pytest.raises(OSError, match="injected temporary fstat failure"):
        _atomic_write_text(output, "replacement\n", mode=0o644, byte_limit=1024)

    assert len(opened) == 1
    temporary, descriptor = opened[0]
    assert closed == [descriptor]
    assert temporary.exists()
    assert output.read_text(encoding="utf-8") == "original\n"
    assert unrelated.read_bytes() == b"keep\n"


def test_mismatched_replaced_temp_is_not_unlinked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "ups.dev"
    output.write_text("original\n", encoding="utf-8")
    replacement = tmp_path / ".replacement.tmp"
    replacement.write_bytes(b"replacement must remain\n")
    opened: list[tuple[Path, int]] = []
    closed: list[int] = []
    real_open = os.open
    real_close = os.close
    real_lstat = os.lstat
    swapped = False

    def tracking_open(*args, **kwargs) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.append((Path(args[0]), descriptor))
        return descriptor

    def tracking_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    def replace_before_lstat(path: str | Path, *args, **kwargs):
        nonlocal swapped
        path_obj = Path(path)
        if not args and not kwargs and opened and path_obj == opened[0][0] and not swapped:
            swapped = True
            os.replace(replacement, path)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr("src.virtual_ups_exporter.os.open", tracking_open)
    monkeypatch.setattr("src.virtual_ups_exporter.os.close", tracking_close)
    monkeypatch.setattr("src.virtual_ups_exporter.os.lstat", replace_before_lstat)

    with pytest.raises(OSError, match="temporary descriptor changed"):
        _atomic_write_text(output, "replacement\n", mode=0o644, byte_limit=1024)

    temporary, descriptor = opened[0]
    assert swapped
    assert closed == [descriptor]
    assert temporary.read_bytes() == b"replacement must remain\n"
    assert output.read_text(encoding="utf-8") == "original\n"


def test_nonregular_replaced_temp_is_not_unlinked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "ups.dev"
    output.write_text("original\n", encoding="utf-8")
    unrelated = tmp_path / ".unrelated.tmp"
    unrelated.write_bytes(b"keep\n")
    opened: list[tuple[Path, int]] = []
    closed: list[int] = []
    real_open = os.open
    real_close = os.close
    real_lstat = os.lstat
    swapped = False

    def tracking_open(*args, **kwargs) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.append((Path(args[0]), descriptor))
        return descriptor

    def tracking_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    def replace_with_symlink(path: str | Path, *args, **kwargs):
        nonlocal swapped
        path_obj = Path(path)
        if not args and not kwargs and opened and path_obj == opened[0][0] and not swapped:
            swapped = True
            path_obj.unlink()
            path_obj.symlink_to(unrelated)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr("src.virtual_ups_exporter.os.open", tracking_open)
    monkeypatch.setattr("src.virtual_ups_exporter.os.close", tracking_close)
    monkeypatch.setattr("src.virtual_ups_exporter.os.lstat", replace_with_symlink)

    with pytest.raises(OSError, match="temporary target is not a regular file"):
        _atomic_write_text(output, "replacement\n", mode=0o644, byte_limit=1024)

    temporary, descriptor = opened[0]
    assert swapped
    assert closed == [descriptor]
    assert temporary.is_symlink()
    assert temporary.read_bytes() == b"keep\n"
    assert unrelated.read_bytes() == b"keep\n"
    assert output.read_text(encoding="utf-8") == "original\n"


def test_worker_cleanup_restores_signal_mask_without_installing_handlers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    temporary = tmp_path / ".ups.dev.worker.tmp"
    temporary.write_bytes(b"payload\n")
    info = temporary.lstat()
    identity = (info.st_dev, info.st_ino)
    forbidden_signal_calls: list[tuple[object, ...]] = []

    def forbidden_signal(*args) -> None:
        forbidden_signal_calls.append(args)
        raise AssertionError("worker cleanup must not call signal.signal")

    monkeypatch.setattr(
        "src.virtual_ups_exporter.atomic_publication_cleanup.signal.signal",
        forbidden_signal,
    )
    results: list[tuple[object, object]] = []

    def worker() -> None:
        desired = {signal.SIGUSR1}
        signal.pthread_sigmask(signal.SIG_SETMASK, desired)
        before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        signal.pthread_sigmask(signal.SIG_SETMASK, before)
        cleanup_atomic_publication(temporary, None, identity, None)
        after = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        signal.pthread_sigmask(signal.SIG_SETMASK, after)
        results.append((before, after))

    thread = Thread(target=worker)
    thread.start()
    thread.join()

    assert results == [({signal.SIGUSR1}, {signal.SIGUSR1})]
    assert forbidden_signal_calls == []
    assert not temporary.exists()


@pytest.mark.parametrize("blocked_operation", ("unlink", "fsync"))
def test_blocked_output_invalidation_is_detected_by_deadline(
    tmp_path: Path,
    monkeypatch,
    blocked_operation: str,
) -> None:
    output = tmp_path / "ups.dev"
    output.write_text("ups.status: OL\n", encoding="utf-8")
    exporter = VirtualUpsExporter(
        virtual_ups_path=output,
        publication_deadline_s=0.02,
    )

    if blocked_operation == "unlink":
        original_unlink = Path.unlink

        def blocked_unlink(path: Path, *, missing_ok: bool = False) -> None:
            if path == output:
                time.sleep(1.0)
            original_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", blocked_unlink)
    else:
        original_fsync = os.fsync

        def blocked_fsync(file_descriptor: int) -> None:
            time.sleep(1.0)
            original_fsync(file_descriptor)

        monkeypatch.setattr("src.virtual_ups_exporter.os.fsync", blocked_fsync)

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="publication invalidation.*deadline"):
        exporter.invalidate_output()

    assert time.monotonic() - started < 0.5
    # A timeout before unlink leaves the old file in place; it is still
    # untrusted because the caller's stop intent is independent of cleanup.
    assert output.exists() is (blocked_operation == "unlink")


def test_signal_after_operation_completion_does_not_fail_before_cancellation(
    monkeypatch,
) -> None:
    original_setitimer = signal.setitimer
    injected = False

    def setitimer(which: int, first: float, interval: float = 0.0) -> tuple[float, float]:
        nonlocal injected
        if first == 0.0 and not injected:
            injected = True
            os.kill(os.getpid(), signal.SIGALRM)
        return original_setitimer(which, first, interval)

    monkeypatch.setattr(signal, "setitimer", setitimer)
    _run_with_deadline(lambda: None, deadline_s=0.2, operation_name="completion-window")

    assert injected


def test_nested_deadline_does_not_extend_outer_alarm() -> None:
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def nested_operation() -> None:
        _run_with_deadline(
            lambda: time.sleep(0.03),
            deadline_s=0.2,
            operation_name="inner",
        )
        time.sleep(0.1)

    with pytest.raises(TimeoutError, match="outer"):
        _run_with_deadline(
            nested_operation,
            deadline_s=0.1,
            operation_name="outer",
        )

    assert time.monotonic() - started < 0.12
    restored_timer = signal.getitimer(signal.ITIMER_REAL)
    assert signal.getsignal(signal.SIGALRM) is previous_handler
    if previous_timer[0] > 0.0:
        assert restored_timer[0] < previous_timer[0]
        assert restored_timer[1] == pytest.approx(previous_timer[1])
    else:
        assert restored_timer == (0.0, 0.0)


def test_periodic_timer_adjustment_advances_to_next_tick() -> None:
    remaining, interval, expired = _adjust_previous_timer((0.05, 0.04), 0.11)

    assert remaining == pytest.approx(0.02)
    assert interval == pytest.approx(0.04)
    assert not expired


def test_nested_deadline_restores_handler_after_inner_exception() -> None:
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def prior_handler(_signum: int, _frame: object) -> None:
        return None

    signal.signal(signal.SIGALRM, prior_handler)
    try:
        with pytest.raises(ValueError, match="inner failure"):
            _run_with_deadline(
                lambda: _run_with_deadline(
                    lambda: (_ for _ in ()).throw(ValueError("inner failure")),
                    deadline_s=0.1,
                    operation_name="inner",
                ),
                deadline_s=0.2,
                operation_name="outer",
            )
        assert signal.getsignal(signal.SIGALRM) is prior_handler
        restored_timer = signal.getitimer(signal.ITIMER_REAL)
        if previous_timer[0] > 0.0:
            assert restored_timer[0] < previous_timer[0]
            assert restored_timer[1] == pytest.approx(previous_timer[1])
        else:
            assert restored_timer == (0.0, 0.0)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)


def test_non_main_safety_publication_fails_closed_without_unbounded_write(tmp_path: Path) -> None:
    exporter = VirtualUpsExporter(
        virtual_ups_path=tmp_path / "ups.dev",
    )
    observation = _observation()
    snapshot = _snapshot()
    calculation = calculate_safety(
        inputs=SafetyInputs(13.3, 20.0, BlackoutKind.ONLINE, 5),
        snapshot=snapshot,
    )
    exporter.stage(PollPublicationContext(observation, snapshot, calculation, 1.0))
    failures: list[BaseException] = []

    def publish_from_worker() -> None:
        try:
            exporter.publish(make_safety_publication(observation, calculation))
        except BaseException as error:
            failures.append(error)

    worker = Thread(target=publish_from_worker)
    worker.start()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], SafetyPublicationError)
    assert not (tmp_path / "ups.dev").exists()
