"""Registry-first START opening for the bounded v3 physical capture store."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from src.adapters.jsonl_v3_blackout_start_codec import decode_blackout_start, encode_blackout_start
from src.adapters.jsonl_v3_errors import V3AppendConflict, V3FileNotFound, V3PersistenceError
from src.adapters.jsonl_v3_filesystem import JsonlV3Filesystem, V3WriteTransaction
from src.adapters.jsonl_v3_registry import JsonlV3WorkRegistry, RegistrySnapshot, V3WorkRegistry
from src.adapters.jsonl_v3_registry_values import (
    MAX_CAPTURE_BYTES,
    CapturingState,
    PreparingCaptureState,
    V3StorageSegmentReceipt,
)
from src.adapters.jsonl_v3_segment_index import OffsetRecordKind, SegmentIndexEntry
from src.adapters.jsonl_v3_storage_paths import V3SegmentPathToken
from src.application.blackout_storage_values import (
    BlackoutCaptureCursor,
    BlackoutCaptureOpened,
    BlackoutChainKind,
    BlackoutRef,
)
from src.domain.blackout_capture import BlackoutStart


class JsonlV3CaptureOpenMixin:
    """Own START encoding, preparation recovery, and idempotent opening."""

    if TYPE_CHECKING:
        filesystem: JsonlV3Filesystem
        _registry: JsonlV3WorkRegistry

        def _new_uuid(self) -> str: ...

        def _ensure_pending_capacity(self, snapshot: RegistrySnapshot) -> None: ...

        def _record_hash(self, line: bytes) -> str: ...

    def open(self, start: BlackoutStart) -> BlackoutCaptureOpened:
        encoded = encode_blackout_start(start)
        with self.filesystem.write_transaction() as tx:
            paths = self.filesystem.paths
            if paths is None:
                raise V3PersistenceError()
            snapshot = self._registry.open_or_create(tx)
            storage_id = self._new_uuid()
            if snapshot.state.capture is not None:
                capture = snapshot.state.capture
                if isinstance(capture, PreparingCaptureState):
                    if (
                        capture.blackout_id != start.blackout_id
                        or capture.logical_segment_id != start.segment_id
                        or capture.start_sha256 != hashlib.sha256(encoded.line).hexdigest()
                        or capture.start_line_utf8.encode() != encoded.line
                    ):
                        raise V3AppendConflict()
                    return self._complete_preparing(tx, snapshot, capture)
                if isinstance(capture, CapturingState) and (
                    capture.blackout_id,
                    capture.logical_segment_id,
                ) == (start.blackout_id, start.segment_id):
                    self._verify_start(tx, capture, encoded.line)
                    return BlackoutCaptureOpened(
                        BlackoutRef(start.blackout_id, start.segment_id), capture.physical_cursor
                    )
                raise V3AppendConflict()
            self._ensure_pending_capacity(snapshot)
            path = paths.segment_token(
                start.wall_time_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                start.blackout_id,
                start.segment_id,
                0,
                storage_id,
            )
            offset = paths.offset_token(path)
            preparing = PreparingCaptureState(
                "preparing",
                start.blackout_id,
                start.segment_id,
                storage_id,
                path,
                offset,
                encoded.line.decode(),
                hashlib.sha256(encoded.line).hexdigest(),
                len(encoded.line),
                path.started_utc,
                start.policy_revision,
            )
            prepared = self._registry.compare_and_replace(
                tx, expected=snapshot, replacement=V3WorkRegistry(preparing, snapshot.state.pending)
            )
            tx.create_and_sync(path, encoded.line, MAX_CAPTURE_BYTES)
            index = tx.create_offset_index(offset)
            index = tx.append_offset_index(
                offset,
                expected=index,
                entry=SegmentIndexEntry(
                    0, 0, len(encoded.line), encoded.record_sha256, OffsetRecordKind.START
                ),
            )
            receipt = V3StorageSegmentReceipt(
                0,
                storage_id,
                path,
                offset,
                len(encoded.line),
                0,
                0,
                encoded.record_sha256,
                None,
                False,
            )
            cursor = BlackoutCaptureCursor(
                start.blackout_id,
                start.segment_id,
                BlackoutChainKind.PHYSICAL,
                1,
                encoded.record_sha256,
            )
            capturing = CapturingState(
                "capturing",
                start.blackout_id,
                start.segment_id,
                start.physical_episode_id,
                start.battery_epoch_id,
                start.observation_origin.value,
                start.uat_intent_id,
                start.policy_revision,
                cursor,
                None,
                len(encoded.line),
                1,
                0,
                0,
                (receipt,),
                None,
                None,
                None,
                None,
            )
            self._registry.compare_and_replace(
                tx, expected=prepared, replacement=V3WorkRegistry(capturing, prepared.state.pending)
            )
            return BlackoutCaptureOpened(BlackoutRef(start.blackout_id, start.segment_id), cursor)

    def _complete_preparing(
        self, tx: V3WriteTransaction, snapshot: RegistrySnapshot, state: PreparingCaptureState
    ) -> BlackoutCaptureOpened:
        """Finish the registry-first START transaction after a restart."""
        try:
            raw, file_snapshot = tx.read_bounded(state.path_token, max_bytes=4 * 1024 * 1024)
        except V3FileNotFound:
            tx.create_and_sync(state.path_token, state.start_line_utf8.encode(), MAX_CAPTURE_BYTES)
            raw = state.start_line_utf8.encode()
            file_snapshot = None
        if raw != state.start_line_utf8.encode() or (
            file_snapshot is not None and file_snapshot.content_sha256 != state.start_sha256
        ):
            raise V3AppendConflict()
        index = tx.create_offset_index(state.offset_token)
        if index.entry_count == 0:
            index = tx.append_offset_index(
                state.offset_token,
                expected=index,
                entry=SegmentIndexEntry(
                    0,
                    0,
                    state.start_length,
                    self._record_hash(state.start_line_utf8.encode()),
                    OffsetRecordKind.START,
                ),
            )
        receipt = V3StorageSegmentReceipt(
            0,
            state.storage_id,
            state.path_token,
            state.offset_token,
            state.start_length,
            0,
            0,
            self._record_hash(state.start_line_utf8.encode()),
            None,
            False,
        )
        cursor = BlackoutCaptureCursor(
            state.blackout_id,
            state.logical_segment_id,
            BlackoutChainKind.PHYSICAL,
            1,
            self._record_hash(state.start_line_utf8.encode()),
        )
        start = decode_blackout_start(state.start_line_utf8.encode())
        capturing = CapturingState(
            "capturing",
            state.blackout_id,
            state.logical_segment_id,
            start.physical_episode_id,
            start.battery_epoch_id,
            start.observation_origin.value,
            start.uat_intent_id,
            state.frozen_policy_revision,
            cursor,
            None,
            state.start_length,
            1,
            0,
            0,
            (receipt,),
            None,
            None,
            None,
            None,
        )
        # The preparing schema intentionally stores no derived identity.  The
        # START bytes are authoritative; recovery preserves that wire data and
        # uses conservative scope values until the next owner load verifies it.
        replacement = V3WorkRegistry(capturing, snapshot.state.pending)
        self._registry.compare_and_replace(tx, expected=snapshot, replacement=replacement)
        return BlackoutCaptureOpened(
            BlackoutRef(state.blackout_id, state.logical_segment_id), cursor
        )

    def _verify_start(self, tx: V3WriteTransaction, state: CapturingState, expected: bytes) -> None:
        receipt = state.storage_segments[0]
        if not isinstance(receipt.path_token, V3SegmentPathToken):
            raise V3AppendConflict()
        region = tx.read_region_bounded(
            receipt.path_token, offset=0, length=len(expected), max_file_bytes=MAX_CAPTURE_BYTES
        )
        if region.contents != expected:
            raise V3AppendConflict()
