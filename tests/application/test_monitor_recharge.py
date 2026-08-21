from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from src.adapters.minimal_jsonl import MinimalJsonlEventStore
from src.application.capture_blackout import BlackoutCapture
from src.application.capture_writer import CaptureCommand, CaptureCommandKind, CaptureWriter
from src.application.recharge_capture import RechargeCapture
from src.application.storage_values import EventRecord, EventRef, EventStart
from src.battery_math.lut import LutPoint
from src.domain.values import FrozenModelSnapshot, PhysicalObservation
from src.monitor import MonitorDaemon, RuntimeDependencies
from src.monitor_config import Config


class _Telemetry:
    def __init__(self, observations: Iterable[PhysicalObservation]) -> None:
        self._observations = iter(observations)

    def read(self) -> PhysicalObservation:
        return next(self._observations)


class _Model:
    def current_snapshot(self) -> FrozenModelSnapshot:
        return FrozenModelSnapshot(
            "2",
            "1",
            "a" * 32,
            "b" * 64,
            7.2,
            12.0,
            510.0,
            1.0,
            1.2,
            0.015,
            0.0,
            (LutPoint(13.7, 1.0, "standard"), LutPoint(10.8, 0.0, "anchor")),
        )

    def close(self) -> None:
        return None


class _Publisher:
    def stage(self, _context: object) -> None:
        return None

    def publish(self, _publication: object) -> None:
        return None

    def record_error(self, _error: BaseException) -> None:
        return None

    def record_channel_error(self, _channel: str, _error: BaseException | str) -> None:
        return None

    def clear_channel_error(self, _channel: str) -> None:
        return None

    def invalidate_output(self) -> None:
        return None

    def handle_poll_failure(self, _error: BaseException, *, now: float | None = None) -> object:
        del now
        return None

    @property
    def watchdog_healthy(self) -> bool:
        return True


class _Coordinator:
    def after_first_safety_publication(self) -> None:
        return None

    def record_poll_error_count(self, _count: int) -> None:
        return None

    def take_recovered_capture(self) -> None:
        return None

    @property
    def capture_enabled(self) -> bool:
        return True


def observation(status: str, second: int) -> PhysicalObservation:
    return PhysicalObservation(
        "boot-a",
        second * 1_000_000_000,
        datetime(2026, 8, 21, tzinfo=timezone.utc) + timedelta(seconds=second),
        status,
        "13.30",
        13.3,
        0.01,
        20.0,
        0.0 if "OB" in status else 230.0,
    )


def drain(writer: CaptureWriter) -> None:
    while writer.drain_one():
        pass


def test_first_restored_ol_starts_one_linked_recharge_episode(tmp_path: Path) -> None:
    writer = CaptureWriter()
    store = MinimalJsonlEventStore(tmp_path)
    recharge = RechargeCapture(store, writer)
    capture = BlackoutCapture(store, writer, on_power_restored=recharge.on_power_restored)
    daemon = MonitorDaemon(
        Config(model_dir=tmp_path),
        RuntimeDependencies(
            telemetry=cast(
                Any,
                _Telemetry(
                    (
                        observation("OB DISCHRG", 0),
                        observation("OL", 1),
                        observation("OL", 2),
                        observation("OL", 3),
                    )
                ),
            ),
            model=cast(Any, _Model()),
            publisher=cast(Any, _Publisher()),
            capture=capture,
            writer=writer,
            coordinator=cast(Any, _Coordinator()),
            store=store,
            recharge=recharge,
        ),
    )

    daemon.poll_once()
    assert not tuple((tmp_path / "events").iterdir())
    drain(writer)
    assert [path.name for path in (tmp_path / "events").iterdir()] == ["telemetry.jsonl"]
    daemon.poll_once()
    assert [path.name for path in (tmp_path / "events").iterdir()] == ["telemetry.jsonl"]
    drain(writer)
    assert [path.name for path in (tmp_path / "events").iterdir()] == ["telemetry.jsonl"]
    daemon.poll_once()
    drain(writer)
    assert [path.name for path in (tmp_path / "events").iterdir()] == ["telemetry.jsonl"]
    daemon.poll_once()
    drain(writer)

    # Without a durable OL transition sample the single stream cannot infer a
    # recharge episode.  Keep the blackout capture visible and fail closed.
    assert store.work_registry().capture is not None
    assert (
        store.sealed_event_projections(
            "2026-08-21T00:00:00Z",
            "2026-08-22T00:00:00Z",
            event_kind="recharge",
        )
        == ()
    )
    store.close()


