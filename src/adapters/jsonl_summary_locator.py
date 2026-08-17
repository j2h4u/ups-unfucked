"""Durable, immutable locators for sealed JSONL event summaries.

The event files remain the source of truth.  A locator is a small derived
receipt which lets recovery find and verify a summary without replaying a
large event or trusting a filename ordering convention.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.adapters.jsonl_errors import (
    EventConflictError,
    EventCorruptionError,
    EventPathError,
    EventPersistenceError,
    EventValidationError,
)
from src.adapters.jsonl_filesystem import JsonlFilesystem, _file_sha256
from src.adapters.jsonl_record_codec import (
    EMPTY_SHA256,
    MAX_SEGMENT_REFS,
    _decode_record_line,
    _is_sha256,
    _validate_path_token,
    _validate_uuid4_hex,
    canonical_json_bytes,
)

LOCATOR_SCHEMA_VERSION = 1
MAX_LOCATOR_BYTES = 128 * 1024


@dataclass(frozen=True, slots=True)
class LocatorSegment:
    """One ordered physical or retained-corrupt segment receipt."""

    segment_id: str
    path_token: str
    file_sha256: str
    final_record_sha256: str
    retained_corrupt: bool = False


@dataclass(frozen=True, slots=True)
class SummaryLocator:
    """Exact summary identity and the evidence needed to verify it."""

    schema_version: int
    blackout_id: str
    final_path_token: str
    outcome_record_sha256: str
    summary_line: bytes
    terminal_catalog_seq: int
    segments: tuple[LocatorSegment, ...]
    logical_root_sha256: str

    @property
    def summary_line_utf8(self) -> str:
        """Expose the exact line in a JSON-friendly form."""
        return self.summary_line.decode("utf-8")

    @property
    def locator_sha256(self) -> str:
        """Hash the canonical locator bytes, excluding no mutable metadata."""
        return hashlib.sha256(_encode_locator(self)).hexdigest()


def _segment_dict(segment: LocatorSegment) -> dict[str, Any]:
    return {
        "segment_id": segment.segment_id,
        "path_token": segment.path_token,
        "file_sha256": segment.file_sha256,
        "final_record_sha256": segment.final_record_sha256,
        "retained_corrupt": segment.retained_corrupt,
    }


def _locator_dict(locator: SummaryLocator) -> dict[str, Any]:
    return {
        "schema_version": locator.schema_version,
        "blackout_id": locator.blackout_id,
        "final_path_token": locator.final_path_token,
        "outcome_record_sha256": locator.outcome_record_sha256,
        "summary_line": locator.summary_line_utf8,
        "terminal_catalog_seq": locator.terminal_catalog_seq,
        "segments": [_segment_dict(segment) for segment in locator.segments],
        "logical_root_sha256": locator.logical_root_sha256,
    }


def _encode_locator(locator: SummaryLocator) -> bytes:
    raw = canonical_json_bytes(_locator_dict(locator)) + b"\n"
    if len(raw) > MAX_LOCATOR_BYTES:
        raise EventValidationError("summary locator exceeds its bounded size")
    return raw


def _root_hash(value: dict[str, Any], segments: tuple[LocatorSegment, ...]) -> str:
    body = {
        "blackout_id": value["blackout_id"],
        "final_path_token": value["final_path_token"],
        "outcome_record_sha256": value["outcome_record_sha256"],
        "summary_line_sha256": hashlib.sha256(value["summary_line"]).hexdigest(),
        "terminal_catalog_seq": value["terminal_catalog_seq"],
        "segments": [_segment_dict(segment) for segment in segments],
    }
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def _validated_segments(raw: Any) -> tuple[LocatorSegment, ...]:
    if not isinstance(raw, Iterable):
        raise EventValidationError("locator segments are invalid")
    ordered = tuple(raw)
    if not ordered or len(ordered) > MAX_SEGMENT_REFS:
        raise EventValidationError("locator segment count is outside its bound")
    seen_tokens: set[str] = set()
    for segment in ordered:
        if not isinstance(segment, LocatorSegment):
            raise EventValidationError("locator segment value is invalid")
        _validate_uuid4_hex(segment.segment_id, "segment_id")
        _validate_path_token(segment.path_token)
        if segment.path_token in seen_tokens:
            raise EventValidationError("locator contains duplicate segment paths")
        seen_tokens.add(segment.path_token)
        if not _is_sha256(segment.file_sha256) or not _is_sha256(segment.final_record_sha256):
            raise EventValidationError("locator segment hash is invalid")
        if not isinstance(segment.retained_corrupt, bool):
            raise EventValidationError("locator segment corruption flag is invalid")
    return ordered


def make_locator(**values: Any) -> SummaryLocator:
    """Construct and strictly validate one locator before it is persisted."""
    blackout_id = values.get("blackout_id")
    final_path_token = values.get("final_path_token")
    outcome_record_sha256 = values.get("outcome_record_sha256")
    summary_line = values.get("summary_line")
    terminal_catalog_seq = values.get("terminal_catalog_seq")
    segments = values.get("segments")
    if not isinstance(blackout_id, str) or not isinstance(final_path_token, str):
        raise EventValidationError("locator identity is invalid")
    _validate_uuid4_hex(blackout_id, "blackout_id")
    _validate_path_token(final_path_token)
    if not isinstance(outcome_record_sha256, str) or not _is_sha256(outcome_record_sha256):
        raise EventValidationError("locator outcome hash is invalid")
    if not isinstance(summary_line, bytes) or not summary_line.endswith(b"\n"):
        raise EventValidationError("locator summary line must be newline terminated bytes")
    try:
        summary = json.loads(summary_line[:-1])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EventValidationError("locator summary line is not JSON") from exc
    if not isinstance(summary, dict) or summary.get("blackout_id") != blackout_id:
        raise EventValidationError("locator summary identifies another blackout")
    if summary.get("segment_filename") != final_path_token:
        raise EventValidationError("locator summary identifies another final segment")
    if summary.get("outcome_record_sha256") != outcome_record_sha256:
        raise EventValidationError("locator summary identifies another outcome")
    if not isinstance(terminal_catalog_seq, int) or isinstance(terminal_catalog_seq, bool):
        raise EventValidationError("locator catalog sequence is invalid")
    if terminal_catalog_seq < 0:
        raise EventValidationError("locator catalog sequence is negative")
    ordered = _validated_segments(segments)
    root = _root_hash(
        {
            "blackout_id": blackout_id,
            "final_path_token": final_path_token,
            "outcome_record_sha256": outcome_record_sha256,
            "summary_line": summary_line,
            "terminal_catalog_seq": terminal_catalog_seq,
        },
        ordered,
    )
    return SummaryLocator(
        LOCATOR_SCHEMA_VERSION,
        blackout_id,
        final_path_token,
        outcome_record_sha256,
        summary_line,
        terminal_catalog_seq,
        ordered,
        root,
    )


def locator_from_projection(**values: Any) -> SummaryLocator:
    """Build segment receipts from an already trusted bounded projection."""
    final_path_token = values["final_path_token"]
    outcome_record_sha256 = values["outcome_record_sha256"]
    summary_line = values["summary_line"]
    terminal_catalog_seq = values["terminal_catalog_seq"]
    projection = values["projection"]
    segment_sources = values["segment_sources"]
    if projection.start is None:
        raise EventValidationError("locator projection has no start record")
    prefixes = tuple(projection.trusted_prefixes)
    descriptors: list[LocatorSegment] = []
    for position, (path, retained_corrupt) in enumerate(segment_sources):
        prefix = prefixes[position] if position < len(prefixes) else ()
        if prefix:
            segment_id = prefix[0].segment_id
            last_hash = prefix[-1].record_sha256
        else:
            # A retained corrupt prefix may contain no trusted record.  Its
            # bytes remain represented, but this event cannot be scientific.
            digest = bytearray(hashlib.sha256(path.name.encode()).digest()[:16])
            digest[6] = (digest[6] & 0x0F) | 0x40
            digest[8] = (digest[8] & 0x3F) | 0x80
            segment_id = bytes(digest).hex()
            last_hash = EMPTY_SHA256
        descriptors.append(
            LocatorSegment(
                segment_id,
                path.name if not path.name.startswith("corrupt-") else path.name.split("-", 2)[-1],
                _file_sha256(path),
                last_hash,
                retained_corrupt,
            )
        )
    return make_locator(
        blackout_id=projection.start.blackout_id,
        final_path_token=final_path_token,
        outcome_record_sha256=outcome_record_sha256,
        summary_line=summary_line,
        terminal_catalog_seq=terminal_catalog_seq,
        segments=descriptors,
    )


def _decode_locator(raw: bytes) -> SummaryLocator:
    if len(raw) > MAX_LOCATOR_BYTES or not raw.endswith(b"\n"):
        raise EventCorruptionError("summary locator is torn or too large")
    value = _decode_locator_mapping(raw)
    summary_line = value["summary_line"]
    if not isinstance(summary_line, str):
        raise EventCorruptionError("summary locator values are invalid")
    segments = _decode_locator_segments(value["segments"])
    try:
        result = make_locator(
            blackout_id=value["blackout_id"],
            final_path_token=value["final_path_token"],
            outcome_record_sha256=value["outcome_record_sha256"],
            summary_line=summary_line.encode("utf-8"),
            terminal_catalog_seq=value["terminal_catalog_seq"],
            segments=segments,
        )
    except (EventValidationError, UnicodeEncodeError) as exc:
        raise EventCorruptionError("summary locator values are invalid") from exc
    if result.logical_root_sha256 != value["logical_root_sha256"]:
        raise EventCorruptionError("summary locator logical root does not match")
    return result


def _decode_locator_mapping(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw[:-1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EventCorruptionError("summary locator is not strict JSON") from exc
    if not isinstance(value, dict):
        raise EventCorruptionError("summary locator is not an object")
    expected = {
        "schema_version",
        "blackout_id",
        "final_path_token",
        "outcome_record_sha256",
        "summary_line",
        "terminal_catalog_seq",
        "segments",
        "logical_root_sha256",
    }
    if set(value) != expected or canonical_json_bytes(value) + b"\n" != raw:
        raise EventCorruptionError("summary locator fields are not canonical")
    return value


def _decode_locator_segments(value: Any) -> list[LocatorSegment]:
    if not isinstance(value, list):
        raise EventCorruptionError("summary locator values are invalid")
    segments: list[LocatorSegment] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "segment_id",
            "path_token",
            "file_sha256",
            "final_record_sha256",
            "retained_corrupt",
        }:
            raise EventCorruptionError("summary locator segment fields are invalid")
        segments.append(
            LocatorSegment(
                item["segment_id"],
                item["path_token"],
                item["file_sha256"],
                item["final_record_sha256"],
                item["retained_corrupt"],
            )
        )
    return segments


class JsonlSummaryLocatorStore:
    """Read/write immutable locator files below ``summary-locators``."""

    def __init__(self, events_path: Path, filesystem: JsonlFilesystem | None = None) -> None:
        self._events_path = events_path
        self._filesystem = filesystem
        self._directory = events_path / "summary-locators"

    def path(self, blackout_id: str) -> Path:
        _validate_uuid4_hex(blackout_id, "blackout_id")
        return self._directory / f"{blackout_id}.json"

    def write(self, locator: SummaryLocator) -> str:
        raw = _encode_locator(locator)
        path = self.path(locator.blackout_id)
        if path.exists():
            existing = self.read(locator.blackout_id)
            if _encode_locator(existing) != raw:
                raise EventConflictError("summary locator already has different bytes")
            return existing.locator_sha256
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._filesystem is not None:
            self._filesystem.atomic_replace(path, raw, mode=0o400)
            self._seal(path)
        else:
            temp = path.with_name(f".{path.name}.tmp")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(temp, flags, 0o400)
            try:
                os.write(fd, raw)
                os.fdatasync(fd)
                os.fchmod(fd, 0o400)
            finally:
                os.close(fd)
            os.replace(temp, path)
        self._sync_directory()
        return locator.locator_sha256

    def read(self, blackout_id: str) -> SummaryLocator:
        path = self.path(blackout_id)
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise EventPathError("summary locator is not a regular file")
            if info.st_size > MAX_LOCATOR_BYTES:
                raise EventCorruptionError("summary locator exceeds its bound")
            return _decode_locator(path.read_bytes())
        except FileNotFoundError as exc:
            raise EventCorruptionError("summary locator is missing") from exc
        except OSError as exc:
            raise EventPersistenceError("cannot read summary locator") from exc

    def verify_segments(self, locator: SummaryLocator) -> None:
        """Verify every descriptor against bytes, including retained corrupt files."""
        for segment in locator.segments:
            path = self._events_path / segment.path_token
            if segment.retained_corrupt:
                candidates = tuple(
                    self._events_path.glob(f"corrupt-{segment.file_sha256}-{segment.path_token}")
                )
                if candidates:
                    path = candidates[0]
            if not path.exists() or _file_sha256(path) != segment.file_sha256:
                raise EventCorruptionError("summary locator segment bytes changed")
            if segment.retained_corrupt:
                # The full corrupt bytes are authenticated above.  Their last
                # newline may be malformed; final_record_sha256 instead binds
                # the trusted prefix represented in the projection.
                continue
            last: bytes | None = None
            with path.open("rb") as stream:
                for line in stream:
                    if line.endswith(b"\n"):
                        last = line
            if (
                last is None
                or _decode_record_line(last).record_sha256 != segment.final_record_sha256
            ):
                raise EventCorruptionError("summary locator final record changed")

    def _seal(self, path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fchmod(fd, 0o400)
            os.fdatasync(fd)
        finally:
            os.close(fd)

    def _sync_directory(self) -> None:
        if self._filesystem is not None:
            self._filesystem.sync_storage_directory(self._directory)
            return
        fd = os.open(self._directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


__all__ = [
    "JsonlSummaryLocatorStore",
    "LocatorSegment",
    "SummaryLocator",
    "make_locator",
]
