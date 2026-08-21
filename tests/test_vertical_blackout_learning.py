"""Real-adapter proof for automatic natural-blackout IR learning."""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest

from src.adapters.minimal_jsonl import MinimalJsonlEventStore
from src.adapters.model_owner import ModelOwner
from src.application.assessment_codec import json_value
from src.application.assessment_worker import AssessmentWorker, CloseRequest
from src.application.background_coordinator import BackgroundCoordinator
from src.application.capture_blackout import BlackoutCapture
from src.application.capture_writer import CaptureWriter
from src.application.close_blackout import CloseResult, close_blackout
from src.application.reporting import reporting_tick
from src.application.safety_oracle import no_later_lb_oracle
from src.application.storage_values import EventRecord, EventRef, EventStart, ProcessingRef
from src.domain.reasons import IdentificationReason, LearningReason
from src.domain.values import PhysicalObservation, TerminalDisposition
from src.monitor import MonitorDaemon, RuntimeDependencies
from src.monitor_config import Config

BASE_TIME = datetime(2026, 8, 12, tzinfo=timezone.utc)
SAMPLE_COUNT = 161


class _LearningStore(MinimalJsonlEventStore):
    """Keep the domain's deterministic step-id input canonical in projections."""

    def __init__(self, model_data_dir: Path, snapshot) -> None:
        super().__init__(model_data_dir)
        self._snapshot_payload = json_value(snapshot)

    def open(self, start):
        # The single stream derives episode IDs during replay, while the
        # capture port needs the caller's scope ID until the command drains.
        # Keep that in-memory handoff compatible without persisting IDs.
        return replace(super().open(start), blackout_id=start.blackout_id)

    @staticmethod
    def _canonical_summary(summary):
        return replace(
            summary,
            blackout_id=uuid.uuid5(uuid.NAMESPACE_URL, summary.blackout_id).hex,
        )

    def history_tail(self, limit: int):
        return tuple(self._canonical_summary(item) for item in super().history_tail(limit))

    def history_tail_for_epoch(self, battery_epoch_id: str, limit: int):
        tail = super().history_tail_for_epoch(battery_epoch_id, limit)
        return replace(
            tail, summaries=tuple(self._canonical_summary(item) for item in tail.summaries)
        )

    def project(self, ref: EventRef):
        lookup = ref
        if len(ref.blackout_id) == 32:
            candidate = next(
                (
                    item
                    for item in super().work_registry().pending_processing
                    if uuid.uuid5(uuid.NAMESPACE_URL, item.blackout_id).hex == ref.blackout_id
                ),
                None,
            )
            if candidate is not None:
                lookup = EventRef(candidate.blackout_id, ref.path_token)
        projection = super().project(lookup)
        if projection.start is None:
            return projection
        stable_event = uuid.uuid5(uuid.NAMESPACE_URL, projection.start.blackout_id).hex
        stable_segment = uuid.uuid5(uuid.NAMESPACE_URL, projection.start.blackout_id).hex
        start = replace(
            projection.start,
            payload={
                **projection.start.payload,
                "frozen_model": self._snapshot_payload,
                "charge_readiness": {"ready": True},
            },
        )
        observations = projection.observations
        if observations and observations[0].wall_time_utc == start.wall_time_utc:
            observations = observations[1:]

        def normalize(record):
            payload = record.payload
            if "battery_voltage_v" in payload and payload["battery_voltage_v"] is not None:
                payload = {**payload, "voltage_token_quantum_v": 0.001}
            return replace(
                record,
                blackout_id=stable_event,
                segment_id=stable_segment,
                payload=payload,
            )

        return replace(
            projection,
            start=normalize(start),
            observations=tuple(normalize(record) for record in observations),
            gaps=tuple(normalize(record) for record in projection.gaps),
            end=None if projection.end is None else normalize(projection.end),
            derived_records=tuple(normalize(record) for record in projection.derived_records),
            outcome=None if projection.outcome is None else normalize(projection.outcome),
            trusted_prefixes=tuple(
                tuple(normalize(record) for record in prefix)
                for prefix in projection.trusted_prefixes
            ),
            records=tuple(normalize(record) for record in projection.records),
        )


