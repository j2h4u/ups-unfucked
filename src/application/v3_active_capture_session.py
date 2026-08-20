"""Serialized dual-chain state for the v3 active blackout capture."""

from dataclasses import dataclass
from threading import Lock

from src.application.blackout_ports import BlackoutCaptureStorePort
from src.application.blackout_storage_values import (
    BlackoutCaptureCursor,
    BlackoutCaptureOpened,
    BlackoutChainKind,
    BlackoutProcessingRef,
    BlackoutProcessingStage,
    BlackoutRef,
    RecoveredCaptureWork,
)
from src.domain.blackout_capture import (
    DischargeGap,
    DischargeSample,
    DischargeSampleIdentity,
)
from src.domain.blackout_terminal import BlackoutEnd, BlackoutTermination
from src.domain.fragments import AnchorKind, EndpointAnchor


@dataclass(frozen=True, slots=True)
class _V3SessionState:
    ref: BlackoutRef
    physical_cursor: BlackoutCaptureCursor
    terminal_cursor: BlackoutCaptureCursor | None


class V3ActiveCaptureSession:
    """Own one v3 capture's physical and terminal append cursors."""

    def __init__(self, store: BlackoutCaptureStorePort) -> None:
        self._store = store
        self._lock = Lock()
        self._state: _V3SessionState | None = None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._state is not None

    @property
    def ref(self) -> BlackoutRef | None:
        with self._lock:
            return None if self._state is None else self._state.ref

    @property
    def physical_cursor(self) -> BlackoutCaptureCursor | None:
        with self._lock:
            return None if self._state is None else self._state.physical_cursor

    @property
    def terminal_cursor(self) -> BlackoutCaptureCursor | None:
        with self._lock:
            return None if self._state is None else self._state.terminal_cursor

    def attach(self, opened: BlackoutCaptureOpened) -> None:
        with self._lock:
            if self._state is not None:
                raise RuntimeError("v3 capture is already active")
            _require_scope(opened.ref, opened.cursor, BlackoutChainKind.PHYSICAL)
            self._state = _V3SessionState(opened.ref, opened.cursor, None)

    def attach_recovered(self, work: RecoveredCaptureWork) -> None:
        with self._lock:
            if self._state is not None:
                raise RuntimeError("v3 capture is already active")
            _require_scope(work.ref, work.cursor, BlackoutChainKind.PHYSICAL)
            if work.terminal_cursor is not None:
                _require_scope(work.ref, work.terminal_cursor, BlackoutChainKind.TERMINAL)
            self._state = _V3SessionState(work.ref, work.cursor, work.terminal_cursor)

    def append_sample(self, sample: DischargeSample) -> None:
        with self._lock:
            state = self._require(sample.blackout_id)
            _require_value_scope(state.ref, sample.blackout_id, sample.segment_id)
            cursor = state.physical_cursor
            if cursor.next_sequence is None:
                self._append_sample_after_rollover(state, cursor, sample)
                return
            try:
                returned = self._store.append_sample(state.ref, cursor, sample)
            except Exception as exc:
                if not getattr(exc, "rollover_required", False):
                    raise
                self._append_sample_after_rollover(state, cursor, sample)
                return
            _validate_advance(cursor, returned, BlackoutChainKind.PHYSICAL)
            self._state = _V3SessionState(state.ref, returned, state.terminal_cursor)

    def _append_sample_after_rollover(
        self,
        state: _V3SessionState,
        cursor: BlackoutCaptureCursor,
        sample: DischargeSample,
    ) -> None:
        if not callable(getattr(self._store, "rollover", None)):
            _require_appendable(cursor)
            return
        opened = self._store.rollover(state.ref, cursor)
        _require_scope(opened.ref, opened.cursor, BlackoutChainKind.PHYSICAL)
        successor_sample = _rebind_sample_scope(sample, opened.ref)
        returned = self._store.append_sample(opened.ref, opened.cursor, successor_sample)
        _validate_advance(opened.cursor, returned, BlackoutChainKind.PHYSICAL)
        self._state = _V3SessionState(opened.ref, returned, None)

    def append_gap(self, gap: DischargeGap) -> None:
        with self._lock:
            state = self._require(gap.blackout_id)
            _require_value_scope(state.ref, gap.blackout_id, gap.segment_id)
            _require_appendable(state.physical_cursor)
            returned = self._store.append_gap(state.ref, state.physical_cursor, gap)
            _validate_advance(state.physical_cursor, returned, BlackoutChainKind.PHYSICAL)
            self._state = _V3SessionState(state.ref, returned, state.terminal_cursor)

    def append_anchor(self, anchor: EndpointAnchor) -> None:
        with self._lock:
            state = self._require(anchor.blackout_id)
            _require_value_scope(state.ref, anchor.blackout_id, anchor.segment_id)
            chain = _anchor_chain(anchor.kind)
            cursor = (
                state.physical_cursor
                if chain is BlackoutChainKind.PHYSICAL
                else (state.terminal_cursor or state.physical_cursor)
            )
            _require_appendable(cursor)
            returned = self._store.append_anchor(state.ref, cursor, anchor)
            _validate_advance(cursor, returned, chain)
            self._state = _V3SessionState(
                state.ref,
                returned if chain is BlackoutChainKind.PHYSICAL else state.physical_cursor,
                returned if chain is BlackoutChainKind.TERMINAL else state.terminal_cursor,
            )

    def close(self, end: BlackoutEnd) -> BlackoutProcessingRef:
        with self._lock:
            state = self._require(end.blackout_id)
            _require_value_scope(state.ref, end.blackout_id, end.segment_id)
            if end.termination is BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED:
                # The budget END remains anchorless in its domain payload.  Its
                # envelope roots only when no terminal record exists; otherwise
                # the existing terminal cursor supplies the linked wire position.
                cursor = state.terminal_cursor or state.physical_cursor
            else:
                if state.terminal_cursor is None:
                    raise RuntimeError("normal END requires a terminal cursor")
                if end.terminal_anchor_record_hash != state.terminal_cursor.last_record_sha256:
                    raise RuntimeError("END does not link the terminal cursor")
                cursor = state.terminal_cursor
            _require_appendable(cursor)
            result = self._store.close(state.ref, cursor, end)
            _validate_close_result(state.ref, result)
            self._state = None
            return result

    def _require(self, blackout_id: str) -> _V3SessionState:
        if self._state is None:
            raise RuntimeError("v3 capture has no durable start")
        if self._state.ref.blackout_id != blackout_id:
            raise RuntimeError("capture command belongs to a different blackout")
        return self._state


