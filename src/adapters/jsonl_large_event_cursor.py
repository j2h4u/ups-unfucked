"""Durable byte cursor for bounded replay of one large event segment."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.adapters.jsonl_errors import EventCorruptionError, EventPathError, EventPersistenceError
from src.adapters.jsonl_filesystem import JsonlFilesystem
from src.adapters.jsonl_record_codec import (
    MAX_LINE_BYTES,
    _validate_path_token,
    canonical_json_bytes,
)

LARGE_EVENT_CURSOR_SCHEMA = 1
LARGE_EVENT_SCAN_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class LargeEventCursor:
    """Identity and byte progress for a replay that spans rebuild ticks."""

    path_token: str
    size: int
    mtime_ns: int
    offset: int


class JsonlLargeEventCursor:
    """Persist one bounded replay cursor without making it index authority."""

    def __init__(self, events_path: Path, filesystem: JsonlFilesystem) -> None:
        self._events_path = events_path
        self._filesystem = filesystem
        self.path = events_path / "index-rebuild.event.cursor.json"

    def read(self) -> LargeEventCursor | None:
        if not self.path.exists():
            return None
        try:
            info = self.path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise EventPathError("large-event cursor is not regular")
            return _decode_large_event_cursor(self.path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise EventCorruptionError("large-event cursor is invalid") from exc

    def advance(self, path: Path, *, offset: int, budget: int) -> LargeEventCursor:
        try:
            info = path.lstat()
        except OSError as exc:
            raise EventPersistenceError("cannot inspect large event") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise EventPathError("large event is not regular")
        if offset < 0 or offset > info.st_size or budget < 1:
            raise ValueError("large-event cursor range is invalid")
        target = min(info.st_size, offset + budget)
        if target < info.st_size:
            with path.open("rb") as stream:
                stream.seek(target)
                allowance = stream.read(MAX_LINE_BYTES)
            newline = allowance.find(b"\n")
            if newline >= 0:
                target += newline + 1
            else:
                target = min(info.st_size, target + MAX_LINE_BYTES)
        cursor = LargeEventCursor(path.name, info.st_size, info.st_mtime_ns, target)
        self._write(cursor)
        return cursor

    def matches(self, path: Path, cursor: LargeEventCursor) -> bool:
        try:
            info = path.lstat()
        except OSError as exc:
            raise EventPersistenceError("cannot inspect large event") from exc
        return (
            path.name == cursor.path_token
            and info.st_size == cursor.size
            and info.st_mtime_ns == cursor.mtime_ns
        )

    def clear(self) -> None:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise EventPersistenceError("cannot inspect large-event cursor") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise EventPathError("large-event cursor is not regular")
        self.path.unlink()
        self._filesystem.sync_storage_directory(self._events_path)

    def _write(self, cursor: LargeEventCursor) -> None:
        value: dict[str, Any] = {
            "schema_version": LARGE_EVENT_CURSOR_SCHEMA,
            "path_token": cursor.path_token,
            "size": cursor.size,
            "mtime_ns": cursor.mtime_ns,
            "offset": cursor.offset,
        }
        self._filesystem.atomic_replace(
            self.path,
            canonical_json_bytes(value) + b"\n",
            mode=0o600,
        )


def _decode_large_event_cursor(raw: bytes) -> LargeEventCursor:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "path_token",
        "size",
        "mtime_ns",
        "offset",
    }:
        raise EventCorruptionError("large-event cursor fields are invalid")
    if canonical_json_bytes(value) + b"\n" != raw:
        raise EventCorruptionError("large-event cursor is not canonical")
    return _cursor_from_mapping(value)


def _cursor_from_mapping(value: dict[str, Any]) -> LargeEventCursor:
    path_token = _cursor_path_token(value)
    size, mtime_ns, offset = _cursor_positions(value)
    return LargeEventCursor(path_token, size, mtime_ns, offset)


def _cursor_path_token(value: dict[str, Any]) -> str:
    path_token = value["path_token"]
    if value["schema_version"] != LARGE_EVENT_CURSOR_SCHEMA or not isinstance(path_token, str):
        raise EventCorruptionError("large-event cursor identity is invalid")
    try:
        _validate_path_token(path_token)
    except (ValueError, EventPathError) as exc:
        raise EventCorruptionError("large-event cursor path is invalid") from exc
    return path_token


def _cursor_positions(value: dict[str, Any]) -> tuple[int, int, int]:
    positions = value["size"], value["mtime_ns"], value["offset"]
    if any(not _is_nonnegative_int(item) for item in positions):
        raise EventCorruptionError("large-event cursor values are invalid")
    size, mtime_ns, offset = (int(item) for item in positions)
    if offset > size:
        raise EventCorruptionError("large-event cursor offset exceeds file size")
    return size, mtime_ns, offset


def _is_nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


__all__ = ["JsonlLargeEventCursor", "LargeEventCursor", "LARGE_EVENT_SCAN_BYTES"]