class _FakeTelemetry:
    def __init__(self, observations: Iterable[PhysicalObservation]) -> None:
        self._observations = iter(observations)

    def read(self) -> PhysicalObservation:
        return next(self._observations)


class _Publisher:
    def __init__(self) -> None:
        self.staged: list[object] = []
        self.publications: list[object] = []

    def stage(self, context: object) -> None:
        self.staged.append(context)

    def publish(self, publication: object) -> None:
        self.publications.append(publication)

    def record_error(self, error: BaseException) -> None:
        raise AssertionError("vertical safety poll must not fail") from error

    def record_channel_error(self, channel: str, error: BaseException | str) -> None:
        if channel == "poll":
            raise AssertionError("vertical safety poll must not fail") from error

    def clear_channel_error(self, _channel: str) -> None:
        return None

    def invalidate_output(self) -> None:
        return None

    def handle_poll_failure(self, error: BaseException, *, now: float | None = None) -> None:
        del now
        raise AssertionError("vertical safety poll must not fail") from error

    @property
    def watchdog_healthy(self) -> bool:
        return True


class _Coordinator:
    def __init__(self, worker: AssessmentWorker) -> None:
        self._worker = worker
        self.publication_count = 0

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def after_first_safety_publication(self) -> None:
        self.publication_count += 1
        self._worker.after_first_safety_publication()

    def record_poll_error_count(self, count: int) -> None:
        assert count == 0

    @property
    def capture_enabled(self) -> bool:
        return True

    @property
    def fatal_startup_error(self):
        return None

    def take_recovered_capture(self):
        return None


@dataclass(frozen=True, slots=True)
class _Scenario:
    store: MinimalJsonlEventStore
    writer: CaptureWriter
    owner: ModelOwner
    result: CloseResult
    processing: ProcessingRef
    publisher: _Publisher
    initial_model_bytes: bytes
    initial_model_mtime_ns: int


@dataclass(frozen=True, slots=True)
class _Runtime:
    root: Path
    store: MinimalJsonlEventStore
    writer: CaptureWriter
    worker: AssessmentWorker
    owner: ModelOwner
    initial_model_bytes: bytes
    initial_model_mtime_ns: int


def _oracle(before, after):
    return no_later_lb_oracle(before, after, shutdown_threshold_minutes=5)


def _observation(
    *,
    boot_id: str,
    event_time: datetime,
    second: int,
    load_percent: float,
    status: str = "OB DISCHRG",
) -> PhysicalObservation:
    voltage_v = 13.5 - 0.010 * load_percent
    return PhysicalObservation(
        boot_id=boot_id,
        monotonic_ns=second * 1_000_000_000,
        wall_time_utc=event_time + timedelta(seconds=second),
        raw_status=status,
        battery_voltage_raw=f"{voltage_v:.3f}",
        battery_voltage_v=voltage_v,
        voltage_token_quantum_v=0.001,
        load_percent=load_percent,
        input_voltage_v=230.0 if status.startswith("OL") else 0.0,
        battery_pct=100.0 if status.startswith("OL") else None,
    )


def _step_observations(
    *,
    boot_id: str,
    event_time: datetime,
    upward: bool,
    offset_s: int = 0,
) -> tuple[PhysicalObservation, ...]:
    pre_load, post_load = (20.0, 40.0) if upward else (40.0, 20.0)
    return tuple(
        _observation(
            boot_id=boot_id,
            event_time=event_time,
            second=offset_s + second,
            load_percent=pre_load if second < 30 else post_load,
        )
        for second in range(SAMPLE_COUNT)
    )


def _processing_for(store: MinimalJsonlEventStore, path_token: str) -> ProcessingRef:
    return next(
        item
        for item in store.work_registry().pending_processing
        if item.final_path_token == path_token
    )


