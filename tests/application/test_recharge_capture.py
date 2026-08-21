from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

from src.adapters.minimal_jsonl import MinimalJsonlEventStore
from src.application.capture_writer import CaptureWriter
from src.application.recharge_capture import RechargeCapture, _restart_gap
from src.application.storage_values import (
    EventHandle,
    EventKind,
    EventProjection,
    EventRef,
    ProjectedEventRecord,
)
from src.domain.recharge import RechargeSamplingPolicy
from src.domain.values import PhysicalObservation


def observation(
    second: int,
    *,
    voltage: float = 12.3,
    status: str = "OL",
    battery_pct: float | None = None,
) -> PhysicalObservation:
    return PhysicalObservation(
        "boot-a",
        second * 1_000_000_000,
        datetime(2026, 8, 21, tzinfo=timezone.utc) + timedelta(seconds=second),
        status,
        f"{voltage:.2f}",
        voltage,
        0.01,
        20.0,
        230.0,
        battery_pct=battery_pct,
    )


def drain(writer: CaptureWriter) -> None:
    while writer.drain_one():
        pass


def active_projection(store: MinimalJsonlEventStore):
    capture = store.work_registry().capture
    assert capture is not None
    return store.project(EventRef(capture.blackout_id, capture.path_token))


def projected_event(
    event_kind: EventKind,
    blackout_id: str,
    *,
    end_second: int | None,
    termination: str = "power_restored",
    preceding_blackout_id: str | None = None,
) -> EventProjection:
    start = ProjectedEventRecord(
        "start",
        "physical",
        blackout_id,
        "segment-a",
        0,
        "boot-a",
        "2026-08-21T00:00:00Z",
        0,
        {"preceding_blackout_id": preceding_blackout_id},
        event_kind,
    )
    end = None
    records: tuple[ProjectedEventRecord, ...] = (start,)
    if end_second is not None:
        end = ProjectedEventRecord(
            "end",
            "physical",
            blackout_id,
            "segment-a",
            1,
            "boot-a",
            f"2026-08-21T00:00:{end_second:02d}Z",
            end_second * 1_000_000_000,
            {"termination": termination},
            event_kind,
        )
        records += (end,)
    return EventProjection(start, (), (), end, (), None, (), records)


def test_service_stop_does_not_invent_a_terminal_telemetry_state(tmp_path: Path) -> None:
    blackout_id = uuid4().hex
    with nullcontext(MinimalJsonlEventStore(tmp_path)) as store:
        writer = CaptureWriter()
        recharge = RechargeCapture(store, writer)
        assert recharge.begin(observation(0), preceding_blackout_id=blackout_id)
        drain(writer)
        assert recharge.observe(observation(1, voltage=12.4))
        drain(writer)
        assert recharge.service_stop(observation(2, voltage=12.5))
        drain(writer)
        events = store.sealed_event_projections(
            "2026-08-21T00:00:00Z",
            "2026-08-22T00:00:00Z",
            event_kind="recharge",
        )

    assert [path.name for path in tmp_path.joinpath("events").iterdir()] == ["telemetry.jsonl"]
    assert events == ()


def test_crash_restored_start_is_adopted_and_acknowledged(tmp_path: Path) -> None:
    crashed = [True]

    def fault(stage: str) -> None:
        if stage == "after_start_append" and crashed[0]:
            crashed[0] = False
            raise RuntimeError("simulated crash")

    store = MinimalJsonlEventStore(tmp_path)
    writer = CaptureWriter()
    recharge = RechargeCapture(store, writer)
    assert recharge.begin(observation(0), preceding_blackout_id=None)
    assert writer.drain_one()
    event = active_projection(store)
    assert event.start is not None
    assert event.start is not None
    assert recharge.service_stop(observation(1))
    drain(writer)
    store.close()


def test_restart_replays_exact_counts_and_stable_since(tmp_path: Path) -> None:
    first_store = MinimalJsonlEventStore(tmp_path)
    first_writer = CaptureWriter()
    first = RechargeCapture(first_store, first_writer)
    assert first.begin(observation(0), preceding_blackout_id=None)
    drain(first_writer)
    assert first.observe(observation(1))
    drain(first_writer)
    expected = len(active_projection(first_store).observations)
    first_store.close()

    second_store = MinimalJsonlEventStore(tmp_path)
    recovered = second_store.recover_startup()
    assert recovered is not None
    RechargeCapture(second_store, CaptureWriter(), recovered=recovered)
    assert len(second_store.project(recovered.handle).observations) == expected
    second_store.close()


