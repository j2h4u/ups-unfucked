from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from src.adapters.jsonl_event_store import JsonlEventStore
from src.application.capture_writer import CaptureWriter
from src.application.recharge_capture import RechargeCapture, _restart_gap
from src.application.storage_values import EventRef
from src.domain.recharge import RechargeAssessmentKind, RechargeSamplingPolicy
from src.domain.values import PhysicalObservation


def observation(second: int, *, voltage: float = 12.3, status: str = "OL") -> PhysicalObservation:
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
    )


def drain(writer: CaptureWriter) -> None:
    while writer.drain_one():
        pass


def active_projection(store: JsonlEventStore):
    capture = store.work_registry().capture
    assert capture is not None
    return store.project(EventRef(capture.blackout_id, capture.path_token))


def test_recharge_is_one_ordinary_event_with_terminal_outcome(tmp_path: Path) -> None:
    blackout_id = uuid4().hex
    with JsonlEventStore(tmp_path) as store:
        writer = CaptureWriter()
        recharge = RechargeCapture(store, writer)
        assert recharge.begin(observation(0), preceding_blackout_id=blackout_id)
        drain(writer)
        assert recharge.observe(observation(1, voltage=12.4))
        drain(writer)
        assert recharge.service_stop(observation(2, voltage=12.5))
        drain(writer)
        event = next(
            iter(
                store.sealed_event_projections(
                    "2026-08-21T00:00:00Z",
                    "2026-08-22T00:00:00Z",
                    event_kind="recharge",
                )
            )
        )

    assert not tuple(tmp_path.joinpath("events").glob("rch-*.jsonl"))
    assert [record.record_type for record in event.records] == [
        "start",
        "observation",
        "end",
        "outcome",
    ]
    assert all(record.event_kind == "recharge" for record in event.records)
    assert event.outcome is not None
    assert event.outcome.payload["assessment"]["kind"] == RechargeAssessmentKind.DIAGNOSTIC.value


def test_crash_restored_start_is_adopted_and_acknowledged(tmp_path: Path) -> None:
    crashed = [True]

    def fault(stage: str) -> None:
        if stage == "after_start_append" and crashed[0]:
            crashed[0] = False
            raise RuntimeError("simulated crash")

    store = JsonlEventStore(tmp_path, fault_hook=fault)
    writer = CaptureWriter()
    recharge = RechargeCapture(store, writer)
    assert recharge.begin(observation(0), preceding_blackout_id=None)
    assert writer.drain_one()
    event = active_projection(store)
    assert event.start is not None
    assert event.start.payload["recharge_state"]["persisted_samples"] == 1
    assert event.start.payload["recharge_state"]["stable_since_utc"] == "2026-08-21T00:00:00Z"
    assert recharge.service_stop(observation(1))
    drain(writer)
    store.close()


def test_restart_replays_exact_counts_and_stable_since(tmp_path: Path) -> None:
    first_store = JsonlEventStore(tmp_path)
    first_writer = CaptureWriter()
    first = RechargeCapture(first_store, first_writer)
    assert first.begin(observation(0), preceding_blackout_id=None)
    drain(first_writer)
    assert first.observe(observation(1))
    drain(first_writer)
    expected = active_projection(first_store).records[-1].payload["recharge_state"]
    first_store.close()

    second_store = JsonlEventStore(tmp_path)
    recovered = second_store.recover_startup()
    assert recovered is not None
    RechargeCapture(second_store, CaptureWriter(), recovered=recovered)
    assert recovered.last_observation.payload["recharge_state"] == expected
    second_store.close()


def test_recovered_recharge_requires_new_stability_after_long_restart_gap(
    tmp_path: Path,
) -> None:
    policy = RechargeSamplingPolicy(
        dense_enrichment_interval_s=1.0,
        required_consecutive_stable_windows=2,
        minimum_stabilization_duration_s=1_800.0,
    )
    first_store = JsonlEventStore(tmp_path)
    first_writer = CaptureWriter()
    first = RechargeCapture(first_store, first_writer, policy=policy)
    assert first.begin(observation(0), preceding_blackout_id=None)
    drain(first_writer)
    assert first.observe(observation(1))
    drain(first_writer)
    first_store.close()

    second_store = JsonlEventStore(tmp_path)
    recovered = second_store.recover_startup()
    assert recovered is not None
    second_writer = CaptureWriter()
    second = RechargeCapture(second_store, second_writer, policy=policy, recovered=recovered)

    assert second.observe(observation(2_000))
    drain(second_writer)
    active = active_projection(second_store)
    assert active.outcome is None
    assert active.records[-1].payload["recharge_state"]["stable_since_utc"] == (
        "2026-08-21T00:33:20Z"
    )
    assert active.records[-1].payload["recharge_state"]["observed_samples"] == 3

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
    assert len(events) == 1
    assert events[0].outcome is not None
    assert events[0].outcome.payload["assessment"]["kind"] == RechargeAssessmentKind.USABLE.value
    assert events[0].outcome.payload["continuity_gap"] is True
    assert events[0].outcome.payload["restart_gap"]["from_utc"] == ("2026-08-21T00:00:01Z")
    second_store.close()


def test_unstable_window_resets_then_stabilizes_after_continuous_duration(
    tmp_path: Path,
) -> None:
    policy = RechargeSamplingPolicy(
        dense_enrichment_interval_s=1.0,
        required_consecutive_stable_windows=2,
        minimum_stabilization_duration_s=1_800.0,
    )
    with JsonlEventStore(tmp_path) as store:
        writer = CaptureWriter()
        recharge = RechargeCapture(store, writer, policy=policy)
        assert recharge.begin(observation(0), preceding_blackout_id=None)
        drain(writer)
        assert recharge.observe(observation(1, voltage=12.6))
        drain(writer)
        assert (
            active_projection(store).records[-1].payload["recharge_state"]["stable_since_utc"]
            is None
        )
        assert recharge.observe(observation(2, voltage=12.6))
        drain(writer)
        assert active_projection(store).records[-1].payload["recharge_state"][
            "stable_since_utc"
        ] == ("2026-08-21T00:00:01Z")
        assert recharge.observe(observation(1_801, voltage=12.6))
        drain(writer)
        events = store.sealed_event_projections(
            "2026-08-21T00:00:00Z",
            "2026-08-22T00:00:00Z",
            event_kind="recharge",
        )
        assert len(events) == 1
        assert events[0].outcome is not None
        assert (
            events[0].outcome.payload["assessment"]["kind"] == RechargeAssessmentKind.USABLE.value
        )


def test_new_blackout_supersedes_recharge_before_new_event_start(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
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
