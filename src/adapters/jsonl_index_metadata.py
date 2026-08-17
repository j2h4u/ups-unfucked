"""Durable cursor and projection-metadata mechanics for the JSONL index."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from src.adapters.jsonl_errors import (
    EventConflictError,
    EventCorruptionError,
    EventPathError,
    EventPersistenceError,
    EventStoreError,
    ProjectionUnavailableError,
)
from src.adapters.jsonl_filesystem import JsonlFilesystem
from src.adapters.jsonl_record_codec import (
    EMPTY_SHA256,
    MAX_EPOCH_INDEX_SCAN_BYTES,
    MAX_REGISTRY_BYTES,
    _strict_json_loads,
    canonical_json_bytes,
)
from src.adapters.jsonl_summary_codec import (
    _bounded_file_suffix,
    _decode_summary_line,
    _summary_key,
)


def _chain_digest(previous: str, line: bytes) -> str:
    return hashlib.sha256(previous.encode("ascii") + line).hexdigest()


if TYPE_CHECKING:
    from src.adapters.jsonl_event_catalog import JsonlEventCatalog


@dataclass(frozen=True, slots=True)
class IndexMetadataPaths:
    """Filesystem paths for one index's durable repair metadata."""

    events_path: Path
    index_path: Path
    cursor_path: Path
    rebuild_path: Path
    merge_path: Path
    delta_path: Path
    catalog_cursor_path: Path
    head_path: Path | None = None
    intent_path: Path | None = None


def _index_tail_is_valid(path: Path) -> bool:
    raw, reached_start = _bounded_file_suffix(path, MAX_EPOCH_INDEX_SCAN_BYTES)
    if not reached_start:
        return False
    try:
        tuple(_decode_summary_line(line) for line in raw.splitlines(keepends=True))
    except EventStoreError:
        return False
    return True


def _index_path_is_available(path: Path, entry_count: int, *, allow_partial: bool) -> bool:
    if not path.exists():
        return entry_count == 0
    if allow_partial:
        return True
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size == 0:
        return entry_count == 0
    return _index_tail_is_valid(path)


