"""Restart-convergent physical-chain damage recovery for the v3 capture store."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol, cast

from src.adapters.jsonl_v3_canonical import EncodedV3Record, decode_v3_record
from src.adapters.jsonl_v3_discharge_gap_codec import encode_discharge_gap
from src.adapters.jsonl_v3_errors import (
    V3AppendConflict,
    V3CapacityError,
    V3FileNotFound,
    V3PersistenceError,
)
from src.adapters.jsonl_v3_filesystem import JsonlV3Filesystem, V3FaultPoint, V3WriteTransaction
from src.adapters.jsonl_v3_filesystem_regions import V3FileSnapshot
from src.adapters.jsonl_v3_registry import (
    JsonlV3WorkRegistry,
    RegistrySnapshot,
    V3WorkRegistry,
)
from src.adapters.jsonl_v3_registry_values import (
    MAX_CAPTURE_BYTES,
    CapturingState,
    V3DamageContinuation,
    V3LastAppend,
    V3StorageSegmentReceipt,
)
from src.adapters.jsonl_v3_segment_index import OffsetRecordKind, SegmentIndexEntry
from src.adapters.jsonl_v3_storage_paths import (
    V3DamagedOffsetPathToken,
    V3DamagedSegmentPathToken,
    V3OffsetPathToken,
    V3SegmentPathToken,
)
from src.application.blackout_storage_values import (
    BlackoutCaptureCursor,
    BlackoutChainKind,
)
from src.domain.blackout_capture import DischargeGap, DischargeGapReason
from src.domain.fragments import ObservationOrigin

# Physical capture bytes stop before the independent two MiB terminal reserve.
# Keep this local to recovery: importing the facade here would create a cycle.
MAX_PHYSICAL_CAPTURE_BYTES = 62 * 1024 * 1024


class _RecoveryOwner(Protocol):
    filesystem: JsonlV3Filesystem
    _registry: JsonlV3WorkRegistry

    def _new_uuid(self) -> str: ...


@dataclass(frozen=True, slots=True)
class _DamagePlan:
    state: CapturingState
    receipt: V3StorageSegmentReceipt
    old_snapshot: V3FileSnapshot
    digest: str
    damaged_path: V3DamagedSegmentPathToken
    damaged_offset: V3DamagedOffsetPathToken
    encoded: EncodedV3Record
    new_path: V3SegmentPathToken
    new_offset: V3OffsetPathToken
    damage: V3DamageContinuation


class JsonlV3CaptureRecoveryMixin:
    """Cohesive same-aggregate damage continuation state machine."""

    if TYPE_CHECKING:
        filesystem: JsonlV3Filesystem
        _registry: JsonlV3WorkRegistry

        def _new_uuid(self) -> str: ...

    def _begin_damage(self, tx: V3WriteTransaction, snapshot: RegistrySnapshot) -> RegistrySnapshot:
        plan = self._damage_plan(tx, snapshot.state.capture)
        state = plan.state
        # The active append intent points at the old mutable path.  Once the
        # damage transaction is reserved, recovery must dispatch from the
        # damage phase even if that path has already been renamed.
        reserved = replace(state, append_intent=None, damage_continuation=plan.damage)
        reserved_snapshot = self._registry.compare_and_replace(
            tx,
            expected=snapshot,
            replacement=V3WorkRegistry(reserved, snapshot.state.pending),
        )
        tx._fault(V3FaultPoint.DAMAGE_AFTER_REGISTRY_ADVANCE)
        return self._resume_damage(tx, reserved_snapshot)

    def _resume_damage(
        self, tx: V3WriteTransaction, snapshot: RegistrySnapshot
    ) -> RegistrySnapshot:
        state = snapshot.state.capture
        if not isinstance(state, CapturingState) or state.damage_continuation is None:
            raise V3AppendConflict()
        damage = state.damage_continuation
        if damage.phase == "reserved":
            snapshot = self._resume_damage_rename(tx, snapshot, damage)
            state = snapshot.state.capture
            if not isinstance(state, CapturingState) or state.damage_continuation is None:
                raise V3AppendConflict()
            damage = state.damage_continuation
        if damage.phase == "old_renamed":
            snapshot = self._resume_damage_successor(tx, snapshot, damage)
            state = snapshot.state.capture
            if not isinstance(state, CapturingState) or state.damage_continuation is None:
                raise V3AppendConflict()
            damage = state.damage_continuation
        if damage.phase == "successor_created":
            snapshot = self._resume_damage_receipts(tx, snapshot, damage)
            state = snapshot.state.capture
            if not isinstance(state, CapturingState) or state.damage_continuation is None:
                raise V3AppendConflict()
            damage = state.damage_continuation
        if damage.phase == "gap_durable":
            # A final physical reference deliberately retains this durable
            # marker.  The facade uses it to run the terminal anchor/close/
            # successor transactions after this transaction has committed.
            # Calling the ordinary gap completion again would otherwise make
            # the marker disappear before the multi-transaction finalizer can
            # be resumed.
            if self._is_final_damage_state(state):
                return snapshot
            return self._resume_damage_gap(tx, snapshot, state, damage)
        return snapshot

    def _resume_damage_rename(
        self, tx: V3WriteTransaction, snapshot: RegistrySnapshot, damage: V3DamageContinuation
    ) -> RegistrySnapshot:
        segment_renamed = self._ensure_damaged_segment(tx, damage)
        if segment_renamed:
            tx._fault(V3FaultPoint.DAMAGE_AFTER_SEGMENT_RENAME)
        offset_renamed = self._ensure_damaged_offset(tx, damage)
        if segment_renamed or offset_renamed:
            tx._fault(V3FaultPoint.DAMAGE_AFTER_SEGMENTS_DIRSYNC)
        updated = replace(damage, phase="old_renamed")
        state = snapshot.state.capture
        if not isinstance(state, CapturingState):
            raise V3AppendConflict()
        advanced = self._registry.compare_and_replace(
            tx,
            expected=snapshot,
            replacement=V3WorkRegistry(
                replace(state, damage_continuation=updated), snapshot.state.pending
            ),
        )
        tx._fault(V3FaultPoint.DAMAGE_AFTER_REGISTRY_ADVANCE)
        return advanced

    def _ensure_damaged_segment(self, tx: V3WriteTransaction, damage: V3DamageContinuation) -> bool:
        """Complete or verify the segment half of a possibly torn rename."""
        try:
            source = self._snapshot(tx, damage.old_path_token)
        except V3FileNotFound:
            target = self._snapshot(tx, damage.damaged_path_token)
            if (
                target.content_sha256 != damage.damaged_file_sha256
                or target.byte_length < damage.trusted_bytes
            ):
                raise V3PersistenceError("damaged segment receipt differs")
            return False
        tx.rename_damaged(damage.old_path_token, damage.damaged_path_token, source)
        return True

    def _ensure_damaged_offset(self, tx: V3WriteTransaction, damage: V3DamageContinuation) -> bool:
        """Complete or verify the offset half after an independent segment rename."""
        try:
            source = self._snapshot(tx, damage.old_offset_token)
        except V3FileNotFound:
            self._snapshot(tx, damage.damaged_offset_token)
            return False
        self._rename_offset_damaged(tx, damage, source)
        return True

    def _rename_offset_damaged(
        self, tx: V3WriteTransaction, damage: V3DamageContinuation, expected: V3FileSnapshot
    ) -> None:
        """Rename offsets idempotently; its basename carries the segment hash."""
        if tx.file_sha256(damage.old_offset_token) != expected.content_sha256:
            raise V3PersistenceError("damaged offset compare-and-swap mismatch")
        fd, source_parent = tx._open(damage.old_offset_token, os.O_RDONLY, 0o600)
        os.fchmod(fd, 0o400)
        os.fsync(fd)
        target_parent, target_name = tx._location(damage.damaged_offset_token)
        try:
            os.lstat(target_name, dir_fd=target_parent)
        except FileNotFoundError:
            pass
        else:
            raise V3PersistenceError("damaged offset target already exists")
        source_name = tx._location(damage.old_offset_token)[1]
        try:
            os.rename(source_name, target_name, src_dir_fd=source_parent, dst_dir_fd=target_parent)
            os.fsync(target_parent)
        except OSError as exc:
            raise V3PersistenceError("damaged offset rename failed") from exc

    def _resume_damage_successor(
        self, tx: V3WriteTransaction, snapshot: RegistrySnapshot, damage: V3DamageContinuation
    ) -> RegistrySnapshot:
        line = damage.gap_line_utf8.encode()
        path = self._new_path(damage)
        try:
            raw, _ = tx.read_bounded(path, max_bytes=4 * 1024 * 1024)
            if raw != line:
                raise V3PersistenceError("damage successor bytes differ")
        except V3FileNotFound:
            tx.create_and_sync(path, line, MAX_CAPTURE_BYTES)
            tx._fault(V3FaultPoint.DAMAGE_AFTER_CONTINUATION_CREATE)
            tx._fault(V3FaultPoint.DAMAGE_AFTER_CONTINUATION_FDATASYNC)
        offset = self._new_offset(damage)
        index = tx.create_offset_index(offset)
        record_hash = decode_v3_record(line).envelope.record_sha256
        if record_hash is None:
            raise V3PersistenceError("damage gap record hash is missing")
        entry = SegmentIndexEntry(
            damage.new_physical_next_seq - 1, 0, len(line), record_hash, OffsetRecordKind.GAP
        )
        existing = tx.get_offset_index(offset, sequence=entry.sequence)
        if existing is None:
            tx.append_offset_index(offset, expected=index, entry=entry)
        elif existing != entry:
            raise V3PersistenceError("damage successor index differs")
        state = snapshot.state.capture
        if not isinstance(state, CapturingState):
            raise V3AppendConflict()
        updated = replace(damage, phase="successor_created")
        advanced = self._registry.compare_and_replace(
            tx,
            expected=snapshot,
            replacement=V3WorkRegistry(
                replace(state, damage_continuation=updated), snapshot.state.pending
            ),
        )
        tx._fault(V3FaultPoint.DAMAGE_AFTER_REGISTRY_ADVANCE)
        return advanced

    def _resume_damage_receipts(
        self, tx: V3WriteTransaction, snapshot: RegistrySnapshot, damage: V3DamageContinuation
    ) -> RegistrySnapshot:
        state = snapshot.state.capture
        if not isinstance(state, CapturingState):
            raise V3AppendConflict()
        receipt = state.storage_segments[-1]
        damaged_receipt = replace(
            receipt,
            path_token=damage.damaged_path_token,
            offset_token=damage.damaged_offset_token,
            damaged_file_sha256=damage.damaged_file_sha256,
        )
        successor = V3StorageSegmentReceipt(
            damage.new_ordinal,
            damage.new_storage_id,
            self._new_path(damage),
            self._new_offset(damage),
            damage.gap_length,
            damage.new_physical_next_seq - 1,
            damage.new_physical_next_seq - 1,
            damage.new_physical_last_record_sha256,
            None,
            False,
        )
        updated = replace(
            state,
            storage_segments=(*state.storage_segments[:-1], damaged_receipt, successor),
            damage_continuation=replace(damage, phase="gap_durable"),
        )
        advanced = self._registry.compare_and_replace(
            tx, expected=snapshot, replacement=V3WorkRegistry(updated, snapshot.state.pending)
        )
        tx._fault(V3FaultPoint.DAMAGE_AFTER_REGISTRY_ADVANCE)
        return advanced

    def _resume_damage_gap(
        self,
        tx: V3WriteTransaction,
        snapshot: RegistrySnapshot,
        state: CapturingState,
        damage: V3DamageContinuation,
    ) -> RegistrySnapshot:
        cursor = BlackoutCaptureCursor(
            state.blackout_id,
            state.logical_segment_id,
            BlackoutChainKind.PHYSICAL,
            damage.new_physical_next_seq,
            damage.new_physical_last_record_sha256,
        )
        # Reference 64 is the last physical receipt.  Keep the explicit
        # ``gap_durable`` phase until the terminal anchor, CAPTURE_DAMAGED
        # END, and linked successor have each committed.  Earlier damage
        # continuations retain the historical one-transaction completion.
        final_damage = len(state.storage_segments) >= 63
        completed = replace(
            state,
            physical_cursor=cursor,
            capture_bytes=state.capture_bytes + damage.gap_length,
            capture_record_count=state.capture_record_count + 1,
            gap_count=state.gap_count + 1,
            append_intent=None,
            damage_continuation=damage if final_damage else None,
            last_append=V3LastAppend(
                "gap",
                state.physical_cursor.last_record_sha256 or "0" * 64,
                damage.new_physical_last_record_sha256,
                damage.new_physical_last_record_sha256,
            ),
        )
        advanced = self._registry.compare_and_replace(
            tx, expected=snapshot, replacement=V3WorkRegistry(completed, snapshot.state.pending)
        )
        tx._fault(V3FaultPoint.DAMAGE_AFTER_REGISTRY_ADVANCE)
        return advanced

    def _is_final_damage_state(self, state: object) -> bool:
        if not isinstance(state, CapturingState) or state.damage_continuation is None:
            return False
        return len(state.storage_segments) >= 64 and any(
            receipt.damaged_file_sha256 is not None for receipt in state.storage_segments
        )

    def _new_path(self, damage: V3DamageContinuation) -> V3SegmentPathToken:
        paths = self.filesystem.paths
        if paths is None:
            raise V3PersistenceError()
        return paths.segment_token(
            damage.old_path_token.started_utc,
            damage.blackout_id,
            damage.logical_segment_id,
            damage.new_ordinal,
            damage.new_storage_id,
        )

    def _new_offset(self, damage: V3DamageContinuation) -> V3OffsetPathToken:
        paths = self.filesystem.paths
        if paths is None:
            raise V3PersistenceError()
        return paths.offset_token(self._new_path(damage))

    def _verify_damaged_pair(self, tx: V3WriteTransaction, damage: V3DamageContinuation) -> None:
        snap = self._snapshot(tx, damage.damaged_path_token)
        if (
            snap.content_sha256 != damage.damaged_file_sha256
            or snap.byte_length < damage.trusted_bytes
        ):
            raise V3PersistenceError("damaged segment receipt differs")
        self._snapshot(tx, damage.damaged_offset_token)

    def _snapshot(
        self,
        tx: V3WriteTransaction,
        token: V3SegmentPathToken
        | V3DamagedSegmentPathToken
        | V3OffsetPathToken
        | V3DamagedOffsetPathToken,
    ) -> V3FileSnapshot:
        """Hash a file in bounded regions so recovery is not capped at 4 MiB."""
        if isinstance(token, (V3OffsetPathToken, V3DamagedOffsetPathToken)):
            return tx.read_bounded(token, max_bytes=4 * 1024 * 1024)[1]
        segment = cast(V3SegmentPathToken | V3DamagedSegmentPathToken, token)
        probe = tx.read_region_bounded(
            segment, offset=0, length=1, max_file_bytes=MAX_CAPTURE_BYTES
        )
        digest = hashlib.sha256()
        offset = 0
        while offset < probe.file_length:
            length = min(1024 * 1024, probe.file_length - offset)
            region = tx.read_region_bounded(
                segment, offset=offset, length=length, max_file_bytes=MAX_CAPTURE_BYTES
            )
            digest.update(region.contents)
            offset += length
        return V3FileSnapshot(probe.file_length, digest.hexdigest())

    def _damage_plan(self, tx: V3WriteTransaction, capture: object) -> _DamagePlan:
        if not isinstance(capture, CapturingState) or capture.append_intent is None:
            raise V3AppendConflict()
        if len(capture.storage_segments) >= 64:
            raise V3CapacityError("physical reference 65 is refused")
        receipt = capture.storage_segments[-1]
        if type(receipt.path_token) is not V3SegmentPathToken:
            raise V3PersistenceError("damage requires an active segment")
        if type(receipt.offset_token) is not V3OffsetPathToken:
            raise V3PersistenceError("damage requires an active offset index")
        old_snapshot = self._snapshot(tx, receipt.path_token)
        digest = old_snapshot.content_sha256
        paths = self.filesystem.paths
        if paths is None:
            raise V3PersistenceError()
        damaged_path, damaged_offset = paths.damaged_tokens(
            capture.blackout_id,
            capture.logical_segment_id,
            receipt.ordinal,
            receipt.storage_id,
            digest,
        )
        encoded = encode_discharge_gap(
            self._corruption_gap(capture),
            seq=capture.physical_cursor.next_sequence or 0,
            previous_record_sha256=capture.physical_cursor.last_record_sha256,
        )
        # The replacement GAP is a real physical record.  A damaged file is
        # retained as evidence, so both its on-disk length and the new gap are
        # charged against the physical 62 MiB boundary.  The accepted logical
        # bytes are checked separately against the 64 MiB aggregate bound.
        if old_snapshot.byte_length + len(encoded.line) > MAX_PHYSICAL_CAPTURE_BYTES:
            raise V3CapacityError("corruption replacement exceeds physical capture limit")
        if capture.capture_bytes + len(encoded.line) > MAX_CAPTURE_BYTES:
            raise V3CapacityError("corruption replacement exceeds aggregate capture limit")
        new_storage_id = self._new_uuid()
        new_path = paths.segment_token(
            receipt.path_token.started_utc,
            capture.blackout_id,
            capture.logical_segment_id,
            receipt.ordinal + 1,
            new_storage_id,
        )
        new_offset = paths.offset_token(new_path)
        damage = V3DamageContinuation(
            "reserved",
            capture.blackout_id,
            capture.logical_segment_id,
            receipt.storage_id,
            new_storage_id,
            receipt.ordinal,
            receipt.ordinal + 1,
            receipt.path_token,
            receipt.offset_token,
            damaged_path,
            damaged_offset,
            receipt.trusted_bytes,
            receipt.last_seq,
            receipt.last_record_sha256,
            digest,
            encoded.line.decode(),
            hashlib.sha256(encoded.line).hexdigest(),
            len(encoded.line),
            (capture.physical_cursor.next_sequence or 0) + 1,
            encoded.record_sha256,
        )
        return _DamagePlan(
            capture,
            receipt,
            old_snapshot,
            digest,
            damaged_path,
            damaged_offset,
            encoded,
            new_path,
            new_offset,
            damage,
        )

    def _corruption_gap(self, state: CapturingState) -> DischargeGap:
        now = datetime.now(timezone.utc)
        return DischargeGap(
            state.blackout_id,
            state.physical_episode_id,
            state.battery_epoch_id,
            state.logical_segment_id,
            ObservationOrigin(state.observation_origin),
            DischargeGapReason.CORRUPT_CHAIN,
            1,
            "storage-recovery",
            "storage-recovery",
            0,
            0,
            "storage-recovery",
            0,
            now,
            now,
            now,
            "jsonl-v3-capture",
            "corrupt-chain",
            None,
            None,
            state.uat_intent_id,
        )
