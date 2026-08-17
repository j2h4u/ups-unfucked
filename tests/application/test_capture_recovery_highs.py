from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import cast

import pytest

from src.adapters.jsonl_event_store import JsonlEventStore
from src.application.capture_blackout import BlackoutCapture
from src.application.capture_storage_commands import CaptureStart
from src.application.capture_writer import (
    CaptureCommand,
    CaptureCommandKind,
    CaptureWriter,
    RecoveryDisposition,
)
from src.application.ports import CaptureRecoveryEventStorePort
from src.application.prestart_loss import (
    PRESTART_BOUNDARY_CAPACITY,
    PRESTART_LOSS_REASON,
    OverflowDeliveryOutcome,
    OverflowDeliveryReservation,
    OverflowDeliveryState,
    PrestartBoundaryOverflowReceipt,
    PrestartLossTracker,
    PrestartRecoveryCallbacks,
    PrestartRecoveryLane,
    PrestartRecoveryResult,
    PrestartRetention,
)
from src.application.storage_values import (
    CaptureCloseReconciliation,
    CaptureCloseState,
    EventHandle,
    EventProjection,
    EventRecord,
    EventRef,
    EventStart,
    ProjectedEventRecord,
    RecoveredCapture,
    RecoveredObservation,
)
from src.battery_math.lut import LutPoint
from src.domain.reasons import order_reasons
from src.domain.values import ChargeReadiness, FrozenModelSnapshot, PhysicalObservation


def _observation(status: str, second: int) -> PhysicalObservation:
    return PhysicalObservation(
        boot_id="boot-a",
        monotonic_ns=second * 1_000_000_000,
        wall_time_utc=datetime(2026, 8, 17, 1, 0, second, tzinfo=timezone.utc),
        raw_status=status,
        battery_voltage_raw="12.30",
        battery_voltage_v=12.3,
        voltage_token_quantum_v=0.01,
        load_percent=20.0,
        input_voltage_v=0.0 if "OB" in status else 230.0,
    )


def _snapshot() -> FrozenModelSnapshot:
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


_READINESS = ChargeReadiness(False, 0.0, None, order_reasons(()))


def _projected_records(
    handle: EventHandle,
    records: list[EventRecord],
) -> tuple[ProjectedEventRecord, ...]:
    return tuple(
        ProjectedEventRecord(
            1,
            record.record_type,
            record.provenance,
            handle.blackout_id,
            handle.segment_id,
            seq,
            record.boot_id,
            record.wall_time_utc,
            record.monotonic_ns,
            None,
            record.payload,
            "e" * 64,
        )
        for seq, record in enumerate(records, start=1)
    )


def _projection(records: tuple[ProjectedEventRecord, ...]) -> EventProjection:
    return EventProjection(
        None,
        (),
        tuple(record for record in records if record.record_type == "gap"),
        None,
        (),
        None,
        (),
        (),
        0,
        records,
    )


def _overflow_receipt() -> PrestartBoundaryOverflowReceipt:
    return PrestartBoundaryOverflowReceipt(3, "boot-a", 2, "boot-b", 8)


def _overflow_record(
    receipt: PrestartBoundaryOverflowReceipt,
    *,
    mismatch_field: str | None = None,
) -> EventRecord:
    payload = {
        "reason": "prestart_boundary_overflow",
        "overflow_count": str(receipt.count),
        "overflow_first_boot_id": receipt.first_boot_id,
        "overflow_first_monotonic_ns": str(receipt.first_monotonic_ns),
        "overflow_last_boot_id": receipt.last_boot_id,
        "overflow_last_monotonic_ns": str(receipt.last_monotonic_ns),
    }
    if mismatch_field is not None:
        payload[mismatch_field] = "mismatch"
    return EventRecord("gap", "boot-a", "2026-08-17T01:00:01Z", 1, payload, "system")


def test_prestart_fifo_groups_repeated_polls_by_open_physical_episode() -> None:
    tracker = PrestartLossTracker()
    for status, second in (("OB", 0), ("OB", 1), ("OL", 2), ("OB", 3), ("OB", 4), ("OL", 5)):
        assert tracker.note(
            _observation(status, second),
            _snapshot(),
            _READINESS,
            blackout_id=f"blackout-{second}",
            segment_id=f"segment-{second}",
        )

    assert tracker.pending is not None
    assert tracker.pending.observation.monotonic_ns == 0
    assert tracker.pending_boundary is not None
    assert tracker.pending_boundary.monotonic_ns == 2_000_000_000
    tracker.mark_durable()
    assert tracker.pending is not None
    assert tracker.pending.observation.monotonic_ns == 3_000_000_000
    assert tracker.pending_boundary is not None
    assert tracker.pending_boundary.monotonic_ns == 5_000_000_000
    tracker.mark_durable()
    assert tracker.pending is None


