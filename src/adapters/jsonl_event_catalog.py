"""Durable, append-only reservations for event-file paths.

The catalog is the discovery index.  Readers consume it by byte offset; they
never need to enumerate the event directory.  A reservation is intentionally
durable before the corresponding event file is created, so a crash can only
leave a harmless missing reservation.
"""

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.adapters.jsonl_errors import (
    EventCorruptionError,
    EventPathError,
    EventPersistenceError,
    EventValidationError,
)
from src.adapters.jsonl_record_codec import (
    EMPTY_SHA256,
    EVENT_FILENAME_RE,
    MAX_REGISTRY_BYTES,
    _bounded_error,
    _is_sha256,
    _strict_json_loads,
    _validate_path_token,
    canonical_json_bytes,
)

if TYPE_CHECKING:
    from src.adapters.jsonl_filesystem import JsonlFilesystem

MAX_CATALOG_LINE_BYTES = 1024
CATALOG_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CatalogEntry:
    """One validated catalog reservation."""

    catalog_seq: int
    path_token: str
    previous_entry_sha256: str
    entry_sha256: str


@dataclass(frozen=True)
class CatalogSnapshot:
    """Stable end point used by one rebuild or health-inventory generation."""

    byte_offset: int
    entry_count: int


@dataclass(frozen=True)
class CatalogBatch:
    """A bounded catalog read and its resumable next position."""

    entries: tuple[CatalogEntry, ...]
    entry_offsets: tuple[int, ...]
    byte_offset: int
    next_seq: int
    previous_entry_sha256: str
    complete: bool


@dataclass(frozen=True)
class _BatchCursor:
    byte_offset: int
    target_offset: int
    expected_seq: int
    previous_hash: str


def _entry_without_hash(seq: int, path_token: str, previous: str) -> dict[str, Any]:
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "catalog_seq": seq,
        "path_token": path_token,
        "previous_entry_sha256": previous,
    }


def _encode_entry(seq: int, path_token: str, previous: str) -> bytes:
    body = _entry_without_hash(seq, path_token, previous)
    digest = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    value = {**body, "entry_sha256": digest}
    line = canonical_json_bytes(value) + b"\n"
    if len(line) > MAX_CATALOG_LINE_BYTES:
        raise EventValidationError("event catalog line exceeds its bound")
    return line


def _decode_entry(line: bytes) -> CatalogEntry:
    if len(line) > MAX_CATALOG_LINE_BYTES or not line.endswith(b"\n"):
        raise EventCorruptionError("event catalog line is torn or exceeds its bound")
    try:
        value = _strict_json_loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EventCorruptionError("event catalog line is not strict JSON") from exc
    value = _validate_entry_value(value, line)
    seq = value["catalog_seq"]
    token = value["path_token"]
    previous = value["previous_entry_sha256"]
    digest = value["entry_sha256"]
    expected = hashlib.sha256(
        canonical_json_bytes(_entry_without_hash(seq, token, previous))
    ).hexdigest()
    if expected != digest:
        raise EventCorruptionError("event catalog entry hash does not match bytes")
    return CatalogEntry(seq, token, previous, digest)


def _validate_entry_value(value: Any, line: bytes) -> dict[str, Any]:
    _validate_entry_shape(value, line)
    _validate_entry_identity(value)
    _validate_entry_hashes(value)
    return value