def test_recovered_recharge_requires_new_stability_after_long_restart_gap(
    tmp_path: Path,
) -> None:
    policy = RechargeSamplingPolicy(
        dense_enrichment_interval_s=1.0,
        required_consecutive_stable_windows=2,
        minimum_stabilization_duration_s=1_800.0,
    )
    first_store = MinimalJsonlEventStore(tmp_path)
    first_writer = CaptureWriter()
    first = RechargeCapture(first_store, first_writer, policy=policy)
    assert first.begin(observation(0), preceding_blackout_id=None)
    drain(first_writer)
    assert first.observe(observation(1))
    drain(first_writer)
    first_store.close()

    second_store = MinimalJsonlEventStore(tmp_path)
    recovered = second_store.recover_startup()
    assert recovered is not None
    second_writer = CaptureWriter()
    second = RechargeCapture(second_store, second_writer, policy=policy, recovered=recovered)

    assert second.observe(observation(2_000))
    drain(second_writer)
    active = active_projection(second_store)
    assert active.outcome is None
    assert len(active.observations) == 3

    assert second.observe(observation(3_799))
    drain(second_writer)
    assert second_store.work_registry().capture is not None

    assert second.observe(observation(3_800))
    drain(second_writer)
    events = second_store.sealed_event_projections(
        "2026-08-21T00:00:00Z",
        "2026-08-22T00:00:00Z",
        event_kind="recharge",
    )
    assert events == ()
    assert second_store.work_registry().capture is not None
    second_store.close()


def test_unstable_window_resets_then_stabilizes_after_continuous_duration(
    tmp_path: Path,
) -> None:
    policy = RechargeSamplingPolicy(
        dense_enrichment_interval_s=1.0,
        required_consecutive_stable_windows=2,
        minimum_stabilization_duration_s=1_800.0,
    )
    with nullcontext(MinimalJsonlEventStore(tmp_path)) as store:
        writer = CaptureWriter()
        recharge = RechargeCapture(store, writer, policy=policy)
        assert recharge.begin(observation(0), preceding_blackout_id=None)
        drain(writer)
        assert recharge.observe(observation(1, voltage=12.6))
        drain(writer)
        assert len(active_projection(store).observations) == 2
        assert recharge.observe(observation(2, voltage=12.6))
        drain(writer)
        assert len(active_projection(store).observations) == 3
        assert recharge.observe(observation(1_801, voltage=12.6))
        drain(writer)
        events = store.sealed_event_projections(
            "2026-08-21T00:00:00Z",
            "2026-08-22T00:00:00Z",
            event_kind="recharge",
        )
        assert events == ()
        assert store.work_registry().capture is not None


def test_online_battery_pct_100_is_persisted_once_as_terminal_sample(tmp_path: Path) -> None:
    policy = RechargeSamplingPolicy(
        dense_enrichment_interval_s=300.0,
        sparse_enrichment_interval_s=300.0,
        backbone_interval_s=300.0,
    )
    with nullcontext(MinimalJsonlEventStore(tmp_path)) as store:
        writer = CaptureWriter()
        recharge = RechargeCapture(store, writer, policy=policy)
        assert recharge.begin(observation(0, battery_pct=99.0), preceding_blackout_id=None)
        drain(writer)

        assert recharge.observe(observation(1, status="OL", battery_pct=100.0))
        drain(writer)
        assert not recharge.observe(observation(2, battery_pct=100.0))

        events = store.sealed_event_projections(
            "2026-08-21T00:00:00Z",
            "2026-08-22T00:00:00Z",
            event_kind="recharge",
        )
        assert len(events) == 1
        assert events[0].outcome is not None
        assert events[0].end is not None
        assert events[0].end.payload["termination"] == "charge_complete"
        assert len(events[0].observations) == 2
        assert events[0].observations[-1].payload["battery_pct"] == 100.0


