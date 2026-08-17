"""Bounded durable recovery of blackout boundaries missed by storage."""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from src.application.capture_storage_commands import (
    CaptureStart,
    GapAppend,
    append_end,
    append_gap,
    append_observation,
    start_capture,
)
from src.application.capture_writer import (
    CaptureCommand,
    CaptureCommandKind,
    CaptureWriter,
    RecoveryDisposition,
)
from src.application.ports import CaptureEventStorePort, CaptureRecoveryEventStorePort
from src.application.storage_values import (
    CaptureCloseState,
    EventHandle,
    RecoveredCapture,
    RecoveredObservation,
)
from src.domain.lifecycle import classify_physical_observation, is_capture_candidate
from src.domain.values import (
    BlackoutKind,
    ChargeReadiness,
    FrozenModelSnapshot,
    PhysicalObservation,
)

PRESTART_LOSS_REASON = "capture_unavailable_after_blackout_start"
PRESTART_BOUNDARY_CAPACITY = 8


class PrestartRetention(StrEnum):
    """Ownership result for a failed pre-start boundary."""

    RETAINED = "retained"
    DUPLICATE = "duplicate"
    AGGREGATED = "aggregated"


@dataclass(frozen=True, slots=True)
class PrestartBoundaryOverflowReceipt:
    """Aggregate provenance for starts beyond the bounded recovery FIFO."""

    count: int
    first_boot_id: str
    first_monotonic_ns: int
    last_boot_id: str
    last_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class OverflowDeliveryReservation:
    """Opaque ownership token for one immutable overflow receipt snapshot."""

    token: int
    receipt: PrestartBoundaryOverflowReceipt


class OverflowDeliveryState(StrEnum):
    """Proof state for one attempted aggregate overflow delivery."""

    NOT_ATTEMPTED = "not_attempted"
    ATTEMPTED_UNPROVEN = "attempted_unproven"
    PROVEN_DURABLE = "proven_durable"


@dataclass(frozen=True, slots=True)
class OverflowDeliveryOutcome:
    """Tagged result for one exact overflow receipt snapshot."""

    state: OverflowDeliveryState
    reservation: OverflowDeliveryReservation | None = None

    def __post_init__(self) -> None:
        has_reservation = self.reservation is not None
        if self.state is OverflowDeliveryState.NOT_ATTEMPTED:
            if has_reservation:
                raise ValueError("not-attempted overflow delivery cannot carry a reservation")
        elif not has_reservation:
            raise ValueError("attempted overflow delivery requires a reservation")

    @classmethod
    def not_attempted(cls) -> "OverflowDeliveryOutcome":
        return cls(OverflowDeliveryState.NOT_ATTEMPTED)

    @classmethod
    def attempted(cls, reservation: OverflowDeliveryReservation) -> "OverflowDeliveryOutcome":
        return cls(OverflowDeliveryState.ATTEMPTED_UNPROVEN, reservation)

    @classmethod
    def proven(cls, reservation: OverflowDeliveryReservation) -> "OverflowDeliveryOutcome":
        return cls(OverflowDeliveryState.PROVEN_DURABLE, reservation)

    @property
    def receipt(self) -> PrestartBoundaryOverflowReceipt | None:
        return None if self.reservation is None else self.reservation.receipt


@dataclass(frozen=True, slots=True)
class PrestartRecoveryResult:
    """Result of writing the previously missed start and its explicit gap."""

    handle: EventHandle | None
    current_kind: BlackoutKind
    overflow_delivery: OverflowDeliveryOutcome = field(
        default_factory=OverflowDeliveryOutcome.not_attempted
    )

    @property
    def attempted_overflow_reservation(self) -> OverflowDeliveryReservation | None:
        return self.overflow_delivery.reservation

    @property
    def durable_overflow_reservation(self) -> OverflowDeliveryReservation | None:
        if self.overflow_delivery.state is OverflowDeliveryState.PROVEN_DURABLE:
            return self.overflow_delivery.reservation
        return None


@dataclass(frozen=True, slots=True)
class PrestartRecoveryPlan:
    """Optional durable suffix to append while recovering one retained start."""

    termination: str | None = None
    recovered_handle: EventHandle | None = None
    overflow_delivery: OverflowDeliveryReservation | None = None


@dataclass(frozen=True, slots=True)
class PrestartRecoveryCallbacks:
    """Completion callbacks for one writer-lane recovery command."""

    on_success: Callable[[PrestartRecoveryResult, PhysicalObservation], None]
    on_no_durable_start: Callable[[PhysicalObservation, OverflowDeliveryReservation | None], None]
    on_partial_failure: Callable[
        [
            RecoveredCapture,
            PhysicalObservation,
            Exception,
            OverflowDeliveryReservation | None,
        ],
        None,
    ]