def _validate_entry_shape(value: Any, line: bytes) -> None:
    fields = {
        "schema_version",
        "catalog_seq",
        "path_token",
        "previous_entry_sha256",
        "entry_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise EventCorruptionError("event catalog fields do not match schema")
    if canonical_json_bytes(value) + b"\n" != line:
        raise EventCorruptionError("event catalog line is not canonical JSON")
    if value["schema_version"] != CATALOG_SCHEMA_VERSION:
        raise EventCorruptionError("event catalog schema version is unsupported")


def _validate_entry_identity(value: dict[str, Any]) -> None:
    seq = value["catalog_seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise EventCorruptionError("event catalog sequence is invalid")
    token = value["path_token"]
    if not isinstance(token, str):
        raise EventCorruptionError("event catalog path token is invalid")
    _validate_path_token(token)
    if EVENT_FILENAME_RE.fullmatch(token) is None:
        raise EventPathError("event catalog path token is not an event filename")


def _validate_entry_hashes(value: dict[str, Any]) -> None:
    previous = value["previous_entry_sha256"]
    digest = value["entry_sha256"]
    if not isinstance(previous, str) or not _is_sha256(previous):
        raise EventCorruptionError("event catalog previous hash is invalid")
    if not isinstance(digest, str) or not _is_sha256(digest):
        raise EventCorruptionError("event catalog entry hash is invalid")


def _safe_catalog_info(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return os.stat_result((0,) * 10)
    except OSError as exc:
        raise EventPersistenceError(f"cannot inspect event catalog: {_bounded_error(exc)}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise EventPathError("event catalog is not a regular file")
    if info.st_size > MAX_REGISTRY_BYTES * 1024:
        raise EventCorruptionError("event catalog exceeds its bounded size")
    return info


def _validate_batch_cursor(cursor: _BatchCursor, max_files: int, info: os.stat_result) -> None:
    if (
        not 0 <= cursor.byte_offset <= cursor.target_offset
        or max_files < 1
        or cursor.expected_seq < 0
    ):
        raise ValueError("catalog read cursor is outside its target")
    if not _is_sha256(cursor.previous_hash):
        raise EventCorruptionError("catalog cursor hash is invalid")
    if info.st_size < cursor.target_offset or cursor.target_offset > info.st_size:
        raise EventCorruptionError("event catalog target is beyond its durable end")


def _read_batch_lines(
    stream: Any, cursor: _BatchCursor, max_files: int
) -> tuple[list[CatalogEntry], list[int], int]:
    entries: list[CatalogEntry] = []
    offsets: list[int] = []
    next_offset = cursor.byte_offset
    while len(entries) < max_files and next_offset < cursor.target_offset:
        line = stream.readline(MAX_CATALOG_LINE_BYTES + 1)
        if not line or not line.endswith(b"\n"):
            raise EventCorruptionError("event catalog has a torn line")
        next_offset += len(line)
        if next_offset > cursor.target_offset:
            raise EventCorruptionError("catalog target splits a line")
        entry = _decode_entry(line)
        if entry.catalog_seq != cursor.expected_seq + len(entries):
            raise EventCorruptionError("event catalog sequence is not contiguous")
        prior = cursor.previous_hash if not entries else entries[-1].entry_sha256
        if entry.previous_entry_sha256 != prior:
            raise EventCorruptionError("event catalog hash chain is broken")
        entries.append(entry)
        offsets.append(next_offset)
    return entries, offsets, next_offset


class JsonlEventCatalog:
    """Append and bounded sequential reads for the event reservation catalog."""

    def __init__(self, catalog_path: Path, filesystem: "JsonlFilesystem") -> None:
        self._catalog_path = catalog_path
        self._filesystem = filesystem

    @property
    def path(self) -> Path:
        return self._catalog_path

    def reserve(self, path_token: str) -> None:
        """Durably append one reservation, idempotent for a repeated tail token."""
        _validate_catalog_token(path_token)
        info = _safe_catalog_info(self.path)
        tail = self._tail(info)
        if tail is not None and tail.path_token == path_token:
            return
        seq = tail.catalog_seq + 1 if tail is not None else 0
        previous = tail.entry_sha256 if tail is not None else EMPTY_SHA256
        line = _encode_entry(seq, path_token, previous)
        fd = self._filesystem._open_append_or_create(self.path, mode=0o600)
        try:
            self._filesystem._append_and_sync_fd(fd, line)
        finally:
            os.close(fd)

    def snapshot(self) -> CatalogSnapshot:
        """Return the current byte end without scanning catalog contents."""
        info = _safe_catalog_info(self.path)
        if info.st_size == 0:
            return CatalogSnapshot(0, 0)
        tail = self._tail(info)
        if tail is None:
            raise EventCorruptionError("non-empty event catalog has no complete tail")
        return CatalogSnapshot(info.st_size, tail.catalog_seq + 1)

    def read_batch(
        self,
        *,
        byte_offset: int,
        target_offset: int,
        expected_seq: int,
        previous_entry_sha256: str,
        max_files: int,
    ) -> CatalogBatch:
        """Read no more than ``max_files`` complete lines through target_offset."""
        info = _safe_catalog_info(self.path)
        cursor = _BatchCursor(byte_offset, target_offset, expected_seq, previous_entry_sha256)
        _validate_batch_cursor(cursor, max_files, info)
        if byte_offset == target_offset:
            return CatalogBatch((), (), byte_offset, expected_seq, previous_entry_sha256, True)
        if not self.path.exists():
            raise EventCorruptionError("catalog cursor points into a missing catalog")
        with self.path.open("rb") as stream:
            stream.seek(byte_offset)
            entries, entry_offsets, next_offset = _read_batch_lines(stream, cursor, max_files)
        final_hash = entries[-1].entry_sha256 if entries else previous_entry_sha256
        return CatalogBatch(
            tuple(entries),
            tuple(entry_offsets),
            next_offset,
            expected_seq + len(entries),
            final_hash,
            next_offset == target_offset,
        )

    def _tail(self, info: os.stat_result) -> CatalogEntry | None:
        if info.st_size == 0:
            return None
        with self.path.open("rb") as stream:
            start = max(0, info.st_size - MAX_CATALOG_LINE_BYTES)
            stream.seek(start)
            raw = stream.read()
        lines = raw.splitlines(keepends=True)
        if not lines or not lines[-1].endswith(b"\n"):
            raise EventCorruptionError("event catalog tail is torn")
        return _decode_entry(lines[-1])


def _validate_catalog_token(path_token: str) -> None:
    _validate_path_token(path_token)
    if EVENT_FILENAME_RE.fullmatch(path_token) is None:
        raise EventPathError("catalog reservation is not an event filename")