def _anchor_chain(kind: AnchorKind) -> BlackoutChainKind:
    if kind in {AnchorKind.TRANSFER_TO_BATTERY, AnchorKind.RAW_FIRMWARE_LB}:
        return BlackoutChainKind.PHYSICAL
    return BlackoutChainKind.TERMINAL


def _require_scope(
    ref: BlackoutRef, cursor: BlackoutCaptureCursor, chain: BlackoutChainKind
) -> None:
    if cursor.chain is not chain:
        raise ValueError("cursor chain does not match the v3 session role")
    _require_value_scope(ref, cursor.blackout_id, cursor.segment_id)


def _require_value_scope(ref: BlackoutRef, blackout_id: str, segment_id: str) -> None:
    if (ref.blackout_id, ref.segment_id) != (blackout_id, segment_id):
        raise ValueError("v3 value scope differs from the active capture")


def _rebind_sample_scope(sample: DischargeSample, ref: BlackoutRef) -> DischargeSample:
    """Copy accepted telemetry into the successor aggregate's value scope."""
    return DischargeSample.from_telemetry(
        sample.sequence,
        sample.captured,
        DischargeSampleIdentity(
            ref.blackout_id,
            sample.physical_episode_id,
            sample.battery_epoch_id,
            ref.segment_id,
            sample.observation_origin,
            sample.uat_intent_id,
        ),
    )


def _require_appendable(cursor: BlackoutCaptureCursor) -> None:
    if cursor.next_sequence is None:
        raise ValueError("v3 cursor is exhausted")


def _validate_advance(
    previous: BlackoutCaptureCursor,
    returned: BlackoutCaptureCursor,
    chain: BlackoutChainKind,
) -> None:
    _require_scope(BlackoutRef(previous.blackout_id, previous.segment_id), returned, chain)
    if previous.next_sequence is None:
        raise ValueError("v3 store cannot append from an exhausted cursor")
    expected_sequence = (
        1
        if previous.chain is BlackoutChainKind.PHYSICAL and chain is BlackoutChainKind.TERMINAL
        else None
        if previous.next_sequence == 3_197
        else previous.next_sequence + 1
    )
    if returned.next_sequence != expected_sequence:
        raise ValueError("v3 store returned a non-advancing cursor")
    if returned.last_record_sha256 is None:
        raise ValueError("v3 store returned a cursor without a record hash")


def _validate_close_result(ref: BlackoutRef, result: BlackoutProcessingRef) -> None:
    if not isinstance(result, BlackoutProcessingRef):
        raise ValueError("v3 store returned an invalid processing ref")
    if result.ref != ref:
        raise ValueError("v3 store returned a processing ref for a different capture")
    if result.stage is not BlackoutProcessingStage.PROCESSING:
        raise ValueError("v3 store returned a non-processing handoff")
