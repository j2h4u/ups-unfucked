"""Bounded recovery-page facade for the v3 capture store."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.adapters.jsonl_v3_canonical import EncodedV3Record, decode_v3_record
from src.adapters.jsonl_v3_errors import (
    V3AppendConflict,
    V3CapacityError,
    V3FileNotFound,
    V3PersistenceError,
    V3ValidationError,
)
from src.adapters.jsonl_v3_filesystem import JsonlV3Filesystem, V3WriteTransaction
from src.adapters.jsonl_v3_registry import (
    JsonlV3WorkRegistry,
    RegistrySnapshot,
    recovery_page,
)
from src.adapters.jsonl_v3_registry_values import (
    MAX_PENDING_ENTRIES,
    CapturingState,
    ProcessingState,
    TailState,
)
from src.adapters.jsonl_v3_terminal_tail_codec import encode_blackout_end
from src.application.blackout_storage_values import (
    MAX_RECOVERY_PAGE_SIZE,
    BlackoutCaptureCursor,
    BlackoutChainKind,
    BlackoutProcessingRef,
    BlackoutProcessingStage,
    BlackoutRecoveryCursor,
    BlackoutRecoveryPage,
    BlackoutRef,
    RecoveredCaptureWork,
)
from src.domain.blackout_terminal import BlackoutEnd, BlackoutTermination


class JsonlV3CaptureRecoveryFacadeMixin:
    """Expose bounded recovery pages and terminal-chain recovery metadata."""

    if TYPE_CHECKING:
        filesystem: JsonlV3Filesystem
        _registry: JsonlV3WorkRegistry

        def _recover_active_capture(
            self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
        ) -> RegistrySnapshot | None: ...

        def _finalize_reference_damage(
            self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, state: CapturingState
        ) -> object: ...

        def rollover_damaged_processing(self, ref: BlackoutRef) -> object: ...

    def _terminal_root_sha256(self, tx: V3WriteTransaction, blackout_id: str) -> str:
        paths = self.filesystem.paths
        if paths is None:
            raise V3PersistenceError()
        try:
            raw, _ = tx.read_bounded(
                paths.terminal_staging_token(blackout_id), max_bytes=2 * 1024 * 1024
            )
            first = raw.splitlines(keepends=True)[0]
            return decode_v3_record(first).record_sha256
        except (V3FileNotFound, ValueError, IndexError) as exc:
            raise V3PersistenceError("terminal chain root is unreadable") from exc

    def _terminal_end_closing_anchor_sha256(
        self, tx: V3WriteTransaction, blackout_id: str, previous_hash: str
    ) -> str | None:
        paths = self.filesystem.paths
        if paths is None:
            raise V3PersistenceError()
        try:
            raw, _ = tx.read_bounded(
                paths.terminal_staging_token(blackout_id), max_bytes=2 * 1024 * 1024
            )
            record = decode_v3_record(raw.splitlines(keepends=True)[-1])
        except (V3FileNotFound, ValueError, IndexError) as exc:
            raise V3PersistenceError("terminal END is unreadable") from exc
        return previous_hash if record.envelope.payload.get("terminal_anchor_record_hash") else None

    def _ensure_pending_capacity(self, snapshot: RegistrySnapshot) -> None:
        if len(snapshot.state.pending) >= MAX_PENDING_ENTRIES:
            raise V3CapacityError("pending capture capacity is exhausted")

    def _pending_end_hash(self, cursor: BlackoutCaptureCursor, end: BlackoutEnd) -> str:
        budget = end.termination is BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED
        sequence = 0 if budget else cursor.next_sequence
        previous = None if budget else cursor.last_record_sha256
        if sequence is None:
            raise V3AppendConflict()
        return encode_blackout_end(end, seq=sequence, previous_record_sha256=previous).record_sha256

    def _pending_result(
        self,
        tx: V3WriteTransaction,
        snapshot: RegistrySnapshot,
        ref: BlackoutRef,
        cursor: BlackoutCaptureCursor,
        end: BlackoutEnd,
    ) -> BlackoutProcessingRef | None:
        for item in snapshot.state.pending:
            if (item.blackout_id, item.logical_segment_id) != (ref.blackout_id, ref.segment_id):
                continue
            self._require_pending_retry(tx, item, cursor, end)
            if isinstance(item, ProcessingState):
                return BlackoutProcessingRef(
                    ref,
                    BlackoutProcessingStage.PROCESSING,
                    item.terminal_end_sha256,
                    item.frozen_policy_revision,
                )
            return BlackoutProcessingRef(
                ref,
                BlackoutProcessingStage.TAIL,
                item.terminal_outcome_sha256,
                item.frozen_policy_revision,
            )
        return None

    def _require_pending_retry(
        self,
        tx: V3WriteTransaction,
        item: ProcessingState | TailState,
        cursor: BlackoutCaptureCursor,
        end: BlackoutEnd,
    ) -> None:
        record = self._pending_end_record(tx, item)
        candidate = encode_blackout_end(
            end,
            seq=record.envelope.seq,
            previous_record_sha256=record.envelope.prev_record_sha256,
        )
        if candidate.line != record.line or record.record_sha256 != item.terminal_end_sha256:
            raise V3AppendConflict()
        if end.termination is BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED:
            expected = item.physical_cursor
        else:
            expected = BlackoutCaptureCursor(
                item.blackout_id,
                item.logical_segment_id,
                BlackoutChainKind.TERMINAL,
                record.envelope.seq,
                record.envelope.prev_record_sha256,
            )
        if cursor != expected:
            raise V3AppendConflict()

    def _pending_end_record(
        self, tx: V3WriteTransaction, item: ProcessingState | TailState
    ) -> EncodedV3Record:
        paths = self.filesystem.paths
        if paths is None:
            raise V3PersistenceError()
        token = (
            item.tail_path_token
            if isinstance(item, TailState)
            else paths.terminal_staging_token(item.blackout_id)
        )
        try:
            raw, _ = tx.read_bounded(token, max_bytes=2 * 1024 * 1024)
            for line in raw.splitlines(keepends=True):
                record = decode_v3_record(line)
                if record.record_sha256 == item.terminal_end_sha256:
                    return record
        except (V3FileNotFound, ValueError, IndexError) as exc:
            raise V3PersistenceError("pending terminal END is unreadable") from exc
        raise V3PersistenceError("pending terminal END is unreadable")

    def recover(
        self, cursor: BlackoutRecoveryCursor | None = None, *, limit: int = MAX_RECOVERY_PAGE_SIZE
    ) -> BlackoutRecoveryPage:
        if isinstance(limit, bool) or not 1 <= limit <= MAX_RECOVERY_PAGE_SIZE:
            raise V3ValidationError("recovery page limit is invalid")
        # Each recovery phase is committed in its own transaction.  This is
        # important for reference-64 damage: the physical GAP, terminal
        # anchor/END, and successor publication are separate durable phases,
        # so a crash between any two must leave a state from which this one
        # public call can continue.
        snapshot: RegistrySnapshot | None = None
        for _ in range(8):
            snapshot, finalize, rollover_ref, progressed = self._recover_one_phase()
            if finalize is not None:
                self._resume_final_damage(finalize)
                continue
            if rollover_ref is not None:
                self._resume_pending_damage(rollover_ref)
                continue
            if not progressed:
                break
        if snapshot is None:
            raise V3PersistenceError("recovery did not open the work registry")
        position = 0 if cursor is None else cursor.processing_offset
        emitted = False if cursor is None else cursor.active_capture_emitted
        page = recovery_page(
            snapshot.state,
            processing_offset=position,
            limit=limit,
            active_capture_emitted=emitted,
        )
        active = None
        if page.active_capture is not None:
            active = RecoveredCaptureWork(
                BlackoutRef(
                    page.active_capture.blackout_id, page.active_capture.logical_segment_id
                ),
                page.active_capture.physical_cursor,
                page.active_capture.terminal_cursor,
            )
        processing = tuple(
            BlackoutProcessingRef(
                BlackoutRef(item.blackout_id, item.logical_segment_id),
                BlackoutProcessingStage.PROCESSING,
                item.terminal_end_sha256,
                item.frozen_policy_revision,
            )
            for item in page.pending
            if isinstance(item, ProcessingState)
        )
        next_cursor = (
            None
            if page.next_cursor is None
            else BlackoutRecoveryCursor(
                page.next_cursor.processing_offset, page.next_cursor.active_capture_emitted
            )
        )
        return BlackoutRecoveryPage(active, processing, next_cursor, page.complete)

    def _recover_one_phase(
        self,
    ) -> tuple[
        RegistrySnapshot,
        tuple[BlackoutRef, BlackoutCaptureCursor, CapturingState] | None,
        BlackoutRef | None,
        bool,
    ]:
        with self.filesystem.write_transaction() as tx:
            snapshot = self._registry.open_or_create(tx)
            capture = snapshot.state.capture
            if self._is_final_damage_capture(capture):
                if not isinstance(capture, CapturingState):
                    raise V3PersistenceError("final damage capture state is invalid")
                return (
                    snapshot,
                    (
                        BlackoutRef(capture.blackout_id, capture.logical_segment_id),
                        capture.physical_cursor,
                        capture,
                    ),
                    None,
                    False,
                )
            recovered = self._recover_active_capture(tx, snapshot)
            if recovered is not None:
                return recovered, None, None, recovered != snapshot
            if capture is None:
                return snapshot, None, self._pending_damage_rollover(tx, snapshot), False
            return snapshot, None, None, False

    def _resume_final_damage(
        self, final: tuple[BlackoutRef, BlackoutCaptureCursor, CapturingState]
    ) -> None:
        try:
            self._finalize_reference_damage(*final)
        except V3CapacityError:
            # The finalizer intentionally reports the closed physical
            # capacity to its original append caller.  Recovery has completed
            # that phase successfully.
            pass

    def _resume_pending_damage(self, ref: BlackoutRef) -> None:
        try:
            self.rollover_damaged_processing(ref)
        except V3AppendConflict:
            # An unrelated active capture owns the registry.  Leave every byte
            # untouched; the delayed request can retry after it closes.
            pass

    def _is_final_damage_capture(self, capture: object) -> bool:
        return bool(
            isinstance(capture, CapturingState)
            and capture.damage_continuation is not None
            and capture.damage_continuation.phase == "gap_durable"
            and len(capture.storage_segments) >= 64
            and any(item.damaged_file_sha256 is not None for item in capture.storage_segments)
        )

    def _pending_damage_rollover(
        self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
    ) -> BlackoutRef | None:
        """Find only a finalized reference-64 damage carrier.

        The pending registry has no active marker after ``close`` commits.
        Its immutable segment receipts and terminal END still provide the
        narrow, typed identity needed to resume the successor reservation.
        """
        for item in snapshot.state.pending:
            if not isinstance(item, ProcessingState):
                continue
            if not self._is_pending_damage_carrier(item):
                continue
            if self._pending_is_capture_damage(tx, item):
                return BlackoutRef(item.blackout_id, item.logical_segment_id)
        return None

    def _is_pending_damage_carrier(self, item: ProcessingState) -> bool:
        if len(item.storage_segments) < 64:
            return False
        if not any(segment.damaged_file_sha256 is not None for segment in item.storage_segments):
            return False
        return item.terminal_closing_anchor_sha256 is not None

    def _pending_is_capture_damage(self, tx: V3WriteTransaction, item: ProcessingState) -> bool:
        paths = self.filesystem.paths
        if paths is None:
            raise V3PersistenceError()
        try:
            raw, _ = tx.read_bounded(
                paths.terminal_staging_token(item.blackout_id), max_bytes=2 * 1024 * 1024
            )
            for line in raw.splitlines(keepends=True):
                record = decode_v3_record(line)
                if record.record_sha256 == item.terminal_end_sha256:
                    return record.envelope.payload.get("termination") == "capture_damaged"
        except (V3FileNotFound, ValueError, IndexError) as exc:
            raise V3PersistenceError("pending damage END is unreadable") from exc
        return False