class PrestartLossTracker:
    """Keep a bounded FIFO of missed physical boundaries until they are durable."""

    def __init__(self) -> None:
        self._pending: deque[CaptureStart] = deque()
        self._pending_end: dict[str, PhysicalObservation] = {}
        self._suppress_until_online = False
        self._overflow_receipt: PrestartBoundaryOverflowReceipt | None = None
        self._overflow_delivery: OverflowDeliveryReservation | None = None
        self._next_delivery_token = 0

    @property
    def pending(self) -> CaptureStart | None:
        return self._pending[0] if self._pending else None

    @property
    def pending_boundary(self) -> PhysicalObservation | None:
        start = self.pending
        return None if start is None else self._pending_end.get(start.blackout_id)

    @property
    def overflowed(self) -> bool:
        return self._overflow_receipt is not None or self._overflow_delivery is not None

    @property
    def overflow_receipt(self) -> PrestartBoundaryOverflowReceipt | None:
        return self._overflow_receipt

    def note(
        self,
        observation: PhysicalObservation,
        snapshot: FrozenModelSnapshot,
        readiness: ChargeReadiness,
        *,
        blackout_id: str,
        segment_id: str,
    ) -> bool:
        """Retain a bounded FIFO of missed physical blackout boundaries."""
        if self._suppress_until_online:
            return False
        kind = classify_physical_observation(observation)
        open_tail = self._open_tail()
        if kind == BlackoutKind.ONLINE and open_tail is not None:
            self._pending_end.setdefault(open_tail.blackout_id, observation)
            return True
        if not is_capture_candidate(observation):
            return True
        if open_tail is not None:
            return True
        if len(self._pending) >= PRESTART_BOUNDARY_CAPACITY:
            self._record_overflow(observation)
            return False
        self._pending.append(
            CaptureStart(blackout_id, segment_id, observation, snapshot, readiness)
        )
        return True

    def retain(self, start: CaptureStart) -> PrestartRetention:
        """Transfer failed-START ownership to retention or the aggregate receipt."""
        if any(item.blackout_id == start.blackout_id for item in self._pending):
            return PrestartRetention.DUPLICATE
        if len(self._pending) >= PRESTART_BOUNDARY_CAPACITY:
            self._record_overflow(start.observation)
            return PrestartRetention.AGGREGATED
        self._pending.append(start)
        return PrestartRetention.RETAINED

    def mark_durable(self) -> None:
        if self._pending:
            finished = self._pending.popleft()
            self._pending_end.pop(finished.blackout_id, None)

    def reserve_overflow_delivery(self) -> OverflowDeliveryReservation | None:
        """Move one immutable pending receipt into the writer in-flight slot."""
        if self._overflow_delivery is not None:
            return self._overflow_delivery
        receipt = self._overflow_receipt
        if receipt is None:
            return None
        self._overflow_receipt = None
        self._next_delivery_token += 1
        reservation = OverflowDeliveryReservation(self._next_delivery_token, receipt)
        self._overflow_delivery = reservation
        return reservation

    def release_overflow_delivery(self, delivered: OverflowDeliveryReservation | None) -> None:
        """Return an unaccepted snapshot, merging boundaries observed meanwhile."""
        if delivered is None:
            return
        current = self._overflow_delivery
        if (
            current is None
            or current.token != delivered.token
            or current.receipt is not delivered.receipt
        ):
            return
        pending = self._overflow_receipt
        if pending is None:
            self._overflow_receipt = delivered.receipt
        else:
            self._overflow_receipt = PrestartBoundaryOverflowReceipt(
                delivered.receipt.count + pending.count,
                delivered.receipt.first_boot_id,
                delivered.receipt.first_monotonic_ns,
                pending.last_boot_id,
                pending.last_monotonic_ns,
            )
        self._overflow_delivery = None

    def acknowledge_overflow_delivery(
        self,
        delivered: OverflowDeliveryReservation,
    ) -> None:
        """Acknowledge only *delivered*, retaining boundaries observed afterward."""
        current = self._overflow_delivery
        if (
            current is None
            or current.token != delivered.token
            or current.receipt is not delivered.receipt
        ):
            return
        self._overflow_delivery = None

    def mark_damaged(self, current_kind: BlackoutKind) -> None:
        if self.pending is not None:
            return
        self._suppress_until_online = current_kind != BlackoutKind.ONLINE

    def blocks(self, current_kind: BlackoutKind) -> bool:
        if not self._suppress_until_online:
            return False
        if current_kind == BlackoutKind.ONLINE:
            self._suppress_until_online = False
        return True

    def _record_overflow(self, observation: PhysicalObservation) -> None:
        receipt = self._overflow_receipt
        if receipt is None:
            self._overflow_receipt = PrestartBoundaryOverflowReceipt(
                1,
                observation.boot_id,
                observation.monotonic_ns,
                observation.boot_id,
                observation.monotonic_ns,
            )
            return
        self._overflow_receipt = PrestartBoundaryOverflowReceipt(
            receipt.count + 1,
            receipt.first_boot_id,
            receipt.first_monotonic_ns,
            observation.boot_id,
            observation.monotonic_ns,
        )

    def _open_tail(self) -> CaptureStart | None:
        for start in reversed(self._pending):
            if start.blackout_id not in self._pending_end:
                return start
        return None