def test_restart_reconciles_durable_end_with_explicit_gap_refusal(tmp_path: Path) -> None:
    blackout_id = uuid4().hex
    segment_id = uuid4().hex
    first_store = MinimalJsonlEventStore(tmp_path)
    handle = first_store.open(
        EventStart(
            blackout_id,
            segment_id,
            "boot-a",
            "2026-08-21T00:00:00Z",
            0,
            {"battery_epoch_id": "epoch-a"},
        )
    )
    first_store.append(
        handle,
        EventRecord(
            "end",
            "boot-a",
            "2026-08-21T00:00:01Z",
            1_000_000_000,
            {"termination": "power_restored"},
            "physical",
        ),
    )
    first_store.close()

    writer = CaptureWriter()
    second_store = MinimalJsonlEventStore(tmp_path)
    recharge = RechargeCapture(second_store, writer)
    capture = BlackoutCapture(second_store, writer, on_power_restored=recharge.on_power_restored)
    daemon = MonitorDaemon(
        Config(model_dir=tmp_path),
        RuntimeDependencies(
            telemetry=cast(Any, _Telemetry((observation("OL", 10),))),
            model=cast(Any, _Model()),
            publisher=cast(Any, _Publisher()),
            capture=capture,
            writer=writer,
            coordinator=cast(Any, _Coordinator()),
            store=second_store,
            recharge=recharge,
        ),
    )
    daemon.poll_once()
    drain(writer)

    active = second_store.work_registry().capture
    assert active is None

    events = second_store.sealed_event_projections(
        "2026-08-21T00:00:00Z",
        "2026-08-22T00:00:00Z",
        event_kind="recharge",
    )
    assert events == ()
    second_store.close()


def test_blackout_supersedes_recharge_before_blackout_start_is_durable(tmp_path: Path) -> None:
    sealed_before_blackout_start: list[int] = []
    store: MinimalJsonlEventStore

    def fault(stage: str) -> None:
        if stage != "after_start_append":
            return
        active = store.work_registry().capture
        if active is not None and active.event_kind == "blackout":
            sealed_before_blackout_start.append(
                len(
                    store.sealed_event_projections(
                        "2026-08-21T00:00:00Z",
                        "2026-08-22T00:00:00Z",
                        event_kind="recharge",
                    )
                )
            )

    store = MinimalJsonlEventStore(tmp_path)
    writer = CaptureWriter()
    recharge = RechargeCapture(store, writer)
    assert recharge.begin(observation("OL", 0), preceding_blackout_id=None)
    drain(writer)
    capture = BlackoutCapture(store, writer, on_power_restored=recharge.on_power_restored)
    daemon = MonitorDaemon(
        Config(model_dir=tmp_path),
        RuntimeDependencies(
            telemetry=cast(Any, _Telemetry((observation("OB DISCHRG", 1),))),
            model=cast(Any, _Model()),
            publisher=cast(Any, _Publisher()),
            capture=capture,
            writer=writer,
            coordinator=cast(Any, _Coordinator()),
            store=store,
            recharge=recharge,
        ),
    )
    daemon.poll_once()
    drain(writer)
    recharge_events = store.sealed_event_projections(
        "2026-08-21T00:00:00Z",
        "2026-08-22T00:00:00Z",
        event_kind="recharge",
    )
    assert len(recharge_events) == 1
    assert recharge_events[0].outcome is not None
    current = store.work_registry().capture
    assert current is not None
    assert current.event_kind == "blackout"
    store.close()


def test_restoration_link_retries_after_start_queue_rejection(tmp_path: Path) -> None:
    store = MinimalJsonlEventStore(tmp_path)
    writer = CaptureWriter()
    for _ in range(8):
        assert writer.submit(CaptureCommand(CaptureCommandKind.GAP, lambda: None))
    recharge = RechargeCapture(store, writer)
    recharge.on_power_restored("blackout-a")
    capture = BlackoutCapture(store, writer, on_power_restored=recharge.on_power_restored)
    daemon = MonitorDaemon(
        Config(model_dir=tmp_path),
        RuntimeDependencies(
            telemetry=cast(Any, _Telemetry((observation("OL", 1), observation("OL", 2)))),
            model=cast(Any, _Model()),
            publisher=cast(Any, _Publisher()),
            capture=capture,
            writer=writer,
            coordinator=cast(Any, _Coordinator()),
            store=store,
            recharge=recharge,
        ),
    )
    daemon.poll_once()
    assert store.work_registry().capture is None
    drain(writer)
    daemon.poll_once()
    drain(writer)
    active = store.work_registry().capture
    assert active is not None
    projection = store.project(EventRef(active.blackout_id, active.path_token))
    assert projection.start is not None
    assert projection.start.payload.get("preceding_blackout_id") is None
    store.close()