def test_prestart_overflow_keeps_exact_aggregate_provenance() -> None:
    tracker = PrestartLossTracker()

    for episode in range(PRESTART_BOUNDARY_CAPACITY):
        second = episode * 3
        assert tracker.note(
            _observation("OB", second),
            _snapshot(),
            _READINESS,
            blackout_id=f"blackout-{second}",
            segment_id=f"segment-{second}",
        )
        assert tracker.note(
            _observation("OB", second + 1),
            _snapshot(),
            _READINESS,
            blackout_id=f"duplicate-{second}",
            segment_id=f"duplicate-segment-{second}",
        )
        assert tracker.note(
            _observation("OL", second + 2),
            _snapshot(),
            _READINESS,
            blackout_id=f"online-{second}",
            segment_id=f"online-segment-{second}",
        )

    overflow_seconds = (PRESTART_BOUNDARY_CAPACITY * 3, PRESTART_BOUNDARY_CAPACITY * 3 + 3)
    for second in overflow_seconds:
        assert (
            tracker.note(
                _observation("OB", second),
                _snapshot(),
                _READINESS,
                blackout_id=f"overflow-{second}",
                segment_id=f"overflow-segment-{second}",
            )
            is False
        )
        assert tracker.note(
            _observation("OL", second + 1),
            _snapshot(),
            _READINESS,
            blackout_id=f"overflow-online-{second}",
            segment_id=f"overflow-online-segment-{second}",
        )

    receipt = tracker.overflow_receipt
    assert receipt is not None
    assert receipt.count == len(overflow_seconds)
    assert receipt.first_boot_id == "boot-a"
    assert receipt.first_monotonic_ns == overflow_seconds[0] * 1_000_000_000
    assert receipt.last_boot_id == "boot-a"
    assert receipt.last_monotonic_ns == overflow_seconds[-1] * 1_000_000_000


def test_prestart_overflow_delivery_acknowledges_snapshot_and_residual_exactly() -> None:
    tracker = PrestartLossTracker()
    for episode in range(PRESTART_BOUNDARY_CAPACITY):
        second = episode * 3
        assert tracker.note(
            _observation("OB", second),
            _snapshot(),
            _READINESS,
            blackout_id=f"retained-{second}",
            segment_id=f"segment-{second}",
        )
        assert tracker.note(
            _observation("OL", second + 1),
            _snapshot(),
            _READINESS,
            blackout_id=f"retained-online-{second}",
            segment_id=f"online-segment-{second}",
        )
    assert not tracker.note(
        _observation("OB", 24),
        _snapshot(),
        _READINESS,
        blackout_id="overflow-original",
        segment_id="overflow-segment",
    )
    delivered = tracker.reserve_overflow_delivery()
    assert delivered is not None
    assert tracker.overflow_receipt is None

    first_residual = replace(_observation("OB", 31), boot_id="boot-b")
    second_residual = replace(_observation("OB", 33), boot_id="boot-c")
    assert not tracker.note(
        first_residual,
        _snapshot(),
        _READINESS,
        blackout_id="overflow-residual-one",
        segment_id="overflow-residual-segment-one",
    )
    assert not tracker.note(
        second_residual,
        _snapshot(),
        _READINESS,
        blackout_id="overflow-residual-two",
        segment_id="overflow-residual-segment-two",
    )
    equal_value_stale = OverflowDeliveryReservation(
        delivered.token - 1,
        replace(delivered.receipt),
    )
    tracker.acknowledge_overflow_delivery(equal_value_stale)
    tracker.release_overflow_delivery(equal_value_stale)
    assert tracker.overflowed
    tracker.acknowledge_overflow_delivery(delivered)

    residual = tracker.overflow_receipt
    assert residual is not None
    assert residual.count == 2
    assert residual.first_boot_id == "boot-b"
    assert residual.first_monotonic_ns == 31_000_000_000
    assert residual.last_boot_id == "boot-c"
    assert residual.last_monotonic_ns == 33_000_000_000
    retried = tracker.reserve_overflow_delivery()
    assert retried is not None
    assert retried.receipt is residual
    assert retried.token != delivered.token
    assert tracker.reserve_overflow_delivery() is retried
    stale_retry = OverflowDeliveryReservation(retried.token - 1, replace(residual))
    tracker.acknowledge_overflow_delivery(stale_retry)
    tracker.release_overflow_delivery(stale_retry)
    tracker.release_overflow_delivery(retried)
    assert tracker.overflow_receipt == residual


def test_prestart_recovery_result_rejects_unattempted_durable_receipt() -> None:
    receipt = _overflow_receipt()
    with pytest.raises(ValueError):
        OverflowDeliveryOutcome(
            OverflowDeliveryState.NOT_ATTEMPTED,
            OverflowDeliveryReservation(1, receipt),
        )


def test_prestart_retain_deduplicates_failed_start_and_aggregates_new_overflow() -> None:
    tracker = PrestartLossTracker()

    def start(second: int) -> CaptureStart:
        observation = _observation("OB", second)
        return CaptureStart(
            f"blackout-{second}",
            f"segment-{second}",
            observation,
            _snapshot(),
            _READINESS,
        )

    original = start(0)
    assert tracker.retain(original) is PrestartRetention.RETAINED
    assert tracker.retain(original) is PrestartRetention.DUPLICATE
    for second in range(1, PRESTART_BOUNDARY_CAPACITY):
        assert tracker.retain(start(second)) is PrestartRetention.RETAINED
    assert tracker.retain(start(PRESTART_BOUNDARY_CAPACITY)) is PrestartRetention.AGGREGATED

    assert tracker.overflow_receipt is not None
    assert tracker.overflow_receipt.count == 1
    assert tracker.overflow_receipt.first_monotonic_ns == (
        PRESTART_BOUNDARY_CAPACITY * 1_000_000_000
    )


