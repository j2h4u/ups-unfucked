"""Bounded physical-chain evidence reads over verified snapshot seams."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from src.adapters.jsonl_v3_blackout_start_codec import decode_blackout_start
from src.adapters.jsonl_v3_canonical import V3CodecError, decode_v3_record
from src.adapters.jsonl_v3_discharge_gap_codec import decode_discharge_gap
from src.adapters.jsonl_v3_discharge_sample_codec import decode_discharge_sample
from src.adapters.jsonl_v3_errors import V3CorruptionError, V3PathError, V3ValidationError
from src.adapters.jsonl_v3_filesystem import JsonlV3Filesystem, V3WriteTransaction
from src.adapters.jsonl_v3_filesystem_regions import V3ReadOnlyFilesystemRegions
from src.adapters.jsonl_v3_registry import JsonlV3WorkRegistry, RegistrySnapshot
from src.adapters.jsonl_v3_registry_values import (
    CapturingState,
    ProcessingState,
    TailState,
    V3StorageSegmentReceipt,
)
from src.adapters.jsonl_v3_segment_index import (
    OffsetRecordKind,
    SegmentIndexEntry,
    SegmentIndexSnapshot,
)
from src.adapters.jsonl_v3_storage_paths import (
    V3DamagedOffsetPathToken,
    V3DamagedSegmentPathToken,
    V3OffsetPathToken,
    V3SegmentPathToken,
)
from src.adapters.jsonl_v3_terminal_tail_codec import decode_endpoint_anchor
from src.application.blackout_storage_values import (
    MAX_EVIDENCE_PAGE_BYTES,
    MAX_EVIDENCE_PAGE_RECORDS,
    BlackoutCaptureCursor,
    BlackoutChainKind,
    BlackoutRecordType,
    BlackoutRef,
    RawEvidencePage,
    StoredPhysicalRecord,
    StoredRecordRef,
)
from src.domain.blackout_capture import BlackoutStart, DischargeGap, DischargeSample
from src.domain.fragment_primitives import AnchorKind
from src.domain.fragments import EndpointAnchor

MAX_RECORD_BYTES = 20 * 1024
MAX_CAPTURE_BYTES = 64 * 1024 * 1024
EvidenceToken = V3SegmentPathToken | V3DamagedSegmentPathToken
OffsetToken = V3OffsetPathToken | V3DamagedOffsetPathToken


class EvidenceAuthority(StrEnum):
    ACTIVE_REGISTRY = "active_registry"
    SEALED_LOCATOR = "sealed_locator"


@dataclass(frozen=True, slots=True)
class EvidenceSegmentReceipt:
    ref: BlackoutRef
    ordinal: int
    storage_id: str
    path_token: EvidenceToken
    offset_token: OffsetToken
    file_sha256: str | None
    offset_table_sha256: str | None
    byte_length: int
    trusted_byte_length: int
    first_sequence: int
    last_sequence: int
    first_record_sha256: str
    last_record_sha256: str
    damaged: bool
    authority: EvidenceAuthority = EvidenceAuthority.ACTIVE_REGISTRY

    def __post_init__(self) -> None:
        _receipt_identity(self)
        _receipt_bounds(self)
        _receipt_tokens(self)


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    ref: BlackoutRef
    physical_chain_root_record_sha256: str
    physical_chain_final_record_sha256: str
    segments: tuple[EvidenceSegmentReceipt, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.ref, BlackoutRef) or not isinstance(self.segments, tuple):
            raise V3ValidationError("evidence snapshot is invalid")
        if not self.segments or len(self.segments) > 64:
            raise V3ValidationError("evidence snapshot segment count is invalid")
        if any(not isinstance(item, EvidenceSegmentReceipt) for item in self.segments):
            raise V3ValidationError("evidence snapshot segment receipt is invalid")
        _hash(self.physical_chain_root_record_sha256)
        _hash(self.physical_chain_final_record_sha256)
        if any(item.ref != self.ref for item in self.segments):
            raise V3PathError("evidence snapshot scope differs")
        if len({item.authority for item in self.segments}) != 1:
            raise V3ValidationError("evidence snapshot authority is mixed")


@dataclass(frozen=True, slots=True)
class _PageReadState:
    next_sequence: int
    previous_hash: str | None
    bytes_used: int
    records_used: int
    record_limit: int


def _receipt_identity(value: EvidenceSegmentReceipt) -> None:
    if not isinstance(value.ref, BlackoutRef) or type(value.ordinal) is not int:
        raise V3ValidationError("evidence receipt identity is invalid")
    if not 0 <= value.ordinal < 64 or not isinstance(value.storage_id, str):
        raise V3ValidationError("evidence receipt identity is invalid")
    if not isinstance(value.path_token, (V3SegmentPathToken, V3DamagedSegmentPathToken)):
        raise V3ValidationError("evidence segment token is invalid")
    if not isinstance(value.offset_token, (V3OffsetPathToken, V3DamagedOffsetPathToken)):
        raise V3ValidationError("evidence offset token is invalid")
    for item in (value.first_record_sha256, value.last_record_sha256):
        _hash(item)
    if value.authority is EvidenceAuthority.SEALED_LOCATOR and (
        value.file_sha256 is None or value.offset_table_sha256 is None
    ):
        raise V3ValidationError("sealed evidence receipt hashes are mandatory")
    if type(value.authority) is not EvidenceAuthority:
        raise V3ValidationError("evidence authority is invalid")
    for item in (value.file_sha256, value.offset_table_sha256):
        if item is not None:
            _hash(item)


def _receipt_bounds(value: EvidenceSegmentReceipt) -> None:
    valid_bytes = (
        type(value.byte_length) is int
        and type(value.trusted_byte_length) is int
        and 0 <= value.trusted_byte_length <= value.byte_length <= MAX_CAPTURE_BYTES
    )
    valid_sequences = (
        type(value.first_sequence) is int
        and type(value.last_sequence) is int
        and 0 <= value.first_sequence <= value.last_sequence <= 3197
    )
    if not valid_bytes or not valid_sequences or type(value.damaged) is not bool:
        raise V3ValidationError("evidence receipt bounds are invalid")


def _receipt_tokens(value: EvidenceSegmentReceipt) -> None:
    damaged = isinstance(value.path_token, V3DamagedSegmentPathToken)
    if value.damaged != damaged:
        raise V3ValidationError("evidence damaged flag is not bound to token")
    if isinstance(value.path_token, V3SegmentPathToken):
        if not isinstance(value.offset_token, V3OffsetPathToken):
            raise V3ValidationError("active segment and offset tokens differ")
        if value.path_token.started_utc != value.offset_token.started_utc:
            raise V3ValidationError("active segment and offset UTC differ")
    elif not isinstance(value.offset_token, V3DamagedOffsetPathToken):
        raise V3ValidationError("damaged segment and offset tokens differ")
    scope = (value.ref.blackout_id, value.ref.segment_id, value.storage_id, value.ordinal)
    path_scope = (
        value.path_token.blackout_id,
        value.path_token.logical_segment_id,
        value.path_token.storage_id,
        value.path_token.ordinal,
    )
    offset_scope = (
        value.offset_token.blackout_id,
        value.offset_token.logical_segment_id,
        value.offset_token.storage_id,
        value.offset_token.ordinal,
    )
    if path_scope != scope or offset_scope != scope:
        raise V3PathError("evidence receipt token scope differs")
    if (
        isinstance(value.path_token, V3DamagedSegmentPathToken)
        and isinstance(value.offset_token, V3DamagedOffsetPathToken)
        and (
            value.file_sha256 is None
            or value.path_token.file_sha256 != value.file_sha256
            or value.offset_token.file_sha256 != value.file_sha256
        )
    ):
        raise V3CorruptionError("damaged evidence hash differs")


class EvidenceSnapshotProvider(Protocol):
    def snapshot(self, ref: BlackoutRef) -> EvidenceSnapshot: ...


class EvidenceReader(Protocol):
    def read_authenticated(
        self, receipt: EvidenceSegmentReceipt, entry: SegmentIndexEntry
    ) -> bytes: ...


class SealedLocatorReader(Protocol):
    """Typed read seam for an immutable terminal locator."""

    def read(self, ref: BlackoutRef) -> "SealedLocator": ...


@dataclass(frozen=True, slots=True)
class SealedLocator:
    """Typed immutable locator payload returned by a per-call reader."""

    ref: BlackoutRef
    physical_chain_root_record_sha256: str
    physical_chain_final_record_sha256: str
    physical_segments: tuple[EvidenceSegmentReceipt, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.ref, BlackoutRef) or not isinstance(self.physical_segments, tuple):
            raise V3ValidationError("sealed locator is invalid")
        if not self.physical_segments:
            raise V3CorruptionError("sealed locator has no physical segments")
        _hash(self.physical_chain_root_record_sha256)
        _hash(self.physical_chain_final_record_sha256)
        if any(not isinstance(item, EvidenceSegmentReceipt) for item in self.physical_segments):
            raise V3CorruptionError("sealed locator segment receipt is invalid")
        if any(item.ref != self.ref for item in self.physical_segments):
            raise V3PathError("sealed locator segment scope differs")


class EvidenceOffsetReader(Protocol):
    def snapshot_and_page(
        self,
        receipt: EvidenceSegmentReceipt,
        *,
        first_sequence: int,
        limit: int,
    ) -> tuple[SegmentIndexEntry, ...]: ...


class ActiveRegistryEvidenceSnapshotProvider:
    """Build one bounded physical snapshot under the registry transaction."""

    def __init__(self, filesystem: JsonlV3Filesystem) -> None:
        self._filesystem = filesystem
        self._registry = JsonlV3WorkRegistry(filesystem)

    def snapshot(self, ref: BlackoutRef) -> EvidenceSnapshot:
        with self._filesystem.write_transaction() as tx:
            registry_snapshot = self._registry.read(tx)
            registry = registry_snapshot.state
            candidates = [registry.capture, *registry.pending]
            state = next(
                (
                    item
                    for item in candidates
                    if item is not None
                    and item.blackout_id == ref.blackout_id
                    and item.logical_segment_id == ref.segment_id
                ),
                None,
            )
            if state is None or not isinstance(state, (CapturingState, ProcessingState, TailState)):
                raise V3PathError("active evidence aggregate is unavailable")
            receipts = tuple(
                _receipt_from_registry(tx, ref, item) for item in state.storage_segments
            )
            if not receipts:
                raise V3CorruptionError("active evidence snapshot has no segments")
            result = EvidenceSnapshot(
                ref,
                receipts[0].first_record_sha256,
                receipts[-1].last_record_sha256,
                receipts,
            )
            _verify_active_registry_snapshot(self._registry.read(tx), registry_snapshot)
            return result


def _receipt_from_registry(
    tx: V3WriteTransaction, ref: BlackoutRef, receipt: V3StorageSegmentReceipt
) -> EvidenceSegmentReceipt:
    offset_token = _registry_offset_token(receipt)
    offset_snapshot = tx.snapshot_offset_index(offset_token)
    first_entry, last_entry = _registry_offset_bounds(tx, offset_token, receipt)
    _validate_registry_offset_topology(offset_snapshot, receipt, first_entry, last_entry)
    byte_length = _registry_file_length(tx, receipt)
    return _build_registry_receipt(
        ref, receipt, offset_snapshot, byte_length, (first_entry, last_entry)
    )


def _registry_offset_token(
    receipt: V3StorageSegmentReceipt,
) -> V3OffsetPathToken | V3DamagedOffsetPathToken:
    if not isinstance(receipt.offset_token, (V3OffsetPathToken, V3DamagedOffsetPathToken)):
        raise V3PathError("active evidence requires a sealed offset token")
    return receipt.offset_token


def _registry_offset_bounds(
    tx: V3WriteTransaction,
    offset_token: V3OffsetPathToken | V3DamagedOffsetPathToken,
    receipt: V3StorageSegmentReceipt,
) -> tuple[SegmentIndexEntry, SegmentIndexEntry]:
    first_entry = tx.get_offset_index(offset_token, sequence=receipt.first_seq)
    last_entry = tx.get_offset_index(offset_token, sequence=receipt.last_seq)
    if first_entry is None or last_entry is None:
        raise V3CorruptionError("active evidence offset bounds are incomplete")
    return first_entry, last_entry


def _validate_registry_offset_topology(
    snapshot: SegmentIndexSnapshot,
    receipt: V3StorageSegmentReceipt,
    first_entry: SegmentIndexEntry,
    last_entry: SegmentIndexEntry,
) -> None:
    if snapshot.first_sequence != receipt.first_seq or snapshot.last_sequence != receipt.last_seq:
        raise V3CorruptionError("active evidence offset topology differs")
    if first_entry.sequence != receipt.first_seq or last_entry.sequence != receipt.last_seq:
        raise V3CorruptionError("active evidence offset topology differs")


def _registry_file_length(tx: V3WriteTransaction, receipt: V3StorageSegmentReceipt) -> int:
    measured_length = tx.file_length(receipt.path_token)
    if not isinstance(measured_length, int):
        raise V3CorruptionError("active evidence file length is invalid")
    if measured_length < receipt.trusted_bytes:
        raise V3CorruptionError("active evidence file is shorter than trusted bytes")
    return measured_length


def _build_registry_receipt(
    ref: BlackoutRef,
    receipt: V3StorageSegmentReceipt,
    snapshot: SegmentIndexSnapshot,
    byte_length: int,
    bounds: tuple[SegmentIndexEntry, SegmentIndexEntry],
) -> EvidenceSegmentReceipt:
    first_entry, last_entry = bounds
    return EvidenceSegmentReceipt(
        ref,
        receipt.ordinal,
        receipt.storage_id,
        receipt.path_token,
        receipt.offset_token,
        receipt.damaged_file_sha256,
        snapshot.append_state_sha256,
        byte_length,
        receipt.trusted_bytes,
        receipt.first_seq,
        receipt.last_seq,
        first_entry.record_sha256,
        last_entry.record_sha256,
        receipt.damaged_file_sha256 is not None,
    )


class JsonlV3FilesystemEvidenceOffsetReader:
    def __init__(self, transaction: V3WriteTransaction | V3ReadOnlyFilesystemRegions) -> None:
        self._transaction = transaction

    def snapshot_and_page(
        self,
        receipt: EvidenceSegmentReceipt,
        *,
        first_sequence: int,
        limit: int,
    ) -> tuple[SegmentIndexEntry, ...]:
        if not isinstance(receipt.offset_token, (V3OffsetPathToken, V3DamagedOffsetPathToken)):
            raise V3PathError("filesystem offset reader requires a sealed offset token")
        ordinal = first_sequence - receipt.first_sequence
        if ordinal < 0:
            ordinal = 0
        if isinstance(self._transaction, V3ReadOnlyFilesystemRegions):
            return self._transaction.authenticated_offset_page(
                receipt.offset_token,
                entry_ordinal=ordinal,
                limit=limit,
                expected_sha256=receipt.offset_table_sha256,
                sealed=receipt.authority is EvidenceAuthority.SEALED_LOCATOR,
            ).entries
        snapshot = self._transaction.snapshot_offset_index(receipt.offset_token)
        if receipt.authority is EvidenceAuthority.SEALED_LOCATOR:
            actual = self._transaction.file_sha256(receipt.offset_token)
            if receipt.offset_table_sha256 != actual:
                raise V3CorruptionError("sealed evidence offset-table hash differs")
        elif (
            receipt.offset_table_sha256 is not None
            and snapshot.append_state_sha256 != receipt.offset_table_sha256
        ):
            raise V3CorruptionError("active evidence offset CAS differs")
        page = self._transaction.page_offset_index(
            receipt.offset_token, entry_ordinal=ordinal, limit=limit
        )
        if self._transaction.snapshot_offset_index(receipt.offset_token) != snapshot:
            raise V3CorruptionError("evidence offset snapshot changed during read")
        return page.entries


class JsonlV3FilesystemEvidenceReader:
    """Read only authenticated bounded regions from active or damaged segments."""

    def __init__(self, transaction: V3WriteTransaction | V3ReadOnlyFilesystemRegions) -> None:
        self._transaction = transaction

    def read_exact(self, token: EvidenceToken, *, offset: int, length: int) -> bytes:
        if not isinstance(token, (V3SegmentPathToken, V3DamagedSegmentPathToken)):
            raise V3PathError("evidence reader requires a segment token")
        region = self._transaction.read_region(
            token,
            offset=offset,
            length=length,
            max_file_bytes=MAX_CAPTURE_BYTES,
        )
        if len(region.contents) != length:
            raise V3CorruptionError("evidence region read was short")
        return region.contents

    def read_authenticated(
        self, receipt: EvidenceSegmentReceipt, entry: SegmentIndexEntry
    ) -> bytes:
        """Read one offset-authorized line without scanning the segment."""
        _validate_entry_bounds(entry, receipt, entry.sequence)
        region = self._transaction.read_region(
            receipt.path_token,
            offset=entry.file_offset,
            length=entry.line_length,
            max_file_bytes=MAX_CAPTURE_BYTES,
        )
        if region.file_length != receipt.byte_length:
            raise V3CorruptionError("evidence segment snapshot changed")
        line = region.contents
        if len(line) != entry.line_length:
            raise V3CorruptionError("authenticated evidence line is short")
        try:
            decoded = decode_v3_record(line)
        except V3CodecError as exc:
            raise V3CorruptionError("authenticated evidence line is not canonical") from exc
        if decoded.envelope.record_sha256 != entry.record_sha256:
            raise V3CorruptionError("authenticated evidence record hash differs")
        return line


class SealedLocatorEvidenceSnapshotProvider:
    """Materialize one immutable physical snapshot from a per-call locator reader."""

    def __init__(self, locator_provider: SealedLocatorReader) -> None:
        self._locator_provider = locator_provider

    def snapshot(self, ref: BlackoutRef) -> EvidenceSnapshot:
        locator = self._locator_provider.read(ref)
        if not isinstance(locator, SealedLocator):
            raise V3CorruptionError("sealed locator reader returned an invalid locator")
        if locator.ref != ref:
            raise V3PathError("sealed locator scope differs")
        return EvidenceSnapshot(
            ref,
            locator.physical_chain_root_record_sha256,
            locator.physical_chain_final_record_sha256,
            tuple(
                replace(item, authority=EvidenceAuthority.SEALED_LOCATOR)
                for item in locator.physical_segments
            ),
        )


class JsonlV3EvidenceStore:
    def __init__(
        self,
        snapshots: EvidenceSnapshotProvider,
        reader: EvidenceReader,
        offsets: EvidenceOffsetReader,
    ) -> None:
        self._snapshots = snapshots
        self._reader = reader
        self._offsets = offsets

    def page(
        self,
        ref: BlackoutRef,
        cursor: BlackoutCaptureCursor | None = None,
        *,
        limit: int = MAX_EVIDENCE_PAGE_RECORDS,
    ) -> RawEvidencePage:
        _page_input(ref, cursor, limit)
        snapshot = self._snapshots.snapshot(ref)
        if snapshot.ref != ref:
            raise V3PathError("evidence snapshot scope differs")
        _topology(snapshot)
        next_sequence, previous_hash = _cursor_state(ref, cursor)
        if cursor is not None:
            _authorize_cursor(snapshot.segments, cursor, self._offsets)
        records, next_sequence, previous_hash = self._read_records(
            snapshot.segments, ref, next_sequence, previous_hash, limit
        )
        final_sequence = snapshot.segments[-1].last_sequence if snapshot.segments else -1
        complete = next_sequence > final_sequence
        if not records and not complete:
            raise V3CorruptionError("evidence page cannot make bounded progress")
        next_cursor = (
            None
            if complete
            else BlackoutCaptureCursor(
                ref.blackout_id,
                ref.segment_id,
                BlackoutChainKind.PHYSICAL,
                next_sequence,
                previous_hash,
            )
        )
        return RawEvidencePage(ref, tuple(records), next_cursor, complete)

    def _read_records(
        self,
        segments: tuple[EvidenceSegmentReceipt, ...],
        ref: BlackoutRef,
        next_sequence: int,
        previous_hash: str | None,
        limit: int,
    ) -> tuple[list[StoredPhysicalRecord], int, str | None]:
        records: list[StoredPhysicalRecord] = []
        state = _PageReadState(next_sequence, previous_hash, 0, 0, limit)
        for receipt in segments:
            if receipt.last_sequence < state.next_sequence:
                continue
            added, state, stopped = self._read_segment(
                receipt,
                ref,
                state,
            )
            records.extend(added)
            if stopped:
                return records, state.next_sequence, state.previous_hash
        return records, state.next_sequence, state.previous_hash

    def _read_segment(
        self,
        receipt: EvidenceSegmentReceipt,
        ref: BlackoutRef,
        state: _PageReadState,
    ) -> tuple[list[StoredPhysicalRecord], _PageReadState, bool]:
        entries = self._offsets.snapshot_and_page(
            receipt,
            first_sequence=max(state.next_sequence, receipt.first_sequence),
            limit=state.record_limit - state.records_used,
        )
        if len(entries) > state.record_limit - state.records_used:
            raise V3CorruptionError("offset page exceeds requested record limit")
        records: list[StoredPhysicalRecord] = []
        for entry in entries:
            if entry.sequence < state.next_sequence:
                continue
            _validate_entry_bounds(entry, receipt, state.next_sequence)
            line = self._reader.read_authenticated(receipt, entry)
            if len(line) > MAX_RECORD_BYTES:
                raise V3CorruptionError("evidence line exceeds its physical bound")
            if state.records_used and state.bytes_used + len(line) > MAX_EVIDENCE_PAGE_BYTES:
                return records, state, True
            record = _decode_entry(line, ref, state.previous_hash, entry)
            records.append(record)
            state = _PageReadState(
                entry.sequence + 1,
                record.ref.record_sha256,
                state.bytes_used + len(line),
                state.records_used + 1,
                state.record_limit,
            )
            if state.records_used >= state.record_limit:
                return records, state, True
        return records, state, False


def _hash(value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise V3ValidationError("evidence hash is invalid")


def _verify_active_registry_snapshot(current: RegistrySnapshot, expected: RegistrySnapshot) -> None:
    """Reject a registry authority change observed during snapshot assembly."""
    if current.state != expected.state:
        raise V3CorruptionError("active registry changed during evidence snapshot")
    if (current.byte_length, current.canonical_sha256) != (
        expected.byte_length,
        expected.canonical_sha256,
    ):
        raise V3CorruptionError("active registry changed during evidence snapshot")


def _page_input(ref: BlackoutRef, cursor: BlackoutCaptureCursor | None, limit: int) -> None:
    if not isinstance(ref, BlackoutRef) or not _valid_limit(limit):
        raise V3ValidationError("evidence page input is invalid")
    if cursor is not None and not _valid_cursor(ref, cursor):
        raise V3ValidationError("evidence cursor is invalid")


def _valid_limit(limit: object) -> bool:
    return type(limit) is int and 1 <= limit <= MAX_EVIDENCE_PAGE_RECORDS


def _valid_cursor(ref: BlackoutRef, cursor: object) -> bool:
    if not isinstance(cursor, BlackoutCaptureCursor):
        return False
    return (
        cursor.chain is BlackoutChainKind.PHYSICAL
        and cursor.blackout_id == ref.blackout_id
        and cursor.segment_id == ref.segment_id
        and (
            (cursor.next_sequence == 0 and cursor.last_record_sha256 is None)
            or (
                cursor.next_sequence is not None
                and cursor.next_sequence > 0
                and cursor.last_record_sha256 is not None
            )
        )
    )


def _cursor_state(ref: BlackoutRef, cursor: BlackoutCaptureCursor | None) -> tuple[int, str | None]:
    if cursor is None:
        return 0, None
    return cursor.next_sequence or 0, cursor.last_record_sha256


def _validate_entry_bounds(
    entry: SegmentIndexEntry, receipt: EvidenceSegmentReceipt, next_sequence: int
) -> None:
    if entry.sequence > receipt.last_sequence:
        raise V3CorruptionError("offset entry exceeds segment receipt")
    if entry.file_offset + entry.line_length > receipt.trusted_byte_length:
        raise V3CorruptionError("offset entry exceeds trusted bytes")
    if entry.sequence != next_sequence:
        raise V3CorruptionError("offset sequence has a gap")


def _authorize_cursor(
    segments: tuple[EvidenceSegmentReceipt, ...],
    cursor: BlackoutCaptureCursor,
    offsets: EvidenceOffsetReader,
) -> None:
    if cursor.next_sequence == 0:
        if cursor.last_record_sha256 is not None:
            raise V3CorruptionError("initial evidence cursor carries a prior hash")
        return
    if cursor.next_sequence is None:
        target = segments[-1].last_sequence
        if cursor.last_record_sha256 != segments[-1].last_record_sha256:
            raise V3CorruptionError("exhausted evidence cursor does not match final hash")
    else:
        target = cursor.next_sequence - 1
    for receipt in segments:
        if receipt.first_sequence <= target <= receipt.last_sequence:
            entries = offsets.snapshot_and_page(receipt, first_sequence=target, limit=1)
            if (
                len(entries) != 1
                or entries[0].sequence != target
                or entries[0].record_sha256 != cursor.last_record_sha256
            ):
                raise V3CorruptionError("evidence cursor is not an authorized offset position")
            return
    raise V3CorruptionError("evidence cursor is outside the physical chain")


def _topology(snapshot: EvidenceSnapshot) -> None:
    if not snapshot.segments:
        raise V3CorruptionError("evidence snapshot is empty")
    previous_sequence = -1
    previous_hash: str | None = None
    for ordinal, receipt in enumerate(snapshot.segments):
        _validate_receipt_topology(receipt, ordinal, previous_sequence, previous_hash)
        previous_sequence = receipt.last_sequence
        previous_hash = receipt.last_record_sha256
    first, last = snapshot.segments[0], snapshot.segments[-1]
    if first.first_sequence != 0:
        raise V3CorruptionError("evidence chain does not start at zero")
    if first.first_record_sha256 != snapshot.physical_chain_root_record_sha256:
        raise V3CorruptionError("evidence root hash differs")
    if last.last_record_sha256 != snapshot.physical_chain_final_record_sha256:
        raise V3CorruptionError("evidence final hash differs")


def _validate_receipt_topology(
    receipt: EvidenceSegmentReceipt,
    ordinal: int,
    previous_sequence: int,
    previous_hash: str | None,
) -> None:
    if receipt.ordinal != ordinal or receipt.first_sequence != previous_sequence + 1:
        raise V3CorruptionError("evidence segment topology is not contiguous")
    if receipt.trusted_byte_length < receipt.byte_length and not receipt.damaged:
        raise V3CorruptionError("undamaged evidence receipt has an untrusted suffix")
    if receipt.byte_length == 0 or receipt.trusted_byte_length == 0:
        raise V3CorruptionError("evidence segment is empty")


def _decode_entry(
    line: bytes, ref: BlackoutRef, previous_hash: str | None, entry: SegmentIndexEntry
) -> StoredPhysicalRecord:
    if len(line) != entry.line_length:
        raise V3CorruptionError("evidence line hash or length differs")
    try:
        envelope = decode_v3_record(line).envelope
        if (
            envelope.blackout_id != ref.blackout_id
            or envelope.segment_id != ref.segment_id
            or envelope.seq != entry.sequence
            or envelope.prev_record_sha256 != previous_hash
            or envelope.record_sha256 != entry.record_sha256
        ):
            raise V3CorruptionError("evidence chain scope or previous hash differs")
        value, record_type = _decode_value(envelope.record_type, line)
        expected_kind = {
            BlackoutRecordType.START: OffsetRecordKind.START,
            BlackoutRecordType.SAMPLE: OffsetRecordKind.SAMPLE,
            BlackoutRecordType.GAP: OffsetRecordKind.GAP,
            BlackoutRecordType.ANCHOR: OffsetRecordKind.ANCHOR,
        }[record_type]
        if entry.record_kind is not expected_kind:
            raise V3CorruptionError("offset record kind differs")
    except (V3CodecError, KeyError, TypeError, ValueError) as exc:
        raise V3CorruptionError("evidence record is invalid") from exc
    return StoredPhysicalRecord(
        StoredRecordRef(
            ref,
            BlackoutChainKind.PHYSICAL,
            record_type,
            entry.sequence,
            entry.record_sha256,
            len(line),
        ),
        value,
    )


def _decode_value(
    line_type: str, line: bytes
) -> tuple[BlackoutStart | DischargeSample | DischargeGap | EndpointAnchor, BlackoutRecordType]:
    if line_type == BlackoutRecordType.START:
        return decode_blackout_start(line), BlackoutRecordType.START
    if line_type == BlackoutRecordType.SAMPLE:
        return decode_discharge_sample(line), BlackoutRecordType.SAMPLE
    if line_type == BlackoutRecordType.GAP:
        return decode_discharge_gap(line), BlackoutRecordType.GAP
    if line_type == BlackoutRecordType.ANCHOR:
        anchor = decode_endpoint_anchor(line)
        if anchor.kind not in {
            AnchorKind.TRANSFER_TO_BATTERY,
            AnchorKind.RAW_FIRMWARE_LB,
        }:
            raise V3CorruptionError("terminal anchor is not physical evidence")
        return anchor, BlackoutRecordType.ANCHOR
    raise V3CorruptionError("non-physical record in evidence chain")


__all__ = [
    "ActiveRegistryEvidenceSnapshotProvider",
    "EvidenceAuthority",
    "EvidenceOffsetReader",
    "EvidenceReader",
    "EvidenceSegmentReceipt",
    "EvidenceSnapshot",
    "EvidenceSnapshotProvider",
    "JsonlV3FilesystemEvidenceOffsetReader",
    "JsonlV3FilesystemEvidenceReader",
    "JsonlV3EvidenceStore",
    "SealedLocator",
    "SealedLocatorReader",
    "SealedLocatorEvidenceSnapshotProvider",
]
