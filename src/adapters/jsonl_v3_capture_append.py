"""Physical and terminal append state machines for the v3 capture facade."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, NoReturn, Protocol

from src.adapters.jsonl_v3_canonical import EncodedV3Record, decode_v3_record
from src.adapters.jsonl_v3_discharge_gap_codec import encode_discharge_gap
from src.adapters.jsonl_v3_discharge_sample_codec import encode_discharge_sample
from src.adapters.jsonl_v3_errors import (
    V3AppendConflict,
    V3CapacityError,
    V3CorruptionError,
    V3FileNotFound,
    V3PersistenceError,
    V3ValidationError,
)
from src.adapters.jsonl_v3_filesystem import JsonlV3Filesystem, V3WriteTransaction
from src.adapters.jsonl_v3_registry import JsonlV3WorkRegistry, RegistrySnapshot, V3WorkRegistry
from src.adapters.jsonl_v3_registry_values import (
    MAX_CAPTURE_BYTES,
    CapturingState,
    V3AppendIntent,
    V3LastAppend,
    V3StorageSegmentReceipt,
)
from src.adapters.jsonl_v3_segment_index import OffsetRecordKind, SegmentIndexEntry
from src.adapters.jsonl_v3_storage_paths import (
    V3OffsetPathToken,
    V3SegmentPathToken,
    V3TerminalStagingToken,
)
from src.adapters.jsonl_v3_terminal_tail_codec import encode_endpoint_anchor
from src.application.blackout_storage_values import (
    MAX_CHAIN_SEQUENCE,
    BlackoutCaptureCursor,
    BlackoutChainKind,
    BlackoutProcessingRef,
    BlackoutRef,
)
from src.domain.blackout_capture import DischargeGap, DischargeSample
from src.domain.fragments import AnchorKind, AnchorProvenance, EndpointAnchor

MAX_PHYSICAL_CAPTURE_BYTES = 62 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _PhysicalAppend:
    snapshot: RegistrySnapshot
    state: CapturingState
    cursor: BlackoutCaptureCursor
    operation: str
    encoded: EncodedV3Record
    kind: OffsetRecordKind
    receipt: V3StorageSegmentReceipt
    expected_offset: int
    gap_recovered: bool = False


class _CaptureOwner(Protocol):
    filesystem: JsonlV3Filesystem
    _registry: JsonlV3WorkRegistry

    def _begin_damage(
        self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
    ) -> RegistrySnapshot: ...

    def _resume_damage(
        self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
    ) -> RegistrySnapshot: ...


class _TerminalAppendSupport:
    """Terminal cursor validation and exact retry recognition."""

    if TYPE_CHECKING:
        _registry: JsonlV3WorkRegistry

        def _close_corrupt_capture(
            self, ref: BlackoutRef, terminal: BlackoutCaptureCursor, state: CapturingState
        ) -> None: ...

        def append_anchor(
            self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, anchor: EndpointAnchor
        ) -> BlackoutCaptureCursor: ...

        def close(
            self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, end: object
        ) -> BlackoutProcessingRef: ...

        def _recover_terminal_intent(
            self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
        ) -> RegistrySnapshot: ...

    def _terminal_retry_matches(
        self, state: CapturingState, cursor: BlackoutCaptureCursor, anchor: EndpointAnchor
    ) -> bool:
        last = state.last_append
        terminal = state.terminal_cursor
        if last is None or last.operation != "anchor" or terminal is None:
            return False
        if cursor.chain is BlackoutChainKind.PHYSICAL:
            first_terminal = terminal.next_sequence == 1 and last.prior_cursor_sha256 == "0" * 64
            sequence = 0 if first_terminal else cursor.next_sequence or 0
            previous = None if first_terminal else cursor.last_record_sha256
            cursor_matches = last.prior_cursor_sha256 == (previous or "0" * 64) or (
                last.prior_cursor_sha256 == "0" * 64 and cursor == state.physical_cursor
            )
        else:
            sequence = max((cursor.next_sequence or 1) - 1, 0)
            sequence = cursor.next_sequence or 0
            previous = cursor.last_record_sha256
            cursor_matches = last.prior_cursor_sha256 == (previous or "0" * 64)
        encoded = encode_endpoint_anchor(anchor, seq=sequence, previous_record_sha256=previous)
        return cursor_matches and last.line_sha256 == encoded.record_sha256

    def _is_exact_terminal_retry(
        self, state: CapturingState, cursor: BlackoutCaptureCursor, anchor: EndpointAnchor
    ) -> bool:
        return self._terminal_retry_matches(state, cursor, anchor)

    def _resolve_terminal_request(
        self,
        snapshot: RegistrySnapshot,
        ref: BlackoutRef,
        cursor: BlackoutCaptureCursor,
        anchor: EndpointAnchor,
    ) -> tuple[CapturingState, BlackoutCaptureCursor | None]:
        state = snapshot.state.capture
        if not isinstance(state, CapturingState):
            raise V3AppendConflict()
        terminal = state.terminal_cursor
        if ref != BlackoutRef(state.blackout_id, state.logical_segment_id):
            raise V3AppendConflict()
        expected = state.physical_cursor if terminal is None else terminal
        if cursor == expected:
            return state, terminal
        if self._has_pending_terminal_intent(state, terminal, cursor):
            return state, terminal
        if terminal is not None and self._terminal_retry_matches(state, cursor, anchor):
            return state, terminal
        raise V3AppendConflict()

    def _has_pending_terminal_intent(
        self,
        state: CapturingState,
        terminal: BlackoutCaptureCursor | None,
        cursor: BlackoutCaptureCursor,
    ) -> bool:
        if (
            terminal is None
            or terminal.next_sequence != 0
            or terminal.last_record_sha256 is not None
        ):
            return False
        intent = state.append_intent
        return intent is not None and intent.chain == "terminal" and cursor == state.physical_cursor

    def _terminal_state(
        self, capture: object, cursor: BlackoutCaptureCursor
    ) -> tuple[CapturingState, BlackoutCaptureCursor | None]:
        if not isinstance(capture, CapturingState):
            raise V3AppendConflict()
        terminal = capture.terminal_cursor
        expected = capture.physical_cursor if terminal is None else terminal
        if cursor != expected:
            raise V3AppendConflict()
        return capture, terminal

    def _current_terminal_cursor(self, capture: object) -> BlackoutCaptureCursor:
        if not isinstance(capture, CapturingState) or capture.terminal_cursor is None:
            raise V3AppendConflict()
        return capture.terminal_cursor

    def _end_intent_matches(
        self,
        intent: V3AppendIntent,
        encoded: EncodedV3Record,
        cursor: BlackoutCaptureCursor,
        offset: int,
    ) -> bool:
        return (
            intent.chain,
            intent.operation,
            intent.expected_seq,
            intent.expected_previous_hash,
            intent.file_offset,
            intent.line_utf8.encode(),
            intent.line_sha256,
        ) == (
            "terminal",
            "end",
            cursor.next_sequence or 0,
            cursor.last_record_sha256,
            offset,
            encoded.line,
            hashlib.sha256(encoded.line).hexdigest(),
        )

    def _recover_end_intent(
        self,
        tx: V3WriteTransaction,
        snapshot: RegistrySnapshot,
        encoded: EncodedV3Record,
        expected: tuple[BlackoutCaptureCursor, int],
    ) -> RegistrySnapshot:
        state = snapshot.state.capture
        if not isinstance(state, CapturingState) or state.append_intent is None:
            raise V3AppendConflict()
        terminal, offset = expected
        if not self._end_intent_matches(state.append_intent, encoded, terminal, offset):
            raise V3AppendConflict()
        return self._recover_terminal_intent(tx, snapshot)

    def _finalize_reference_damage(
        self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, state: CapturingState
    ) -> NoReturn:
        """Close the aggregate after the reserved final physical gap."""
        final_hash = cursor.last_record_sha256
        if final_hash is None:
            raise V3AppendConflict()
        canonical_hash = hashlib.sha256(
            f"capture-damaged:{state.blackout_id}:{state.logical_segment_id}:{final_hash}".encode()
        ).hexdigest()
        observed_at = datetime(1970, 1, 1, tzinfo=timezone.utc)
        anchor = EndpointAnchor(
            canonical_hash,
            AnchorKind.CORRUPTION,
            AnchorProvenance.OPERATIONAL,
            "storage-recovery",
            observed_at,
            0,
            final_hash,
            state.blackout_id,
            state.physical_episode_id,
            state.logical_segment_id,
        )
        terminal = self.append_anchor(ref, cursor, anchor)
        self._close_corrupt_capture(ref, terminal, state)
        raise V3CapacityError("final physical damage receipt closed the aggregate")


class _PhysicalAppendSupport:
    """Physical intent replay and bounded-line commit support."""

    if TYPE_CHECKING:
        _registry: JsonlV3WorkRegistry

        def _recover_existing_append(
            self,
            tx: V3WriteTransaction,
            snapshot: RegistrySnapshot,
            encoded: EncodedV3Record,
            expected: tuple[str, str, int, str | None, int],
        ) -> RegistrySnapshot: ...

        def _finish_physical_append(
            self,
            tx: V3WriteTransaction,
            snapshot: RegistrySnapshot,
            *,
            operation: str,
            kind: OffsetRecordKind,
            line: bytes,
        ) -> RegistrySnapshot: ...

    def _recover_for_append(
        self, tx: V3WriteTransaction, request: _PhysicalAppend
    ) -> _PhysicalAppend:
        recovered = self._recover_existing_append(
            tx,
            request.snapshot,
            request.encoded,
            (
                "physical",
                request.operation,
                request.cursor.next_sequence or 0,
                request.cursor.last_record_sha256,
                request.expected_offset,
            ),
        )
        current = recovered.state.capture
        if not isinstance(current, CapturingState):
            raise V3AppendConflict()
        before = request.snapshot.state.capture
        if not isinstance(before, CapturingState):
            raise V3AppendConflict()
        if current.gap_count == before.gap_count:
            return replace(request, snapshot=recovered, state=current)
        receipt = current.storage_segments[-1]
        return replace(
            request,
            snapshot=recovered,
            state=current,
            cursor=current.physical_cursor,
            receipt=receipt,
            expected_offset=receipt.trusted_bytes,
            gap_recovered=True,
        )

    def _write_physical_append(
        self, tx: V3WriteTransaction, request: _PhysicalAppend
    ) -> BlackoutCaptureCursor:
        snapshot = request.snapshot
        state = request.state
        cursor = request.cursor
        operation = request.operation
        encoded = request.encoded
        kind = request.kind
        receipt = request.receipt
        expected_offset = request.expected_offset
        if (
            type(receipt.path_token) is not V3SegmentPathToken
            or type(receipt.offset_token) is not V3OffsetPathToken
        ):
            raise V3PersistenceError("active capture receipt is damaged")
        intent = V3AppendIntent(
            "physical",
            operation,
            cursor.next_sequence or 0,
            cursor.last_record_sha256,
            receipt.ordinal,
            expected_offset,
            encoded.line.decode(),
            hashlib.sha256(encoded.line).hexdigest(),
            len(encoded.line),
            cursor.last_record_sha256 or "0" * 64,
        )
        intended = self._registry.compare_and_replace(
            tx,
            expected=snapshot,
            replacement=V3WorkRegistry(
                replace(state, append_intent=intent), snapshot.state.pending
            ),
        )
        tx.append_and_sync(
            receipt.path_token,
            expected_offset=expected_offset,
            contents=encoded.line,
            max_result_bytes=MAX_CAPTURE_BYTES,
        )
        finished = self._finish_physical_append(
            tx, intended, operation=operation, kind=kind, line=encoded.line
        )
        current = finished.state.capture
        if not isinstance(current, CapturingState):
            raise V3AppendConflict()
        return current.physical_cursor

    def _is_final_damage_state(self, state: CapturingState) -> bool:
        return len(state.storage_segments) >= 64 and any(
            receipt.damaged_file_sha256 is not None for receipt in state.storage_segments
        )


class JsonlV3CaptureAppendMixin(_TerminalAppendSupport, _PhysicalAppendSupport):
    """Exact retry/conflict handling for both independent chains."""

    if TYPE_CHECKING:
        filesystem: JsonlV3Filesystem
        _registry: JsonlV3WorkRegistry

        def _begin_damage(
            self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
        ) -> RegistrySnapshot: ...
        def _resume_damage(
            self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
        ) -> RegistrySnapshot: ...

    def append_sample(
        self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, sample: DischargeSample
    ) -> BlackoutCaptureCursor:
        return self._append(ref, cursor, sample, "sample")

    def append_gap(
        self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, gap: DischargeGap
    ) -> BlackoutCaptureCursor:
        return self._append(ref, cursor, gap, "gap")

    def append_anchor(
        self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, anchor: EndpointAnchor
    ) -> BlackoutCaptureCursor:
        if not isinstance(anchor, EndpointAnchor):
            raise V3ValidationError("anchor value is invalid")
        if anchor.kind in {AnchorKind.TRANSFER_TO_BATTERY, AnchorKind.RAW_FIRMWARE_LB}:
            return self._append(ref, cursor, anchor, "anchor")
        return self._append_terminal_anchor(ref, cursor, anchor)

    def _append_terminal_anchor(
        self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, anchor: EndpointAnchor
    ) -> BlackoutCaptureCursor:
        if cursor.chain not in (BlackoutChainKind.PHYSICAL, BlackoutChainKind.TERMINAL):
            raise V3ValidationError("anchor cursor chain is invalid")
        with self.filesystem.write_transaction() as tx:
            snapshot = self._registry.read(tx)
            state, terminal = self._resolve_terminal_request(snapshot, ref, cursor, anchor)
            paths = self.filesystem.paths
            if paths is None:
                raise V3PersistenceError()
            tail = paths.terminal_staging_token(ref.blackout_id)
            encoded = encode_endpoint_anchor(
                anchor,
                seq=0 if terminal is None else terminal.next_sequence or 0,
                previous_record_sha256=None if terminal is None else terminal.last_record_sha256,
            )
            if terminal is not None and self._is_exact_terminal_retry(state, cursor, anchor):
                return terminal
            offset = 0 if terminal is None else self._tail_length(tx, tail)
            if state.append_intent is not None:
                if not self._intent_matches(
                    state.append_intent,
                    encoded.line,
                    (
                        "terminal",
                        "anchor",
                        encoded.envelope.seq,
                        encoded.envelope.prev_record_sha256,
                        offset,
                    ),
                ):
                    raise V3AppendConflict()
                self._recover_terminal_intent(tx, snapshot)
            else:
                intent = V3AppendIntent(
                    "terminal",
                    "anchor",
                    encoded.envelope.seq,
                    encoded.envelope.prev_record_sha256,
                    None,
                    offset,
                    encoded.line.decode(),
                    hashlib.sha256(encoded.line).hexdigest(),
                    len(encoded.line),
                    terminal.last_record_sha256 or "0" * 64 if terminal is not None else "0" * 64,
                )
                intended = self._registry.compare_and_replace(
                    tx,
                    expected=snapshot,
                    replacement=V3WorkRegistry(
                        replace(
                            state,
                            terminal_cursor=(
                                terminal
                                if terminal is not None
                                else BlackoutCaptureCursor(
                                    state.blackout_id,
                                    state.logical_segment_id,
                                    BlackoutChainKind.TERMINAL,
                                    0,
                                    None,
                                )
                            ),
                            append_intent=intent,
                        ),
                        snapshot.state.pending,
                    ),
                )
                self._write_terminal_intent(tx, tail, intent)
                self._recover_terminal_intent(tx, intended)
            current = self._registry.read(tx).state.capture
            return self._current_terminal_cursor(current)

    def _append(
        self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, value: object, operation: str
    ) -> BlackoutCaptureCursor:
        if cursor.chain is not BlackoutChainKind.PHYSICAL or cursor.next_sequence is None:
            raise V3ValidationError("physical cursor is invalid")
        final_damage: tuple[BlackoutRef, BlackoutCaptureCursor, CapturingState] | None = None
        with self.filesystem.write_transaction() as tx:
            snapshot = self._registry.read(tx)
            state = self._check_append_state(snapshot.state.capture, ref)
            receipt = state.storage_segments[-1]
            encoded, kind = self._encode_append_value(
                value, operation, cursor.next_sequence, cursor.last_record_sha256
            )
            if state.physical_cursor != cursor:
                return self._same_append_retry(state, cursor, encoded, operation)
            request = _PhysicalAppend(
                snapshot,
                state,
                cursor,
                operation,
                encoded,
                kind,
                receipt,
                receipt.trusted_bytes,
            )
            if state.capture_bytes + len(encoded.line) > MAX_PHYSICAL_CAPTURE_BYTES:
                raise V3CapacityError("capture requires aggregate rollover")
            if state.append_intent is not None:
                request = self._recover_for_append(tx, request)
                if not request.gap_recovered:
                    return request.state.physical_cursor
                if self._is_final_damage_state(request.state):
                    final_damage = (ref, request.cursor, request.state)
                else:
                    encoded, kind = self._encode_append_value(
                        value,
                        operation,
                        request.cursor.next_sequence or 0,
                        request.cursor.last_record_sha256,
                    )
                    request = replace(
                        request,
                        cursor=request.cursor,
                        encoded=encoded,
                        kind=kind,
                        receipt=request.state.storage_segments[-1],
                        expected_offset=request.state.storage_segments[-1].trusted_bytes,
                    )
            if final_damage is None:
                return self._write_physical_append(tx, request)
        if final_damage is not None:
            self._finalize_reference_damage(*final_damage)
        raise V3AppendConflict()

    def _check_append_state(self, state: object, ref: BlackoutRef) -> CapturingState:
        if not isinstance(state, CapturingState) or (
            ref.blackout_id,
            ref.segment_id,
        ) != (state.blackout_id, state.logical_segment_id):
            raise V3AppendConflict()
        return state

    def _same_append_retry(
        self,
        state: CapturingState,
        cursor: BlackoutCaptureCursor,
        encoded: EncodedV3Record,
        operation: str,
    ) -> BlackoutCaptureCursor:
        last = state.last_append
        if last is None or last.operation != operation:
            raise V3AppendConflict()
        if (cursor.blackout_id, cursor.segment_id) != (
            state.blackout_id,
            state.logical_segment_id,
        ):
            raise V3AppendConflict()
        if last.prior_cursor_sha256 == (cursor.last_record_sha256 or "0" * 64) and (
            last.line_sha256 == encoded.record_sha256
        ):
            return state.physical_cursor
        raise V3AppendConflict()

    def _recover_existing_append(
        self,
        tx: V3WriteTransaction,
        snapshot: RegistrySnapshot,
        encoded: EncodedV3Record,
        expected: tuple[str, str, int, str | None, int],
    ) -> RegistrySnapshot:
        state = snapshot.state.capture
        if not isinstance(state, CapturingState) or state.append_intent is None:
            raise V3AppendConflict()
        if not self._intent_matches(
            state.append_intent,
            encoded.line,
            expected,
        ):
            raise V3AppendConflict()
        return self._recover_append_intent(tx, snapshot)

    def _encode_append_value(
        self, value: object, operation: str, sequence: int, previous_hash: str | None
    ) -> tuple[EncodedV3Record, OffsetRecordKind]:
        if operation == "sample" and isinstance(value, DischargeSample):
            return encode_discharge_sample(
                value, seq=sequence, previous_record_sha256=previous_hash
            ), OffsetRecordKind.SAMPLE
        if operation == "gap" and isinstance(value, DischargeGap):
            return encode_discharge_gap(
                value, seq=sequence, previous_record_sha256=previous_hash
            ), OffsetRecordKind.GAP
        if operation == "anchor" and isinstance(value, EndpointAnchor):
            return encode_endpoint_anchor(
                value, seq=sequence, previous_record_sha256=previous_hash
            ), OffsetRecordKind.ANCHOR
        raise V3ValidationError(f"{operation} value is invalid")

    def _intent_matches(
        self, intent: V3AppendIntent, line: bytes, expected: tuple[str, str, int, str | None, int]
    ) -> bool:
        chain, operation, sequence, previous_hash, offset = expected
        return (
            intent.chain == chain
            and intent.operation == operation
            and intent.expected_seq == sequence
            and intent.expected_previous_hash == previous_hash
            and intent.file_offset == offset
            and intent.line_length == len(line)
            and intent.line_sha256 == hashlib.sha256(line).hexdigest()
            and intent.line_utf8.encode() == line
        )

    def _tail_length(self, tx: V3WriteTransaction, token: V3TerminalStagingToken) -> int:
        try:
            raw, _ = tx.read_bounded(token, max_bytes=2 * 1024 * 1024)
        except V3FileNotFound:
            return 0
        return len(raw)

    def _write_terminal_intent(
        self, tx: V3WriteTransaction, token: V3TerminalStagingToken, intent: V3AppendIntent
    ) -> None:
        line = intent.line_utf8.encode()
        if intent.file_offset == 0:
            try:
                tx.create_and_sync(token, line, 2 * 1024 * 1024)
            except V3PersistenceError:
                raw, _ = tx.read_bounded(token, max_bytes=2 * 1024 * 1024)
                if raw != line:
                    raise
        else:
            tx.append_and_sync(
                token,
                expected_offset=intent.file_offset,
                contents=line,
                max_result_bytes=2 * 1024 * 1024,
            )

    def _recover_terminal_intent(
        self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
    ) -> RegistrySnapshot:
        state = snapshot.state.capture
        if (
            not isinstance(state, CapturingState)
            or state.append_intent is None
            or state.append_intent.chain != "terminal"
        ):
            raise V3AppendConflict()
        if state.damage_continuation is not None:
            return self._resume_damage(tx, snapshot)
        intent = state.append_intent
        paths = self.filesystem.paths
        if paths is None:
            raise V3PersistenceError()
        token = paths.terminal_staging_token(state.blackout_id)
        line = intent.line_utf8.encode()
        try:
            terminal_length = len(tx.read_bounded(token, max_bytes=2 * 1024 * 1024)[0])
        except V3FileNotFound:
            terminal_length = -1
        if terminal_length in {-1, intent.file_offset}:
            self._write_terminal_intent(tx, token, intent)
        try:
            region = tx.read_region_bounded(
                token,
                offset=intent.file_offset,
                length=intent.line_length,
                max_file_bytes=2 * 1024 * 1024,
            )
        except V3FileNotFound:
            self._write_terminal_intent(tx, token, intent)
            region = tx.read_region_bounded(
                token,
                offset=intent.file_offset,
                length=intent.line_length,
                max_file_bytes=2 * 1024 * 1024,
            )
        if region.file_length != intent.file_offset + intent.line_length or region.contents != line:
            raise V3PersistenceError("terminal append has unexpected bytes")
        terminal = BlackoutCaptureCursor(
            state.blackout_id,
            state.logical_segment_id,
            BlackoutChainKind.TERMINAL,
            None if intent.expected_seq == MAX_CHAIN_SEQUENCE else intent.expected_seq + 1,
            self._record_hash(line),
        )
        completed = replace(
            state,
            terminal_cursor=terminal,
            append_intent=None,
            last_append=V3LastAppend(
                intent.operation,
                intent.expected_previous_hash or "0" * 64,
                self._record_hash(line),
                self._record_hash(line),
            ),
        )
        return self._registry.compare_and_replace(
            tx,
            expected=snapshot,
            replacement=V3WorkRegistry(completed, snapshot.state.pending),
        )

    def _recover_append_intent(
        self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
    ) -> RegistrySnapshot:
        state = snapshot.state.capture
        if (
            not isinstance(state, CapturingState)
            or state.append_intent is None
            or state.append_intent.chain != "physical"
            or not state.storage_segments
        ):
            raise V3AppendConflict()
        intent = state.append_intent
        receipt = state.storage_segments[-1]
        if type(receipt.path_token) is not V3SegmentPathToken:
            raise V3PersistenceError("active capture receipt is damaged")
        line = intent.line_utf8.encode()
        if tx.file_length(receipt.path_token) == intent.file_offset:
            tx.append_and_sync(
                receipt.path_token,
                expected_offset=intent.file_offset,
                contents=line,
                max_result_bytes=MAX_CAPTURE_BYTES,
            )
        try:
            region = tx.read_region_bounded(
                receipt.path_token,
                offset=intent.file_offset,
                length=intent.line_length,
                max_file_bytes=MAX_CAPTURE_BYTES,
            )
        except V3CorruptionError:
            return self._begin_damage(tx, snapshot)
        except V3FileNotFound as exc:
            raise V3PersistenceError("append segment disappeared") from exc
        if region.file_length != intent.file_offset + intent.line_length or region.contents != line:
            return self._begin_damage(tx, snapshot)
        kind = {
            "sample": OffsetRecordKind.SAMPLE,
            "gap": OffsetRecordKind.GAP,
            "anchor": OffsetRecordKind.ANCHOR,
        }.get(intent.operation)
        if kind is None:
            raise V3ValidationError("append operation is invalid")
        return self._finish_physical_append(
            tx, snapshot, operation=intent.operation, kind=kind, line=line
        )

    def _finish_physical_append(
        self,
        tx: V3WriteTransaction,
        snapshot: RegistrySnapshot,
        *,
        operation: str,
        kind: OffsetRecordKind,
        line: bytes,
    ) -> RegistrySnapshot:
        state = snapshot.state.capture
        if not isinstance(state, CapturingState) or state.append_intent is None:
            raise V3AppendConflict()
        intent = state.append_intent
        receipt = state.storage_segments[-1]
        if type(receipt.offset_token) is not V3OffsetPathToken:
            raise V3PersistenceError("active capture index is damaged")
        expected = tx.snapshot_offset_index(receipt.offset_token)
        existing = tx.get_offset_index(receipt.offset_token, sequence=intent.expected_seq)
        entry = SegmentIndexEntry(
            intent.expected_seq, intent.file_offset, len(line), self._record_hash(line), kind
        )
        if existing is None:
            tx.append_offset_index(receipt.offset_token, expected=expected, entry=entry)
        elif existing != entry:
            raise V3PersistenceError("offset receipt differs from append intent")
        cursor = BlackoutCaptureCursor(
            state.blackout_id,
            state.logical_segment_id,
            BlackoutChainKind.PHYSICAL,
            None if intent.expected_seq == MAX_CHAIN_SEQUENCE else intent.expected_seq + 1,
            self._record_hash(line),
        )
        new_receipt = replace(
            receipt,
            trusted_bytes=intent.file_offset + len(line),
            last_seq=intent.expected_seq,
            last_record_sha256=self._record_hash(line),
        )
        completed = replace(
            state,
            physical_cursor=cursor,
            capture_bytes=state.capture_bytes + len(line),
            capture_record_count=state.capture_record_count + 1,
            sample_count=state.sample_count + (operation == "sample"),
            gap_count=state.gap_count + (operation == "gap"),
            storage_segments=(*state.storage_segments[:-1], new_receipt),
            append_intent=None,
            last_append=V3LastAppend(
                operation,
                intent.expected_previous_hash or "0" * 64,
                self._record_hash(line),
                self._record_hash(line),
            ),
        )
        return self._registry.compare_and_replace(
            tx, expected=snapshot, replacement=V3WorkRegistry(completed, snapshot.state.pending)
        )

    def _record_hash(self, line: bytes) -> str:
        value = decode_v3_record(line).envelope.record_sha256
        if value is None:
            raise V3PersistenceError("record hash is missing")
        return value