def test_failed_start_overflow_releases_scope_and_preserves_fifo_recovery() -> None:
    tracker = PrestartLossTracker()
    for episode in range(PRESTART_BOUNDARY_CAPACITY):
        second = episode * 3
        assert tracker.note(
            _observation("OB", second),
            _snapshot(),
            _READINESS,
            blackout_id=f"retained-{second}",
            segment_id=f"segment-{second}",
        )
        assert tracker.note(
            _observation("OL", second + 1),
            _snapshot(),
            _READINESS,
            blackout_id=f"retained-online-{second}",
            segment_id=f"online-segment-{second}",
        )

    overflow = CaptureStart(
        "overflow-boundary",
        "overflow-segment",
        _observation("OB", PRESTART_BOUNDARY_CAPACITY * 3),
        _snapshot(),
        _READINESS,
    )
    calls: list[str] = []
    writer = CaptureWriter()

    def recover(_exc: Exception) -> RecoveryDisposition:
        assert tracker.retain(overflow) is PrestartRetention.AGGREGATED
        return RecoveryDisposition.TERMINAL_SUCCESS

    assert writer.submit(
        CaptureCommand(
            CaptureCommandKind.START,
            lambda: (_ for _ in ()).throw(OSError("start")),
            scope_id=overflow.blackout_id,
            recover_failure=recover,
        )
    )
    assert writer.submit(
        CaptureCommand(
            CaptureCommandKind.OBSERVATION,
            lambda: calls.append("discarded-overflow-observation"),
            scope_id=overflow.blackout_id,
        )
    )
    assert writer.submit(
        CaptureCommand(CaptureCommandKind.MODEL_COMMIT, lambda: calls.append("maintenance"))
    )

    assert writer.drain_one()
    assert writer.drain_one()
    assert calls == ["maintenance"]
    assert writer.health().discarded_command_count == 1
    assert tracker.overflow_receipt is not None
    assert tracker.overflow_receipt.count == 1
    retained = tracker.pending
    assert retained is not None

    assert writer.submit(
        CaptureCommand(
            CaptureCommandKind.START,
            lambda: calls.append("retained-start"),
            scope_id=retained.blackout_id,
        )
    )
    assert writer.submit(
        CaptureCommand(CaptureCommandKind.MODEL_COMMIT, lambda: calls.append("blocked-maintenance"))
    )
    assert writer.drain_one()
    assert not writer.drain_one()
    assert writer.submit(
        CaptureCommand(
            CaptureCommandKind.END,
            lambda: calls.append("retained-end"),
            scope_id=retained.blackout_id,
        )
    )
    assert writer.drain_one()
    assert writer.drain_one()
    assert calls == [
        "maintenance",
        "retained-start",
        "retained-end",
        "blocked-maintenance",
    ]


class _Store:
    def __init__(self) -> None:
        self.starts = []
        self.records = []
        self.failures: set[str] = set()

    def open(self, start):
        self.starts.append(start)
        return EventHandle(start.blackout_id, start.segment_id, "event.jsonl", 1, "a" * 64)

    def append(self, handle, record):
        if record.record_type in self.failures:
            self.failures.remove(record.record_type)
            raise OSError(f"{record.record_type} failed")
        self.records.append(record)
        return replace(handle, next_seq=handle.next_seq + 1, last_record_sha256="b" * 64)

    def checkpoint_processing(self, _handle, _stage):
        return None

    def project(self, handle):
        return _projection(_projected_records(handle, self.records))

    def recover_startup(self):
        return None

    def reconcile_damaged_close(self, _blackout_id, current_handle):
        return CaptureCloseReconciliation(CaptureCloseState.UNKNOWN, current_handle)

    def acknowledge_capture_recovery(self):
        return None


class _PreparingRecoveryStore(_Store):
    def __init__(self) -> None:
        super().__init__()
        self.recovery_calls = 0
        self.pending: RecoveredCapture | None = None

    def open(self, start):
        self.starts.append(start)
        self.pending = RecoveredCapture(
            EventHandle(start.blackout_id, start.segment_id, "event.jsonl", 1, "a" * 64),
            start.boot_id,
            RecoveredObservation(
                start.boot_id,
                start.wall_time_utc,
                start.monotonic_ns,
                {},
            ),
        )
        raise OSError("open failed after preparing registry")

    def recover_startup(self):
        self.recovery_calls += 1
        if self.recovery_calls < 3:
            raise OSError("recovery unavailable")
        return self.pending


class _PrestartEndFailureStore(_Store):
    def __init__(self) -> None:
        super().__init__()
        self._active_handle: EventHandle | None = None
        self._start: EventStart | None = None
        self._fail_end = True

    def open(self, start):
        self._start = start
        self._active_handle = super().open(start)
        return self._active_handle

    def append(self, handle, record):
        if record.record_type == "end" and self._fail_end:
            self._fail_end = False
            raise OSError("end failed after durable pre-start gaps")
        self._active_handle = super().append(handle, record)
        return self._active_handle

    def recover_startup(self):
        if self._active_handle is None or self._start is None:
            return None
        return RecoveredCapture(
            self._active_handle,
            self._start.boot_id,
            RecoveredObservation(
                self._start.boot_id,
                self._start.wall_time_utc,
                self._start.monotonic_ns,
                {},
            ),
        )


class _PrestartEndDurableStore(_PrestartEndFailureStore):
    def append(self, handle, record):
        if record.record_type == "end" and self._fail_end:
            self._fail_end = False
            self._active_handle = _Store.append(self, handle, record)
            raise OSError("end acknowledgement failed after durable append")
        return super().append(handle, record)

    def recover_startup(self):
        return None

    def reconcile_damaged_close(self, blackout_id, current_handle):
        if self._active_handle is None:
            return CaptureCloseReconciliation(CaptureCloseState.UNKNOWN, current_handle)
        return CaptureCloseReconciliation(CaptureCloseState.END, self._active_handle)


