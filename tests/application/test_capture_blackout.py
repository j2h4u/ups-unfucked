import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from typing import cast

import pytest

import src.application.capture_blackout as capture_module
from src.adapters.jsonl_event_store import JsonlEventStore
from src.application.capture_blackout import BlackoutCapture, RuntimeErrorBoundary
from src.application.capture_writer import (
    LIFECYCLE_CAPACITY,
    CaptureCommand,
    CaptureCommandKind,
    CaptureWriter,
)
from src.application.ports import CaptureRecoveryEventStorePort
from src.application.startup_recovery import recover_startup_metadata
from src.application.storage_values import (
    CaptureCloseReconciliation,
    CaptureCloseState,
    EventHandle,
    EventRecord,
    EventRef,
    EventStart,
    RecoveredCapture,
    RecoveredObservation,
)
from src.battery_math.lut import LutPoint
from src.domain.lifecycle import UNKNOWN_PRELUDE_GAP_REASON, LifecycleState
from src.domain.reasons import order_reasons
from src.domain.values import ChargeReadiness, FrozenModelSnapshot, PhysicalObservation


class Store:
    def __init__(self):
        self.starts = []
        self.records = []
        self.checkpoints = []

    def open(self, start):
        self.starts.append(start)
        return EventHandle(start.blackout_id, start.segment_id, "event.jsonl", 1, "a" * 64)

    def append(self, handle, record):
        self.records.append(record)
        return replace(handle, next_seq=handle.next_seq + 1, last_record_sha256="b" * 64)

    def checkpoint_processing(self, handle, frozen_stage):
        self.checkpoints.append((handle, frozen_stage))

    def recover_startup(self):
        return None

    def reconcile_damaged_close(self, _blackout_id, current_handle):
        return CaptureCloseReconciliation(CaptureCloseState.UNKNOWN, current_handle)

    def acknowledge_capture_recovery(self):
        return None


class FailOnceStore(Store):
    def __init__(self, *, fail_open: bool = False, fail_record_type: str | None = None):
        super().__init__()
        self.fail_open = fail_open
        self.fail_record_type = fail_record_type
        self.open_attempts = 0

    def open(self, start):
        self.open_attempts += 1
        if self.fail_open:
            self.fail_open = False
            raise OSError("open failed")
        return super().open(start)

    def append(self, handle, record):
        if record.record_type == self.fail_record_type:
            self.fail_record_type = None
            raise OSError(f"{record.record_type} failed")
        return super().append(handle, record)


class BrokenAppendStore(Store):
    def append(self, handle, record):
        raise OSError(f"{record.record_type} failed")


class BlockingOpenStore(Store):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def open(self, start):
        self.entered.set()
        assert self.release.wait(timeout=1.0)
        return super().open(start)


def capture_for(store: Store, writer: CaptureWriter) -> BlackoutCapture:
    return BlackoutCapture(cast(CaptureRecoveryEventStorePort, store), writer)


def observation(status: str, second: int, *, boot_id: str = "boot-a") -> PhysicalObservation:
    return PhysicalObservation(
        boot_id=boot_id,
        monotonic_ns=second * 1_000_000_000,
        wall_time_utc=datetime(2026, 8, 16, 1, 0, second, tzinfo=timezone.utc),
        raw_status=status,
        battery_voltage_raw="12.30",
        battery_voltage_v=12.3,
        voltage_token_quantum_v=0.01,
        load_percent=20.0,
        input_voltage_v=0.0 if "OB" in status else 230.0,
    )


def snapshot() -> FrozenModelSnapshot:
    return FrozenModelSnapshot(
        "2",
        "1",
        "c" * 32,
        "d" * 64,
        7.2,
        12.0,
        510.0,
        1.0,
        1.2,
        0.015,
        0.0,
        (LutPoint(13.7, 1.0, "standard"), LutPoint(10.8, 0.0, "anchor")),
    )


READINESS = ChargeReadiness(False, 0.0, None, order_reasons(()))


def drain(writer: CaptureWriter) -> None:
    while writer.drain_one():
        pass


