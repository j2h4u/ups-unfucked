"""Pure codecs and facade for the typed v3 offset index."""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol

from src.adapters.jsonl_v3_errors import V3CorruptionError, V3ValidationError
from src.adapters.jsonl_v3_storage_paths import V3OffsetPathToken

OFFSET_HEADER = b"UBMV3OF\x01"
OFFSET_HEADER_SIZE = 8
OFFSET_ENTRY_SIZE = 56
OFFSET_ENTRY_STRUCT = struct.Struct(">IQI32sB7x")
MAX_LINE_BYTES = 20 * 1024
MAX_CHAIN_SEQUENCE = 3_197
HASH_RE = re.compile(r"\A[0-9a-f]{64}\Z")


class OffsetRecordKind(IntEnum):
    START = 1
    SAMPLE = 2
    GAP = 3
    ANCHOR = 4
    END = 5


@dataclass(frozen=True, slots=True)
class OffsetEntry:
    seq: int
    file_offset: int
    line_length: int
    record_sha256: str
    record_kind: int

    def __post_init__(self) -> None:
        _uint(self.seq, MAX_CHAIN_SEQUENCE, "seq")
        _uint(self.file_offset, (1 << 64) - 1, "file_offset")
        _uint(self.line_length, (1 << 32) - 1, "line_length")
        if (
            not 1 <= self.line_length <= MAX_LINE_BYTES
            or HASH_RE.fullmatch(self.record_sha256) is None
        ):
            raise V3ValidationError("offset entry is outside bounds")
        _uint(self.record_kind, 255, "record_kind")
        if self.record_kind not in {int(kind) for kind in OffsetRecordKind}:
            raise V3ValidationError("offset record kind is invalid")


@dataclass(frozen=True, slots=True)
class SegmentIndexEntry:
    sequence: int
    file_offset: int
    line_length: int
    record_sha256: str
    record_kind: OffsetRecordKind

    def __post_init__(self) -> None:
        if type(self.record_kind) is not OffsetRecordKind:
            raise V3ValidationError("segment index record kind is invalid")
        OffsetEntry(
            self.sequence,
            self.file_offset,
            self.line_length,
            self.record_sha256,
            int(self.record_kind),
        )


@dataclass(frozen=True, slots=True)
class SegmentIndexSnapshot:
    entry_count: int
    first_sequence: int | None
    last_sequence: int | None
    byte_length: int
    append_state_sha256: str


@dataclass(frozen=True, slots=True)
class SegmentIndexPage:
    entries: tuple[SegmentIndexEntry, ...]
    next_entry_ordinal: int | None
    complete: bool


class OffsetTransaction(Protocol):
    def assert_owner(self, filesystem: object) -> None: ...
    def create_offset_index(self, token: V3OffsetPathToken) -> SegmentIndexSnapshot: ...
    def snapshot_offset_index(self, token: V3OffsetPathToken) -> SegmentIndexSnapshot: ...
    def append_offset_index(
        self, token: V3OffsetPathToken, *, expected: SegmentIndexSnapshot, entry: SegmentIndexEntry
    ) -> SegmentIndexSnapshot: ...
    def get_offset_index(
        self, token: V3OffsetPathToken, *, sequence: int
    ) -> SegmentIndexEntry | None: ...
    def page_offset_index(
        self, token: V3OffsetPathToken, *, entry_ordinal: int, limit: int
    ) -> SegmentIndexPage: ...


def encode_offset_header() -> bytes:
    return OFFSET_HEADER


def encode_offset_entry(entry: OffsetEntry) -> bytes:
    return OFFSET_ENTRY_STRUCT.pack(
        entry.seq,
        entry.file_offset,
        entry.line_length,
        bytes.fromhex(entry.record_sha256),
        entry.record_kind,
    )


def decode_offset_entry(raw: bytes) -> OffsetEntry:
    if len(raw) != OFFSET_ENTRY_SIZE or raw[-7:] != b"\0" * 7:
        raise V3CorruptionError("offset entry is invalid")
    try:
        return OffsetEntry(*_unpack(raw))
    except (struct.error, V3ValidationError) as exc:
        raise V3CorruptionError("offset entry is invalid") from exc


def _unpack(raw: bytes) -> tuple[int, int, int, str, int]:
    seq, offset, length, digest, kind = OFFSET_ENTRY_STRUCT.unpack(raw)
    return seq, offset, length, digest.hex(), kind


def append_state_hash(byte_length: int, first: bytes | None, last: bytes | None) -> str:
    digest = hashlib.sha256(b"UBMV3-IDX-CAS-v1\0" + OFFSET_HEADER + byte_length.to_bytes(8, "big"))
    if first is not None and last is not None:
        digest.update(first + last)
    return digest.hexdigest()


def _uint(value: object, maximum: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise V3ValidationError(f"{field} is outside bounds")


class JsonlV3SegmentIndex:
    def __init__(self, filesystem: object, token: V3OffsetPathToken) -> None:
        if not isinstance(token, V3OffsetPathToken):
            raise V3ValidationError("segment index requires an offset token")
        self.filesystem = filesystem
        self.token = token

    def create(self, transaction: OffsetTransaction) -> SegmentIndexSnapshot:
        transaction.assert_owner(self.filesystem)
        return transaction.create_offset_index(self.token)

    def snapshot(self, transaction: OffsetTransaction) -> SegmentIndexSnapshot:
        transaction.assert_owner(self.filesystem)
        return transaction.snapshot_offset_index(self.token)

    def append(
        self,
        transaction: OffsetTransaction,
        *,
        expected: SegmentIndexSnapshot,
        entry: SegmentIndexEntry,
    ) -> SegmentIndexSnapshot:
        transaction.assert_owner(self.filesystem)
        return transaction.append_offset_index(self.token, expected=expected, entry=entry)

    def get(self, transaction: OffsetTransaction, sequence: int) -> SegmentIndexEntry | None:
        transaction.assert_owner(self.filesystem)
        return transaction.get_offset_index(self.token, sequence=sequence)

    def page(
        self, transaction: OffsetTransaction, *, entry_ordinal: int = 0, limit: int = 1024
    ) -> SegmentIndexPage:
        transaction.assert_owner(self.filesystem)
        return transaction.page_offset_index(self.token, entry_ordinal=entry_ordinal, limit=limit)