class _ProjectionTerminalStore(_Store):
    def __init__(self, records: list[EventRecord]) -> None:
        super().__init__()
        self._projection_records = records
        self._active_handle: EventHandle | None = None

    def open(self, start):
        self._active_handle = super().open(start)
        return self._active_handle

    def append(self, _handle, _record):
        raise OSError("terminal event is already durable")

    def reconcile_damaged_close(self, _blackout_id, current_handle):
        if self._active_handle is None:
            return CaptureCloseReconciliation(CaptureCloseState.UNKNOWN, current_handle)
        self._active_handle = replace(
            self._active_handle,
            next_seq=len(self._projection_records) + 1,
            last_record_sha256="f" * 64,
        )
        return CaptureCloseReconciliation(CaptureCloseState.END, self._active_handle)

    def project(self, handle):
        return _projection(_projected_records(handle, self._projection_records))


class _PrestartReconciledEndStore(_Store):
    def __init__(self) -> None:
        super().__init__()
        self._active_handle: EventHandle | None = None
        self._fail_first_gap = True
        self._first_open = True

    def open(self, start):
        handle = super().open(start)
        if self._first_open:
            self._first_open = False
            self._active_handle = replace(handle, next_seq=5, last_record_sha256="c" * 64)
            self.records.extend(
                (
                    EventRecord(
                        "gap",
                        start.boot_id,
                        start.wall_time_utc,
                        start.monotonic_ns,
                        {"reason": "unrelated_gap"},
                        "system",
                    ),
                    EventRecord(
                        "observation",
                        start.boot_id,
                        start.wall_time_utc,
                        start.monotonic_ns + 1,
                        {"raw_status": "OB"},
                        "physical",
                    ),
                    EventRecord(
                        "gap",
                        start.boot_id,
                        start.wall_time_utc,
                        start.monotonic_ns + 2,
                        {"reason": "another_gap"},
                        "system",
                    ),
                    EventRecord(
                        "end",
                        start.boot_id,
                        start.wall_time_utc,
                        start.monotonic_ns + 3,
                        {"termination": "power_restored"},
                        "physical",
                    ),
                )
            )
        return handle

    def append(self, handle, record):
        if self._fail_first_gap and record.record_type == "gap":
            self._fail_first_gap = False
            raise OSError("already-reconciled END before gap acknowledgement")
        return super().append(handle, record)

    def recover_startup(self):
        return None

    def reconcile_damaged_close(self, blackout_id, current_handle):
        if self._active_handle is None:
            return CaptureCloseReconciliation(CaptureCloseState.UNKNOWN, current_handle)
        return CaptureCloseReconciliation(CaptureCloseState.END, self._active_handle)


class _PrestartOverflowBarrierStore(_Store):
    def __init__(self) -> None:
        super().__init__()
        self.on_overflow_append: Callable[[], None] | None = None

    def append(self, handle, record):
        next_handle = super().append(handle, record)
        callback = self.on_overflow_append
        if record.payload.get("reason") == "prestart_boundary_overflow" and callback:
            self.on_overflow_append = None
            callback()
        return next_handle


class _PrestartOverflowFailureStore(_PrestartEndFailureStore):
    def __init__(self) -> None:
        super().__init__()
        self._fail_overflow = True
        self._fail_end = False

    def append(self, handle, record):
        if (
            record.record_type == "gap"
            and record.payload.get("reason") == "prestart_boundary_overflow"
            and self._fail_overflow
        ):
            self._fail_overflow = False
            raise OSError("overflow gap acknowledgement failed")
        return super().append(handle, record)


class _PrestartOpenFailureStore(_Store):
    def open(self, _start):
        raise OSError("pre-start open failed")


def _terminal_reconcile_result(records: list[EventRecord]) -> PrestartRecoveryResult:
    store = _ProjectionTerminalStore(records)
    writer = CaptureWriter()
    completed: list[PrestartRecoveryResult] = []
    lane = PrestartRecoveryLane(
        cast(CaptureRecoveryEventStorePort, store),
        writer,
        PrestartRecoveryCallbacks(
            lambda result, _observation: completed.append(result),
            lambda _observation, _overflow: None,
            lambda _recovered, _observation, _exc, _overflow: None,
        ),
    )
    start = CaptureStart(
        "projection-reconcile-blackout",
        "projection-reconcile-segment",
        _observation("OB", 0),
        _snapshot(),
        _READINESS,
    )
    assert lane.submit(
        start,
        _observation("OL", 1),
        termination="power_restored",
        overflow_delivery=OverflowDeliveryReservation(1, _overflow_receipt()),
    )
    assert writer.drain_one()
    assert len(completed) == 1
    return completed[0]


@pytest.mark.parametrize(
    "mismatch_field",
    (
        "overflow_count",
        "overflow_first_boot_id",
        "overflow_first_monotonic_ns",
        "overflow_last_boot_id",
        "overflow_last_monotonic_ns",
    ),
)
def test_terminal_reconcile_requires_exact_overflow_provenance(mismatch_field: str) -> None:
    receipt = _overflow_receipt()
    result = _terminal_reconcile_result(
        [
            EventRecord(
                "gap",
                "boot-a",
                "2026-08-17T01:00:00Z",
                0,
                {"reason": PRESTART_LOSS_REASON},
                "system",
            ),
            _overflow_record(receipt, mismatch_field=mismatch_field),
        ]
    )
    assert result.overflow_delivery.receipt == receipt
    assert result.overflow_delivery.state is OverflowDeliveryState.ATTEMPTED_UNPROVEN


