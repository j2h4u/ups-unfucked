"""Durable manifest and aggregate-size policy for JSONL event streams."""

import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from src.adapters.jsonl_errors import (
    EventConflictError,
    EventCorruptionError,
    EventPathError,
    EventPersistenceError,
    EventValidationError,
)
from src.adapters.jsonl_filesystem import JsonlFilesystem, _file_sha256
from src.adapters.jsonl_record_codec import (
    CAPTURE_APPEND_LIMIT,
    EVENT_FILENAME_RE,
    MAX_EVENT_BYTES,
    MAX_LINE_BYTES,
    MAX_REGISTRY_BYTES,
    MAX_SEGMENT_REFS,
    _bounded_error,
    _decode_record_line,
    _is_sha256,
    _StoredRecord,
    _validate_path_token,
    _validate_uuid4_hex,
)
from src.adapters.jsonl_summary_codec import _bounded_tail_lines, _iter_complete_lines
from src.application.storage_values import (
    CapturingEventRef,
    EventHandle,
    PreparingCaptureRef,
    WorkRegistry,
)

if TYPE_CHECKING:
    from src.adapters.jsonl_work_registry import JsonlWorkRegistry


class _EventCapacityExceeded(EventCorruptionError):
    """A logical event exceeded its durable projection budget."""


@dataclass(frozen=True, slots=True)
class PreparedCapacityContinuation:
    """Durable evidence needed by the stream to create a continuation segment."""

    damaged_sha256: str


def _event_filename_belongs_to(filename: str, blackout_id: str) -> bool:
    match = EVENT_FILENAME_RE.fullmatch(filename)
    return match is not None and match.group("blackout") == blackout_id


def _segment_order_key(filename: str) -> tuple[int, str, str]:
    match = EVENT_FILENAME_RE.fullmatch(filename)
    if match is None:
        return MAX_SEGMENT_REFS + 1, filename, ""
    segment = match.group("segment")
    timestamp = filename[4:24]
    if segment is None:
        return 0, timestamp, ""
    ordinal = match.group("ordinal")
    return int(ordinal) if ordinal is not None else 1, timestamp, segment


def _decode_manifest_line(line: bytes, blackout_id: str) -> tuple[str, str | None]:
    try:
        value = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EventCorruptionError("segment manifest is invalid") from exc
    if not isinstance(value, dict) or set(value) != {"path_token", "damaged_sha256"}:
        raise EventCorruptionError("segment manifest fields are invalid")
    token = value["path_token"]
    damaged = value["damaged_sha256"]
    if not isinstance(token, str) or not _event_filename_belongs_to(token, blackout_id):
        raise EventCorruptionError("segment manifest path is invalid")
    if damaged is not None and (not isinstance(damaged, str) or not _is_sha256(damaged)):
        raise EventCorruptionError("segment manifest damaged hash is invalid")
    return token, damaged