class IndexMetadataStore:
    """Persist and validate cursors and projection files independently."""

    def __init__(
        self,
        paths: IndexMetadataPaths,
        filesystem: JsonlFilesystem,
        catalog: JsonlEventCatalog,
        validate_rebuild_cursor: Callable[[Mapping[str, Any]], None],
        validate_catalog_cursor: Callable[[Mapping[str, Any]], None],
    ) -> None:
        self._paths = paths
        self._filesystem = filesystem
        self._catalog = catalog
        self._validate_rebuild_cursor = validate_rebuild_cursor
        self._validate_catalog_cursor = validate_catalog_cursor

    @property
    def _head_path(self) -> Path:
        return self._paths.head_path or self._paths.events_path / "index-head.json"

    @property
    def _intent_path(self) -> Path:
        return self._paths.intent_path or self._paths.events_path / "index-append-intent.json"

    def _read_index_head(self) -> dict[str, Any]:
        path = self._head_path
        if not path.exists():
            return {
                "schema_version": 1,
                "offset": 0,
                "count": 0,
                "last_line_sha256": EMPTY_SHA256,
                "cumulative_sha256": EMPTY_SHA256,
            }
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise EventPathError("index head is not regular")
            if info.st_size > MAX_REGISTRY_BYTES:
                raise EventCorruptionError("index head is too large")
            raw = path.read_bytes()
            value = _strict_json_loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise EventCorruptionError("index head is invalid") from exc
        if (
            not isinstance(value, dict)
            or set(value)
            != {"schema_version", "offset", "count", "last_line_sha256", "cumulative_sha256"}
            or canonical_json_bytes(value) + b"\n" != raw
        ):
            raise EventCorruptionError("index head fields are invalid")
        if (
            value["schema_version"] != 1
            or any(
                isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < 0
                for key in ("offset", "count")
            )
            or not all(
                isinstance(value[key], str) and len(value[key]) == 64
                for key in ("last_line_sha256", "cumulative_sha256")
            )
        ):
            raise EventCorruptionError("index head values are invalid")
        return value

    def _write_index_head(self, value: Mapping[str, Any]) -> None:
        self._filesystem.atomic_replace(
            self._head_path,
            canonical_json_bytes(value) + b"\n",
            mode=0o600,
        )

    def _update_index_head(self, line: bytes, *, destination: Path) -> dict[str, Any]:
        """Advance the head only after the exact line is durable."""
        if destination != self._paths.index_path:
            return self._read_index_head()
        info = destination.stat()
        previous = self._read_index_head()
        line_hash = hashlib.sha256(line).hexdigest()
        if previous["offset"] == info.st_size and previous["last_line_sha256"] == line_hash:
            return previous
        if previous["offset"] not in {0, info.st_size - len(line)}:
            raise EventCorruptionError("index head offset disagrees with durable bytes")
        if previous["offset"] == 0 and info.st_size > len(line):
            # Legacy index without a head: reconstruct the cumulative digest
            # once, while all subsequent appends remain constant-memory.
            cumulative = EMPTY_SHA256
            count = 0
            with destination.open("rb") as stream:
                for existing in stream:
                    cumulative = _chain_digest(cumulative, existing)
                    count += 1
            previous = {**previous, "offset": info.st_size - len(line), "count": count - 1}
        cumulative = _chain_digest(previous["cumulative_sha256"], line)
        updated = {
            "schema_version": 1,
            "offset": info.st_size,
            "count": previous["count"] + 1,
            "last_line_sha256": line_hash,
            "cumulative_sha256": cumulative,
        }
        self._write_index_head(updated)
        return updated

    def _rebuild_index_head(self) -> dict[str, Any]:
        """Create a head for an atomically promoted index without byte-zero reads later."""
        if not self._paths.index_path.exists():
            return self._read_index_head()
        cumulative = EMPTY_SHA256
        last = EMPTY_SHA256
        count = 0
        offset = 0
        try:
            with self._paths.index_path.open("rb") as stream:
                for line in stream:
                    _decode_summary_line(line)
                    cumulative = _chain_digest(cumulative, line)
                    last = hashlib.sha256(line).hexdigest()
                    count += 1
                    offset += len(line)
        except OSError as exc:
            raise EventPersistenceError("cannot build index head") from exc
        value = {
            "schema_version": 1,
            "offset": offset,
            "count": count,
            "last_line_sha256": last,
            "cumulative_sha256": cumulative,
        }
        self._write_index_head(value)
        return value

    def _has_index_intent(self) -> bool:
        return self._intent_path.exists()

    def _append_raw_index_line(self, destination: Path, line: bytes) -> None:
        fd = self._filesystem._open_append_or_create(destination, mode=0o600)
        try:
            self._filesystem._append_and_sync_fd(fd, line)
        finally:
            os.close(fd)

    def _truncate_index_destination(self, destination: Path, offset: int) -> None:
        fd = self._filesystem._open_existing(destination, writable=True)
        try:
            os.ftruncate(fd, offset)
            os.fdatasync(fd)
        finally:
            os.close(fd)

    def _index_head_is_current(self, destination: Path) -> bool:
        if destination != self._paths.index_path or not self._head_path.exists():
            return False
        head = self._read_index_head()
        try:
            current_size = destination.stat().st_size
        except OSError as exc:
            raise EventPersistenceError("cannot inspect indexed summary size") from exc
        if current_size != head["offset"]:
            raise EventCorruptionError("index head disagrees with durable summary bytes")
        return True

    def _append_summary(self, destination: Path, summary: Any, line: bytes) -> None:
        identical_found = False
        head_is_current = self._index_head_is_current(destination)
        if destination.exists() and not head_is_current:
            raw, reached_start = _bounded_file_suffix(destination, MAX_EPOCH_INDEX_SCAN_BYTES)
            lines = raw.splitlines(keepends=True)
            if not reached_start and lines:
                lines = lines[1:]
            for existing_line in lines:
                existing = _decode_summary_line(existing_line)
                if _summary_key(existing) != _summary_key(summary):
                    continue
                if existing_line != line:
                    raise EventConflictError("projection key exists with different summary bytes")
                identical_found = True
            if not identical_found and not reached_start:
                raise ProjectionUnavailableError(
                    "summary idempotency lookup exceeds bounded suffix"
                )
        if identical_found:
            return
        self._append_raw_index_line(destination, line)
        self._update_index_head(line, destination=destination)

    def _read_append_intent(self) -> dict[str, Any] | None:
        if not self._intent_path.exists():
            return None
        try:
            raw = self._intent_path.read_bytes()
            value = _strict_json_loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise EventCorruptionError("index append intent is invalid") from exc
        if not isinstance(value, dict) or canonical_json_bytes(value) + b"\n" != raw:
            raise EventCorruptionError("index append intent is not canonical")
        required = {
            "schema_version",
            "destination",
            "generation",
            "offset",
            "summary_line",
            "summary_sha256",
            "outbox_identity",
            "phase",
        }
        if set(value) != required:
            raise EventCorruptionError("index append intent fields are invalid")
        return value

    def _recover_intent_line(
        self,
        destination: Path,
        intent: Mapping[str, Any],
    ) -> bytes:
        """Converge the exact frozen index bytes after a partial append."""
        line = intent["summary_line"].encode("utf-8")
        offset = intent["offset"]
        if hashlib.sha256(line).hexdigest() != intent["summary_sha256"]:
            raise EventCorruptionError("index append intent summary hash is invalid")
        actual = destination.stat().st_size if destination.exists() else 0
        if actual < offset:
            raise EventConflictError("index append destination is shorter than frozen offset")
        if actual == offset:
            self._append_raw_index_line(destination, line)
            return line
        self._recover_existing_intent_suffix(destination, offset, line)
        return line

    def _recover_existing_intent_suffix(
        self,
        destination: Path,
        offset: int,
        line: bytes,
    ) -> None:
        suffix = destination.read_bytes()[offset:]
        if suffix == line:
            return
        if not line.startswith(suffix):
            raise EventConflictError("index append destination has conflicting tail bytes")
        self._truncate_index_destination(destination, offset)
        self._append_raw_index_line(destination, line)

    def _intent_outbox_identity(
        self,
        intent: Mapping[str, Any],
    ) -> tuple[str, str, str]:
        """Decode the exact report identity bound into one append intent."""
        del self
        identity = intent["outbox_identity"]
        if (
            not isinstance(identity, list)
            or len(identity) != 3
            or not all(isinstance(item, str) for item in identity)
        ):
            raise EventCorruptionError("index append outbox identity is invalid")
        return identity[0], identity[1], identity[2]

    def _write_append_intent(self, value: Mapping[str, Any]) -> None:
        self._filesystem.atomic_replace(
            self._intent_path,
            canonical_json_bytes(value) + b"\n",
            mode=0o600,
        )

    def _clear_append_intent(self) -> None:
        self.unlink_projection_file(self._intent_path)
        self._filesystem.sync_storage_directory(self._paths.events_path)

    def write_rebuild_cursor(self, cursor: Mapping[str, Any]) -> None:
        self._validate_rebuild_cursor(cursor)
        self._filesystem.atomic_replace(
            self._paths.cursor_path,
            canonical_json_bytes(cursor) + b"\n",
            mode=0o600,
        )

    def read_rebuild_cursor(self) -> Mapping[str, Any] | None:
        path = self._paths.cursor_path
        if not path.exists():
            return None
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise EventPathError("index rebuild cursor is not a regular file")
            if info.st_size > MAX_REGISTRY_BYTES:
                raise EventCorruptionError("index rebuild cursor exceeds 128 KiB")
            raw = path.read_bytes()
            value = _strict_json_loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise EventCorruptionError("index rebuild cursor is invalid") from exc
        if not isinstance(value, dict):
            raise EventCorruptionError("index rebuild cursor is not an object")
        if canonical_json_bytes(value) + b"\n" != raw:
            raise EventCorruptionError("index rebuild cursor is not canonical JSON")
        self._validate_rebuild_cursor(value)
        return value

    def read_catalog_cursor(self) -> Mapping[str, Any]:
        path = self._paths.catalog_cursor_path
        if not path.exists():
            raise EventCorruptionError("index rebuild catalog cursor is missing")
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise EventPathError("index rebuild catalog cursor is not regular")
            if info.st_size > MAX_REGISTRY_BYTES:
                raise EventCorruptionError("index rebuild catalog cursor is too large")
            raw = path.read_bytes()
            value = _strict_json_loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise EventCorruptionError("index rebuild catalog cursor is invalid") from exc
        if not isinstance(value, dict) or canonical_json_bytes(value) + b"\n" != raw:
            raise EventCorruptionError("index rebuild catalog cursor is not canonical")
        self._validate_catalog_cursor(value)
        return value

    def write_catalog_cursor(self, cursor: Mapping[str, Any]) -> None:
        self._validate_catalog_cursor(cursor)
        self._filesystem.atomic_replace(
            self._paths.catalog_cursor_path,
            canonical_json_bytes(cursor) + b"\n",
            mode=0o600,
        )

    def clear_rebuild_metadata(self) -> None:
        self.unlink_projection_file(self._paths.rebuild_path)
        self._filesystem._trip("after_rebuild_cleanup_rebuild")
        self.unlink_projection_file(self._paths.merge_path)
        self._filesystem._trip("after_rebuild_cleanup_merge")
        self.unlink_projection_file(self._paths.delta_path)
        self._filesystem._trip("after_rebuild_cleanup_delta")
        self.unlink_projection_file(self._paths.catalog_cursor_path)
        self._filesystem.sync_storage_directory(self._paths.events_path)
        self._filesystem._trip("before_rebuild_cleanup_cursor")
        self.unlink_projection_file(self._paths.cursor_path)
        self._filesystem.sync_storage_directory(self._paths.events_path)

    def clear_orphan_rebuild_metadata(self) -> None:
        """Remove projection artifacts only after the index is authoritative."""
        for path in (
            self._paths.rebuild_path,
            self._paths.merge_path,
            self._paths.delta_path,
            self._paths.catalog_cursor_path,
        ):
            self.unlink_projection_file(path)
        self._filesystem.sync_storage_directory(self._paths.events_path)

    @staticmethod
    def unlink_projection_file(path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise EventPathError(f"projection metadata is not a regular file: {path.name}")
        path.unlink()

    def projection_destination(self) -> Path:
        if self._paths.cursor_path.exists():
            return self._paths.delta_path
        return self._paths.index_path

    def index_available(self, *, allow_partial: bool = False) -> bool:
        if self._paths.cursor_path.exists() or self._has_index_intent():
            return False
        try:
            snapshot = self._catalog.snapshot()
        except EventStoreError:
            return False
        head_path = self._head_path
        if head_path.exists() and self._paths.index_path.exists():
            return self._head_available(snapshot.entry_count)
        return _index_path_is_available(
            self._paths.index_path, snapshot.entry_count, allow_partial=allow_partial
        )

    def _head_available(self, catalog_count: int) -> bool:
        try:
            head = self._read_index_head()
            size = self._paths.index_path.stat().st_size
            if size != head["offset"]:
                return False
            if size == 0:
                return head["count"] == 0
            tail, _ = _bounded_file_suffix(self._paths.index_path, MAX_EPOCH_INDEX_SCAN_BYTES)
            lines = tail.splitlines(keepends=True)
            if not lines or hashlib.sha256(lines[-1]).hexdigest() != head["last_line_sha256"]:
                return False
            _decode_summary_line(lines[-1])
            return head["count"] == catalog_count or catalog_count == 0
        except (EventStoreError, OSError, KeyError):
            return False
