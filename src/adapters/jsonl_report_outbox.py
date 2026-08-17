"""Durable at-least-once report notices for sealed blackout outcomes."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from src.adapters.jsonl_errors import (
    EventConflictError,
    EventCorruptionError,
    EventPathError,
    EventPersistenceError,
    EventStoreError,
    EventValidationError,
)
from src.adapters.jsonl_filesystem import JsonlFilesystem
from src.adapters.jsonl_record_codec import (
    EMPTY_SHA256,
    MAX_INDEX_LINE_BYTES,
    MAX_REGISTRY_BYTES,
    _is_sha256,
    _strict_json_loads,
    _validate_path_token,
    _validate_uuid4_hex,
    canonical_json_bytes,
)

OUTBOX_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ReportNotice:
    """Identity of one report whose source is already durably sealed."""

    sequence: int
    blackout_id: str
    segment_filename: str
    locator_sha256: str
    index_head_sha256: str
    previous_notice_sha256: str
    notice_sha256: str

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.blackout_id, self.segment_filename, self.locator_sha256


@dataclass(frozen=True, slots=True)
class OutboxCursor:
    offset: int
    next_sequence: int
    previous_notice_sha256: str


@dataclass(frozen=True, slots=True)
class _NoticeInput:
    sequence: int
    blackout_id: str
    segment_filename: str
    locator_sha256: str
    index_head_sha256: str
    previous_notice_sha256: str


def _notice_without_hash(value: _NoticeInput) -> dict[str, Any]:
    return {
        "schema_version": OUTBOX_SCHEMA_VERSION,
        "sequence": value.sequence,
        "blackout_id": value.blackout_id,
        "segment_filename": value.segment_filename,
        "locator_sha256": value.locator_sha256,
        "index_head_sha256": value.index_head_sha256,
        "previous_notice_sha256": value.previous_notice_sha256,
    }


def _validated_hash(value: Any) -> str:
    if not isinstance(value, str) or not _is_sha256(value):
        raise EventValidationError("report outbox hash is invalid")
    return value


def _encode_notice(notice: ReportNotice) -> bytes:
    body = _notice_without_hash(
        _NoticeInput(
            notice.sequence,
            notice.blackout_id,
            notice.segment_filename,
            notice.locator_sha256,
            notice.index_head_sha256,
            notice.previous_notice_sha256,
        )
    )
    value = {**body, "notice_sha256": notice.notice_sha256}
    raw = canonical_json_bytes(value) + b"\n"
    if len(raw) > MAX_INDEX_LINE_BYTES:
        raise EventValidationError("report outbox notice exceeds its bound")
    return raw


def make_notice(**values: Any) -> ReportNotice:
    """Create a notice from keyword fields, keeping the public seam small."""
    sequence = values.get("sequence")
    blackout_id = values.get("blackout_id")
    segment_filename = values.get("segment_filename")
    locator_sha256 = values.get("locator_sha256")
    index_head_sha256 = values.get("index_head_sha256")
    previous_notice_sha256 = values.get("previous_notice_sha256")
    if not isinstance(blackout_id, str) or not isinstance(segment_filename, str):
        raise EventValidationError("report outbox identity is invalid")
    _validate_uuid4_hex(blackout_id, "blackout_id")
    _validate_path_token(segment_filename)
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise EventValidationError("report outbox sequence is invalid")
    locator_sha256 = _validated_hash(locator_sha256)
    index_head_sha256 = _validated_hash(index_head_sha256)
    previous_notice_sha256 = _validated_hash(previous_notice_sha256)
    value = _NoticeInput(
        sequence,
        blackout_id,
        segment_filename,
        locator_sha256,
        index_head_sha256,
        previous_notice_sha256,
    )
    body = _notice_without_hash(value)
    digest = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    return ReportNotice(
        value.sequence,
        value.blackout_id,
        value.segment_filename,
        value.locator_sha256,
        value.index_head_sha256,
        value.previous_notice_sha256,
        digest,
    )


def _decode_notice(raw: bytes) -> ReportNotice:
    if not raw.endswith(b"\n") or len(raw) > MAX_INDEX_LINE_BYTES:
        raise EventCorruptionError("report outbox line is torn or oversized")
    try:
        value = _strict_json_loads(raw[:-1])
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EventCorruptionError("report outbox line is invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "sequence",
        "blackout_id",
        "segment_filename",
        "locator_sha256",
        "index_head_sha256",
        "previous_notice_sha256",
        "notice_sha256",
    }:
        raise EventCorruptionError("report outbox fields do not match schema")
    if canonical_json_bytes(value) + b"\n" != raw:
        raise EventCorruptionError("report outbox line is not canonical")
    try:
        result = make_notice(
            sequence=value["sequence"],
            blackout_id=value["blackout_id"],
            segment_filename=value["segment_filename"],
            locator_sha256=value["locator_sha256"],
            index_head_sha256=value["index_head_sha256"],
            previous_notice_sha256=value["previous_notice_sha256"],
        )
    except EventValidationError as exc:
        raise EventCorruptionError("report outbox values are invalid") from exc
    if result.notice_sha256 != value["notice_sha256"]:
        raise EventCorruptionError("report outbox hash does not match bytes")
    return result


class JsonlReportOutbox:
    """Append notices and acknowledge them through a durable byte cursor."""

    def __init__(
        self,
        events_path: Path,
        filesystem: JsonlFilesystem | None = None,
        *,
        outbox_path: Path | None = None,
        cursor_path: Path | None = None,
    ) -> None:
        self._events_path = events_path
        self._filesystem = filesystem
        self.path = outbox_path or events_path / "report-outbox.jsonl"
        self.cursor_path = cursor_path or events_path / "report-outbox.cursor.json"
        self._lock = RLock()

    def append(
        self,
        *,
        blackout_id: str,
        segment_filename: str,
        locator_sha256: str,
        index_head_sha256: str,
    ) -> ReportNotice:
        with self._lock:
            cursor, tail_notice = self._tail_state()
            candidate = make_notice(
                sequence=cursor.next_sequence,
                blackout_id=blackout_id,
                segment_filename=segment_filename,
                locator_sha256=locator_sha256,
                index_head_sha256=index_head_sha256,
                previous_notice_sha256=cursor.previous_notice_sha256,
            )
            # The index append-intent is held until this notice is durable. A
            # retry therefore can only duplicate the current tail, not an older
            # notice. Keeping this check tail-local avoids an O(history) scan on
            # every sealed event while preserving retry idempotency.
            if tail_notice is not None and tail_notice.identity == candidate.identity:
                self._sync_parent_directory()
                return tail_notice
            fd = self._open_append()
            try:
                self._append_sync(fd, _encode_notice(candidate))
            finally:
                os.close(fd)
            self._sync_parent_directory()
            return candidate

    def pending(self, *, limit: int = 32) -> tuple[ReportNotice, ...]:
        with self._lock:
            if limit < 0 or limit > 256:
                raise ValueError("outbox pending limit is outside its bound")
            if limit == 0:
                return ()
            # A process can die after a short append but before the index intent
            # is replayed. Repair only an invalid final suffix; valid history is
            # never rewritten and the next recovery append can converge.
            self._tail_cursor()
            cursor = self._read_cursor()
            result: list[ReportNotice] = []
            offset = cursor.offset
            previous = cursor.previous_notice_sha256
            expected = cursor.next_sequence
            records = self._iter_from(offset)
            try:
                for notice, next_offset in records:
                    if notice.sequence != expected or notice.previous_notice_sha256 != previous:
                        raise EventCorruptionError("report outbox hash chain is broken")
                    result.append(notice)
                    offset, previous, expected = (
                        next_offset,
                        notice.notice_sha256,
                        notice.sequence + 1,
                    )
                    if len(result) == limit:
                        break
            finally:
                records.close()
            return tuple(result)

    def head(self) -> ReportNotice | None:
        """Return the exact next FIFO notice without scanning later entries."""
        with self._lock:
            self._tail_cursor()
            cursor = self._read_cursor()
            found = self._head_record(cursor.offset)
            if found is None:
                return None
            notice, _ = found
            if (
                notice.sequence != cursor.next_sequence
                or notice.previous_notice_sha256 != cursor.previous_notice_sha256
            ):
                raise EventCorruptionError("report outbox hash chain is broken")
            return notice

    def acknowledge(self, notice: ReportNotice) -> None:
        with self._lock:
            self._tail_cursor()
            cursor = self._read_cursor()
            if (
                notice.sequence != cursor.next_sequence
                or notice.previous_notice_sha256 != cursor.previous_notice_sha256
            ):
                raise EventConflictError("report outbox acknowledgement is not FIFO")
            found = self._head_record(cursor.offset)
            if found is None or found[0] != notice:
                raise EventConflictError(
                    "report outbox acknowledgement does not match durable notice"
                )
            self._write_cursor(OutboxCursor(found[1], notice.sequence + 1, notice.notice_sha256))

    def _head_record(self, offset: int) -> tuple[ReportNotice, int] | None:
        try:
            fd = self._open_read()
        except FileNotFoundError:
            return None
        try:
            with os.fdopen(fd, "rb") as stream:
                stream.seek(offset)
                line = stream.readline(MAX_INDEX_LINE_BYTES + 1)
                if not line:
                    return None
                return _decode_notice(line), stream.tell()
        except EventStoreError:
            raise
        except OSError as exc:
            raise EventPersistenceError("cannot read report outbox head") from exc

    def _iter_from(self, offset: int) -> Generator[tuple[ReportNotice, int], None, None]:
        try:
            fd = self._open_read()
        except FileNotFoundError:
            return
        try:
            with os.fdopen(fd, "rb") as stream:
                stream.seek(offset)
                while True:
                    start = stream.tell()
                    line = stream.readline(MAX_INDEX_LINE_BYTES + 1)
                    if not line:
                        return
                    yield _decode_notice(line), start + len(line)
        except EventStoreError:
            raise
        except OSError as exc:
            raise EventPersistenceError("cannot read report outbox") from exc

    def _tail_cursor(self) -> OutboxCursor:
        return self._tail_state()[0]

    def _tail_state(self) -> tuple[OutboxCursor, ReportNotice | None]:
        try:
            fd = self._open_read(writable=True)
        except FileNotFoundError:
            return OutboxCursor(0, 0, EMPTY_SHA256), None
        try:
            size = os.fstat(fd).st_size
            if size == 0:
                return OutboxCursor(0, 0, EMPTY_SHA256), None
            suffix_offset, suffix = self._read_tail_suffix(fd, size)
            notice, end = self._decode_tail_suffix(suffix_offset, suffix)
            if end < size:
                self._truncate_tail(fd, end)
            if notice is None:
                return OutboxCursor(end, 0, EMPTY_SHA256), None
            return OutboxCursor(end, notice.sequence + 1, notice.notice_sha256), notice
        finally:
            os.close(fd)

    @staticmethod
    def _read_tail_suffix(fd: int, size: int) -> tuple[int, bytes]:
        window = 2 * (MAX_INDEX_LINE_BYTES + 1)
        offset = max(0, size - window)
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            chunks: list[bytes] = []
            remaining = size - offset
            while remaining:
                try:
                    chunk = os.read(fd, remaining)
                except OSError as exc:
                    if exc.errno == errno.EINTR:
                        continue
                    raise
                if not chunk:
                    raise OSError("unexpected EOF while reading report outbox tail")
                chunks.append(chunk)
                remaining -= len(chunk)
        except OSError as exc:
            raise EventPersistenceError("cannot read report outbox tail") from exc
        return offset, b"".join(chunks)

    def _decode_tail_suffix(self, offset: int, suffix: bytes) -> tuple[ReportNotice | None, int]:
        last_newline = suffix.rfind(b"\n")
        if last_newline < 0:
            if offset:
                raise EventCorruptionError("report outbox tail exceeds its bound")
            return None, 0
        line_start = suffix.rfind(b"\n", 0, last_newline) + 1
        line = suffix[line_start : last_newline + 1]
        return _decode_notice(line), offset + last_newline + 1

    @staticmethod
    def _truncate_tail(fd: int, offset: int) -> None:
        try:
            os.ftruncate(fd, offset)
            JsonlReportOutbox._fdatasync(fd)
        except OSError as exc:
            raise EventPersistenceError("cannot repair report outbox tail") from exc

    def _read_cursor(self) -> OutboxCursor:
        if not self.cursor_path.exists():
            return OutboxCursor(0, 0, EMPTY_SHA256)
        try:
            info = self.cursor_path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise EventPathError("report outbox cursor is not regular")
            if info.st_size > MAX_REGISTRY_BYTES:
                raise EventCorruptionError("report outbox cursor is too large")
            value = _strict_json_loads(self.cursor_path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise EventCorruptionError("report outbox cursor is invalid") from exc
        if not isinstance(value, dict) or set(value) != {
            "offset",
            "next_sequence",
            "previous_notice_sha256",
        }:
            raise EventCorruptionError("report outbox cursor fields are invalid")
        offset = value["offset"]
        sequence = value["next_sequence"]
        previous = value["previous_notice_sha256"]
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in (offset, sequence)
        ) or not _is_sha256(previous):
            raise EventCorruptionError("report outbox cursor values are invalid")
        return OutboxCursor(offset, sequence, previous)

    def _write_cursor(self, cursor: OutboxCursor) -> None:
        value = {
            "offset": cursor.offset,
            "next_sequence": cursor.next_sequence,
            "previous_notice_sha256": cursor.previous_notice_sha256,
        }
        raw = canonical_json_bytes(value) + b"\n"
        if self._filesystem is not None:
            self._filesystem.atomic_replace(self.cursor_path, raw, mode=0o600)
            return
        self.cursor_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.cursor_path.with_name(f".{self.cursor_path.name}.tmp-{uuid.uuid4().hex}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temporary, flags, 0o600)
        try:
            self._write_all(fd, raw)
            self._fdatasync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, self.cursor_path)
        self._sync_parent_directory()

    def _open_append(self) -> int:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise EventPathError("report outbox is not a regular file")
            os.fchmod(fd, 0o600)
            return fd
        except EventStoreError:
            if "fd" in locals():
                os.close(fd)
            raise
        except OSError as exc:
            if "fd" in locals():
                os.close(fd)
            raise EventPersistenceError("cannot open report outbox") from exc

    def _open_read(self, *, writable: bool = False) -> int:
        flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise EventPathError("report outbox is not a regular file")
            return fd
        except EventStoreError:
            if "fd" in locals():
                os.close(fd)
            raise
        except FileNotFoundError:
            raise
        except OSError as exc:
            if "fd" in locals():
                os.close(fd)
            raise EventPersistenceError("cannot open report outbox") from exc

    def _append_sync(self, fd: int, raw: bytes) -> None:
        try:
            self._write_all(fd, raw)
            self._fdatasync(fd)
        except OSError as exc:
            raise EventPersistenceError("cannot append report outbox") from exc

    @staticmethod
    def _write_all(fd: int, raw: bytes) -> None:
        view = memoryview(raw)
        while view:
            try:
                written = os.write(fd, view)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise
            if written <= 0:
                raise OSError(errno.EIO, "report outbox write made no progress")
            view = view[written:]

    @staticmethod
    def _fdatasync(fd: int) -> None:
        while True:
            try:
                os.fdatasync(fd)
                return
            except OSError as exc:
                if exc.errno != errno.EINTR:
                    raise

    def _sync_parent_directory(self) -> None:
        if self._filesystem is not None:
            self._filesystem.sync_storage_directory(self.path.parent)
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path.parent, flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise EventPersistenceError("cannot sync report outbox directory") from exc


__all__ = ["JsonlReportOutbox", "OutboxCursor", "ReportNotice", "make_notice"]