def test_new_blackout_supersedes_recharge_before_new_event_start(tmp_path: Path) -> None:
    with nullcontext(MinimalJsonlEventStore(tmp_path)) as store:
        writer = CaptureWriter()
        recharge = RechargeCapture(store, writer)
        assert recharge.begin(observation(0), preceding_blackout_id=None)
        drain(writer)
        assert recharge.supersede_by_blackout(observation(1, status="OB DISCHRG"), blackout_id=None)
        drain(writer)
        events = tuple(
            store.sealed_event_projections(
                "2026-08-21T00:00:00Z", "2026-08-22T00:00:00Z", event_kind="recharge"
            )
        )
    assert len(events) == 1
    assert events[0].end is not None
    assert events[0].end.payload["termination"] == "superseded_by_blackout"


def test_reconcile_restart_uses_newest_unlinked_restoration() -> None:
    store = Mock()
    store.open.return_value = EventHandle("newest", "segment-a", "telemetry", 1, "recharge")
    writer = CaptureWriter()
    recharge = RechargeCapture(store, writer)
    linked = projected_event("blackout", "linked", end_second=2)
    older = projected_event("blackout", "older", end_second=3)
    newest = projected_event("blackout", "newest", end_second=9)
    recharge_link = projected_event(
        "recharge",
        "recharge-a",
        end_second=None,
        preceding_blackout_id="linked",
    )

    assert recharge.reconcile_restart(
        observation(10),
        (linked, older, newest, recharge_link),
    )
    drain(writer)

    start = store.open.call_args.args[0]
    assert start.payload["preceding_blackout_id"] == "newest"
    assert start.payload["restart_gap"] == {
        "kind": "restart_before_recharge_start",
        "from_utc": "2026-08-21T00:00:09Z",
        "to_utc": "2026-08-21T00:00:10Z",
        "reason": "process restarted before recharge start was durable",
        "science_usable": False,
    }


def test_reconcile_restart_refuses_when_state_or_history_is_not_ready(tmp_path: Path) -> None:
    store = MinimalJsonlEventStore(tmp_path)
    writer = CaptureWriter()
    recharge = RechargeCapture(store, writer)
    candidate = projected_event("blackout", "blackout-a", end_second=1)

    assert not recharge.reconcile_restart(observation(2), ())
    recharge.on_power_restored("pending")
    assert not recharge.reconcile_restart(observation(2), (candidate,))
    recharge.acknowledge_pending_restoration("pending")
    assert recharge.begin(observation(0), preceding_blackout_id=None)
    assert recharge.reconcile_restart(observation(2), (candidate,))
    store.close()


def test_reconcile_restart_ignores_non_restorations_and_incomplete_projections(
    tmp_path: Path,
) -> None:
    store = MinimalJsonlEventStore(tmp_path)
    writer = CaptureWriter()
    recharge = RechargeCapture(store, writer)
    projections = (
        projected_event("blackout", "not-ended", end_second=None),
        projected_event("blackout", "not-restored", end_second=1, termination="unknown"),
        projected_event("recharge", "missing-start", end_second=2),
    )

    assert not recharge.reconcile_restart(observation(3), projections)
    store.close()


@pytest.mark.parametrize(
    "value",
    [
        1,
        {},
        {"kind": "wrong"},
        {
            "kind": "restart_before_recharge_start",
            "from_utc": 1,
            "to_utc": "2026-08-21T00:00:10Z",
            "reason": "restart",
            "science_usable": False,
        },
        {
            "kind": "restart_before_recharge_start",
            "from_utc": "2026-08-21T00:00:01Z",
            "to_utc": "2026-08-21T00:00:10Z",
            "reason": "restart",
            "science_usable": True,
        },
    ],
)
def test_restart_gap_rejects_malformed_payload(value: object) -> None:
    with pytest.raises(ValueError, match="restart gap"):
        _restart_gap(value)


def test_restart_gap_accepts_explicit_interval() -> None:
    assert _restart_gap(
        {
            "kind": "restart_before_recharge_start",
            "from_utc": "2026-08-21T00:00:01Z",
            "to_utc": "2026-08-21T00:00:10Z",
            "reason": "restart",
            "science_usable": False,
        }
    ) == (
        "2026-08-21T00:00:01Z",
        "2026-08-21T00:00:10Z",
        "restart",
    )
