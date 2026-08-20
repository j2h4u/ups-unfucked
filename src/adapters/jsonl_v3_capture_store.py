"""Typed v3 physical capture adapter."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from src.adapters.jsonl_v3_canonical import EncodedV3Record
from src.adapters.jsonl_v3_capture_append import JsonlV3CaptureAppendMixin
from src.adapters.jsonl_v3_capture_open import JsonlV3CaptureOpenMixin
from src.adapters.jsonl_v3_capture_recovery import JsonlV3CaptureRecoveryMixin
from src.adapters.jsonl_v3_capture_recovery_facade import JsonlV3CaptureRecoveryFacadeMixin
from src.adapters.jsonl_v3_capture_rollover import JsonlV3CaptureRolloverMixin
from src.adapters.jsonl_v3_errors import (
    V3AppendConflict,
    V3CapacityError,
    V3PersistenceError,
    V3ValidationError,
)
from src.adapters.jsonl_v3_filesystem import JsonlV3Filesystem, V3FaultPoint, V3WriteTransaction
from src.adapters.jsonl_v3_registry import (
    JsonlV3WorkRegistry,
    RegistrySnapshot,
    V3WorkRegistry,
)
from src.adapters.jsonl_v3_registry_values import (
    MAX_PENDING_ENTRIES,
    CapturingState,
    PreparingCaptureState,
    ProcessingState,
    TailState,
    V3AppendIntent,
    V3LastAppend,
)
from src.adapters.jsonl_v3_terminal_tail_codec import encode_blackout_end
from src.application.blackout_storage_values import (
    BlackoutCaptureCursor,
    BlackoutCaptureOpened,
    BlackoutChainKind,
    BlackoutProcessingRef,
    BlackoutProcessingStage,
    BlackoutRef,
)
from src.domain.blackout_capture import ObservationOrigin
from src.domain.blackout_terminal import BlackoutEnd, BlackoutTermination, BudgetKind

CAPTURE_PHYSICAL_LIMIT = 62 * 1024 * 1024


class _ReferenceDamageFinalizer:
    """Terminalize the reserved final physical damage receipt."""

    if TYPE_CHECKING:

        def close(
            self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, end: BlackoutEnd
        ) -> object: ...

        def rollover_damaged_processing(self, ref: BlackoutRef) -> BlackoutCaptureOpened: ...

    def _close_corrupt_capture(
        self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, state: CapturingState
    ) -> None:
        observed_at = datetime(1970, 1, 1, tzinfo=timezone.utc)
        end = BlackoutEnd(
            state.blackout_id,
            state.physical_episode_id,
            state.battery_epoch_id,
            state.logical_segment_id,
            BlackoutTermination.CAPTURE_DAMAGED,
            ObservationOrigin(state.observation_origin),
            observed_at,
            0,
            "storage-recovery",
            terminal_anchor_record_hash=cursor.last_record_sha256,
            uat_intent_id=state.uat_intent_id,
        )
        self.close(ref, cursor, end)
        self.rollover_damaged_processing(ref)


class _RecoveredEndPromoter:
    """Promote a durably recovered END into the processing registry state."""

    if TYPE_CHECKING:
        _registry: JsonlV3WorkRegistry

        def _complete_preparing(
            self, tx: V3WriteTransaction, snapshot: RegistrySnapshot, state: object
        ) -> object: ...
        def _recover_append_intent(
            self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
        ) -> RegistrySnapshot: ...
        def _recover_terminal_intent(
            self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
        ) -> RegistrySnapshot: ...
        def _resume_damage(
            self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
        ) -> RegistrySnapshot: ...
        def _resume_rollover(
            self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
        ) -> object: ...

        def _terminal_root_sha256(self, tx: V3WriteTransaction, blackout_id: str) -> str: ...

        def _terminal_end_closing_anchor_sha256(
            self, tx: V3WriteTransaction, blackout_id: str, previous_hash: str
        ) -> str | None: ...

    def _recover_active_capture(
        self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
    ) -> RegistrySnapshot | None:
        capture = snapshot.state.capture
        if isinstance(capture, PreparingCaptureState):
            self._complete_preparing(tx, snapshot, capture)
        elif isinstance(capture, CapturingState):
            if self._recover_capturing_state(tx, snapshot, capture) is False:
                return None
        else:
            return None
        return self._registry.read(tx)

    def _recover_capturing_state(
        self, tx: V3WriteTransaction, snapshot: RegistrySnapshot, capture: CapturingState
    ) -> bool:
        if capture.append_intent is not None:
            self._recover_capturing_intent(tx, snapshot, capture)
            return True
        if capture.last_append is not None and capture.last_append.operation == "end":
            self._promote_recovered_end(tx, snapshot)
            return True
        if capture.damage_continuation is not None:
            self._resume_damage(tx, snapshot)
            return True
        if capture.rollover is not None:
            self._resume_rollover(tx, snapshot)
            return True
        return False

    def _recover_capturing_intent(
        self, tx: V3WriteTransaction, snapshot: RegistrySnapshot, capture: CapturingState
    ) -> None:
        if capture.append_intent is None:
            raise V3AppendConflict()
        if capture.append_intent.chain == "physical":
            self._recover_physical_capture_intent(tx, snapshot)
            return
        self._recover_terminal_capture_intent(tx, snapshot)

    def _recover_physical_capture_intent(
        self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
    ) -> None:
        self._recover_append_intent(tx, snapshot)

    def _recover_terminal_capture_intent(
        self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
    ) -> None:
        recovered = self._recover_terminal_intent(tx, snapshot)
        if self._terminal_intent_completed_end(recovered):
            self._promote_recovered_end(tx, recovered)

    def _terminal_intent_completed_end(self, snapshot: RegistrySnapshot) -> bool:
        current = snapshot.state.capture
        return bool(
            isinstance(current, CapturingState)
            and current.last_append is not None
            and current.last_append.operation == "end"
        )

    def _promote_recovered_end(
        self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
    ) -> RegistrySnapshot:
        state = snapshot.state.capture
        if not isinstance(state, CapturingState) or state.last_append is None:
            raise V3AppendConflict()
        terminal = state.terminal_cursor
        last = state.last_append
        if terminal is None or last.operation != "end":
            raise V3AppendConflict()
        root = self._terminal_root_sha256(tx, state.blackout_id)
        closing_anchor = self._terminal_end_closing_anchor_sha256(
            tx, state.blackout_id, last.prior_cursor_sha256
        )
        processing = ProcessingState(
            "processing",
            state.blackout_id,
            state.logical_segment_id,
            state.physical_episode_id,
            state.battery_epoch_id,
            state.observation_origin,
            state.uat_intent_id,
            state.frozen_policy_revision,
            state.physical_cursor,
            terminal,
            root,
            closing_anchor,
            last.line_sha256,
            state.capture_bytes,
            state.capture_record_count,
            state.sample_count,
            state.gap_count,
            state.storage_segments,
            None,
        )
        return self._registry.compare_and_replace(
            tx,
            expected=snapshot,
            replacement=V3WorkRegistry(None, (*snapshot.state.pending, processing)),
        )

    def _promote_end_retry(
        self,
        tx: V3WriteTransaction,
        snapshot: RegistrySnapshot,
        ref: BlackoutRef,
        cursor: BlackoutCaptureCursor,
        end: BlackoutEnd,
    ) -> BlackoutProcessingRef | None:
        state = snapshot.state.capture
        if not isinstance(state, CapturingState) or not self._end_retry_candidate(
            state, ref, cursor, end
        ):
            return None
        promoted = self._promote_recovered_end(tx, snapshot)
        processing = promoted.state.pending[-1]
        if not isinstance(processing, ProcessingState):
            raise V3AppendConflict()
        return BlackoutProcessingRef(
            ref,
            BlackoutProcessingStage.PROCESSING,
            processing.terminal_end_sha256,
            processing.frozen_policy_revision,
        )

    def _end_retry_candidate(
        self,
        state: CapturingState,
        ref: BlackoutRef,
        cursor: BlackoutCaptureCursor,
        end: BlackoutEnd,
    ) -> bool:
        if ref != BlackoutRef(state.blackout_id, state.logical_segment_id):
            return False
        last = state.last_append
        if last is None or last.operation != "end":
            return False
        expected = encode_blackout_end(
            end,
            seq=cursor.next_sequence or 0,
            previous_record_sha256=cursor.last_record_sha256,
        ).record_sha256
        return self._matches_end_retry(state, cursor, last, expected)

    def _matches_end_retry(
        self,
        state: CapturingState,
        cursor: BlackoutCaptureCursor,
        last: V3LastAppend,
        expected: str,
    ) -> bool:
        if state.terminal_cursor == cursor:
            return False
        return bool(
            last.prior_cursor_sha256 == (cursor.last_record_sha256 or "0" * 64)
            and last.line_sha256 == expected
        )


class JsonlV3CaptureStore(
    _ReferenceDamageFinalizer,
    _RecoveredEndPromoter,
    JsonlV3CaptureAppendMixin,
    JsonlV3CaptureOpenMixin,
    JsonlV3CaptureRecoveryMixin,
    JsonlV3CaptureRecoveryFacadeMixin,
    JsonlV3CaptureRolloverMixin,
):
    """The sole typed writer for physical capture records."""

    if TYPE_CHECKING:

        def _resume_rollover(
            self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
        ) -> BlackoutCaptureOpened: ...

    def __init__(
        self, filesystem: JsonlV3Filesystem, *, uuid4_hex: Callable[[], str] | None = None
    ) -> None:
        self.filesystem = filesystem
        self._uuid4_hex = uuid4_hex or (lambda: uuid.uuid4().hex)
        self._registry = JsonlV3WorkRegistry(filesystem)

    def _require_pending_retry(
        self,
        tx: V3WriteTransaction,
        item: ProcessingState | TailState,
        cursor: BlackoutCaptureCursor,
        end: BlackoutEnd,
    ) -> None:
        """Bind an exact pending END retry to its original chain cursor."""
        record = self._pending_end_record(tx, item)
        candidate = encode_blackout_end(
            end,
            seq=record.envelope.seq,
            previous_record_sha256=record.envelope.prev_record_sha256,
        )
        if candidate.line != record.line or record.record_sha256 != item.terminal_end_sha256:
            raise V3AppendConflict()
        if end.termination is BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED:
            linked = record.envelope.seq != 0 or record.envelope.prev_record_sha256 is not None
            expected = (
                BlackoutCaptureCursor(
                    item.blackout_id,
                    item.logical_segment_id,
                    BlackoutChainKind.TERMINAL,
                    record.envelope.seq,
                    record.envelope.prev_record_sha256,
                )
                if linked
                else item.physical_cursor
            )
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

    def rollover(
        self,
        ref: BlackoutRef,
        cursor: BlackoutCaptureCursor,
        *,
        budget_kind: BudgetKind = BudgetKind.BYTES,
    ) -> BlackoutCaptureOpened:
        """Roll to a successor, including from an exhausted physical cursor."""
        if cursor.next_sequence is not None:
            return super().rollover(ref, cursor, budget_kind=budget_kind)
        if cursor.chain is not BlackoutChainKind.PHYSICAL:
            raise V3ValidationError("rollover requires a physical cursor")
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
            reserved_snapshot = self._registry.compare_and_replace(
                tx,
                expected=snapshot,
                replacement=V3WorkRegistry(reserved, snapshot.state.pending),
            )
            tx._fault(V3FaultPoint.ROLLOVER_AFTER_REGISTRY_RESERVE)
            return self._resume_rollover(tx, reserved_snapshot)

    def close(
        self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, end: BlackoutEnd
    ) -> BlackoutProcessingRef:
        if end.termination is not BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED:
            return self._close_with_terminal(ref, cursor, end)
        return self._close_budget(ref, cursor, end)

    def _close_budget(
        self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, end: BlackoutEnd
    ) -> BlackoutProcessingRef:
        with self.filesystem.write_transaction() as tx:
            snapshot = self._registry.read(tx)
            self._validate_active_close_cursor(snapshot, ref, cursor, budget=True)
            existing = self._pending_result(tx, snapshot, ref, cursor, end)
            if existing is not None:
                return existing
            state = snapshot.state.capture
            if not isinstance(state, CapturingState):
                raise V3AppendConflict()
            self._ensure_pending_capacity(snapshot)
            paths = self.filesystem.paths
            if paths is None:
                raise V3PersistenceError()
            tail = paths.terminal_staging_token(ref.blackout_id)
            terminal_cursor = state.terminal_cursor
            if terminal_cursor is None:
                terminal_cursor = BlackoutCaptureCursor(
                    ref.blackout_id,
                    ref.segment_id,
                    BlackoutChainKind.TERMINAL,
                    0,
                    None,
                )
            encoded = encode_blackout_end(
                end,
                seq=terminal_cursor.next_sequence or 0,
                previous_record_sha256=terminal_cursor.last_record_sha256,
            )
            retry = self._budget_end_retry_encoding(state, end)
            if state.last_append is not None and state.last_append.operation == "end":
                if retry is None:
                    raise V3AppendConflict()
                encoded = retry
            elif state.append_intent is not None:
                if not self._end_intent_matches(
                    state.append_intent,
                    encoded,
                    terminal_cursor,
                    state.append_intent.file_offset,
                ):
                    raise V3AppendConflict()
                snapshot = self._recover_terminal_intent(tx, snapshot)
            else:
                offset = self._tail_length(tx, tail)
                intent = self._end_intent(encoded, terminal_cursor, offset)
                intended = self._registry.compare_and_replace(
                    tx,
                    expected=snapshot,
                    replacement=V3WorkRegistry(
                        replace(state, terminal_cursor=terminal_cursor, append_intent=intent),
                        snapshot.state.pending,
                    ),
                )
                self._write_terminal_intent(tx, tail, intent)
                snapshot = self._recover_terminal_intent(tx, intended)
            current = snapshot.state.capture
            if not isinstance(current, CapturingState) or current.terminal_cursor is None:
                raise V3AppendConflict()
            terminal_root = self._terminal_root_sha256(tx, ref.blackout_id)
            processing = ProcessingState(
                "processing",
                current.blackout_id,
                current.logical_segment_id,
                current.physical_episode_id,
                current.battery_epoch_id,
                current.observation_origin,
                current.uat_intent_id,
                current.frozen_policy_revision,
                current.physical_cursor,
                current.terminal_cursor,
                terminal_root,
                None,
                encoded.record_sha256,
                current.capture_bytes,
                current.capture_record_count,
                current.sample_count,
                current.gap_count,
                current.storage_segments,
                None,
            )
            self._registry.compare_and_replace(
                tx,
                expected=snapshot,
                replacement=V3WorkRegistry(None, (*snapshot.state.pending, processing)),
            )
            return BlackoutProcessingRef(
                ref,
                BlackoutProcessingStage.PROCESSING,
                encoded.record_sha256,
                state.frozen_policy_revision,
            )

    def _close_with_terminal(
        self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, end: BlackoutEnd
    ) -> BlackoutProcessingRef:
        with self.filesystem.write_transaction() as tx:
            snapshot = self._registry.read(tx)
            self._validate_active_close_cursor(snapshot, ref, cursor, budget=False)
            existing = self._pending_result(tx, snapshot, ref, cursor, end)
            if existing is not None:
                return existing
            recovered_retry = self._promote_end_retry(tx, snapshot, ref, cursor, end)
            if recovered_retry is not None:
                return recovered_retry
            state = snapshot.state.capture
            if not isinstance(state, CapturingState):
                raise V3AppendConflict()
            self._ensure_pending_capacity(snapshot)
            terminal = state.terminal_cursor
            if terminal is None or terminal.next_sequence is None:
                raise V3ValidationError("close requires a durable terminal anchor")
            paths = self.filesystem.paths
            if paths is None:
                raise V3PersistenceError()
            tail = paths.terminal_staging_token(ref.blackout_id)
            raw, _ = tx.read_bounded(tail, max_bytes=2 * 1024 * 1024)
            encoded = encode_blackout_end(
                end, seq=terminal.next_sequence, previous_record_sha256=terminal.last_record_sha256
            )
            if state.append_intent is not None:
                snapshot = self._recover_end_intent(
                    tx, snapshot, encoded, (terminal, state.append_intent.file_offset)
                )
            elif state.last_append is not None and (
                state.last_append.operation == "end"
                and state.last_append.line_sha256 == encoded.record_sha256
                and terminal.last_record_sha256 == encoded.record_sha256
            ):
                pass
            else:
                intent = self._end_intent(encoded, terminal, len(raw))
                intended = self._registry.compare_and_replace(
                    tx,
                    expected=snapshot,
                    replacement=V3WorkRegistry(
                        replace(state, append_intent=intent), snapshot.state.pending
                    ),
                )
                self._write_terminal_intent(tx, tail, intent)
                snapshot = self._recover_terminal_intent(tx, intended)
            current = snapshot.state.capture
            if not isinstance(current, CapturingState):
                raise V3AppendConflict()
            after = current.terminal_cursor
            if after is None or after.last_record_sha256 != encoded.record_sha256:
                raise V3AppendConflict()
            processing = ProcessingState(
                "processing",
                state.blackout_id,
                state.logical_segment_id,
                state.physical_episode_id,
                state.battery_epoch_id,
                state.observation_origin,
                state.uat_intent_id,
                state.frozen_policy_revision,
                current.physical_cursor,
                after,
                self._terminal_root_sha256(tx, ref.blackout_id),
                terminal.last_record_sha256,
                encoded.record_sha256,
                current.capture_bytes,
                current.capture_record_count,
                current.sample_count,
                current.gap_count,
                current.storage_segments,
                None,
            )
            self._registry.compare_and_replace(
                tx,
                expected=snapshot,
                replacement=V3WorkRegistry(None, (*snapshot.state.pending, processing)),
            )
            return BlackoutProcessingRef(
                ref,
                BlackoutProcessingStage.PROCESSING,
                encoded.record_sha256,
                state.frozen_policy_revision,
            )

    def _validate_active_close_cursor(
        self,
        snapshot: RegistrySnapshot,
        ref: BlackoutRef,
        cursor: BlackoutCaptureCursor,
        *,
        budget: bool,
    ) -> None:
        """Reject a stale active cursor before consulting pending receipts."""
        state = snapshot.state.capture
        if not isinstance(state, CapturingState):
            return
        self._validate_active_close_ref(state, ref)
        if budget:
            self._validate_budget_close_cursor(state, cursor)
            return

        self._validate_terminal_close_cursor(state, cursor)

    def _validate_active_close_ref(self, state: CapturingState, ref: BlackoutRef) -> None:
        if ref != BlackoutRef(state.blackout_id, state.logical_segment_id):
            raise V3AppendConflict()

    def _validate_terminal_close_cursor(
        self, state: CapturingState, cursor: BlackoutCaptureCursor
    ) -> None:
        if state.terminal_cursor != cursor:
            raise V3AppendConflict()

    def _validate_budget_close_cursor(
        self, state: CapturingState, cursor: BlackoutCaptureCursor
    ) -> None:
        terminal = state.terminal_cursor
        if terminal is None:
            if cursor != state.physical_cursor:
                raise V3AppendConflict()
            return
        if cursor.chain is BlackoutChainKind.TERMINAL:
            if cursor != terminal:
                raise V3AppendConflict()
            return
        if not self._is_root_budget_retry(state, terminal):
            raise V3AppendConflict()

    def _is_root_budget_retry(self, state: CapturingState, terminal: BlackoutCaptureCursor) -> bool:
        last = state.last_append
        return bool(
            last is not None
            and last.operation == "end"
            and last.prior_cursor_sha256 == "0" * 64
            and terminal.next_sequence == 1
        )

    def _end_intent(
        self, encoded: EncodedV3Record, cursor: BlackoutCaptureCursor, offset: int = 0
    ) -> V3AppendIntent:
        line = encoded.line
        digest = hashlib.sha256(encoded.line).hexdigest()
        return V3AppendIntent(
            "terminal",
            "end",
            cursor.next_sequence or 0,
            cursor.last_record_sha256,
            None,
            offset,
            line.decode(),
            digest,
            len(line),
            cursor.last_record_sha256 or "0" * 64,
        )

    def _budget_end_retry_encoding(
        self, state: CapturingState, end: BlackoutEnd
    ) -> EncodedV3Record | None:
        last = state.last_append
        terminal = state.terminal_cursor
        if state.append_intent is not None or terminal is None or last is None:
            return None
        if last.operation != "end" or terminal.next_sequence in (None, 0):
            return None
        if terminal.last_record_sha256 != last.line_sha256:
            return None
        previous = None if last.prior_cursor_sha256 == "0" * 64 else last.prior_cursor_sha256
        encoded = encode_blackout_end(
            end, seq=terminal.next_sequence - 1, previous_record_sha256=previous
        )
        return encoded if encoded.record_sha256 == last.line_sha256 else None