class PrestartRecoveryLane:
    """Submit one retained-start recovery through the sole storage writer."""

    def __init__(
        self,
        store: CaptureRecoveryEventStorePort,
        writer: CaptureWriter,
        callbacks: PrestartRecoveryCallbacks,
    ) -> None:
        self._store = store
        self._writer = writer
        self._callbacks = callbacks

    def submit(
        self,
        start: CaptureStart,
        current: PhysicalObservation,
        *,
        termination: str | None = None,
        overflow_delivery: OverflowDeliveryReservation | None = None,
    ) -> bool:
        kind = classify_physical_observation(current)
        command_kind = (
            CaptureCommandKind.END
            if termination is not None or kind == BlackoutKind.ONLINE
            else CaptureCommandKind.START
        )

        def execute() -> None:
            self._callbacks.on_success(
                persist_prestart_recovery(
                    self._store,
                    start,
                    current,
                    PrestartRecoveryPlan(termination, None, overflow_delivery),
                ),
                current,
            )

        def recover(_exc: Exception) -> bool | RecoveryDisposition:
            try:
                recovered = self._store.recover_startup()
            except Exception:
                recovered = None
            if recovered is not None and recovered.blackout_id == start.blackout_id:
                return self._resume(
                    start,
                    current,
                    PrestartRecoveryPlan(termination, recovered.handle, overflow_delivery),
                    recovered,
                )
            return self._reconcile_missing_start(start, current, termination, overflow_delivery)

        self._writer.clear_failed_scope(start.blackout_id)
        return self._writer.submit(
            CaptureCommand(
                kind=command_kind,
                execute=execute,
                scope_id=start.blackout_id,
                recover_failure=recover,
            )
        )

    def _resume(
        self,
        start: CaptureStart,
        current: PhysicalObservation,
        plan: PrestartRecoveryPlan,
        recovered: RecoveredCapture | None,
    ) -> bool:
        try:
            result = persist_prestart_recovery(
                self._store,
                start,
                current,
                plan,
            )
        except Exception as retry_exc:
            if recovered is None:
                handle = plan.recovered_handle
                if handle is None:
                    raise RuntimeError("pre-start retry lost its recovered handle") from retry_exc
                recovered = RecoveredCapture(
                    handle,
                    current.boot_id,
                    RecoveredObservation(
                        current.boot_id,
                        current.wall_time_utc.isoformat(),
                        current.monotonic_ns,
                        {},
                    ),
                )
            partial = recovered
            self._callbacks.on_partial_failure(
                partial,
                current,
                retry_exc,
                plan.overflow_delivery,
            )
            return False
        self._callbacks.on_success(result, current)
        return True

    def _reconcile_missing_start(
        self,
        start: CaptureStart,
        current: PhysicalObservation,
        termination: str | None,
        overflow_delivery: OverflowDeliveryReservation | None,
    ) -> bool | RecoveryDisposition:
        try:
            reconciliation = self._store.reconcile_damaged_close(start.blackout_id, None)
        except Exception:
            reconciliation = None
        if reconciliation is None:
            self._callbacks.on_no_durable_start(current, overflow_delivery)
            return False
        if reconciliation.state in {CaptureCloseState.END, CaptureCloseState.OUTCOME}:
            overflow_recorded = _durable_overflow_gap_present(
                self._store,
                reconciliation.handle,
                None if overflow_delivery is None else overflow_delivery.receipt,
            )
            self._callbacks.on_success(
                PrestartRecoveryResult(
                    None,
                    classify_physical_observation(current),
                    _overflow_delivery_outcome(
                        overflow_delivery,
                        proven=overflow_recorded,
                    ),
                ),
                current,
            )
            return RecoveryDisposition.TERMINAL_SUCCESS
        if reconciliation.state is CaptureCloseState.ACTIVE and reconciliation.handle is not None:
            return self._resume(
                start,
                current,
                PrestartRecoveryPlan(termination, reconciliation.handle, overflow_delivery),
                None,
            )
        self._callbacks.on_no_durable_start(current, overflow_delivery)
        return False