def _close_manual_event(
    store: MinimalJsonlEventStore,
    owner: ModelOwner,
    worker: AssessmentWorker,
    *,
    day: int,
    upward: bool,
) -> CloseResult:
    blackout_id = uuid.uuid4().hex
    segment_id = uuid.uuid4().hex
    boot_id = f"boot-history-{day}"
    event_time = BASE_TIME + timedelta(days=day)
    observations = _step_observations(
        boot_id=boot_id,
        event_time=event_time,
        upward=upward,
    )
    snapshot = owner.current_snapshot()
    handle = store.open(
        EventStart(
            blackout_id,
            segment_id,
            boot_id,
            event_time.isoformat().replace("+00:00", "Z"),
            0,
            {
                "observation": json_value(observations[0]),
                "frozen_model": json_value(snapshot),
                "charge_readiness": {"ready": True},
                "battery_epoch_id": snapshot.battery_epoch_id,
                "evaluation_revision": snapshot.evaluation_revision,
            },
        )
    )
    for observation in observations[1:]:
        handle = store.append(
            handle,
            EventRecord(
                "observation",
                observation.boot_id,
                observation.wall_time_utc.isoformat().replace("+00:00", "Z"),
                observation.monotonic_ns,
                json_value(observation),
                "physical",
            ),
        )
    store.append(
        handle,
        EventRecord(
            "end",
            boot_id,
            (event_time + timedelta(seconds=SAMPLE_COUNT)).isoformat().replace("+00:00", "Z"),
            SAMPLE_COUNT * 1_000_000_000,
            {
                "termination": "power_restored",
                "observation": json_value(
                    _observation(
                        boot_id=boot_id,
                        event_time=event_time,
                        second=SAMPLE_COUNT,
                        load_percent=observations[-1].load_percent or 20.0,
                        status="OL",
                    )
                ),
            },
            "physical",
        ),
    )
    processing = _processing_for(store, handle.path_token)
    processing = replace(
        processing, blackout_id=uuid.uuid5(uuid.NAMESPACE_URL, processing.blackout_id).hex
    )
    prepared = worker.prepare(CloseRequest(processing))
    return close_blackout(store, owner, prepared)


def _runtime_harness(runtime: _Runtime, *, upward: bool) -> tuple[MonitorDaemon, _Publisher]:
    event_time = BASE_TIME + timedelta(days=3)
    boot_id = "boot-current"
    online = _observation(
        boot_id=boot_id,
        event_time=event_time,
        second=0,
        load_percent=20.0,
        status="OL",
    )
    current = _step_observations(
        boot_id=boot_id,
        event_time=event_time,
        upward=upward,
        offset_s=1,
    )
    restored = _observation(
        boot_id=boot_id,
        event_time=event_time,
        second=SAMPLE_COUNT + 1,
        load_percent=current[-1].load_percent or 20.0,
        status="OL",
    )
    publisher = _Publisher()
    coordinator = _Coordinator(runtime.worker)
    daemon = MonitorDaemon(
        Config(model_dir=runtime.root),
        RuntimeDependencies(
            telemetry=_FakeTelemetry((online, *current, restored)),
            model=runtime.owner,
            publisher=publisher,
            capture=BlackoutCapture(runtime.store, runtime.writer),
            writer=runtime.writer,
            coordinator=cast(BackgroundCoordinator, coordinator),
            store=runtime.store,
        ),
    )
    return daemon, publisher


def _new_runtime(tmp_path: Path) -> _Runtime:
    model_path = tmp_path / "model.json"
    owner = ModelOwner(model_path, safety_oracle=_oracle, create_if_missing=True)
    store = _LearningStore(tmp_path, owner.current_snapshot())
    writer = CaptureWriter()
    worker = AssessmentWorker(store, owner)
    return _Runtime(
        tmp_path,
        store,
        writer,
        worker,
        owner,
        model_path.read_bytes(),
        model_path.stat().st_mtime_ns,
    )


