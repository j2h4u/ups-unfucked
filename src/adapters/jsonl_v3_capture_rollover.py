"""Atomic successor-aggregate rollover for bounded v3 physical capture."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Protocol

from src.adapters.jsonl_v3_blackout_start_codec import decode_blackout_start, encode_blackout_start
from src.adapters.jsonl_v3_canonical import decode_v3_record
from src.adapters.jsonl_v3_errors import (
    V3AppendConflict,
    V3CapacityError,
    V3FileNotFound,
    V3PersistenceError,
    V3ValidationError,
)
from src.adapters.jsonl_v3_filesystem import JsonlV3Filesystem, V3FaultPoint, V3WriteTransaction
from src.adapters.jsonl_v3_registry import JsonlV3WorkRegistry, RegistrySnapshot, V3WorkRegistry
from src.adapters.jsonl_v3_registry_values import (
    MAX_CAPTURE_BYTES,
    MAX_CHAIN_SEQUENCE,
    MAX_PENDING_ENTRIES,
    CapturingState,
    ProcessingState,
    V3RolloverReservation,
    V3StorageSegmentReceipt,
)
from src.adapters.jsonl_v3_segment_index import OffsetRecordKind, SegmentIndexEntry
from src.adapters.jsonl_v3_storage_paths import (
    V3OffsetPathToken,
    V3SegmentPathToken,
    validate_uuid4_hex,
)
from src.adapters.jsonl_v3_terminal_tail_codec import encode_blackout_end
from src.application.blackout_storage_values import (
    BlackoutCaptureCursor,
    BlackoutCaptureOpened,
    BlackoutChainKind,
    BlackoutRef,
    BlackoutStart,
)
from src.domain.blackout_terminal import (
    BlackoutEnd,
    BlackoutTermination,
    BudgetKind,
    ContinuationKind,
)


class _RolloverOwner(Protocol):
    filesystem: JsonlV3Filesystem
    _registry: JsonlV3WorkRegistry
    _uuid4_hex: Callable[[], str]

    def _new_uuid(self) -> str: ...

    def _make_reservation(
        self, tx: V3WriteTransaction, state: CapturingState, budget_kind: BudgetKind
    ) -> V3RolloverReservation: ...
    def _resume_rollover(
        self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
    ) -> BlackoutCaptureOpened: ...
    def _write_successor(
        self,
        tx: V3WriteTransaction,
        snapshot: RegistrySnapshot,
        state: CapturingState,
        reservation: V3RolloverReservation,
    ) -> RegistrySnapshot: ...
    def _write_carrier(
        self,
        tx: V3WriteTransaction,
        snapshot: RegistrySnapshot,
        state: CapturingState,
        reservation: V3RolloverReservation,
    ) -> RegistrySnapshot: ...
    def _swap_successor(
        self,
        tx: V3WriteTransaction,
        snapshot: RegistrySnapshot,
        state: CapturingState,
        reservation: V3RolloverReservation,
    ) -> BlackoutCaptureOpened: ...
    def _terminal_root_sha256(self, tx: V3WriteTransaction, blackout_id: str) -> str: ...
    def _successor_offset(self, reservation: V3RolloverReservation) -> V3OffsetPathToken: ...
    def _record_hash(self, line: bytes) -> str: ...
    def _processing_reservation(
        self,
        tx: V3WriteTransaction,
        processing: ProcessingState,
        state: CapturingState,
    ) -> V3RolloverReservation: ...
    def _reserve_processing_rollover(
        self,
        tx: V3WriteTransaction,
        snapshot: RegistrySnapshot,
        processing: ProcessingState,
    ) -> BlackoutCaptureOpened: ...
    def _processing_start(self, tx: V3WriteTransaction, state: CapturingState) -> BlackoutStart: ...


def _processing_as_capturing(processing: ProcessingState) -> CapturingState:
    return CapturingState(
        "capturing",
        processing.blackout_id,
        processing.logical_segment_id,
        processing.physical_episode_id,
        processing.battery_epoch_id,
        processing.observation_origin,
        processing.uat_intent_id,
        processing.frozen_policy_revision,
        processing.physical_cursor,
        processing.terminal_cursor_after_end,
        processing.capture_bytes,
        processing.capture_record_count,
        processing.sample_count,
        processing.gap_count,
        processing.storage_segments,
        None,
        None,
        None,
        None,
    )


def _find_processing(snapshot: RegistrySnapshot, ref: BlackoutRef) -> ProcessingState | None:
    return next(
        (
            item
            for item in snapshot.state.pending
            if isinstance(item, ProcessingState)
            and (item.blackout_id, item.logical_segment_id) == (ref.blackout_id, ref.segment_id)
        ),
        None,
    )


def _carrier_processing(
    state: CapturingState,
    reservation: V3RolloverReservation,
    terminal_root_sha256: str,
) -> ProcessingState:
    """Represent the old carrier END as pending while successor capture is active."""
    try:
        end_record = decode_v3_record(reservation.carrier_end_line_utf8.encode())
    except ValueError as exc:
        raise V3PersistenceError("rollover carrier END is unreadable") from exc
    end_hash = end_record.record_sha256
    end_sequence = end_record.envelope.seq
    next_sequence = None if end_sequence == MAX_CHAIN_SEQUENCE else end_sequence + 1
    terminal_cursor = BlackoutCaptureCursor(
        reservation.old_blackout_id,
        reservation.old_logical_segment_id,
        BlackoutChainKind.TERMINAL,
        next_sequence,
        end_hash,
    )
    return ProcessingState(
        "processing",
        reservation.old_blackout_id,
        reservation.old_logical_segment_id,
        reservation.physical_episode_id,
        state.battery_epoch_id,
        state.observation_origin,
        state.uat_intent_id,
        state.frozen_policy_revision,
        state.physical_cursor,
        terminal_cursor,
        terminal_root_sha256,
        None,
        end_hash,
        state.capture_bytes,
        state.capture_record_count,
        state.sample_count,
        state.gap_count,
        state.storage_segments,
        None,
    )


def _published_successor(
    owner: _RolloverOwner,
    tx: V3WriteTransaction,
    processing: ProcessingState,
    active: CapturingState,
) -> BlackoutCaptureOpened | None:
    """Return an already-swapped successor without allocating another one."""
    if active.blackout_id == processing.blackout_id:
        return None
    try:
        start = owner._processing_start(tx, active)
    except (V3FileNotFound, V3PersistenceError, ValueError):
        return None
    if (
        start.continued_from != processing.blackout_id
        or start.continuation_kind is not ContinuationKind.SIZE_ROLLOVER
    ):
        return None
    return BlackoutCaptureOpened(
        BlackoutRef(active.blackout_id, active.logical_segment_id),
        active.physical_cursor,
    )


class JsonlV3CaptureRolloverMixin:
    """Persist successor bytes before publishing the successor as active."""

    def _new_uuid(self: _RolloverOwner) -> str:
        value = self._uuid4_hex()
        try:
            return validate_uuid4_hex(value, "storage ID")
        except V3ValidationError as exc:
            raise V3ValidationError("storage ID is invalid") from exc

    def rollover(
        self: _RolloverOwner,
        ref: BlackoutRef,
        cursor: BlackoutCaptureCursor,
        *,
        budget_kind: BudgetKind = BudgetKind.BYTES,
    ) -> BlackoutCaptureOpened:
        if cursor.chain is not BlackoutChainKind.PHYSICAL or cursor.next_sequence is None:
            raise V3ValidationError("rollover requires an appendable physical cursor")
        if budget_kind not in (BudgetKind.BYTES, BudgetKind.SEGMENT_REFS):
            raise V3ValidationError("rollover budget kind is invalid")
        with self.filesystem.write_transaction() as tx:
            snapshot = self._registry.read(tx)
            state = snapshot.state.capture
            if not isinstance(state, CapturingState) or state.physical_cursor != cursor:
                raise V3AppendConflict()
            if ref != BlackoutRef(state.blackout_id, state.logical_segment_id):
                raise V3AppendConflict()
            if len(snapshot.state.pending) >= MAX_PENDING_ENTRIES:
                raise V3CapacityError("pending capture capacity is exhausted")
            reservation = self._make_reservation(tx, state, budget_kind)
            reserved = replace(state, rollover=reservation)
            snapshot = self._registry.compare_and_replace(
                tx, expected=snapshot, replacement=V3WorkRegistry(reserved, snapshot.state.pending)
            )
            tx._fault(V3FaultPoint.ROLLOVER_AFTER_REGISTRY_RESERVE)
            return self._resume_rollover(tx, snapshot)

    def rollover_damaged_processing(
        self: _RolloverOwner, ref: BlackoutRef
    ) -> BlackoutCaptureOpened:
        """Publish a successor for a finalized reference-64 damage carrier.

        The caller invokes this after the old aggregate has durably written its
        corruption anchor and ``CAPTURE_DAMAGED`` END, so the old aggregate is
        already a ``ProcessingState``.  The transaction first records a normal
        rollover reservation in a transient active state, then reuses the
        ordinary successor-before-carrier/swap recovery path.  The existing
        processing item remains in ``pending``; it is never recreated or
        removed by this helper.

        The method owns its write transaction and is safe to retry after any
        injected fault.  It returns the successor's ref and physical cursor.
        """
        with self.filesystem.write_transaction() as tx:
            snapshot = self._registry.read(tx)
            processing = _find_processing(snapshot, ref)
            if processing is None:
                raise V3AppendConflict()
            active = snapshot.state.capture
            if isinstance(active, CapturingState):
                if active.rollover is not None:
                    if (
                        active.rollover.old_blackout_id != processing.blackout_id
                        or active.rollover.old_logical_segment_id != processing.logical_segment_id
                    ):
                        raise V3AppendConflict()
                    return self._resume_rollover(tx, snapshot)
                published = _published_successor(self, tx, processing, active)
                if published is not None:
                    return published
                raise V3AppendConflict()
            return self._reserve_processing_rollover(tx, snapshot, processing)

    def _find_processing(
        self, snapshot: RegistrySnapshot, ref: BlackoutRef
    ) -> ProcessingState | None:
        return next(
            (
                item
                for item in snapshot.state.pending
                if isinstance(item, ProcessingState)
                and (item.blackout_id, item.logical_segment_id) == (ref.blackout_id, ref.segment_id)
            ),
            None,
        )

    def _reserve_processing_rollover(
        self: _RolloverOwner,
        tx: V3WriteTransaction,
        snapshot: RegistrySnapshot,
        processing: ProcessingState,
    ) -> BlackoutCaptureOpened:
        state = _processing_as_capturing(processing)
        reservation = self._processing_reservation(tx, processing, state)
        reserved = replace(state, rollover=reservation)
        intended = self._registry.compare_and_replace(
            tx,
            expected=snapshot,
            replacement=V3WorkRegistry(reserved, snapshot.state.pending),
        )
        tx._fault(V3FaultPoint.ROLLOVER_AFTER_REGISTRY_RESERVE)
        return self._resume_rollover(tx, intended)

    def _processing_reservation(
        self: _RolloverOwner,
        tx: V3WriteTransaction,
        processing: ProcessingState,
        state: CapturingState,
    ) -> V3RolloverReservation:
        """Build an exact reservation around an already durable terminal END."""
        receipt = state.storage_segments[-1]
        if type(receipt.path_token) is not V3SegmentPathToken:
            raise V3PersistenceError("damage rollover requires an active final segment")
        start = self._processing_start(tx, state)
        successor_id, successor_segment, successor_storage = (
            self._new_uuid(),
            self._new_uuid(),
            self._new_uuid(),
        )
        successor = replace(
            start,
            blackout_id=successor_id,
            segment_id=successor_segment,
            continued_from=state.blackout_id,
            continuation_kind=ContinuationKind.SIZE_ROLLOVER,
        )
        encoded_start = encode_blackout_start(successor)
        paths = self.filesystem.paths
        if paths is None:
            raise V3PersistenceError()
        terminal = processing.terminal_cursor_after_end
        if terminal.next_sequence is None:
            raise V3CapacityError("terminal rollover budget is exhausted")
        carrier = BlackoutEnd(
            processing.blackout_id,
            processing.physical_episode_id,
            processing.battery_epoch_id,
            processing.logical_segment_id,
            BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED,
            start.observation_origin,
            datetime.now(timezone.utc),
            start.monotonic_ns,
            start.boot_id,
            budget_kind=BudgetKind.SEGMENT_REFS,
            continued_by=successor_id,
            continuation_kind=ContinuationKind.SIZE_ROLLOVER,
            uat_intent_id=processing.uat_intent_id,
        )
        carrier_line = encode_blackout_end(
            carrier,
            seq=terminal.next_sequence,
            previous_record_sha256=terminal.last_record_sha256,
        ).line
        successor_path = paths.segment_token(
            receipt.path_token.started_utc,
            successor_id,
            successor_segment,
            0,
            successor_storage,
        )
        return V3RolloverReservation(
            "reserved",
            BudgetKind.SEGMENT_REFS.value,
            processing.blackout_id,
            processing.logical_segment_id,
            processing.physical_episode_id,
            receipt.storage_id,
            receipt.path_token,
            successor_id,
            successor_segment,
            successor_storage,
            successor_path,
            encoded_start.line.decode(),
            carrier_line.decode(),
            "size_rollover",
            hashlib.sha256(encoded_start.line).hexdigest(),
            hashlib.sha256(carrier_line).hexdigest(),
            len(encoded_start.line),
            len(carrier_line),
        )

    def _processing_start(
        self: _RolloverOwner, tx: V3WriteTransaction, state: CapturingState
    ) -> BlackoutStart:
        first = next((item for item in state.storage_segments if item.first_seq == 0), None)
        if first is None:
            raise V3PersistenceError("rollover START segment is missing")
        start_entry = tx.get_offset_index(first.offset_token, sequence=0)
        if start_entry is None:
            raise V3PersistenceError("rollover START offset is missing")
        start_line = tx.read_region_bounded(
            first.path_token,
            offset=start_entry.file_offset,
            length=start_entry.line_length,
            max_file_bytes=MAX_CAPTURE_BYTES,
        ).contents
        return decode_blackout_start(start_line)

    def _make_reservation(
        self: _RolloverOwner,
        tx: V3WriteTransaction,
        state: CapturingState,
        budget_kind: BudgetKind,
    ) -> V3RolloverReservation:
        receipt = state.storage_segments[-1]
        if type(receipt.path_token) is not V3SegmentPathToken:
            raise V3PersistenceError("rollover requires an active segment")
        if type(receipt.offset_token) is not V3OffsetPathToken:
            raise V3PersistenceError("rollover requires an active offset index")
        start_receipt = next(
            (candidate for candidate in state.storage_segments if candidate.first_seq == 0),
            None,
        )
        if start_receipt is None:
            raise V3PersistenceError("rollover START segment is missing")
        start_entry = tx.get_offset_index(start_receipt.offset_token, sequence=0)
        if start_entry is None:
            raise V3PersistenceError("rollover START offset is missing")
        start_raw = tx.read_region_bounded(
            start_receipt.path_token,
            offset=start_entry.file_offset,
            length=start_entry.line_length,
            max_file_bytes=MAX_CAPTURE_BYTES,
        ).contents
        start = decode_blackout_start(start_raw)
        successor_id = self._new_uuid()
        successor_segment = self._new_uuid()
        successor_storage = self._new_uuid()
        successor = replace(
            start,
            blackout_id=successor_id,
            segment_id=successor_segment,
            continued_from=state.blackout_id,
            continuation_kind=ContinuationKind.SIZE_ROLLOVER,
        )
        encoded_start = encode_blackout_start(successor)
        carrier = BlackoutEnd(
            state.blackout_id,
            state.physical_episode_id,
            state.battery_epoch_id,
            state.logical_segment_id,
            BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED,
            start.observation_origin,
            datetime.now(timezone.utc),
            start.monotonic_ns,
            start.boot_id,
            budget_kind=budget_kind,
            continued_by=successor_id,
            continuation_kind=ContinuationKind.SIZE_ROLLOVER,
            uat_intent_id=state.uat_intent_id,
        )
        terminal_sequence = (
            0 if state.terminal_cursor is None else state.terminal_cursor.next_sequence
        )
        if terminal_sequence is None:
            raise V3CapacityError("terminal rollover budget is exhausted")
        encoded_end = encode_blackout_end(
            carrier,
            seq=terminal_sequence,
            previous_record_sha256=(
                None if state.terminal_cursor is None else state.terminal_cursor.last_record_sha256
            ),
        )
        paths = self.filesystem.paths
        if paths is None:
            raise V3PersistenceError()
        successor_path = paths.segment_token(
            receipt.path_token.started_utc,
            successor_id,
            successor_segment,
            0,
            successor_storage,
        )
        return V3RolloverReservation(
            "reserved",
            budget_kind.value,
            state.blackout_id,
            state.logical_segment_id,
            state.physical_episode_id,
            receipt.storage_id,
            receipt.path_token,
            successor_id,
            successor_segment,
            successor_storage,
            successor_path,
            encoded_start.line.decode(),
            encoded_end.line.decode(),
            "size_rollover",
            hashlib.sha256(encoded_start.line).hexdigest(),
            hashlib.sha256(encoded_end.line).hexdigest(),
            len(encoded_start.line),
            len(encoded_end.line),
        )

    def _resume_rollover(
        self: _RolloverOwner, tx: V3WriteTransaction, snapshot: RegistrySnapshot
    ) -> BlackoutCaptureOpened:
        state = snapshot.state.capture
        if not isinstance(state, CapturingState) or state.rollover is None:
            raise V3AppendConflict()
        reservation = state.rollover
        if reservation.phase == "reserved":
            snapshot = self._write_successor(tx, snapshot, state, reservation)
            state = snapshot.state.capture
            if not isinstance(state, CapturingState) or state.rollover is None:
                raise V3AppendConflict()
            reservation = state.rollover
        if reservation.phase == "successor_started":
            snapshot = self._write_carrier(tx, snapshot, state, reservation)
            state = snapshot.state.capture
            if not isinstance(state, CapturingState) or state.rollover is None:
                raise V3AppendConflict()
            reservation = state.rollover
        if reservation.phase != "carrier_ended":
            raise V3PersistenceError("rollover did not reach carrier END")
        return self._swap_successor(tx, snapshot, state, reservation)

    def _successor_offset(
        self: _RolloverOwner, reservation: V3RolloverReservation
    ) -> V3OffsetPathToken:
        paths = self.filesystem.paths
        if paths is None:
            raise V3PersistenceError()
        return paths.offset_token(reservation.successor_path_token)

    def _write_successor(
        self: _RolloverOwner,
        tx: V3WriteTransaction,
        snapshot: RegistrySnapshot,
        state: CapturingState,
        reservation: V3RolloverReservation,
    ) -> RegistrySnapshot:
        line = reservation.successor_start_line_utf8.encode()
        try:
            raw = tx.read_region_bounded(
                reservation.successor_path_token,
                offset=0,
                length=len(line),
                max_file_bytes=MAX_CAPTURE_BYTES,
            ).contents
            if raw != line:
                raise V3PersistenceError("successor START bytes differ")
        except V3FileNotFound:
            tx.create_and_sync(reservation.successor_path_token, line, MAX_CAPTURE_BYTES)
            tx._fault(V3FaultPoint.ROLLOVER_AFTER_SUCCESSOR_CREATE)
            tx._fault(V3FaultPoint.ROLLOVER_AFTER_SUCCESSOR_FDATASYNC)
            tx._fault(V3FaultPoint.ROLLOVER_AFTER_SUCCESSOR_DIRSYNC)
        offset: V3OffsetPathToken = self._successor_offset(reservation)
        index = tx.create_offset_index(offset)
        start_record = SegmentIndexEntry(
            0, 0, len(line), self._record_hash(line), OffsetRecordKind.START
        )
        if tx.get_offset_index(offset, sequence=0) is None:
            tx.append_offset_index(offset, expected=index, entry=start_record)
        updated = replace(reservation, phase="successor_started")
        return self._registry.compare_and_replace(
            tx,
            expected=snapshot,
            replacement=V3WorkRegistry(replace(state, rollover=updated), snapshot.state.pending),
        )

    def _write_carrier(
        self: _RolloverOwner,
        tx: V3WriteTransaction,
        snapshot: RegistrySnapshot,
        state: CapturingState,
        reservation: V3RolloverReservation,
    ) -> RegistrySnapshot:
        paths = self.filesystem.paths
        if paths is None:
            raise V3PersistenceError()
        token = paths.terminal_staging_token(state.blackout_id)
        line = reservation.carrier_end_line_utf8.encode()
        try:
            raw, _ = tx.read_bounded(token, max_bytes=2 * 1024 * 1024)
            if raw != line:
                # A terminal marker may already root the carrier chain.  The
                # reservation stores the exact END bytes, so recovery accepts
                # the already-appended suffix or appends it at the read-back
                # length; it never rewrites the marker or roots a second chain.
                if raw.endswith(line):
                    pass
                else:
                    tx.append_and_sync(
                        token,
                        expected_offset=len(raw),
                        contents=line,
                        max_result_bytes=2 * 1024 * 1024,
                    )
                    tx._fault(V3FaultPoint.ROLLOVER_AFTER_CARRIER_END_FDATASYNC)
        except V3FileNotFound:
            tx.create_and_sync(token, line, 2 * 1024 * 1024)
            tx._fault(V3FaultPoint.ROLLOVER_AFTER_CARRIER_END_FDATASYNC)
        updated = replace(reservation, phase="carrier_ended")
        return self._registry.compare_and_replace(
            tx,
            expected=snapshot,
            replacement=V3WorkRegistry(replace(state, rollover=updated), snapshot.state.pending),
        )

    def _swap_successor(
        self: _RolloverOwner,
        tx: V3WriteTransaction,
        snapshot: RegistrySnapshot,
        state: CapturingState,
        reservation: V3RolloverReservation,
    ) -> BlackoutCaptureOpened:
        line = reservation.successor_start_line_utf8.encode()
        record_hash = self._record_hash(line)
        receipt = V3StorageSegmentReceipt(
            0,
            reservation.successor_storage_id,
            reservation.successor_path_token,
            self._successor_offset(reservation),
            len(line),
            0,
            0,
            record_hash,
            None,
            False,
        )
        cursor = BlackoutCaptureCursor(
            reservation.successor_blackout_id,
            reservation.successor_logical_segment_id,
            BlackoutChainKind.PHYSICAL,
            1,
            record_hash,
        )
        successor = CapturingState(
            "capturing",
            reservation.successor_blackout_id,
            reservation.successor_logical_segment_id,
            state.physical_episode_id,
            state.battery_epoch_id,
            state.observation_origin,
            state.uat_intent_id,
            state.frozen_policy_revision,
            cursor,
            None,
            len(line),
            1,
            0,
            0,
            (receipt,),
            None,
            None,
            None,
            None,
        )
        carrier = _carrier_processing(
            state,
            reservation,
            self._terminal_root_sha256(tx, reservation.old_blackout_id),
        )
        already_pending = any(
            isinstance(item, ProcessingState)
            and item.blackout_id == carrier.blackout_id
            and item.logical_segment_id == carrier.logical_segment_id
            for item in snapshot.state.pending
        )
        pending = snapshot.state.pending if already_pending else (*snapshot.state.pending, carrier)
        self._registry.compare_and_replace(
            tx, expected=snapshot, replacement=V3WorkRegistry(successor, pending)
        )
        tx._fault(V3FaultPoint.ROLLOVER_AFTER_REGISTRY_SWAP)
        return BlackoutCaptureOpened(
            BlackoutRef(
                reservation.successor_blackout_id, reservation.successor_logical_segment_id
            ),
            cursor,
        )

    def _record_hash(self, line: bytes) -> str:
        from src.adapters.jsonl_v3_canonical import decode_v3_record

        value = decode_v3_record(line).envelope.record_sha256
        if value is None:
            raise V3PersistenceError("record hash is missing")
        return value

    def _terminal_root_sha256(
        self: _RolloverOwner, tx: V3WriteTransaction, blackout_id: str
    ) -> str:
        paths = self.filesystem.paths
        if paths is None:
            raise V3PersistenceError()
        raw, _ = tx.read_bounded(
            paths.terminal_staging_token(blackout_id), max_bytes=2 * 1024 * 1024
        )
        lines = raw.splitlines(keepends=True)
        if not lines:
            raise V3PersistenceError("rollover terminal chain is empty")
        root = decode_v3_record(lines[0]).envelope.record_sha256
        if root is None:
            raise V3PersistenceError("rollover terminal root hash is missing")
        return root