def test_terminal_reconcile_accepts_exact_overflow_gap() -> None:
    receipt = _overflow_receipt()
    result = _terminal_reconcile_result(
        [
            EventRecord(
                "gap",
                "boot-a",
                "2026-08-17T01:00:00Z",
                0,
                {"reason": PRESTART_LOSS_REASON},
                "system",
            ),
            _overflow_record(receipt),
        ]
    )
    assert result.overflow_delivery.receipt == receipt
    assert result.overflow_delivery.state is OverflowDeliveryState.PROVEN_DURABLE


def test_terminal_reconcile_rejects_unrelated_four_record_tail() -> None:
    receipt = _overflow_receipt()
    result = _terminal_reconcile_result(
        [
            EventRecord(
                "gap",
                "boot-a",
                "2026-08-17T01:00:00Z",
                0,
                {"reason": PRESTART_LOSS_REASON},
                "system",
            ),
            EventRecord(
                "observation",
                "boot-a",
                "2026-08-17T01:00:01Z",
                1,
                {"raw_status": "OB"},
                "physical",
            ),
            EventRecord(
                "gap",
                "boot-a",
                "2026-08-17T01:00:02Z",
                2,
                {"reason": "unrelated_gap"},
                "system",
            ),
            EventRecord(
                "end",
                "boot-a",
                "2026-08-17T01:00:03Z",
                3,
                {"termination": "power_restored"},
                "physical",
            ),
        ]
    )
    assert result.overflow_delivery.receipt == receipt
    assert result.overflow_delivery.state is OverflowDeliveryState.ATTEMPTED_UNPROVEN


def _drain(writer: CaptureWriter) -> None:
    while writer.drain_one():
        pass


def test_prestart_retry_does_not_duplicate_durable_gaps_before_end() -> None:
    store = _PrestartEndFailureStore()
    writer = CaptureWriter()
    completed: list[PrestartRecoveryResult] = []
    missing: list[PhysicalObservation] = []
    partial: list[Exception] = []
    callbacks = PrestartRecoveryCallbacks(
        lambda result, _observation: completed.append(result),
        lambda observation, _overflow: missing.append(observation),
        lambda _recovered, _observation, exc, _overflow: partial.append(exc),
    )
    lane = PrestartRecoveryLane(cast(CaptureRecoveryEventStorePort, store), writer, callbacks)
    start = CaptureStart(
        "prestart-retry-blackout",
        "prestart-retry-segment",
        _observation("OB", 0),
        _snapshot(),
        _READINESS,
    )
    overflow = PrestartBoundaryOverflowReceipt(1, "boot-a", 2, "boot-a", 2)
    delivery = OverflowDeliveryReservation(1, overflow)

    assert lane.submit(
        start,
        _observation("OL", 1),
        termination="power_restored",
        overflow_delivery=delivery,
    )
    assert writer.drain_one()

    assert [record.record_type for record in store.records] == ["gap", "gap", "end"]
    assert sum(record.record_type == "gap" for record in store.records) == 2
    assert sum(record.record_type == "end" for record in store.records) == 1
    assert len(completed) == 1
    assert completed[0].handle is None
    assert not missing
    assert not partial


def test_prestart_retry_appends_only_missing_overflow_gap_after_first_gap() -> None:
    store = _PrestartOverflowFailureStore()
    writer = CaptureWriter()
    lane = PrestartRecoveryLane(
        cast(CaptureRecoveryEventStorePort, store),
        writer,
        PrestartRecoveryCallbacks(lambda *_args: None, lambda *_args: None, lambda *_args: None),
    )
    start = CaptureStart(
        "prestart-overflow-retry-blackout",
        "prestart-overflow-retry-segment",
        _observation("OB", 0),
        _snapshot(),
        _READINESS,
    )
    overflow = PrestartBoundaryOverflowReceipt(1, "boot-a", 2, "boot-a", 2)
    delivery = OverflowDeliveryReservation(1, overflow)

    assert lane.submit(
        start,
        _observation("OL", 1),
        termination="power_restored",
        overflow_delivery=delivery,
    )
    assert writer.drain_one()

    assert [record.record_type for record in store.records] == ["gap", "gap", "end"]
    assert store.records[0].payload["reason"] == "capture_unavailable_after_blackout_start"
    assert store.records[1].payload["reason"] == "prestart_boundary_overflow"


