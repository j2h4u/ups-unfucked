"""Focused integration contracts for the single post-cutover runtime path."""

from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, current_thread
from time import monotonic, sleep
from types import SimpleNamespace
from typing import Any, cast

import pytest

from src.adapters.jsonl_event_store import JsonlEventStore
from src.adapters.model_state_schema import TargetModelStateError
from src.application.assessment_worker import AssessmentWorker
from src.application.background_coordinator import (
    BackgroundCoordinator,
    BackgroundDependencies,
    BackgroundSettings,
)
from src.application.capture_blackout import BlackoutCapture
from src.application.capture_writer import CaptureCommand, CaptureCommandKind, CaptureWriter
from src.application.model_port import ModelPolicyProjection
from src.application.prestart_loss import PRESTART_LOSS_REASON
from src.application.startup_recovery import StartupRecovery, recover_startup_metadata
from src.application.storage_values import (
    CaptureCloseReconciliation,
    CaptureCloseState,
    EventHandle,
    EventRef,
    RecoveredCapture,
)
from src.battery_math.lut import LutPoint
from src.domain.lifecycle import UNKNOWN_PRELUDE_GAP_REASON
from src.domain.readiness import ReadinessState
from src.domain.reasons import order_reasons
from src.domain.values import (
    BlackoutKind,
    ChargeReadiness,
    FrozenModelSnapshot,
    PhysicalObservation,
)
from src.monitor import (
    MonitorDaemon,
    RuntimeClocks,
    RuntimeDependencies,
    RuntimeErrorBoundary,
    _validate_observation,
    build_daemon,
)
from src.monitor_config import Config
from src.virtual_ups_exporter import SafetyPublicationError


class _Telemetry:
    def __init__(self, observations: Iterable[PhysicalObservation]) -> None:
        self._observations = iter(observations)
        self.read_count = 0

    def read(self) -> PhysicalObservation:
        self.read_count += 1
        return next(self._observations)


class _Model:
    def __init__(self, snapshot: FrozenModelSnapshot) -> None:
        self.snapshot = snapshot
        self.snapshot_count = 0

    def current_snapshot(self) -> FrozenModelSnapshot:
        self.snapshot_count += 1
        return self.snapshot

    def close(self) -> None:
        return None


class _AssessmentModel(_Model):
    def __init__(self, snapshot: FrozenModelSnapshot) -> None:
        super().__init__(snapshot)
        self.prepare_calls = 0
        self.commit_calls = 0

    def policy_projection(self) -> ModelPolicyProjection:
        return ModelPolicyProjection(
            self.snapshot,
            self.snapshot.scientific_fingerprint,
            self.snapshot.ir_k_v_per_pp,
            None,
            frozenset(),
        )

    def prepare_commit(self, *_args, **_kwargs):
        self.prepare_calls += 1
        raise AssertionError("censored invalid telemetry must not prepare a model commit")

    def commit_prepared(self, *_args, **_kwargs):
        self.commit_calls += 1
        raise AssertionError("censored invalid telemetry must not commit the model")


class _Publisher:
    def __init__(self) -> None:
        self.staged = []
        self.publications = []
        self.errors = []
        self.poll_failures = []

    def stage(self, context) -> None:
        self.staged.append(context)

    def publish(self, publication) -> None:
        self.publications.append(publication)

    def record_error(self, error: BaseException) -> None:
        self.errors.append(error)

    def record_channel_error(self, channel: str, error: BaseException | str) -> None:
        if channel == "poll":
            self.errors.append(error)

    def clear_channel_error(self, channel: str) -> None:
        del channel

    def invalidate_output(self) -> None:
        return None

    def handle_poll_failure(self, error: BaseException, *, now: float | None = None) -> None:
        del now
        self.poll_failures.append(error)

    @property
    def watchdog_healthy(self) -> bool:
        return True


class _ColdStartPublisher(_Publisher):
    """Model a real exporter with no output and no healthy watchdog."""

    def handle_poll_failure(self, error: BaseException, *, now: float | None = None) -> None:
        del error, now

    @property
    def watchdog_healthy(self) -> bool:
        return False


class _AlwaysFailingTelemetry:
    def __init__(self) -> None:
        self.read_count = 0

    def read(self) -> PhysicalObservation:
        self.read_count += 1
        raise ConnectionError("NUT remains unavailable")


