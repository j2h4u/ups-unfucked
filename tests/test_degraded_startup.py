"""Safety-first startup fixtures for unavailable scientific storage."""

from __future__ import annotations

from pathlib import Path
from threading import Event

from src.adapters import model_state_persistence as model_files
from src.adapters.jsonl_errors import EventConflictError
from src.application.assessment_worker import AssessmentWorker
from src.application.background_coordinator import (
    BackgroundCoordinator,
    BackgroundDependencies,
    BackgroundSettings,
)
from src.application.capture_writer import CaptureWriter
from src.application.degraded_startup import DegradedEventStore, EventStorageUnavailable
from src.application.startup_recovery import StartupRecovery
from src.monitor import build_daemon
from src.monitor_config import Config


class _Model:
    def current_snapshot(self):
        raise AssertionError("degraded startup must not assess or read model policy")


class _Reporter:
    def __init__(self) -> None:
        self.calls = 0

    def tick(self, *, consecutive_errors: int):
        del consecutive_errors
        self.calls += 1
        return None


class _Sink:
    def publish(self, _report) -> None:
        raise AssertionError("degraded startup must not publish a scientific report")


def test_corrupt_storage_is_explicit_no_capture_health() -> None:
    store = DegradedEventStore("registry is corrupt")
    health = store.storage_health()

    assert not health.capture_available
    assert health.alarm == "startup_degraded"
    assert health.bounded_error == "registry is corrupt"
    try:
        store.open(None)
    except EventStorageUnavailable as exc:
        assert "startup_degraded" in str(exc)
    else:
        raise AssertionError("degraded store accepted an event mutation")


def test_startup_recovery_failure_latches_and_retries_off_poll_thread() -> None:
    store = DegradedEventStore("temporary recovery failure")
    writer = CaptureWriter()
    reporter = _Reporter()
    attempts = []
    now = [0.0]
    degraded = []
    recovered = []

    def load():
        attempts.append(now[0])
        if len(attempts) == 1:
            raise OSError("corrupt registry")
        return StartupRecovery(None, ())

    coordinator = BackgroundCoordinator(
        BackgroundDependencies(
            assessment_store=store,  # type: ignore[arg-type]
            startup_store=store,  # type: ignore[arg-type]
            reporting_store=store,  # type: ignore[arg-type]
            commit_model=_Model(),  # type: ignore[arg-type]
            policy_model=_Model(),  # type: ignore[arg-type]
            worker=AssessmentWorker(store, _Model()),  # type: ignore[arg-type]
            writer=writer,
            reporter=reporter,  # type: ignore[arg-type]
            report_sink=_Sink(),  # type: ignore[arg-type]
        ),
        StartupRecovery(None, ()),
        BackgroundSettings(
            reporting_interval_s=60.0,
            monotonic_clock=lambda: now[0],
            startup_loader=load,
            startup_retry_interval_s=10.0,
            on_startup_degraded=degraded.append,
            on_startup_recovered=lambda: recovered.append(True),
        ),
    )

    publication_seen = True
    coordinator.after_first_safety_publication()
    coordinator.run_one()
    assert publication_seen
    assert not coordinator.capture_enabled
    assert len(degraded) == 1
    assert reporter.calls == 1

    now[0] = 9.0
    coordinator.run_one()
    assert len(attempts) == 1
    now[0] = 20.0
    coordinator.run_one()
    assert len(attempts) == 2
    assert coordinator.capture_enabled
    assert recovered == [True]

    writer.stop(drain=True)
    store.close()


def test_store_lock_conflict_is_fatal_after_first_safety_publication() -> None:
    store = DegradedEventStore("storage deferred")
    writer = CaptureWriter()
    errors: list[BaseException] = []
    seen = Event()

    def load():
        raise EventConflictError("another event-store writer owns monitor.lock")

    coordinator = BackgroundCoordinator(
        BackgroundDependencies(
            assessment_store=store,  # type: ignore[arg-type]
            startup_store=store,  # type: ignore[arg-type]
            reporting_store=store,  # type: ignore[arg-type]
            commit_model=_Model(),  # type: ignore[arg-type]
            policy_model=_Model(),  # type: ignore[arg-type]
            worker=AssessmentWorker(store, _Model()),  # type: ignore[arg-type]
            writer=writer,
            reporter=_Reporter(),  # type: ignore[arg-type]
            report_sink=_Sink(),  # type: ignore[arg-type]
        ),
        StartupRecovery(None, ()),
        BackgroundSettings(
            reporting_interval_s=60.0,
            startup_loader=load,
            on_error=lambda error: (errors.append(error), seen.set()),
        ),
    )

    coordinator.start()
    coordinator.after_first_safety_publication()
    try:
        assert seen.wait(timeout=1.0)
        assert isinstance(coordinator.fatal_startup_error, EventConflictError)
        assert isinstance(errors[0], EventConflictError)
    finally:
        coordinator.stop()
        writer.stop(drain=True)
        store.close()


class _ModelOwner:
    @classmethod
    def open_runtime(cls, model_path, **_kwargs):
        owner = cls()
        owner.adopt_writer_lock(
            model_files.acquire_writer_lock(Path(model_path).parent / "monitor.lock")
        )
        return owner

    def __init__(self, *_args, **_kwargs) -> None:
        self.writer_lock_fd = None

    def adopt_writer_lock(self, writer_lock_fd: int) -> None:
        self.writer_lock_fd = writer_lock_fd

    def close(self) -> None:
        if self.writer_lock_fd is not None:
            model_files.release_writer_lock(self.writer_lock_fd)
            self.writer_lock_fd = None


def test_store_constructor_failure_builds_safety_only_daemon(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("src.monitor.ModelOwner", _ModelOwner)
    constructor_calls: list[Path] = []

    def fail_store(_path):
        constructor_calls.append(_path)
        raise OSError("event directory permission denied")

    monkeypatch.setattr("src.monitor.JsonlEventStore", fail_store)
    daemon = build_daemon(
        Config(model_dir=tmp_path),
        virtual_ups_path=tmp_path / "ups.dev",
        health_path=tmp_path / "health.json",
    )
    try:
        assert constructor_calls == []
        assert not daemon._coordinator.capture_enabled
        assert daemon._coordinator._startup_degraded
    finally:
        daemon.shutdown()