def test_first_observation_lives_only_in_start_then_event_closes() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)

    capture.accept_after_safety_publish(
        observation("OB DISCHRG", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    capture.accept_after_safety_publish(
        observation("OB DISCHRG", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    capture.accept_after_safety_publish(
        observation("OL", 2), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    assert store.starts == []
    assert store.records == []
    drain(writer)

    assert len(store.starts) == 1
    assert store.starts[0].payload["observation"]["monotonic_ns"] == 0
    assert [record.record_type for record in store.records] == ["observation", "end"]
    assert store.records[0].monotonic_ns == 1_000_000_000


def test_missing_input_voltage_is_still_a_real_blackout_start() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    raw = replace(observation("OB DISCHRG", 0), input_voltage_v=None)

    assert capture.accept_after_safety_publish(
        raw, safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert len(store.starts) == 1


def test_unknown_low_line_voltage_prelude_is_gapped_before_real_observation() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    unknown = replace(observation("COMMFAULT", 0), input_voltage_v=0.0)

    assert capture.accept_after_safety_publish(
        unknown, safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    assert capture.accept_after_safety_publish(
        observation("OB DISCHRG", 1),
        safety_snapshot=snapshot(),
        charge_readiness=READINESS,
    )
    assert capture.accept_after_safety_publish(
        observation("OL", 2), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)

    assert len(store.starts) == 1
    assert store.starts[0].payload["observation"]["raw_status"] == "COMMFAULT"
    assert [record.record_type for record in store.records] == [
        "gap",
        "observation",
        "end",
    ]
    assert store.records[0].payload["reason"] == UNKNOWN_PRELUDE_GAP_REASON


def test_unknown_healthy_line_voltage_does_not_start_capture() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)

    assert capture.accept_after_safety_publish(
        observation("COMMFAULT", 0),
        safety_snapshot=snapshot(),
        charge_readiness=READINESS,
    )
    drain(writer)

    assert store.starts == []
    assert store.records == []


def test_boot_change_records_gap_before_new_boot_observation() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    capture.accept_after_safety_publish(
        observation("OB", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    capture.accept_after_safety_publish(
        observation("OB", 1, boot_id="boot-b"),
        safety_snapshot=snapshot(),
        charge_readiness=READINESS,
    )
    drain(writer)
    assert [record.record_type for record in store.records] == ["gap", "observation"]


def test_unknown_status_inside_blackout_is_durable_not_silently_dropped() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    capture.accept_after_safety_publish(
        observation("OB", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    capture.accept_after_safety_publish(
        observation("WAIT", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    capture.accept_after_safety_publish(
        observation("OL", 2), safety_snapshot=snapshot(), charge_readiness=READINESS
    )

    drain(writer)

    assert [record.record_type for record in store.records] == ["observation", "end"]
    assert store.records[0].payload["raw_status"] == "WAIT"


def test_observation_transition_and_durable_end_share_one_state_lock(monkeypatch) -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert capture.accept_after_safety_publish(
        observation("OL", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )

    original = capture_module.advance_lifecycle
    transition_entered = Event()
    release_transition = Event()

    def controlled_advance(state, current, signal):
        if current is not None and current.monotonic_ns == 2_000_000_000:
            transition_entered.set()
            assert release_transition.wait(timeout=1.0)
        return original(state, current, signal)

    monkeypatch.setattr(capture_module, "advance_lifecycle", controlled_advance)
    accepted: list[bool] = []
    poll_thread = Thread(
        target=lambda: accepted.append(
            capture.accept_after_safety_publish(
                observation("OB", 2),
                safety_snapshot=snapshot(),
                charge_readiness=READINESS,
            )
        )
    )
    durable_thread = Thread(target=lambda: capture.mark_end_durable(observation("OL", 1)))
    poll_thread.start()
    assert transition_entered.wait(timeout=1.0)
    durable_thread.start()
    release_transition.set()
    poll_thread.join(timeout=1.0)
    durable_thread.join(timeout=1.0)
    assert not poll_thread.is_alive()
    assert not durable_thread.is_alive()
    drain(writer)
    assert accepted == [False]
    assert len(store.starts) == 1


def test_new_blackout_while_prior_end_is_pending_is_recovered_with_gap() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert capture.accept_after_safety_publish(
        observation("OL", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    assert not capture.accept_after_safety_publish(
        observation("OB", 2), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 3), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)

    assert len(store.starts) == 2
    assert store.starts[1].monotonic_ns == 2_000_000_000
    assert [record.record_type for record in store.records] == [
        "end",
        "gap",
        "observation",
    ]
    assert store.records[1].payload["reason"] == "capture_unavailable_after_blackout_start"


def test_complete_second_blackout_before_prior_end_durable_is_still_recorded() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert capture.accept_after_safety_publish(
        observation("OL", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    assert not capture.accept_after_safety_publish(
        observation("OB", 2), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    assert capture.accept_after_safety_publish(
        observation("OL", 3), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert capture.accept_after_safety_publish(
        observation("OL", 4), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)

    assert len(store.starts) == 2
    assert store.starts[1].monotonic_ns == 2_000_000_000
    assert [record.record_type for record in store.records] == ["end", "gap", "end"]


def test_pending_second_blackout_survives_prior_end_execution_failure() -> None:
    store = FailOnceStore()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    store.fail_record_type = "end"
    assert capture.accept_after_safety_publish(
        observation("OL", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    assert not capture.accept_after_safety_publish(
        observation("OB", 2), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    assert capture.accept_after_safety_publish(
        observation("OL", 3), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert capture.accept_after_safety_publish(
        observation("OL", 4), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)

    assert len(store.starts) == 2
    assert store.starts[1].monotonic_ns == 2_000_000_000
    assert [record.record_type for record in store.records] == [
        "gap",
        "end",
        "gap",
        "end",
    ]


def test_service_stop_durably_closes_blackout_retained_behind_prior_end() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert capture.accept_after_safety_publish(
        observation("OL", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    assert not capture.accept_after_safety_publish(
        observation("OB", 2), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    assert capture.service_stop(observation("OL", 3))
    drain(writer)

    assert len(store.starts) == 2
    assert store.starts[1].monotonic_ns == 2_000_000_000
    assert [record.record_type for record in store.records] == ["end", "gap", "end"]
    assert store.records[-1].payload == {"termination": "service_stop"}


def test_service_stop_closes_retained_blackout_after_prior_end_is_already_durable() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert capture.accept_after_safety_publish(
        observation("OL", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    assert not capture.accept_after_safety_publish(
        observation("OB", 2), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert capture.service_stop(observation("OL", 3))
    drain(writer)

    assert len(store.starts) == 2
    assert store.starts[1].monotonic_ns == 2_000_000_000
    assert [record.record_type for record in store.records] == ["end", "gap", "end"]


def test_service_stop_retries_retained_gap_before_end_after_queue_rejection() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)

    for _ in range(120):
        assert writer.submit(CaptureCommand(CaptureCommandKind.OBSERVATION, lambda: None))
    for _ in range(LIFECYCLE_CAPACITY):
        assert writer.submit(CaptureCommand(CaptureCommandKind.GAP, lambda: None))

    assert not capture.accept_after_safety_publish(
        observation("OB", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    assert not capture.service_stop(observation("OL", 2))
    drain(writer)

    assert capture.service_stop(observation("OL", 2))
    drain(writer)

    assert [record.record_type for record in store.records] == ["gap", "end"]
    assert store.records[0].monotonic_ns == 1_000_000_000
    assert store.records[0].payload["reason"] == "observation_queue_overflow"
    assert store.records[1].payload == {"termination": "service_stop"}


def test_rejected_end_then_new_blackout_preserves_two_event_boundaries() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    for _ in range(LIFECYCLE_CAPACITY):
        assert writer.submit(CaptureCommand(CaptureCommandKind.GAP, lambda: None))

    assert not capture.accept_after_safety_publish(
        observation("OL", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    assert not capture.accept_after_safety_publish(
        observation("OB", 2), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert not capture.accept_after_safety_publish(
        observation("OB", 3), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 4), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)

    assert len(store.starts) == 2
    assert store.starts[1].monotonic_ns == 2_000_000_000
    assert [record.record_type for record in store.records] == [
        "end",
        "gap",
        "observation",
    ]
    assert store.records[0].monotonic_ns == 1_000_000_000


def test_rejected_observation_and_gap_cannot_be_bypassed_by_later_observation() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    for _ in range(120):
        assert writer.submit(CaptureCommand(CaptureCommandKind.OBSERVATION, lambda: None))
    for _ in range(LIFECYCLE_CAPACITY):
        assert writer.submit(CaptureCommand(CaptureCommandKind.GAP, lambda: None))

    assert not capture.accept_after_safety_publish(
        observation("OB", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert not capture.accept_after_safety_publish(
        observation("OB", 2), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 3), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)

    assert [record.record_type for record in store.records] == ["gap", "observation"]
    assert store.records[0].payload["reason"] == "observation_queue_overflow"


def test_failed_start_submission_recovers_original_start_with_explicit_gap() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    for _ in range(LIFECYCLE_CAPACITY):
        assert writer.submit(CaptureCommand(CaptureCommandKind.GAP, lambda: None))

    assert not capture.accept_after_safety_publish(
        observation("OB", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert len(store.starts) == 1
    assert store.starts[0].monotonic_ns == 0
    assert [record.record_type for record in store.records] == ["gap", "observation"]
    assert store.records[0].payload["reason"] == "capture_unavailable_after_blackout_start"


def test_failed_end_submission_is_retried_on_next_online_poll() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    for _ in range(LIFECYCLE_CAPACITY):
        assert writer.submit(CaptureCommand(CaptureCommandKind.GAP, lambda: None))

    assert not capture.accept_after_safety_publish(
        observation("OL", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert capture.accept_after_safety_publish(
        observation("OL", 2), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert [record.record_type for record in store.records] == ["end"]


def test_failed_start_execution_retries_original_start_with_explicit_gap() -> None:
    store = FailOnceStore(fail_open=True)
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    assert capture.accept_after_safety_publish(
        observation("OB", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )

    drain(writer)

    assert writer.health().discarded_command_count == 1
    assert capture.accept_after_safety_publish(
        observation("OB", 2), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert store.open_attempts == 2
    assert len(store.starts) == 1
    assert store.starts[0].monotonic_ns == 0
    assert [record.record_type for record in store.records] == ["gap", "observation"]


def test_capture_unavailable_mid_blackout_writes_original_start_and_gap() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)

    capture.note_capture_unavailable(observation("OB", 0), snapshot(), READINESS)
    assert capture.accept_after_safety_publish(
        observation("OB", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert capture.accept_after_safety_publish(
        observation("WAIT", 2), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    assert capture.accept_after_safety_publish(
        observation("OL", 3), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)

    assert len(store.starts) == 1
    assert store.starts[0].monotonic_ns == 0
    assert [record.record_type for record in store.records] == [
        "gap",
        "observation",
        "observation",
        "end",
    ]


def test_failed_observation_execution_closes_capture_damaged_and_discards_end() -> None:
    store = FailOnceStore()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    store.fail_record_type = "observation"
    assert capture.accept_after_safety_publish(
        observation("OB", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    assert capture.accept_after_safety_publish(
        observation("OL", 2), safety_snapshot=snapshot(), charge_readiness=READINESS
    )

    drain(writer)

    assert [record.record_type for record in store.records] == ["gap", "end"]
    assert store.records[0].payload == {
        "reason": "observation_execution_failure",
        "failed_command": "observation",
        "error_type": "OSError",
    }
    assert store.records[1].payload == {"termination": "capture_damaged"}
    assert [stage for _handle, stage in store.checkpoints] == ["capture_damaged"]
    assert not writer.health().capture_available
    assert writer.health().discarded_command_count == 1


def test_failed_end_execution_closes_capture_damaged_in_registry() -> None:
    store = FailOnceStore()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    store.fail_record_type = "end"
    assert capture.accept_after_safety_publish(
        observation("OL", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )

    drain(writer)

    assert [record.record_type for record in store.records] == ["gap", "end"]
    assert store.records[0].payload["reason"] == "end_execution_failure"
    assert store.records[1].payload == {"termination": "capture_damaged"}
    assert [stage for _handle, stage in store.checkpoints] == ["capture_damaged"]
    assert not capture.has_unacknowledged_capture
    assert capture.accept_after_safety_publish(
        observation("OB", 2), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert len(store.starts) == 2
    assert store.starts[1].monotonic_ns == 2_000_000_000


def test_full_processing_fifo_rejects_one_capture_then_next_blackout_is_isolated(
    tmp_path: Path,
) -> None:
    with JsonlEventStore(tmp_path) as store:
        for number in range(8):
            blackout_id = uuid.UUID(int=number + 500, version=4).hex
            segment_id = uuid.UUID(int=number + 600, version=4).hex
            handle = store.open(
                EventStart(
                    blackout_id,
                    segment_id,
                    "boot-a",
                    f"2026-08-16T10:{number:02d}:00.000000Z",
                    number * 2,
                    {},
                )
            )
            store.append(
                handle,
                EventRecord(
                    "end",
                    "boot-a",
                    f"2026-08-16T10:{number:02d}:01.000000Z",
                    number * 2 + 1,
                    {"termination": "power_restored"},
                    "physical",
                ),
            )

        writer = CaptureWriter()
        capture = BlackoutCapture(store, writer)
        assert capture.accept_after_safety_publish(
            observation("OB", 10), safety_snapshot=snapshot(), charge_readiness=READINESS
        )
        drain(writer)
        ninth_ref = store.work_registry().capture
        assert ninth_ref is not None

        assert capture.accept_after_safety_publish(
            observation("OB", 11), safety_snapshot=snapshot(), charge_readiness=READINESS
        )
        assert capture.accept_after_safety_publish(
            observation("OL", 12), safety_snapshot=snapshot(), charge_readiness=READINESS
        )
        drain(writer)

        assert capture._lifecycle_state == LifecycleState.IDLE
        assert not capture._session.active
        assert not capture.has_unacknowledged_capture
        assert store.work_registry().capture is None
        assert len(store.work_registry().pending_processing) == 8
        rejected = store.project(EventRef(ninth_ref.blackout_id, ninth_ref.path_token))
        assert [record.record_type for record in rejected.records] == [
            "start",
            "observation",
            "end",
            "outcome",
        ]
        assert rejected.outcome is not None
        assert rejected.outcome.payload["disposition"] == "rejected"
        assert rejected.outcome.payload["reasons"] == ["processing_backlog_full"]
        assert store.index_tail(1)[0].blackout_id == ninth_ref.blackout_id
        assert not writer.health().capture_available
        assert "ProcessingBacklogFullError" in (writer.health().bounded_error or "")

        assert capture.accept_after_safety_publish(
            observation("OB", 20), safety_snapshot=snapshot(), charge_readiness=READINESS
        )
        drain(writer)
        tenth_ref = store.work_registry().capture
        assert tenth_ref is not None
        assert tenth_ref.blackout_id != ninth_ref.blackout_id
        assert capture.accept_after_safety_publish(
            observation("OB", 21), safety_snapshot=snapshot(), charge_readiness=READINESS
        )
        drain(writer)

        captured = store.project(EventRef(tenth_ref.blackout_id, tenth_ref.path_token))
        assert [record.record_type for record in captured.records] == ["start", "observation"]
        assert captured.start is not None
        assert captured.start.payload["observation"]["monotonic_ns"] == 20_000_000_000
        assert captured.observations[0].payload["monotonic_ns"] == 21_000_000_000


def test_unrecoverable_append_failure_cannot_cross_into_a_new_event() -> None:
    store = BrokenAppendStore()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert capture.accept_after_safety_publish(
        observation("OB", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    assert capture.accept_after_safety_publish(
        observation("OL", 2), safety_snapshot=snapshot(), charge_readiness=READINESS
    )

    drain(writer)

    health = writer.health()
    assert health.discarded_command_count == 1
    assert "terminal_recovery_failed OSError: gap failed" in (health.bounded_error or "")
    assert not capture.has_unacknowledged_capture
    assert not capture.accept_after_safety_publish(
        observation("OL", 3), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    assert not capture.accept_after_safety_publish(
        observation("OB", 3), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    drain(writer)
    assert len(store.starts) == 1
    assert store.records == []


def test_sticky_timeout_does_not_resurrect_inflight_start() -> None:
    store = BlockingOpenStore()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    assert capture.accept_after_safety_publish(
        observation("OB DISCHRG", 0), safety_snapshot=snapshot(), charge_readiness=READINESS
    )
    writer.start()
    assert store.entered.wait(timeout=1.0)

    assert capture.expire_sticky_recovery("sticky recovery deadline exceeded")
    store.release.set()
    writer.stop(drain=True)

    assert not capture.has_unacknowledged_capture
    assert not capture.accept_after_safety_publish(
        observation("OB DISCHRG", 1), safety_snapshot=snapshot(), charge_readiness=READINESS
    )


def test_recovered_end_failure_uses_terminal_reset_without_claiming_durability() -> None:
    store = BrokenAppendStore()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    recovered = RecoveredCapture(
        handle=EventHandle("b" * 32, "c" * 32, "event.jsonl", 1, "a" * 64),
        last_boot_id="boot-a",
        last_observation=RecoveredObservation(
            boot_id="boot-a",
            wall_time_utc="2026-08-16T01:00:00.000000Z",
            monotonic_ns=1_000_000_000,
            payload={},
        ),
    )

    assert capture.close_recovered_capture(recovered)
    drain(writer)

    assert store.records == []
    assert store.checkpoints == []
    assert not capture.has_unacknowledged_capture
    assert not capture.close_recovered_capture(recovered)
    health = writer.health()
    assert not health.capture_available
    assert "OSError: end failed" in (health.bounded_error or "")
    assert "terminal_recovery_failed OSError: gap failed" in (health.bounded_error or "")


def test_recovered_attach_conflict_is_a_poll_boundary() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)
    recovered = RecoveredCapture(
        handle=EventHandle("b" * 32, "c" * 32, "event.jsonl", 1, "a" * 64),
        last_boot_id="boot-a",
        last_observation=RecoveredObservation(
            boot_id="boot-a",
            wall_time_utc="2026-08-16T01:00:00.000000Z",
            monotonic_ns=1_000_000_000,
            payload={},
        ),
    )

    assert capture.accept_after_safety_publish(
        observation("OB DISCHRG", 0),
        safety_snapshot=snapshot(),
        charge_readiness=READINESS,
    )
    with pytest.raises(RuntimeErrorBoundary, match="already active"):
        capture.attach_recovered_capture(recovered.handle, boot_id="boot-a")
    writer.stop(drain=False)


def test_missing_prestart_recovery_scope_is_a_poll_boundary() -> None:
    store = Store()
    writer = CaptureWriter()
    capture = capture_for(store, writer)

    with pytest.raises(RuntimeErrorBoundary, match="no retained start"):
        capture._submit_prestart_recovery(  # pyright: ignore[reportPrivateUsage]
            observation("OB DISCHRG", 0)
        )


def test_restart_attach_continues_same_event_then_online_ends_it(tmp_path: Path) -> None:
    first_store = JsonlEventStore(tmp_path)
    first_writer = CaptureWriter()
    first_capture = BlackoutCapture(first_store, first_writer)
    assert first_capture.accept_after_safety_publish(
        observation("OB", 0),
        safety_snapshot=snapshot(),
        charge_readiness=READINESS,
    )
    drain(first_writer)
    original = first_store.work_registry().capture
    assert original is not None
    first_store.close()

    second_store = JsonlEventStore(tmp_path)
    try:
        recovery = recover_startup_metadata(second_store)
        assert recovery.recovered_capture is not None
        second_writer = CaptureWriter()
        second_capture = BlackoutCapture(second_store, second_writer)
        second_capture.attach_recovered_capture(
            recovery.recovered_capture.handle,
            boot_id="boot-a",
        )

        assert second_capture.accept_after_safety_publish(
            observation("OB", 1),
            safety_snapshot=snapshot(),
            charge_readiness=READINESS,
        )
        assert second_capture.accept_after_safety_publish(
            observation("OL", 2),
            safety_snapshot=snapshot(),
            charge_readiness=READINESS,
        )
        drain(second_writer)

        registry = second_store.work_registry()
        assert registry.capture is None
        assert len(registry.pending_processing) == 1
        pending = registry.pending_processing[0]
        assert pending.blackout_id == original.blackout_id
        projection = second_store.project(EventRef(pending.blackout_id, pending.final_path_token))
        assert projection.start is not None
        assert len(projection.observations) == 1
        assert projection.end is not None
        assert len(tuple((tmp_path / "events").glob("evt-*.jsonl"))) == 1
    finally:
        second_store.close()


def test_failed_start_after_registry_prepare_recovers_in_process(tmp_path: Path) -> None:
    armed = True

    def crash_once(stage: str) -> None:
        nonlocal armed
        if armed and stage == "after_registry_prepare":
            armed = False
            raise RuntimeError("injected start crash")

    store = JsonlEventStore(tmp_path, fault_hook=crash_once)
    try:
        writer = CaptureWriter()
        capture = BlackoutCapture(store, writer)
        assert capture.accept_after_safety_publish(
            observation("OB", 0),
            safety_snapshot=snapshot(),
            charge_readiness=READINESS,
        )
        assert capture.accept_after_safety_publish(
            observation("OB", 1),
            safety_snapshot=snapshot(),
            charge_readiness=READINESS,
        )

        drain(writer)

        registry = store.work_registry()
        assert registry.capture is not None
        recovered = store.recover_startup()
        assert recovered is not None
        projection = store.project(EventRef(recovered.blackout_id, recovered.path_token))
        assert projection.start is not None
        assert len(projection.observations) == 1
        assert writer.health().discarded_command_count == 0
    finally:
        store.close()