class JsonlEventCapacity:
    """Own manifest discovery, aggregate byte accounting, and size continuation state."""

    def __init__(
        self,
        events_path: Path,
        filesystem: JsonlFilesystem,
        registry: Callable[[], "JsonlWorkRegistry"],
    ) -> None:
        self._events_path = events_path
        self._filesystem = filesystem
        self._registry = registry

    def reserve_segment_manifest(self, path_token: str, damaged_sha256: str | None = None) -> None:
        """Record an exact segment path before its event file is created."""
        match = EVENT_FILENAME_RE.fullmatch(path_token)
        if match is None:
            raise EventPathError("manifest path token is not an event filename")
        blackout_id = match.group("blackout")
        manifest = self._events_path / f"segments-{blackout_id}.jsonl"
        value = {"path_token": path_token, "damaged_sha256": damaged_sha256}
        line = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        if len(line) > 512:
            raise EventValidationError("segment manifest line exceeds its bound")
        if manifest.exists():
            info = manifest.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise EventPathError("segment manifest is not a regular file")
            if info.st_size > MAX_REGISTRY_BYTES:
                raise EventCorruptionError("segment manifest exceeds its bound")
            for existing in _iter_complete_lines(manifest, 512):
                try:
                    decoded = json.loads(existing)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise EventCorruptionError("segment manifest is invalid") from exc
                if decoded == value:
                    return
                if (
                    isinstance(decoded, dict)
                    and decoded.get("path_token") == path_token
                    and decoded.get("damaged_sha256") is not None
                    and damaged_sha256 is None
                ):
                    return
        fd = self._filesystem._open_append_or_create(manifest, mode=0o600)
        try:
            self._filesystem._append_and_sync_fd(fd, line)
        finally:
            os.close(fd)

    def manifest_entries(self, blackout_id: str) -> tuple[tuple[str, str | None], ...]:
        _validate_uuid4_hex(blackout_id, "blackout_id")
        manifest = self._events_path / f"segments-{blackout_id}.jsonl"
        if not manifest.exists():
            return ()
        info = manifest.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise EventPathError("segment manifest is not a regular file")
        if info.st_size > MAX_REGISTRY_BYTES:
            raise EventCorruptionError("segment manifest exceeds its bound")
        entries: list[tuple[str, str | None]] = []
        for line in _iter_complete_lines(manifest, 512):
            entries.append(_decode_manifest_line(line, blackout_id))
            if len(entries) > MAX_SEGMENT_REFS * 2:
                raise EventCorruptionError("segment manifest contains too many entries")
        latest: dict[str, str | None] = {}
        for token, damaged in entries:
            latest[token] = damaged
        return tuple(latest.items())

    def segment_sources(self, blackout_id: str) -> tuple[tuple[Path, bool], ...]:
        sources: list[tuple[str, Path, bool]] = []
        for token, damaged in self.manifest_entries(blackout_id):
            path = self._filesystem._event_path(token)
            if damaged is not None and not path.exists():
                path = self._events_path / f"corrupt-{damaged}-{token}"
                is_corrupt = True
            else:
                is_corrupt = False
            if not path.exists():
                if damaged is None and self._is_precreate_reservation(token):
                    continue
                raise EventCorruptionError(f"manifest-referenced event segment is missing: {token}")
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise EventPathError(f"event path is not a regular file: {path.name}")
            if damaged is not None and _file_sha256(path) != damaged:
                raise EventCorruptionError("segment manifest hash does not match bytes")
            sources.append((token, path, is_corrupt))
        sources.sort(key=lambda item: (_segment_order_key(item[0]), item[1].name))
        return tuple((path, corrupt) for _name, path, corrupt in sources)

    def damaged_hashes(self, blackout_id: str) -> tuple[str, ...]:
        ordered = [
            (token, damaged)
            for token, damaged in self.manifest_entries(blackout_id)
            if damaged is not None
        ]
        ordered.sort(key=lambda item: _segment_order_key(item[0]))
        return tuple(digest for _original, digest in ordered if digest is not None)

    def event_total_bytes(self, blackout_id: str) -> int:
        sources = self.segment_sources(blackout_id)
        if len(sources) > MAX_SEGMENT_REFS:
            raise _EventCapacityExceeded("event references more than 64 segments")
        total = 0
        for path, _is_corrupt in sources:
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise EventPersistenceError(
                    f"cannot inspect event size: {_bounded_error(exc)}"
                ) from exc
            total += size
            if total > MAX_EVENT_BYTES:
                raise _EventCapacityExceeded("event exceeds 64 MiB")
        return total

    def ensure_path_within_limit(self, path: Path) -> None:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise EventPersistenceError(
                f"cannot inspect event size: {_bounded_error(exc)}"
            ) from exc
        if size > MAX_EVENT_BYTES:
            raise _EventCapacityExceeded("event segment exceeds 64 MiB")

    def validate_before_full_read(self, path: Path, blackout_id: str | None = None) -> None:
        """Validate aggregate bytes before a caller reads an entire segment."""
        self.ensure_path_within_limit(path)
        self.event_total_bytes(blackout_id or self.blackout_id_from_path(path))

    def append_capacity_exceeded(
        self,
        handle: EventHandle,
        *,
        record_type: str = "observation",
        line_size: int = MAX_LINE_BYTES,
    ) -> bool:
        if line_size < 0 or line_size > MAX_LINE_BYTES:
            raise EventValidationError("record line size is outside its bound")
        total = self.event_total_bytes(handle.blackout_id)
        limit = CAPTURE_APPEND_LIMIT if record_type == "observation" else MAX_EVENT_BYTES
        return total + line_size > limit

    def capacity_continuation(self, blackout_id: str) -> EventHandle | None:
        """Find a durable size-limit continuation without process-local state."""
        for path, is_corrupt in reversed(self.segment_sources(blackout_id)):
            if is_corrupt:
                continue
            first_line = next(iter(_iter_complete_lines(path, MAX_LINE_BYTES)), None)
            if first_line is None:
                continue
            first = _decode_record_line(first_line)
            if first.record_type != "gap" or first.payload.get("reason") != "event_size_limit":
                continue
            last = self._last_record(path)
            if last is None:
                raise EventCorruptionError("size-limit continuation has no durable record")
            return EventHandle(
                blackout_id,
                last.segment_id,
                path.name,
                last.seq + 1,
                last.record_sha256,
            )
        return None

    def prepare_capacity_continuation(
        self,
        handle: EventHandle,
        path: Path,
        last: _StoredRecord | None,
    ) -> PreparedCapacityContinuation:
        if last is None:
            raise EventCorruptionError("capacity-limited event has no durable tail")
        capture = self._registry()._read_registry().capture
        if not isinstance(capture, CapturingEventRef) or capture.blackout_id != handle.blackout_id:
            raise EventConflictError("capacity-limited capture is no longer active")
        damaged_sha256 = _file_sha256(path)
        self.reserve_segment_manifest(path.name, damaged_sha256)
        return PreparedCapacityContinuation(damaged_sha256)

    def activate_capacity_continuation(self, handle: EventHandle) -> None:
        registry = self._registry()._read_registry()
        capture = registry.capture
        if isinstance(capture, CapturingEventRef) and capture.path_token == handle.path_token:
            return
        if not isinstance(capture, CapturingEventRef) or capture.blackout_id != handle.blackout_id:
            raise EventConflictError("capacity-limited capture is no longer active")
        self._registry()._write_registry(
            WorkRegistry(
                CapturingEventRef(handle.blackout_id, handle.segment_id, handle.path_token),
                registry.pending_processing,
            )
        )

    @staticmethod
    def blackout_id_from_path(path: Path) -> str:
        _validate_path_token(path.name)
        match = EVENT_FILENAME_RE.fullmatch(path.name)
        if match is None:
            raise EventPathError("event filename does not match the version 2 layout")
        blackout_id = match.group("blackout")
        _validate_uuid4_hex(blackout_id, "blackout_id")
        return blackout_id

    def _last_record(self, path: Path) -> _StoredRecord | None:
        lines = _bounded_tail_lines(path, 1, MAX_LINE_BYTES)
        if not lines:
            return None
        return _decode_record_line(lines[-1])

    def _is_precreate_reservation(self, path_token: str) -> bool:
        capture = self._registry()._read_registry().capture
        return isinstance(capture, PreparingCaptureRef) and capture.path_token == path_token
