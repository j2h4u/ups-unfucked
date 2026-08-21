"""Durable manifest and aggregate-size policy for JSONL event streams."""

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from src.adapters.jsonl_errors import (
    EventConflictError,
    EventCorruptionError,
    EventPathError,
    EventValidationError,
)
from src.adapters.jsonl_event_read_codec import (
    ensure_path_within_limit as read_ensure_path_within_limit,
)
from src.adapters.jsonl_event_read_codec import (
    event_total_bytes as read_event_total_bytes,
)
from src.adapters.jsonl_event_read_codec import (
    last_record as read_last_record,
)
from src.adapters.jsonl_event_read_codec import (
    manifest_entries as read_manifest_entries,
)
from src.adapters.jsonl_event_read_codec import (
    segment_sources as read_segment_sources,
)
from src.adapters.jsonl_event_read_codec import (
    validate_before_full_read as read_validate_before_full_read,
)
from src.adapters.jsonl_filesystem import JsonlFilesystem
from src.adapters.jsonl_record_codec import (
    CAPTURE_APPEND_LIMIT,
    EVENT_FILENAME_RE,
    MAX_EVENT_BYTES,
    MAX_LINE_BYTES,
    MAX_REGISTRY_BYTES,
    _decode_record_line,
    _StoredRecord,
)
from src.adapters.jsonl_summary_codec import _iter_complete_lines
from src.application.storage_values import (
    CapturingEventRef,
    EventHandle,
    PreparingCaptureRef,
    WorkRegistry,
)

if TYPE_CHECKING:
    from src.adapters.jsonl_work_registry import JsonlWorkRegistry


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

    def reserve_segment_manifest(self, path_token: str) -> None:
        """Record an exact segment path before its event file is created."""
        match = EVENT_FILENAME_RE.fullmatch(path_token)
        if match is None:
            raise EventPathError("manifest path token is not an event filename")
        blackout_id = match.group("blackout")
        manifest = self._events_path / f"segments-{blackout_id}.jsonl"
        value = {"path_token": path_token}
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
                if isinstance(decoded, dict) and decoded.get("path_token") == path_token:
                    return
        fd = self._filesystem._open_append_or_create(manifest, mode=0o600)
        try:
            self._filesystem._append_and_sync_fd(fd, line)
        finally:
            os.close(fd)

    def manifest_entries(self, blackout_id: str) -> tuple[tuple[str, str | None], ...]:
        return read_manifest_entries(self._events_path, blackout_id)

    def segment_sources(self, blackout_id: str) -> tuple[tuple[Path, bool], ...]:
        return read_segment_sources(
            self._events_path,
            blackout_id,
            reserved_paths=self._reserved_paths(),
        )

    def event_total_bytes(self, blackout_id: str) -> int:
        return read_event_total_bytes(
            self._events_path,
            blackout_id,
            reserved_paths=self._reserved_paths(),
        )

    def ensure_path_within_limit(self, path: Path) -> None:
        read_ensure_path_within_limit(path)

    def validate_before_full_read(self, path: Path, blackout_id: str | None = None) -> None:
        """Validate aggregate bytes before a caller reads an entire segment."""
        read_validate_before_full_read(
            self._events_path,
            path,
            blackout_id,
            reserved_paths=self._reserved_paths(),
        )

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
            last = read_last_record(path)
            if last is None:
                raise EventCorruptionError("size-limit continuation has no durable record")
            return EventHandle(
                blackout_id,
                last.segment_id,
                path.name,
                last.seq + 1,
                last.event_kind,
            )
        return None

    def prepare_capacity_continuation(
        self,
        handle: EventHandle,
        path: Path,
        last: _StoredRecord | None,
    ) -> None:
        if last is None:
            raise EventCorruptionError("capacity-limited event has no durable tail")
        capture = self._registry()._read_registry().capture
        if not isinstance(capture, CapturingEventRef) or capture.blackout_id != handle.blackout_id:
            raise EventConflictError("capacity-limited capture is no longer active")
        self.reserve_segment_manifest(path.name)

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

    def _reserved_paths(self) -> frozenset[str]:
        """Return only pre-created paths the writer has reserved but not made."""
        capture = self._registry()._read_registry().capture
        if isinstance(capture, PreparingCaptureRef):
            return frozenset({capture.path_token})
        return frozenset()