def test_no_durable_start_releases_exact_reserved_overflow_before_reset() -> None:
    store = _PrestartOpenFailureStore()
    writer = CaptureWriter()
    capture = BlackoutCapture(cast(CaptureRecoveryEventStorePort, store), writer)
    for episode in range(PRESTART_BOUNDARY_CAPACITY):
        second = episode * 3
        assert capture._prestart_loss.note(
            _observation("OB", second),
            _snapshot(),
            _READINESS,
            blackout_id=f"retained-{second}",
            segment_id=f"segment-{second}",
        )
        assert capture._prestart_loss.note(
            _observation("OL", second + 1),
            _snapshot(),
            _READINESS,
            blackout_id=f"retained-online-{second}",
            segment_id=f"online-segment-{second}",
        )
    assert not capture._prestart_loss.note(
        _observation("OB", 24),
        _snapshot(),
        _READINESS,
        blackout_id="overflow-original",
        segment_id="overflow-segment",
    )
    attempted = capture._prestart_loss.reserve_overflow_delivery()
    assert attempted is not None
    assert capture.accept_after_safety_publish(
        _observation("OL", 30), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    assert writer.drain_one()

    assert capture._prestart_loss.overflow_receipt is attempted.receipt
    assert capture._prestart_loss.pending is not None
    assert not store.starts


def test_prestart_durable_end_releases_scope_and_acknowledges_fifo_once() -> None:
    store = _PrestartEndDurableStore()
    writer = CaptureWriter()
    capture = BlackoutCapture(cast(CaptureRecoveryEventStorePort, store), writer)
    for episode in range(PRESTART_BOUNDARY_CAPACITY):
        second = episode * 3
        assert capture._prestart_loss.note(
            _observation("OB", second),
            _snapshot(),
            _READINESS,
            blackout_id=f"retained-{second}",
            segment_id=f"segment-{second}",
        )
        assert capture._prestart_loss.note(
            _observation("OL", second + 1),
            _snapshot(),
            _READINESS,
            blackout_id=f"retained-online-{second}",
            segment_id=f"online-segment-{second}",
        )
    assert not capture._prestart_loss.note(
        _observation("OB", PRESTART_BOUNDARY_CAPACITY * 3),
        _snapshot(),
        _READINESS,
        blackout_id="overflow-boundary",
        segment_id="overflow-segment",
    )

    assert capture.accept_after_safety_publish(
        _observation("OL", 30), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    scope_id = capture._submitted_blackout_id
    assert scope_id is not None
    discarded: list[str] = []
    assert writer.submit(
        CaptureCommand(
            CaptureCommandKind.OBSERVATION,
            lambda: discarded.append("must-not-run"),
            scope_id=scope_id,
        )
    )
    assert writer.drain_one()

    assert [record.record_type for record in store.records] == ["gap", "gap", "end"]
    assert discarded == []
    assert writer.health().discarded_command_count == 1
    assert not capture._prestart_loss.overflowed
    retained = capture._prestart_loss.pending
    assert retained is not None
    assert retained.observation.monotonic_ns == 3_000_000_000

    committed: list[str] = []
    assert writer.submit(
        CaptureCommand(CaptureCommandKind.MODEL_COMMIT, lambda: committed.append("commit"))
    )
    assert writer.drain_one()
    assert committed == ["commit"]


def test_service_stop_does_not_duplicate_inflight_prestart_online_recovery() -> None:
    store = _Store()
    writer = CaptureWriter()
    capture = BlackoutCapture(cast(CaptureRecoveryEventStorePort, store), writer)
    capture.note_capture_unavailable(_observation("OB", 0), _snapshot(), _READINESS)

    assert capture.accept_after_safety_publish(
        _observation("OL", 1), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    assert capture.service_stop(_observation("OL", 2))
    assert writer.drain_one()
    assert not writer.drain_one()

    assert len(store.starts) == 1
    assert [record.record_type for record in store.records] == ["gap", "end"]
    assert all(record.payload.get("termination") != "capture_damaged" for record in store.records)


def test_prestart_reconciled_end_keeps_overflow_until_next_fifo_event() -> None:
    store = _PrestartReconciledEndStore()
    writer = CaptureWriter()
    capture = BlackoutCapture(cast(CaptureRecoveryEventStorePort, store), writer)
    for episode in range(PRESTART_BOUNDARY_CAPACITY):
        second = episode * 3
        assert capture._prestart_loss.note(
            _observation("OB", second),
            _snapshot(),
            _READINESS,
            blackout_id=f"retained-{second}",
            segment_id=f"segment-{second}",
        )
        assert capture._prestart_loss.note(
            _observation("OL", second + 1),
            _snapshot(),
            _READINESS,
            blackout_id=f"retained-online-{second}",
            segment_id=f"online-segment-{second}",
        )
    assert not capture._prestart_loss.note(
        _observation("OB", PRESTART_BOUNDARY_CAPACITY * 3),
        _snapshot(),
        _READINESS,
        blackout_id="overflow-boundary",
        segment_id="overflow-segment",
    )

    assert capture.accept_after_safety_publish(
        _observation("OL", 30), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    scope_id = capture._submitted_blackout_id
    assert scope_id is not None
    assert writer.drain_one()
    assert writer.health().discarded_command_count == 0
    assert capture._prestart_loss.overflowed
    assert capture._prestart_loss.overflow_receipt == PrestartBoundaryOverflowReceipt(
        1, "boot-a", 24_000_000_000, "boot-a", 24_000_000_000
    )
    committed: list[str] = []
    assert writer.submit(
        CaptureCommand(CaptureCommandKind.MODEL_COMMIT, lambda: committed.append("released"))
    )
    assert writer.drain_one()
    assert committed == ["released"]
    retained = capture._prestart_loss.pending
    assert retained is not None
    assert retained.observation.monotonic_ns == 3_000_000_000

    assert capture.accept_after_safety_publish(
        _observation("OB", 31), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    assert writer.drain_one()

    overflow = [
        record
        for record in store.records
        if record.payload.get("reason") == "prestart_boundary_overflow"
    ]
    assert len(overflow) == 1
    assert overflow[0].payload["overflow_count"] == "1"
    assert not capture._prestart_loss.overflowed
    next_retained = capture._prestart_loss.pending
    assert next_retained is not None
    assert next_retained.observation.monotonic_ns == 6_000_000_000


def test_partial_prestart_failure_releases_attempted_overflow_before_carrier_pop() -> None:
    store = _Store()
    writer = CaptureWriter()
    capture = BlackoutCapture(cast(CaptureRecoveryEventStorePort, store), writer)
    for episode in range(PRESTART_BOUNDARY_CAPACITY):
        second = episode * 3
        assert capture._prestart_loss.note(
            _observation("OB", second),
            _snapshot(),
            _READINESS,
            blackout_id=f"retained-{second}",
            segment_id=f"segment-{second}",
        )
        assert capture._prestart_loss.note(
            _observation("OL", second + 1),
            _snapshot(),
            _READINESS,
            blackout_id=f"retained-online-{second}",
            segment_id=f"online-segment-{second}",
        )
    assert not capture._prestart_loss.note(
        _observation("OB", 24),
        _snapshot(),
        _READINESS,
        blackout_id="overflow-original",
        segment_id="overflow-segment",
    )
    attempted = capture._prestart_loss.reserve_overflow_delivery()
    assert attempted is not None
    assert capture.accept_after_safety_publish(
        _observation("OB", 30), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    blackout_id = capture._submitted_blackout_id
    assert blackout_id is not None
    recovered = RecoveredCapture(
        EventHandle(blackout_id, "partial-segment", "event.jsonl", 1, "a" * 64),
        "boot-a",
        RecoveredObservation("boot-a", _observation("OB", 30).wall_time_utc.isoformat(), 30, {}),
    )
    capture._capture_recovery.close_partial_prestart(
        recovered,
        _observation("OB", 30),
        OSError("pre-start retry failed"),
        attempted,
    )

    assert capture._prestart_loss.overflow_receipt is attempted.receipt
    retained = capture._prestart_loss.pending
    assert retained is not None
    assert retained.observation.monotonic_ns == 3_000_000_000


def test_prestart_overflow_barrier_acknowledges_only_submitted_snapshot() -> None:
    store = _PrestartOverflowBarrierStore()
    writer = CaptureWriter()
    capture = BlackoutCapture(cast(CaptureRecoveryEventStorePort, store), writer)
    for episode in range(PRESTART_BOUNDARY_CAPACITY):
        second = episode * 3
        assert capture._prestart_loss.note(
            _observation("OB", second),
            _snapshot(),
            _READINESS,
            blackout_id=f"retained-{second}",
            segment_id=f"segment-{second}",
        )
        assert capture._prestart_loss.note(
            _observation("OL", second + 1),
            _snapshot(),
            _READINESS,
            blackout_id=f"retained-online-{second}",
            segment_id=f"online-segment-{second}",
        )
    assert not capture._prestart_loss.note(
        _observation("OB", 24),
        _snapshot(),
        _READINESS,
        blackout_id="overflow-original",
        segment_id="overflow-segment",
    )

    def add_boundaries_while_gap_is_in_flight() -> None:
        assert not capture.accept_after_safety_publish(
            replace(_observation("OB", 31), boot_id="boot-b"),
            safety_snapshot=_snapshot(),
            charge_readiness=_READINESS,
        )
        assert not capture.accept_after_safety_publish(
            replace(_observation("OB", 33), boot_id="boot-c"),
            safety_snapshot=_snapshot(),
            charge_readiness=_READINESS,
        )

    assert capture.accept_after_safety_publish(
        _observation("OL", 30), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    store.on_overflow_append = add_boundaries_while_gap_is_in_flight
    assert writer.drain_one()

    overflow = [
        record
        for record in store.records
        if record.payload.get("reason") == "prestart_boundary_overflow"
    ]
    assert len(overflow) == 1
    assert overflow[0].payload["overflow_count"] == "1"
    assert overflow[0].payload["overflow_first_monotonic_ns"] == "24000000000"
    residual = capture._prestart_loss.overflow_receipt
    assert residual is not None
    assert residual.count == 2
    assert residual.first_boot_id == "boot-b"
    assert residual.first_monotonic_ns == 31_000_000_000
    assert residual.last_boot_id == "boot-c"
    assert residual.last_monotonic_ns == 33_000_000_000

    assert not writer.health().capture_available
    assert writer.health().discarded_command_count == 0

    assert capture.accept_after_safety_publish(
        _observation("OB", 35), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    assert writer.drain_one()
    overflow = [
        record
        for record in store.records
        if record.payload.get("reason") == "prestart_boundary_overflow"
    ]
    assert len(overflow) == 2
    assert overflow[1].payload["overflow_count"] == "2"
    assert overflow[1].payload["overflow_first_boot_id"] == "boot-b"
    assert overflow[1].payload["overflow_first_monotonic_ns"] == "31000000000"
    assert overflow[1].payload["overflow_last_boot_id"] == "boot-c"
    assert overflow[1].payload["overflow_last_monotonic_ns"] == "33000000000"
    assert not capture._prestart_loss.overflowed


def test_preparing_recovery_keeps_original_identity_until_matching_adoption() -> None:
    store = _PreparingRecoveryStore()
    writer = CaptureWriter()
    capture = BlackoutCapture(cast(CaptureRecoveryEventStorePort, store), writer)

    assert capture.accept_after_safety_publish(
        _observation("OB", 0), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    assert capture.accept_after_safety_publish(
        _observation("OB", 1), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    _drain(writer)
    assert writer.health().discarded_command_count == 1
    original = store.starts[0]

    assert capture.accept_after_safety_publish(
        _observation("OB", 2), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    _drain(writer)

    assert store.recovery_calls == 3
    assert len(store.starts) == 1
    assert store.starts[0].blackout_id == original.blackout_id
    assert store.starts[0].segment_id == original.segment_id
    assert [record.record_type for record in store.records] == ["gap", "observation"]


def test_failed_terminal_close_retries_then_recovers_and_preserves_next_boundary() -> None:
    now = [0.0]
    store = _Store()
    writer = CaptureWriter(monotonic_clock=lambda: now[0])
    capture = BlackoutCapture(cast(CaptureRecoveryEventStorePort, store), writer)

    assert capture.accept_after_safety_publish(
        _observation("OB", 0), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    _drain(writer)
    store.failures.update({"observation", "gap"})
    assert capture.accept_after_safety_publish(
        _observation("OB", 1), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    _drain(writer)
    assert not writer.health().capture_available

    assert not capture.accept_after_safety_publish(
        _observation("OB", 2), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    now[0] = 1.0
    assert not capture.accept_after_safety_publish(
        _observation("OB", 3), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    _drain(writer)

    assert writer.health().capture_available
    assert capture.accept_after_safety_publish(
        _observation("OB", 4), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    _drain(writer)
    assert len(store.starts) == 2
    assert [record.record_type for record in store.records] == [
        "gap",
        "end",
        "gap",
        "observation",
    ]


def test_terminal_retry_keeps_completed_boundaries_fifo(tmp_path) -> None:
    now = [0.0]
    store = _Store()
    writer = CaptureWriter(monotonic_clock=lambda: now[0])
    capture = BlackoutCapture(cast(CaptureRecoveryEventStorePort, store), writer)
    assert capture.accept_after_safety_publish(
        _observation("OB", 0), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    _drain(writer)
    store.failures.update({"observation", "gap"})
    assert capture.accept_after_safety_publish(
        _observation("OB", 1), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    _drain(writer)

    assert not capture.accept_after_safety_publish(
        _observation("OB", 2), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    assert not capture.accept_after_safety_publish(
        _observation("OL", 3), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    assert not capture.accept_after_safety_publish(
        _observation("OB", 4), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    now[0] = 1.0
    assert not capture.accept_after_safety_publish(
        _observation("OB", 5), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    _drain(writer)

    assert capture.accept_after_safety_publish(
        _observation("OB", 6), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    _drain(writer)
    assert capture.accept_after_safety_publish(
        _observation("OB", 7), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    _drain(writer)
    assert [start.monotonic_ns for start in store.starts] == [0, 2_000_000_000, 4_000_000_000]


def test_prestart_fifo_overflow_is_health_latched_and_durable_receipt() -> None:
    now = [0.0]
    store = _Store()
    writer = CaptureWriter(monotonic_clock=lambda: now[0])
    capture = BlackoutCapture(cast(CaptureRecoveryEventStorePort, store), writer)
    assert capture.accept_after_safety_publish(
        _observation("OB", 0), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    _drain(writer)
    store.failures.update({"observation", "gap"})
    assert capture.accept_after_safety_publish(
        _observation("OB", 1), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    _drain(writer)

    for second in range(2, 18, 2):
        assert not capture.accept_after_safety_publish(
            _observation("OB", second),
            safety_snapshot=_snapshot(),
            charge_readiness=_READINESS,
        )
        assert not capture.accept_after_safety_publish(
            _observation("OL", second + 1),
            safety_snapshot=_snapshot(),
            charge_readiness=_READINESS,
        )
    assert not capture.accept_after_safety_publish(
        _observation("OB", 18), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    assert not writer.health().capture_available
    assert capture._prestart_loss.overflowed

    now[0] = 1.0
    assert not capture.accept_after_safety_publish(
        _observation("OL", 19), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    _drain(writer)
    assert capture.accept_after_safety_publish(
        _observation("OB", 20), safety_snapshot=_snapshot(), charge_readiness=_READINESS
    )
    _drain(writer)

    overflow = [
        record
        for record in store.records
        if record.payload.get("reason") == "prestart_boundary_overflow"
    ]
    assert len(overflow) == 1
    assert overflow[0].payload["overflow_count"] == "1"
    assert overflow[0].payload["overflow_first_monotonic_ns"] == str(18_000_000_000)
    assert overflow[0].payload["overflow_last_monotonic_ns"] == str(18_000_000_000)
    assert not capture._prestart_loss.overflowed


class _CheckpointFailStore(JsonlEventStore):
    def __init__(self, root) -> None:
        super().__init__(root)
        self.fail_checkpoint = True
        self.fail_observation = True

    def append(self, handle, record):
        if self.fail_observation and record.record_type == "observation":
            self.fail_observation = False
            raise OSError("observation failed")
        return super().append(handle, record)

    def checkpoint_processing(self, handle, frozen_stage):
        if self.fail_checkpoint:
            self.fail_checkpoint = False
            raise OSError("checkpoint failed")
        return super().checkpoint_processing(handle, frozen_stage)


def test_terminal_retry_reconstructs_end_before_checkpoint(tmp_path) -> None:
    now = [0.0]
    store = _CheckpointFailStore(tmp_path)
    writer = CaptureWriter(monotonic_clock=lambda: now[0])
    capture = BlackoutCapture(store, writer)
    try:
        assert capture.accept_after_safety_publish(
            _observation("OB", 0), safety_snapshot=_snapshot(), charge_readiness=_READINESS
        )
        _drain(writer)
        assert capture.accept_after_safety_publish(
            _observation("OB", 1), safety_snapshot=_snapshot(), charge_readiness=_READINESS
        )
        _drain(writer)
        now[0] = 1.0
        assert not capture.accept_after_safety_publish(
            _observation("OB", 2), safety_snapshot=_snapshot(), charge_readiness=_READINESS
        )
        _drain(writer)

        assert writer.health().capture_available
        storage_health = store.storage_health()
        assert storage_health.capture_available
        assert storage_health.bounded_error is None
        pending = store.work_registry().pending_processing
        assert len(pending) == 1
        projection = store.project(EventRef(pending[0].blackout_id, pending[0].final_path_token))
        assert [record.record_type for record in projection.records] == ["start", "gap", "end"]
        assert pending[0].frozen_stage == "capture_damaged"
        assert capture.accept_after_safety_publish(
            _observation("OB", 3), safety_snapshot=_snapshot(), charge_readiness=_READINESS
        )
    finally:
        store.close()