def persist_prestart_recovery(
    store: CaptureEventStorePort,
    start: CaptureStart,
    current: PhysicalObservation,
    plan: PrestartRecoveryPlan | None = None,
) -> PrestartRecoveryResult:
    """Write the original start, explicit loss gap, and current boundary."""
    plan = plan or PrestartRecoveryPlan()
    handle = plan.recovered_handle or _adopt_matching_recovered_handle(store, start)
    if handle is None:
        handle = start_capture(store, start)
    overflow_receipt = None if plan.overflow_delivery is None else plan.overflow_delivery.receipt
    handle = _append_missing_prestart_gaps(store, handle, current, overflow_receipt)
    current_kind = classify_physical_observation(current)
    if plan.termination is not None:
        append_end(store, handle, current, plan.termination)
        return PrestartRecoveryResult(
            None,
            current_kind,
            _overflow_delivery_outcome(plan.overflow_delivery, proven=True),
        )
    if current_kind == BlackoutKind.ONLINE:
        append_end(store, handle, current, "power_restored")
        return PrestartRecoveryResult(
            None,
            current_kind,
            _overflow_delivery_outcome(plan.overflow_delivery, proven=True),
        )
    handle = append_observation(store, handle, current)
    return PrestartRecoveryResult(
        handle,
        current_kind,
        _overflow_delivery_outcome(plan.overflow_delivery, proven=True),
    )


def _overflow_delivery_outcome(
    reservation: OverflowDeliveryReservation | None,
    *,
    proven: bool,
) -> OverflowDeliveryOutcome:
    if reservation is None:
        return OverflowDeliveryOutcome.not_attempted()
    if proven:
        return OverflowDeliveryOutcome.proven(reservation)
    return OverflowDeliveryOutcome.attempted(reservation)


def _append_missing_prestart_gaps(
    store: CaptureEventStorePort,
    handle: EventHandle,
    current: PhysicalObservation,
    overflow_receipt: PrestartBoundaryOverflowReceipt | None,
) -> EventHandle:
    """Resume the bounded gap prefix without duplicating durable records."""
    expected_next_seq = 2 + int(overflow_receipt is not None)
    if handle.next_seq > expected_next_seq:
        raise RuntimeError("pre-start recovery tail advanced beyond its planned gaps")
    if handle.next_seq == 1:
        handle = append_gap(store, handle, GapAppend(current, PRESTART_LOSS_REASON))
    if overflow_receipt is not None and handle.next_seq == 2:
        handle = append_gap(
            store,
            handle,
            GapAppend(
                current,
                "prestart_boundary_overflow",
                overflow_count=overflow_receipt.count,
                overflow_first_boot_id=overflow_receipt.first_boot_id,
                overflow_first_monotonic_ns=overflow_receipt.first_monotonic_ns,
                overflow_last_boot_id=overflow_receipt.last_boot_id,
                overflow_last_monotonic_ns=overflow_receipt.last_monotonic_ns,
            ),
        )
    if handle.next_seq != expected_next_seq:
        raise RuntimeError("pre-start recovery tail is missing an expected gap")
    return handle


def _durable_overflow_gap_present(
    store: CaptureRecoveryEventStorePort,
    handle: EventHandle | None,
    overflow_receipt: PrestartBoundaryOverflowReceipt | None,
) -> bool:
    """Prove the exact aggregate gap is present in the terminal projection."""
    if overflow_receipt is None or handle is None:
        return False
    try:
        projection = store.project(handle)
    except Exception:
        return False
    expected_payload = {
        "reason": "prestart_boundary_overflow",
        "overflow_count": str(overflow_receipt.count),
        "overflow_first_boot_id": overflow_receipt.first_boot_id,
        "overflow_first_monotonic_ns": str(overflow_receipt.first_monotonic_ns),
        "overflow_last_boot_id": overflow_receipt.last_boot_id,
        "overflow_last_monotonic_ns": str(overflow_receipt.last_monotonic_ns),
    }
    for record in projection.records:
        if (
            record.record_type != "gap"
            or record.provenance != "system"
            or record.blackout_id != handle.blackout_id
            or record.seq != 2
        ):
            continue
        if all(record.payload.get(key) == value for key, value in expected_payload.items()):
            return True
    return False


def _adopt_matching_recovered_handle(
    store: CaptureEventStorePort,
    start: CaptureStart,
) -> EventHandle | None:
    """Adopt a registry-authoritative START instead of opening a second event."""
    recovered = store.recover_startup()
    if recovered is None:
        return None
    if recovered.blackout_id != start.blackout_id:
        raise RuntimeError("active capture belongs to a different retained blackout")
    return recovered.handle