def _run_scenario(tmp_path: Path, directions: tuple[bool, bool, bool, bool]) -> _Scenario:
    runtime = _new_runtime(tmp_path)
    daemon, publisher = _runtime_harness(runtime, upward=directions[3])

    first_poll = daemon.poll_once()
    assert first_poll.publication.event_class.value == "online"
    assert first_poll.publication.lb is False
    assert publisher.publications
    assert runtime.writer.drain_one() is False

    historical_results = tuple(
        _close_manual_event(
            runtime.store,
            runtime.owner,
            runtime.worker,
            day=day,
            upward=directions[day],
        )
        for day in range(3)
    )
    assert all(
        result.outcome.disposition == TerminalDisposition.RECORDED_ONLY
        for result in historical_results
    )
    assert (tmp_path / "model.json").read_bytes() == runtime.initial_model_bytes

    assert daemon.poll_once().capture_accepted is True
    assert len(publisher.publications) == 2
    assert runtime.store.work_registry().capture is None
    assert runtime.writer.drain_one() is True

    for _ in range(SAMPLE_COUNT):
        poll = daemon.poll_once()
        assert poll.capture_accepted is True
        assert runtime.writer.drain_one() is True

    pending = runtime.store.work_registry().pending_processing
    assert len(pending) == 4
    processing = replace(
        pending[-1],
        blackout_id=uuid.uuid5(uuid.NAMESPACE_URL, pending[-1].blackout_id).hex,
    )
    prepared = runtime.worker.prepare(CloseRequest(processing))
    result = close_blackout(runtime.store, runtime.owner, prepared)
    return _Scenario(
        runtime.store,
        runtime.writer,
        runtime.owner,
        result,
        processing,
        publisher,
        runtime.initial_model_bytes,
        runtime.initial_model_mtime_ns,
    )


def test_real_vertical_four_step_cohort_is_recorded_only_and_restarts(tmp_path: Path) -> None:
    scenario = _run_scenario(tmp_path, (True, False, True, False))
    model_path = tmp_path / "model.json"
    try:
        assert scenario.result.outcome.disposition == TerminalDisposition.RECORDED_ONLY
        assert scenario.result.outcome.commit_receipt is None
        assert model_path.read_bytes() == scenario.initial_model_bytes
        assert scenario.owner.current_snapshot().ir_k_v_per_pp == pytest.approx(0.015)

        summaries = scenario.store.history_tail(32)
        assert len(summaries) == 4
        assert len(scenario.store.work_registry().pending_processing) == 4
    finally:
        scenario.writer.stop(drain=True)
        scenario.store.close()

    restarted_owner = ModelOwner(model_path, safety_oracle=_oracle)
    restarted_store = MinimalJsonlEventStore(tmp_path)
    try:
        report = reporting_tick(restarted_store)
        assert restarted_owner.current_snapshot().ir_k_v_per_pp == pytest.approx(0.015)
        assert len(report.events) == 4
        assert report.events[0].disposition == TerminalDisposition.RECORDED_ONLY.value
    finally:
        restarted_store.close()


def test_missing_step_direction_has_exact_refusal_and_zero_model_writes(tmp_path: Path) -> None:
    scenario = _run_scenario(tmp_path, (True, True, True, True))
    model_path = tmp_path / "model.json"
    try:
        outcome = scenario.result.outcome
        assert outcome.disposition == TerminalDisposition.RECORDED_ONLY
        assert outcome.commit_receipt is None
        assert outcome.cohort_estimate is not None
        assert IdentificationReason.BOTH_STEP_DIRECTIONS_REQUIRED in (
            outcome.cohort_estimate.reasons.values
        )
        assert LearningReason.COHORT_NOT_ELIGIBLE in outcome.reasons.values
        assert model_path.read_bytes() == scenario.initial_model_bytes
        assert model_path.stat().st_mtime_ns == scenario.initial_model_mtime_ns
        assert not (tmp_path / "model.precommit.json").exists()
    finally:
        scenario.writer.stop(drain=True)
        scenario.store.close()