class _CorrelatedFailureStore:
    def __init__(self) -> None:
        self.append_attempts: list[str] = []
        self.closed = False

    def open(self, start) -> EventHandle:
        return EventHandle(start.blackout_id, start.segment_id, "event.jsonl", 1, "a" * 64)

    def append(self, _handle, record):
        self.append_attempts.append(record.record_type)
        raise OSError(f"{record.record_type} failed")

    def checkpoint_processing(self, _handle, _frozen_stage) -> None:
        raise AssertionError("checkpoint requires a durable recovery end")

    def reconcile_damaged_close(self, _blackout_id, current_handle):
        return CaptureCloseReconciliation(CaptureCloseState.UNKNOWN, current_handle)

    def acknowledge_capture_recovery(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _RecoveredAppendFailureStore:
    def __init__(self, delegate: JsonlEventStore) -> None:
        self._delegate = delegate
        self.append_attempts: list[str] = []

    def append(self, _handle, record):
        self.append_attempts.append(record.record_type)
        raise OSError(f"{record.record_type} failed")

    def checkpoint_processing(self, _handle, _frozen_stage) -> None:
        raise AssertionError("checkpoint requires a durable recovery end")

    def reconcile_damaged_close(self, blackout_id, current_handle):
        return self._delegate.reconcile_damaged_close(blackout_id, current_handle)

    def acknowledge_capture_recovery(self) -> None:
        self._delegate.acknowledge_capture_recovery()

    def close(self) -> None:
        self._delegate.close()


class _RecoveredTerminalFailureStore:
    """Allow recovered OB continuation, then fail the END and its recovery GAP."""

    def __init__(self, delegate: JsonlEventStore) -> None:
        self._delegate = delegate
        self.append_attempts: list[str] = []
        self._end_failed = False

    def append(self, handle, record):
        self.append_attempts.append(record.record_type)
        if record.record_type == "end":
            self._end_failed = True
            raise OSError("end failed")
        if self._end_failed and record.record_type == "gap":
            raise OSError("gap failed")
        return self._delegate.append(handle, record)

    def checkpoint_processing(self, _handle, _frozen_stage) -> None:
        raise AssertionError("checkpoint requires a durable recovery end")

    def reconcile_damaged_close(self, blackout_id, current_handle):
        return self._delegate.reconcile_damaged_close(blackout_id, current_handle)

    def acknowledge_capture_recovery(self) -> None:
        self._delegate.acknowledge_capture_recovery()

    def close(self) -> None:
        self._delegate.close()


class _Coordinator:
    def __init__(self) -> None:
        self.publication_count = 0
        self.error_counts = []

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def after_first_safety_publication(self) -> None:
        self.publication_count += 1

    def record_poll_error_count(self, count: int) -> None:
        self.error_counts.append(count)

    @property
    def capture_enabled(self) -> bool:
        return True

    @property
    def fatal_startup_error(self):
        return None

    def take_recovered_capture(self):
        return None


def _snapshot() -> FrozenModelSnapshot:
    return FrozenModelSnapshot(
        schema_revision="2",
        evaluation_revision="1",
        battery_epoch_id="a" * 32,
        scientific_fingerprint="b" * 64,
        rated_capacity_ah=7.2,
        nominal_voltage_v=12.0,
        nominal_power_watts=510.0,
        soh=1.0,
        peukert_exponent=1.2,
        ir_k_v_per_pp=0.015,
        ir_reference_load_percent=0.0,
        lut=(LutPoint(13.7, 1.0, "standard"), LutPoint(10.8, 0.0, "anchor")),
    )


def _observation(
    status: str,
    second: int,
    *,
    boot_id: str = "boot-a",
    input_voltage_v: float | None = 0.0,
    voltage_v: float = 13.3,
) -> PhysicalObservation:
    return PhysicalObservation(
        boot_id=boot_id,
        monotonic_ns=second * 1_000_000_000,
        wall_time_utc=datetime(2026, 8, 16, tzinfo=timezone.utc) + timedelta(seconds=second),
        raw_status=status,
        battery_voltage_raw=f"{voltage_v:.2f}",
        battery_voltage_v=voltage_v,
        voltage_token_quantum_v=0.01,
        load_percent=20.0,
        input_voltage_v=input_voltage_v,
    )


def _daemon(
    tmp_path: Path,
    observations: Iterable[PhysicalObservation],
    *,
    recovered=None,
    clocks: RuntimeClocks | None = None,
) -> tuple[
    MonitorDaemon,
    JsonlEventStore,
    CaptureWriter,
    _Telemetry,
    _Model,
    _Publisher,
]:
    store = JsonlEventStore(tmp_path)
    writer = CaptureWriter()
    telemetry = _Telemetry(observations)
    model = _Model(_snapshot())
    publisher = _Publisher()
    capture = BlackoutCapture(store, writer)
    coordinator = _Coordinator()
    daemon = MonitorDaemon(
        Config(model_dir=tmp_path),
        RuntimeDependencies(
            telemetry=telemetry,
            model=cast(Any, model),
            publisher=publisher,
            capture=capture,
            writer=writer,
            coordinator=cast(Any, coordinator),
            store=store,
            recovered_capture=recovered,
        ),
        clocks=clocks or RuntimeClocks(),
    )
    return daemon, store, writer, telemetry, model, publisher


def _drain(writer: CaptureWriter) -> None:
    while writer.drain_one():
        pass


def _durable_daemon(
    tmp_path: Path,
    observations: Iterable[PhysicalObservation],
) -> tuple[
    MonitorDaemon,
    JsonlEventStore,
    CaptureWriter,
    BackgroundCoordinator,
    _AssessmentModel,
    _Publisher,
]:
    store = JsonlEventStore(tmp_path)
    writer = CaptureWriter()
    model = _AssessmentModel(_snapshot())
    worker = AssessmentWorker(store, model)
    coordinator = BackgroundCoordinator(
        BackgroundDependencies(
            assessment_store=store,
            startup_store=store,
            reporting_store=store,
            commit_model=cast(Any, model),
            policy_model=cast(Any, model),
            worker=worker,
            writer=writer,
            reporter=cast(Any, _NoopReporter()),
            report_sink=cast(Any, _NoopReportSink()),
            report_outbox_store=store.report_outbox,
        ),
        StartupRecovery(None, ()),
        BackgroundSettings(reporting_interval_s=60.0),
    )
    publisher = _Publisher()
    daemon = MonitorDaemon(
        Config(model_dir=tmp_path),
        RuntimeDependencies(
            telemetry=_Telemetry(observations),
            model=cast(Any, model),
            publisher=publisher,
            capture=BlackoutCapture(store, writer),
            writer=writer,
            coordinator=coordinator,
            store=store,
        ),
    )
    return daemon, store, writer, coordinator, model, publisher


def _finish_durable_outcome(
    coordinator: BackgroundCoordinator,
    writer: CaptureWriter,
    store: JsonlEventStore,
):
    coordinator.run_one()
    _drain(writer)
    coordinator.run_one()
    summary = store.history_tail(1)[0]
    return store.project(EventRef(summary.blackout_id, summary.segment_filename))


def test_shutdown_retries_pending_boundary_after_lifecycle_capacity_returns(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class RetryCapture:
        attempts = 0

        def service_stop(self, _observation) -> bool:
            calls.append("capture_stop")
            self.attempts += 1
            return self.attempts == 2

    class RetryWriter:
        def wait_for_lifecycle_capacity(self, timeout_s: float) -> bool:
            calls.append(f"wait:{timeout_s}")
            return True

        def stop(self, *, drain: bool) -> None:
            calls.append(f"writer_stop:{drain}")

    coordinator = SimpleNamespace(stop=lambda: calls.append("coordinator_stop"))
    store = SimpleNamespace(close=lambda: calls.append("store_close"))
    model = SimpleNamespace(close=lambda: calls.append("model_close"))
    daemon = MonitorDaemon(
        Config(model_dir=tmp_path),
        RuntimeDependencies(
            telemetry=cast(Any, object()),
            model=cast(Any, model),
            publisher=cast(Any, object()),
            capture=cast(Any, RetryCapture()),
            writer=cast(Any, RetryWriter()),
            coordinator=cast(Any, coordinator),
            store=cast(Any, store),
        ),
    )
    daemon._last_observation = _observation("OL", 1)  # pyright: ignore[reportPrivateUsage]

    daemon.shutdown()

    assert calls == [
        "coordinator_stop",
        "capture_stop",
        "wait:5.0",
        "capture_stop",
        "writer_stop:True",
        "store_close",
        "model_close",
    ]


def test_shutdown_preserves_fatal_error_and_drains_after_boundary_failure(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class FailedCapture:
        def service_stop(self, _observation) -> bool:
            calls.append("capture_stop")
            return False

    class ShutdownWriter:
        def wait_for_lifecycle_capacity(self, timeout_s: float) -> bool:
            calls.append(f"wait:{timeout_s}")
            return False

        def stop(self, *, drain: bool) -> None:
            calls.append(f"writer_stop:{drain}")

    daemon = MonitorDaemon(
        Config(model_dir=tmp_path),
        RuntimeDependencies(
            telemetry=cast(Any, object()),
            model=cast(Any, object()),
            publisher=cast(Any, object()),
            capture=cast(Any, FailedCapture()),
            writer=cast(Any, ShutdownWriter()),
            coordinator=cast(Any, SimpleNamespace(stop=lambda: calls.append("coordinator_stop"))),
            store=cast(Any, SimpleNamespace(close=lambda: calls.append("store_close"))),
        ),
        clocks=RuntimeClocks(notify=lambda status: calls.append(status)),
    )
    daemon._last_observation = _observation("OL", 1)  # pyright: ignore[reportPrivateUsage]
    fatal = SafetyPublicationError("original publication failure")
    daemon._fatal_error = fatal  # pyright: ignore[reportPrivateUsage]

    daemon.shutdown()

    assert calls == [
        "coordinator_stop",
        "capture_stop",
        "wait:5.0",
        "writer_stop:True",
        "store_close",
        "STOPPING=1",
    ]
    assert daemon._fatal_error is fatal  # pyright: ignore[reportPrivateUsage]


def test_first_ob_freezes_prior_online_readiness_in_event_start(tmp_path: Path) -> None:
    daemon, store, writer, *_ = _daemon(
        tmp_path,
        (_observation("OB DISCHRG", 43_201, voltage_v=13.45),),
    )
    daemon._readiness = ReadinessState(  # pyright: ignore[reportPrivateUsage]
        "boot-a",
        0,
        43_200_000_000_000,
        ((41_400_000_000_000, 13.4), (43_200_000_000_000, 13.5)),
        None,
    )
    try:
        daemon.poll_once()
        _drain(writer)
        active = store.work_registry().capture
        assert active is not None
        projection = store.project(EventRef(active.blackout_id, active.path_token))
        assert projection.start is not None
        readiness = projection.start.payload["charge_readiness"]
        assert isinstance(readiness, dict)
        assert readiness["ready"] is True
        assert readiness["continuous_online_s"] == 43_200.0
    finally:
        store.close()


def _open_recovered_state(
    state_path: Path,
) -> tuple[JsonlEventStore, RecoveredCapture, str]:
    first_store = JsonlEventStore(state_path)
    first_writer = CaptureWriter()
    first_capture = BlackoutCapture(first_store, first_writer)
    assert first_capture.accept_after_safety_publish(
        _observation("OB DISCHRG LB", 5, boot_id="boot-before", voltage_v=10.9),
        safety_snapshot=_snapshot(),
        charge_readiness=ChargeReadiness(False, 0.0, None, order_reasons(())),
    )
    _drain(first_writer)
    original = first_store.work_registry().capture
    assert original is not None
    first_store.close()

    recovered_store = JsonlEventStore(state_path)
    recovery = recover_startup_metadata(recovered_store)
    assert recovery.recovered_capture is not None
    return recovered_store, recovery.recovered_capture, original.blackout_id


def test_run_recovers_next_tick_and_sends_ready_only_after_safety_publication(
    tmp_path: Path,
) -> None:
    ticks = iter((0.0, 0.0, 0.0, 0.0, 1.0, 1.01, 1.1))
    notifications: list[str] = []
    sleep_calls: list[float] = []
    daemon_holder: list[MonitorDaemon] = []

    def sleep_until_next_tick(delay: float) -> None:
        sleep_calls.append(delay)
        if len(sleep_calls) == 2:
            daemon_holder[0].request_stop()

    invalid = replace(_observation("OB DISCHRG", 0), battery_voltage_v=None)
    daemon, _store, _writer, telemetry, model, publisher = _daemon(
        tmp_path,
        (invalid, _observation("OL", 1, input_voltage_v=230.0)),
        clocks=RuntimeClocks(
            monotonic=lambda: next(ticks),
            sleep=sleep_until_next_tick,
            notify=lambda status: notifications.append(status),
        ),
    )
    daemon_holder.append(daemon)

    daemon.run()

    assert telemetry.read_count == 2
    assert model.snapshot_count == 2
    assert len(publisher.errors) == 1
    assert isinstance(publisher.errors[0], RuntimeErrorBoundary)
    assert len(publisher.staged) == len(publisher.publications) == 1
    assert notifications == ["WATCHDOG=1", "READY=1", "WATCHDOG=1", "STOPPING=1"]
    assert sleep_calls == pytest.approx([1.0, 0.9])


def test_capture_boundary_after_publication_keeps_poll_loop_alive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon, store, _writer, _telemetry, _model, publisher = _daemon(
        tmp_path,
        (_observation("OB DISCHRG", 0),),
    )

    def fail_capture(*_args, **_kwargs) -> bool:
        raise RuntimeErrorBoundary("capture bookkeeping invariant failed")

    monkeypatch.setattr(daemon._capture, "accept_after_safety_publish", fail_capture)  # pyright: ignore[reportPrivateUsage]

    try:
        assert daemon._run_poll_iteration() == (True, False)  # pyright: ignore[reportPrivateUsage]
        assert len(publisher.publications) == 1
        assert len(publisher.poll_failures) == 1
        assert isinstance(publisher.poll_failures[0], RuntimeErrorBoundary)
    finally:
        daemon.shutdown()


@pytest.mark.parametrize("missing_field", ("battery_voltage_v", "load_percent"))
def test_invalid_ob_safety_input_is_durably_rejected_after_ol(
    tmp_path: Path,
    missing_field: str,
) -> None:
    invalid = replace(_observation("OB DISCHRG", 0), **{missing_field: None})
    daemon, store, writer, coordinator, model, publisher = _durable_daemon(
        tmp_path,
        (invalid, _observation("OL", 1, input_voltage_v=230.0)),
    )
    try:
        assert daemon._run_poll_iteration() == (True, False)  # pyright: ignore[reportPrivateUsage]
        assert publisher.publications == []
        result = daemon.poll_once()
        assert result.publication.virtual_status_token == "OL"
        _drain(writer)

        projection = _finish_durable_outcome(coordinator, writer, store)
        assert projection.start is not None
        assert projection.start.payload["observation"][missing_field] is None
        assert [gap.payload["reason"] for gap in projection.gaps] == [PRESTART_LOSS_REASON]
        assert projection.outcome is not None
        assert projection.outcome.payload["disposition"] == "rejected"
        assert model.prepare_calls == model.commit_calls == 0
    finally:
        daemon.shutdown()


def test_invalid_ob_load_survives_graceful_restart_and_is_rejected(
    tmp_path: Path,
) -> None:
    invalid = replace(_observation("OB DISCHRG", 0), load_percent=None)
    daemon, _store, _writer, _coordinator, _model, publisher = _durable_daemon(
        tmp_path,
        (invalid,),
    )
    daemon.start()
    try:
        assert daemon._run_poll_iteration() == (True, False)  # pyright: ignore[reportPrivateUsage]
        assert publisher.publications == []
    finally:
        daemon.shutdown()

    restarted, store, writer, coordinator, model, publisher = _durable_daemon(
        tmp_path,
        (_observation("OL", 1, input_voltage_v=230.0),),
    )
    try:
        assert restarted.poll_once().publication.virtual_status_token == "OL"
        _drain(writer)
        projection = _finish_durable_outcome(coordinator, writer, store)
        assert projection.start is not None
        assert projection.start.payload["observation"]["load_percent"] is None
        assert [gap.payload["reason"] for gap in projection.gaps] == [PRESTART_LOSS_REASON]
        assert projection.outcome is not None
        assert projection.outcome.payload["disposition"] == "rejected"
        assert model.prepare_calls == model.commit_calls == 0
    finally:
        restarted.shutdown()


@pytest.mark.parametrize(
    ("raw_status", "input_voltage_v", "missing_field"),
    (
        ("COMMFAULT", None, "battery_voltage_v"),
        ("COMMFAULT", 0.0, "load_percent"),
        ("", 99.9, "battery_voltage_v"),
        ("", None, "load_percent"),
    ),
)
def test_invalid_unknown_candidate_is_durably_rejected_after_ol(
    tmp_path: Path,
    raw_status: str,
    input_voltage_v: float | None,
    missing_field: str,
) -> None:
    invalid = replace(
        _observation(raw_status, 0, input_voltage_v=input_voltage_v),
        **{missing_field: None},
    )
    daemon, store, writer, coordinator, model, publisher = _durable_daemon(
        tmp_path,
        (invalid, _observation("OL", 1, input_voltage_v=230.0)),
    )
    try:
        assert daemon._run_poll_iteration() == (True, False)  # pyright: ignore[reportPrivateUsage]
        assert len(publisher.poll_failures) == 1
        assert publisher.publications == []

        result = daemon.poll_once()
        assert result.publication.virtual_status_token == "OL"
        _drain(writer)

        projection = _finish_durable_outcome(coordinator, writer, store)
        assert projection.start is not None
        assert projection.start.payload["observation"]["raw_status"] == raw_status
        assert projection.start.payload["observation"][missing_field] is None
        assert projection.start.payload["charge_readiness"]["ready"] is False
        assert [gap.payload["reason"] for gap in projection.gaps] == [PRESTART_LOSS_REASON]
        assert projection.outcome is not None
        assert projection.outcome.payload["disposition"] == "rejected"
        assert projection.outcome.payload["evidence_class"] == "rejected"
        assert projection.outcome.payload["commit_receipt_id"] is None
        assert projection.outcome.payload["decline_evidence_eligible"] is False
        assert all(record.record_type != "model_commit" for record in projection.derived_records)
        assert model.prepare_calls == model.commit_calls == 0
    finally:
        daemon.shutdown()


def test_cold_start_poll_loss_keeps_monitor_alive_without_watchdog(
    tmp_path: Path,
) -> None:
    daemon_holder: list[MonitorDaemon] = []
    notifications: list[str] = []
    daemon, store, _writer, _telemetry, _model, _publisher = _daemon(
        tmp_path,
        (_observation("OL", 0),),
        clocks=RuntimeClocks(
            sleep=lambda _delay: daemon_holder[0].request_stop(),
            notify=lambda status: notifications.append(status),
        ),
    )
    telemetry = _AlwaysFailingTelemetry()
    daemon._telemetry = cast(Any, telemetry)
    daemon._publisher = cast(Any, _ColdStartPublisher())
    daemon_holder.append(daemon)

    try:
        daemon.run()
    finally:
        store.close()

    assert telemetry.read_count == 1
    assert daemon._fatal_error is None
    assert notifications == ["STOPPING=1"]


@pytest.mark.parametrize(
    "invalid",
    (
        replace(_observation("OL", 0), battery_voltage_v=float("nan")),
        replace(_observation("OL", 0), battery_voltage_v=0.0),
        replace(_observation("OL", 0), load_percent=None),
        replace(_observation("OL", 0), load_percent=float("nan")),
        replace(_observation("OL", 0), load_percent=-0.1),
        replace(_observation("OL", 0), load_percent=100.1),
    ),
)
def test_validate_observation_rejects_invalid_safety_inputs(
    invalid: PhysicalObservation,
) -> None:
    with pytest.raises(RuntimeErrorBoundary):
        _validate_observation(invalid)


def test_fatal_safety_publication_failure_propagates_after_orderly_shutdown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon, _store, _writer, _telemetry, _model, _publisher = _daemon(
        tmp_path,
        (_observation("OL", 0),),
    )

    def fail_poll() -> None:
        raise SafetyPublicationError("virtual safety output failed")

    monkeypatch.setattr(daemon, "poll_once", fail_poll)

    with pytest.raises(SafetyPublicationError, match="virtual safety output failed"):
        daemon.run()

    assert daemon._closed
    assert not daemon._running


def test_publication_failure_requests_stop_before_output_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon, store, _writer, _telemetry, _model, publisher = _daemon(
        tmp_path,
        (_observation("OL", 0),),
    )
    daemon._running = True
    observed_running: list[bool] = []

    def blocked_cleanup() -> None:
        observed_running.append(daemon._running)
        raise TimeoutError("publication invalidation exceeded deadline")

    monkeypatch.setattr(publisher, "invalidate_output", blocked_cleanup)
    try:
        daemon._handle_publication_failure(SafetyPublicationError("publication failed"))
    finally:
        store.close()

    assert observed_running == [False]
    assert not daemon._running
    assert isinstance(daemon._fatal_error, SafetyPublicationError)


class _BlockingWorker:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self._request = object()

    def after_first_safety_publication(self) -> None:
        return None

    def defer(self, _request) -> bool:
        return False

    def peek_pending(self):
        return self._request

    def prepare(self, _request):
        self.entered.set()
        assert self.release.wait(timeout=2.0)
        return None

    def discard_pending(self, _request) -> None:
        self._request = None


class _NoopReporter:
    def tick(self, *, consecutive_errors: int):
        del consecutive_errors
        return None


class _NoopReportSink:
    def publish(self, _report) -> None:
        return None


class _PreparedWorker(_BlockingWorker):
    def __init__(self, prepared) -> None:
        super().__init__()
        self.release.set()
        self._prepared = prepared

    def prepare(self, _request):
        prepared, self._prepared = self._prepared, None
        return prepared


def test_blocked_assessment_thread_does_not_block_current_capture_writer(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path)
    writer = CaptureWriter()
    worker = _BlockingWorker()
    model = _Model(_snapshot())
    coordinator = BackgroundCoordinator(
        BackgroundDependencies(
            assessment_store=store,
            startup_store=store,
            reporting_store=store,
            commit_model=cast(Any, model),
            policy_model=cast(Any, model),
            worker=cast(Any, worker),
            writer=writer,
            reporter=cast(Any, _NoopReporter()),
            report_sink=cast(Any, _NoopReportSink()),
        ),
        StartupRecovery(None, ()),
        BackgroundSettings(reporting_interval_s=60.0),
    )
    publisher = _Publisher()
    daemon = MonitorDaemon(
        Config(model_dir=tmp_path),
        RuntimeDependencies(
            telemetry=_Telemetry((_observation("OB DISCHRG", 0),)),
            model=cast(Any, model),
            publisher=publisher,
            capture=BlackoutCapture(store, writer),
            writer=writer,
            coordinator=coordinator,
            store=store,
        ),
    )
    daemon.start()
    try:
        daemon.poll_once()
        assert worker.entered.wait(timeout=2.0)
        deadline = monotonic() + 2.0
        while store.work_registry().capture is None and monotonic() < deadline:
            sleep(0.01)

        assert len(publisher.publications) == 1
        assert store.work_registry().capture is not None
    finally:
        worker.release.set()
        daemon.shutdown()


def test_slow_idle_maintenance_does_not_delay_low_battery_publication(
    tmp_path: Path,
) -> None:
    daemon, _store, writer, _telemetry, _model, publisher = _daemon(
        tmp_path,
        (_observation("OB DISCHRG", 0, voltage_v=10.9),),
    )
    entered = Event()
    release = Event()

    def slow_maintenance() -> None:
        entered.set()
        assert release.wait(timeout=2.0)

    assert writer.submit(CaptureCommand(CaptureCommandKind.RECOVERY_RECEIPT, slow_maintenance))
    writer.start()
    assert entered.wait(timeout=1.0)
    started = monotonic()
    try:
        result = daemon.poll_once()
        elapsed = monotonic() - started
        assert result.publication.lb is True
        assert len(publisher.publications) == 1
        assert elapsed < 0.5
    finally:
        release.set()
        released_at = monotonic()
        deadline = released_at + 0.5
        while _store.work_registry().capture is None and monotonic() < deadline:
            sleep(0.01)
        assert _store.work_registry().capture is not None
        assert monotonic() - released_at < 0.5
        assert writer.health().max_busy_time_s <= 2.0
        daemon.shutdown()


def test_prepared_close_and_model_commit_execute_only_on_capture_writer_thread(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = JsonlEventStore(tmp_path)
    writer = CaptureWriter()
    blackout_id = "a" * 32
    prepared = SimpleNamespace(
        request=SimpleNamespace(
            processing=SimpleNamespace(
                blackout_id=blackout_id,
                final_path_token="event.jsonl",
            )
        )
    )
    worker = _PreparedWorker(prepared)
    executed = Event()
    execution_threads = []

    def close_on_writer(_store, _model, received):
        assert received is prepared
        execution_threads.append(current_thread().name)
        executed.set()
        return SimpleNamespace()

    monkeypatch.setattr("src.application.background_coordinator.close_blackout", close_on_writer)
    coordinator = BackgroundCoordinator(
        BackgroundDependencies(
            assessment_store=store,
            startup_store=store,
            reporting_store=store,
            commit_model=cast(Any, _Model(_snapshot())),
            policy_model=cast(Any, _Model(_snapshot())),
            worker=cast(Any, worker),
            writer=writer,
            reporter=cast(Any, _NoopReporter()),
            report_sink=cast(Any, _NoopReportSink()),
        ),
        StartupRecovery(None, ()),
        BackgroundSettings(reporting_interval_s=60.0),
    )
    writer.start()
    try:
        coordinator.after_first_safety_publication()
        coordinator.run_one()

        assert executed.wait(timeout=2.0)
        assert execution_threads == ["ups-capture-writer"]
    finally:
        writer.stop(drain=True)
        store.close()


def test_production_composition_refuses_to_provision_a_missing_model(tmp_path: Path) -> None:
    with pytest.raises(TargetModelStateError, match="does not exist"):
        build_daemon(
            Config(model_dir=tmp_path),
            virtual_ups_path=tmp_path / "ups.dev",
            health_path=tmp_path / "health.json",
        )

    assert not (tmp_path / "model.json").exists()


def test_cutover_tree_has_no_parallel_legacy_runtime_or_daemon_commands() -> None:
    repository = Path(__file__).parents[1]
    retired = (
        "capacity_estimator.py",
        "discharge_collector.py",
        "discharge_handler.py",
        "discharge_journal.py",
        "discharge_types.py",
        "event_classifier.py",
        "replacement_predictor.py",
        "runtime_calculator.py",
        "sag_tracker.py",
        "soc_predictor.py",
        "soh_calculator.py",
        "virtual_ups.py",
    )
    assert all(not (repository / "src" / filename).exists() for filename in retired)
    retired_math = (
        "calibration.py",
        "integration.py",
        "regression.py",
        "rls.py",
        "scheduler.py",
        "types.py",
    )
    assert all(
        not (repository / "src" / "battery_math" / filename).exists() for filename in retired_math
    )

    runtime_source = "\n".join(
        (repository / relative).read_text()
        for relative in (
            "src/monitor.py",
            "src/application/reporting_scheduler.py",
            "src/virtual_ups_exporter/__init__.py",
        )
    )
    for forbidden in (
        "upscmd",
        "INSTCMD",
        "test.battery",
        "soc_predictor",
    ):
        assert forbidden not in runtime_source


def test_current_docs_preserve_event_evidence_and_learning_boundary() -> None:
    repository = Path(__file__).parents[1]
    documents = (
        repository / "README.md",
        repository / "docs" / "internal" / "CONTEXT.md",
    )
    required = (
        "events/",
        "index.jsonl",
        "active.json",
        "capture -> assess -> compare -> identify -> decide -> report",
        "ir_k",
        "partial",
        "CAL",
        "Peukert",
        "LUT",
        "discharge-events-v1.jsonl",
        "read-only archive",
    )
    retired_claims = (
        "Event classifier",
        "local operational source of truth",
        "auto-calibrated from real discharge data",
    )

    for path in documents:
        text = path.read_text()
        assert all(marker in text for marker in required), path
        assert all(claim not in text for claim in retired_claims), path
        assert "does not open or import" in text or "neither opens nor imports" in text


def test_unknown_between_ob_and_ol_is_current_safe_and_durable(tmp_path: Path) -> None:
    observations = (
        _observation("OB DISCHRG", 0),
        _observation("COMMFAULT", 1, input_voltage_v=None),
        _observation("OL", 2, input_voltage_v=230.0),
    )
    daemon, store, writer, telemetry, model, publisher = _daemon(tmp_path, observations)
    try:
        first = daemon.poll_once()
        assert store.work_registry().capture is None
        _drain(writer)
        second = daemon.poll_once()
        _drain(writer)
        third = daemon.poll_once()
        _drain(writer)

        assert first.publication.event_class == BlackoutKind.BLACKOUT_REAL
        assert second.publication.raw_status == "COMMFAULT"
        assert second.publication.event_class == BlackoutKind.BLACKOUT_REAL
        assert second.publication.lb is False
        assert second.publication.virtual_status_token == "OB DISCHRG"
        assert third.publication.event_class == BlackoutKind.ONLINE
        assert telemetry.read_count == 3
        assert model.snapshot_count == 3
        assert len(publisher.publications) == 3

        pending = store.work_registry().pending_processing
        assert len(pending) == 1
        projection = store.project(EventRef(pending[0].blackout_id, pending[0].final_path_token))
        assert [item.payload["raw_status"] for item in projection.observations] == ["COMMFAULT"]
        assert projection.derived_records == ()
    finally:
        store.close()


def test_idle_unknown_outage_prelude_is_published_and_durably_gapped(
    tmp_path: Path,
) -> None:
    daemon, store, writer, _telemetry, _model, publisher = _daemon(
        tmp_path,
        (
            _observation("COMMFAULT", 0, input_voltage_v=None, voltage_v=10.9),
            _observation("OB DISCHRG", 1, input_voltage_v=0.0, voltage_v=10.9),
            _observation("OL", 2, input_voltage_v=230.0, voltage_v=13.3),
            _observation("OL", 3, input_voltage_v=230.0, voltage_v=13.3),
        ),
    )
    try:
        first = daemon.poll_once()
        _drain(writer)
        second = daemon.poll_once()
        _drain(writer)
        third = daemon.poll_once()
        _drain(writer)
        fourth = daemon.poll_once()
        _drain(writer)

        assert first.publication.event_class == BlackoutKind.BLACKOUT_REAL
        assert first.publication.virtual_status_token == "OB DISCHRG LB"
        assert first.publication.lb is True
        assert second.publication.event_class == BlackoutKind.BLACKOUT_REAL
        assert third.publication.event_class == BlackoutKind.BLACKOUT_REAL
        assert fourth.publication.event_class == BlackoutKind.ONLINE

        pending = store.work_registry().pending_processing
        assert len(pending) == 1
        projection = store.project(EventRef(pending[0].blackout_id, pending[0].final_path_token))
        assert projection.start is not None
        assert projection.start.payload["observation"]["raw_status"] == "COMMFAULT"
        assert [record.record_type for record in projection.records] == [
            "start",
            "gap",
            "observation",
            "end",
        ]
        assert projection.gaps[0].payload["reason"] == UNKNOWN_PRELUDE_GAP_REASON
        assert projection.derived_records == ()
        assert publisher.errors == []
    finally:
        store.close()


def test_idle_healthy_input_unknown_publishes_safely_without_event(tmp_path: Path) -> None:
    daemon, store, writer, _telemetry, _model, _publisher = _daemon(
        tmp_path,
        (_observation("COMMFAULT", 0, input_voltage_v=230.0, voltage_v=13.3),),
    )
    try:
        result = daemon.poll_once()
        _drain(writer)

        assert result.publication.event_class == BlackoutKind.BLACKOUT_REAL
        assert result.publication.virtual_status_token == "OB DISCHRG"
        assert result.publication.lb is False
        assert store.work_registry().capture is None
        assert store.work_registry().pending_processing == ()
    finally:
        store.close()


def test_unknown_status_with_low_reserve_publishes_lb(tmp_path: Path) -> None:
    daemon, store, _writer, _telemetry, _model, _publisher = _daemon(
        tmp_path,
        (_observation("COMMFAULT", 0, input_voltage_v=None, voltage_v=10.9),),
    )
    try:
        publication = daemon.poll_once().publication

        assert publication.event_class == BlackoutKind.BLACKOUT_REAL
        assert publication.lb is True
        assert publication.virtual_status_token == "OB DISCHRG LB"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("input_voltage_v", "expected"),
    (
        (None, BlackoutKind.BLACKOUT_REAL),
        (99.9, BlackoutKind.BLACKOUT_REAL),
        (230.0, BlackoutKind.BLACKOUT_TEST),
    ),
)
def test_calibration_classification_is_fail_closed(
    tmp_path: Path,
    input_voltage_v: float | None,
    expected: BlackoutKind,
) -> None:
    daemon, store, _writer, _telemetry, _model, _publisher = _daemon(
        tmp_path,
        (_observation("CAL OB", 0, input_voltage_v=input_voltage_v),),
    )
    try:
        assert daemon.poll_once().publication.event_class == expected
    finally:
        store.close()


def test_raw_lb_is_diagnostic_only_above_modeled_threshold(tmp_path: Path) -> None:
    daemon, store, _writer, _telemetry, _model, _publisher = _daemon(
        tmp_path,
        (_observation("OB DISCHRG LB", 0, voltage_v=13.6),),
    )
    try:
        publication = daemon.poll_once().publication
        assert publication.raw_lb_observed is True
        assert publication.lb is False
        assert publication.virtual_status_token == "OB DISCHRG"
    finally:
        store.close()


def test_sticky_virtual_lb_clears_only_after_durable_end(tmp_path: Path) -> None:
    observations = (
        _observation("OB DISCHRG", 0, voltage_v=10.9),
        _observation("OL", 1, input_voltage_v=230.0, voltage_v=13.3),
        _observation("OL", 2, input_voltage_v=230.0, voltage_v=13.3),
        _observation("OL", 3, input_voltage_v=230.0, voltage_v=13.3),
    )
    daemon, store, writer, _telemetry, _model, _publisher = _daemon(tmp_path, observations)
    try:
        assert daemon.poll_once().publication.lb is True
        _drain(writer)

        queued_end = daemon.poll_once()
        assert queued_end.publication.lb is True
        assert daemon.poll_once().publication.lb is True

        _drain(writer)
        durable_online = daemon.poll_once()
        assert durable_online.publication.lb is False
        assert durable_online.publication.virtual_status_token == "OL"
    finally:
        store.close()


def test_sticky_recovery_deadline_releases_lb_and_disables_learning(
    tmp_path: Path,
) -> None:
    now = [0.0]
    daemon, store, writer, _telemetry, _model, publisher = _daemon(
        tmp_path,
        (
            _observation("OB DISCHRG", 0, voltage_v=10.9),
            _observation("OL", 1, input_voltage_v=230.0, voltage_v=13.3),
            _observation("OL", 2, input_voltage_v=230.0, voltage_v=13.3),
        ),
        clocks=RuntimeClocks(monotonic=lambda: now[0]),
    )
    daemon._sticky_recovery_deadline_s = 1.0
    try:
        first = daemon.poll_once()
        assert first.publication.lb is True
        # START is intentionally left queued, representing a writer blocked
        # in storage.  The safety poll must remain independent of that lane.
        now[0] = 1.0
        sticky = daemon.poll_once()
        assert sticky.publication.lb is True
        assert daemon._fatal_error is None

        now[0] = 2.1
        recovered = daemon.poll_once()
        assert recovered.publication.lb is False
        assert recovered.publication.virtual_status_token == "OL"
        assert recovered.capture_accepted is False
        assert daemon._recovery_requested
        assert isinstance(daemon._fatal_error, SafetyPublicationError)
        assert not writer.health().capture_available
        assert "sticky recovery deadline" in (writer.health().bounded_error or "")
        assert publisher.errors == []

        # Once the deadline expires, a queued model lane cannot turn the
        # timed-out event into a scientific update.
        assert not writer.submit(CaptureCommand(CaptureCommandKind.MODEL_COMMIT, lambda: None))
    finally:
        daemon.shutdown()
        store.close()


def test_terminal_capture_recovery_failure_clears_sticky_wait_but_stays_unhealthy(
    tmp_path: Path,
) -> None:
    store = _CorrelatedFailureStore()
    writer = CaptureWriter()
    capture = BlackoutCapture(cast(Any, store), writer)
    publisher = _Publisher()
    daemon = MonitorDaemon(
        Config(model_dir=tmp_path),
        RuntimeDependencies(
            telemetry=_Telemetry(
                (
                    _observation("OB DISCHRG", 0, voltage_v=10.9),
                    _observation("OL", 1, input_voltage_v=230.0, voltage_v=13.3),
                    _observation("OL", 2, input_voltage_v=230.0, voltage_v=13.3),
                )
            ),
            model=cast(Any, _Model(_snapshot())),
            publisher=publisher,
            capture=capture,
            writer=writer,
            coordinator=cast(Any, _Coordinator()),
            store=cast(Any, store),
        ),
    )
    try:
        assert daemon.poll_once().publication.lb is True
        _drain(writer)

        assert daemon.poll_once().publication.lb is True
        _drain(writer)
        assert store.append_attempts == ["end", "gap"]
        assert not capture.has_unacknowledged_capture

        recovered_online = daemon.poll_once()
        assert recovered_online.publication.lb is False
        assert recovered_online.publication.virtual_status_token == "OL"
        assert recovered_online.capture_accepted is False
        assert store.append_attempts == ["end", "gap"]

        health = writer.health()
        assert not health.capture_available
        assert "terminal_recovery_failed OSError: gap failed" in (health.bounded_error or "")
    finally:
        daemon.shutdown()

    assert store.closed


@pytest.mark.parametrize(("next_boot", "gap_count"), (("boot-a", 0), ("boot-b", 1)))
def test_recovered_capture_uses_last_durable_boot_identity(
    tmp_path: Path,
    next_boot: str,
    gap_count: int,
) -> None:
    first_store = JsonlEventStore(tmp_path)
    first_writer = CaptureWriter()
    first_capture = BlackoutCapture(first_store, first_writer)
    from src.domain.reasons import order_reasons
    from src.domain.values import ChargeReadiness

    assert first_capture.accept_after_safety_publish(
        _observation("OB DISCHRG", 0),
        safety_snapshot=_snapshot(),
        charge_readiness=ChargeReadiness(False, 0.0, None, order_reasons(())),
    )
    _drain(first_writer)
    first_store.close()

    second_store = JsonlEventStore(tmp_path)
    recovery = recover_startup_metadata(second_store)
    assert recovery.recovered_capture is not None
    assert recovery.recovered_capture.last_boot_id == "boot-a"
    second_store.close()

    daemon, store, writer, _telemetry, _model, publisher = _daemon(
        tmp_path,
        (_observation("OB DISCHRG", 1, boot_id=next_boot),),
        recovered=recovery.recovered_capture,
    )
    try:
        publication = daemon.poll_once().publication
        assert len(publisher.publications) == 1
        assert publication.lb is False
        assert publication.virtual_status_token == "OB DISCHRG"
        _drain(writer)
        capture = store.work_registry().capture
        assert capture is not None
        projection = store.project(EventRef(capture.blackout_id, capture.path_token))
        assert len(projection.gaps) == gap_count
        if gap_count:
            assert projection.gaps[0].payload["reason"] == "boot_changed"
    finally:
        store.close()


def test_cold_boot_after_raw_lb_closes_online_without_shutdown_loop(tmp_path: Path) -> None:
    from src.domain.reasons import order_reasons
    from src.domain.values import ChargeReadiness

    first_store = JsonlEventStore(tmp_path)
    first_writer = CaptureWriter()
    first_capture = BlackoutCapture(first_store, first_writer)
    assert first_capture.accept_after_safety_publish(
        _observation("OB DISCHRG LB", 5, boot_id="boot-before", voltage_v=10.9),
        safety_snapshot=_snapshot(),
        charge_readiness=ChargeReadiness(False, 0.0, None, order_reasons(())),
    )
    _drain(first_writer)
    first_store.close()

    metadata_store = JsonlEventStore(tmp_path)
    recovery = recover_startup_metadata(metadata_store)
    metadata_store.close()
    assert recovery.recovered_capture is not None

    daemon, store, writer, _telemetry, _model, publisher = _daemon(
        tmp_path,
        (
            _observation("OL", 100, boot_id="boot-after", input_voltage_v=230.0),
            _observation("OL", 101, boot_id="boot-after", input_voltage_v=230.0),
        ),
        recovered=recovery.recovered_capture,
    )
    try:
        first = daemon.poll_once()
        assert first.publication.lb is False
        assert first.publication.virtual_status_token == "OL"
        _drain(writer)
        second = daemon.poll_once()
        assert second.publication.lb is False
        assert second.publication.virtual_status_token == "OL"
        assert all(not publication.lb for publication in publisher.publications)

        pending = store.work_registry().pending_processing
        assert len(pending) == 1
        projection = store.project(EventRef(pending[0].blackout_id, pending[0].final_path_token))
        assert projection.end is not None
        assert projection.end.payload["termination"] == "closed_restart_gap"
        assert projection.end.boot_id == "boot-before"
        assert projection.end.monotonic_ns == 5_000_000_000
    finally:
        store.close()


def test_failed_recovered_end_clears_wait_and_remains_discoverable(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "recovered-end-failure"
    recovered_store, recovered, original_id = _open_recovered_state(state_path)
    failing_store = _RecoveredAppendFailureStore(recovered_store)
    writer = CaptureWriter()
    capture = BlackoutCapture(cast(Any, failing_store), writer)
    daemon = MonitorDaemon(
        Config(model_dir=state_path),
        RuntimeDependencies(
            telemetry=_Telemetry(
                (
                    _observation("OL", 100, boot_id="boot-after", input_voltage_v=230.0),
                    _observation("OL", 101, boot_id="boot-after", input_voltage_v=230.0),
                )
            ),
            model=cast(Any, _Model(_snapshot())),
            publisher=_Publisher(),
            capture=capture,
            writer=writer,
            coordinator=cast(Any, _Coordinator()),
            store=cast(Any, failing_store),
            recovered_capture=recovered,
        ),
    )
    try:
        first_online = daemon.poll_once()
        assert first_online.publication.lb is False
        assert first_online.publication.virtual_status_token == "OL"
        assert first_online.capture_accepted is True
        _drain(writer)

        assert failing_store.append_attempts == ["end", "gap"]
        assert not capture.has_unacknowledged_capture
        second_online = daemon.poll_once()
        assert second_online.publication.lb is False
        assert second_online.publication.virtual_status_token == "OL"
        assert second_online.capture_accepted is False

        health = writer.health()
        assert not health.capture_available
        assert "terminal_recovery_failed OSError: gap failed" in (health.bounded_error or "")
    finally:
        daemon.shutdown()

    final_store = JsonlEventStore(state_path)
    try:
        still_recovered = recover_startup_metadata(final_store)
        assert still_recovered.recovered_capture is not None
        assert still_recovered.recovered_capture.blackout_id == original_id
    finally:
        final_store.close()


def test_recovered_capture_armed_lb_clears_after_terminal_end_failure(tmp_path: Path) -> None:
    state_path = tmp_path / "recovered-armed-lb-failure"
    recovered_store, recovered, _original_id = _open_recovered_state(state_path)
    failing_store = _RecoveredTerminalFailureStore(recovered_store)
    writer = CaptureWriter()
    capture = BlackoutCapture(cast(Any, failing_store), writer)
    daemon = MonitorDaemon(
        Config(model_dir=state_path),
        RuntimeDependencies(
            telemetry=_Telemetry(
                (
                    _observation("OB DISCHRG", 100, boot_id="boot-before", voltage_v=10.9),
                    _observation(
                        "OL",
                        101,
                        boot_id="boot-before",
                        input_voltage_v=230.0,
                    ),
                    _observation(
                        "OL",
                        102,
                        boot_id="boot-before",
                        input_voltage_v=230.0,
                    ),
                )
            ),
            model=cast(Any, _Model(_snapshot())),
            publisher=_Publisher(),
            capture=capture,
            writer=writer,
            coordinator=cast(Any, _Coordinator()),
            store=cast(Any, failing_store),
            recovered_capture=recovered,
        ),
    )
    try:
        # A direct recovered END is reachable only when the first poll is OL;
        # startup intentionally has no synthetic LB latch then.  The closest
        # legitimate armed state is a low OB first poll, which attaches the
        # recovered capture before the shared END failure path runs on OL.
        armed = daemon.poll_once()
        assert armed.publication.lb is True
        assert armed.capture_accepted is True
        _drain(writer)

        failed_end = daemon.poll_once()
        assert failed_end.publication.lb is True
        assert failed_end.capture_accepted is True
        _drain(writer)
        assert failing_store.append_attempts == ["observation", "end", "gap"]
        assert not capture.has_unacknowledged_capture

        cleared = daemon.poll_once()
        assert cleared.publication.lb is False
        assert cleared.publication.virtual_status_token == "OL"
        assert cleared.capture_accepted is False
    finally:
        daemon.shutdown()


@pytest.mark.parametrize(
    ("voltage_v", "expected_lb"),
    ((13.3, False), (10.9, True)),
)
def test_restart_during_ob_recomputes_current_modeled_threshold(
    tmp_path: Path,
    voltage_v: float,
    expected_lb: bool,
) -> None:
    state_path = tmp_path / f"case-{voltage_v}"
    first_store = JsonlEventStore(state_path)
    first_writer = CaptureWriter()
    first_capture = BlackoutCapture(first_store, first_writer)
    from src.domain.reasons import order_reasons
    from src.domain.values import ChargeReadiness

    assert first_capture.accept_after_safety_publish(
        _observation("OB DISCHRG", 0),
        safety_snapshot=_snapshot(),
        charge_readiness=ChargeReadiness(False, 0.0, None, order_reasons(())),
    )
    _drain(first_writer)
    first_store.close()

    metadata_store = JsonlEventStore(state_path)
    recovery = recover_startup_metadata(metadata_store)
    metadata_store.close()
    assert recovery.recovered_capture is not None

    daemon, store, writer, _telemetry, _model, _publisher = _daemon(
        state_path,
        (_observation("OB DISCHRG", 1, voltage_v=voltage_v),),
        recovered=recovery.recovered_capture,
    )
    try:
        publication = daemon.poll_once().publication

        assert publication.lb is expected_lb
        assert publication.virtual_status_token == (
            "OB DISCHRG LB" if expected_lb else "OB DISCHRG"
        )
        _drain(writer)
    finally:
        store.close()
