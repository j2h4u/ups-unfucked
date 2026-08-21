"""Pure reads from the append-only JSONL event representation.

This module deliberately knows only paths and codecs.  It does not compose a
filesystem, event stream, registry, or event store; callers that own mutable
state may use these functions after performing any required write-side repair.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Collection
from pathlib import Path
from typing import Any

from src.adapters.jsonl_errors import (
    EventConflictError,
    EventCorruptionError,
    EventPathError,
    EventPersistenceError,
)
from src.adapters.jsonl_record_codec import (
    DERIVED_RECORD_TYPES,
    EVENT_FILENAME_RE,
    MAX_EVENT_BYTES,
    MAX_LINE_BYTES,
    MAX_PENDING_PROCESSING,
    MAX_REGISTRY_BYTES,
    MAX_SEGMENT_REFS,
    _bounded_error,
    _decode_record_line,
    _deduplicate_and_validate_chain,
    _first_record,
    _flatten_records,
    _is_sha256,
    _projected_record,
    _records_of_type,
    _StoredRecord,
    _strict_json_loads,
    _validate_path_token,
    _validate_segmented_record_order,
    _validate_uuid4_hex,
)
from src.adapters.jsonl_summary_codec import _bounded_tail_lines, _iter_complete_lines
from src.application.storage_values import EventProjection, ProjectedEventRecord


class _EventCapacityExceeded(EventCorruptionError):
    """A logical event exceeded its durable projection budget."""


def event_file_sha256(path: Path) -> str:
    """Hash one event file without opening any mutable storage collaborator."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise EventPersistenceError(f"cannot hash event file: {_bounded_error(exc)}") from exc
    return digest.hexdigest()


def manifest_entries(events_path: Path, blackout_id: str) -> tuple[tuple[str, str | None], ...]:
    """Read and validate the durable segment manifest for one logical event."""
    _validate_uuid4_hex(blackout_id, "blackout_id")
    manifest = events_path / f"segments-{blackout_id}.jsonl"
    if not manifest.exists():
        return ()
    info = _lstat_regular(manifest, "segment manifest")
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


def segment_sources(
    events_path: Path,
    blackout_id: str,
    *,
    reserved_paths: Collection[str] = (),
) -> tuple[tuple[Path, bool], ...]:
    """Resolve manifest entries to event or preserved-corruption files."""
    reserved = frozenset(reserved_paths)
    sources: list[tuple[str, Path, bool]] = []
    for token, damaged in manifest_entries(events_path, blackout_id):
        path = events_path / token
        if damaged is not None and not path.exists():
            path = events_path / f"corrupt-{damaged}-{token}"
            is_corrupt = True
        else:
            is_corrupt = False
        if not path.exists():
            if damaged is None and token in reserved:
                continue
            raise EventCorruptionError(f"manifest-referenced event segment is missing: {token}")
        _lstat_regular(path, "event segment")
        if damaged is not None and event_file_sha256(path) != damaged:
            raise EventCorruptionError("segment manifest hash does not match bytes")
        sources.append((token, path, is_corrupt))
    sources.sort(key=lambda item: (_segment_order_key(item[0]), item[1].name))
    return tuple((path, corrupt) for _name, path, corrupt in sources)


def damaged_hashes(events_path: Path, blackout_id: str) -> tuple[str, ...]:
    """Return preserved damaged-segment hashes in durable segment order."""
    ordered = [
        (token, damaged)
        for token, damaged in manifest_entries(events_path, blackout_id)
        if damaged is not None
    ]
    ordered.sort(key=lambda item: _segment_order_key(item[0]))
    return tuple(digest for _original, digest in ordered if digest is not None)


def event_total_bytes(
    events_path: Path,
    blackout_id: str,
    *,
    reserved_paths: Collection[str] = (),
) -> int:
    """Bound the aggregate bytes read for one logical event."""
    sources = segment_sources(events_path, blackout_id, reserved_paths=reserved_paths)
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


def ensure_path_within_limit(path: Path) -> None:
    """Bound one segment before reading it in full."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise EventPersistenceError(f"cannot inspect event size: {_bounded_error(exc)}") from exc
    if size > MAX_EVENT_BYTES:
        raise _EventCapacityExceeded("event segment exceeds 64 MiB")


def validate_before_full_read(
    events_path: Path,
    path: Path,
    blackout_id: str | None = None,
    *,
    reserved_paths: Collection[str] = (),
) -> None:
    """Validate both one-file and aggregate bounds before a full read."""
    ensure_path_within_limit(path)
    event_id = blackout_id or _blackout_id_from_path(path)
    event_total_bytes(events_path, event_id, reserved_paths=reserved_paths)


def last_record(path: Path) -> _StoredRecord | None:
    """Read one durable record from the bounded file tail."""
    try:
        if path.stat().st_size == 0:
            return None
    except OSError as exc:
        raise EventPersistenceError(f"cannot inspect event tail: {_bounded_error(exc)}") from exc
    lines = _bounded_tail_lines(path, 1, MAX_LINE_BYTES)
    if not lines:
        return None
    return _decode_record_line(lines[-1])


def read_all_records(events_path: Path, path: Path) -> tuple[_StoredRecord, ...]:
    """Read all complete records from a bounded event segment."""
    validate_before_full_read(events_path, path)
    return tuple(_decode_record_line(line) for line in _iter_complete_lines(path, MAX_LINE_BYTES))


def trusted_prefix(
    events_path: Path,
    path: Path,
    blackout_id: str | None = None,
) -> tuple[_StoredRecord, ...]:
    """Return the longest valid prefix without repairing or rewriting bytes."""
    records: tuple[_StoredRecord, ...] = ()
    try:
        validate_before_full_read(events_path, path, blackout_id)
        with path.open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n") or len(line) > MAX_LINE_BYTES:
                    break
                try:
                    candidate = _decode_record_line(line)
                    records = _deduplicate_and_validate_chain((*records, candidate))
                except _EventCapacityExceeded:
                    raise
                except (EventConflictError, EventCorruptionError):
                    break
    except OSError as exc:
        raise EventPersistenceError(f"cannot read trusted prefix: {_bounded_error(exc)}") from exc
    return records


def trusted_segment_prefixes(
    events_path: Path,
    blackout_id: str,
    *,
    reserved_paths: Collection[str] = (),
) -> tuple[tuple[_StoredRecord, ...], ...]:
    """Read all trusted segment prefixes without write-side tail repair."""
    sources = segment_sources(events_path, blackout_id, reserved_paths=reserved_paths)
    if not sources:
        raise EventCorruptionError("event has no discoverable segment")
    event_total_bytes(events_path, blackout_id, reserved_paths=reserved_paths)
    prefixes: list[tuple[_StoredRecord, ...]] = []
    for path, is_corrupt in sources:
        prefix = (
            trusted_prefix(events_path, path, blackout_id)
            if is_corrupt
            else _deduplicate_and_validate_chain(read_all_records(events_path, path))
        )
        if any(record.blackout_id != blackout_id for record in prefix):
            raise EventConflictError("event reference blackout ID does not match its records")
        prefixes.append(prefix)
    if not any(prefixes):
        raise EventCorruptionError("event contains no trusted record")
    return tuple(prefixes)


def project_event(
    events_path: Path,
    blackout_id: str,
    *,
    reserved_paths: Collection[str] = (),
) -> EventProjection:
    """Project one event entirely from durable bytes and manifests."""
    sources = segment_sources(events_path, blackout_id, reserved_paths=reserved_paths)
    prefixes = trusted_segment_prefixes(
        events_path,
        blackout_id,
        reserved_paths=reserved_paths,
    )
    stored_records = _flatten_records(prefixes)
    start = _first_record(stored_records, "start")
    end = _first_record(stored_records, "end")
    outcome = _first_record(stored_records, "outcome")
    _validate_segmented_record_order(
        prefixes,
        start=start,
        end=end,
        outcome=outcome,
        segment_file_hashes=tuple(event_file_sha256(path) for path, _ in sources),
    )
    public_prefixes = tuple(
        tuple(_projected_record(record) for record in prefix) for prefix in prefixes
    )
    public_records = _flatten_records(public_prefixes)
    return build_projection(blackout_id, public_prefixes, public_records, events_path)


def build_projection(
    blackout_id: str,
    prefixes: tuple[tuple[ProjectedEventRecord, ...], ...],
    records: tuple[ProjectedEventRecord, ...],
    events_path: Path,
) -> EventProjection:
    """Build the public projection from records and damaged-segment receipts."""
    damaged = damaged_hashes(events_path, blackout_id)
    return EventProjection(
        _first_record(records, "start"),
        _records_of_type(records, "observation"),
        _records_of_type(records, "gap"),
        _first_record(records, "end"),
        tuple(
            record
            for record in records
            if record.record_type in DERIVED_RECORD_TYPES and record.record_type != "outcome"
        ),
        _first_record(records, "outcome"),
        prefixes,
        damaged[:16],
        max(0, len(damaged) - 16),
        records,
    )


def owner_approved_event_paths(events_path: Path) -> frozenset[str]:
    """Read active/pending ownership paths from the durable registry only."""
    value = _read_owner_registry(events_path)
    owned: set[str] = set()
    _read_capture_ownership(value["capture"], owned)
    _read_pending_ownership(events_path, value["pending_processing"], owned)
    return frozenset(owned)


def _read_owner_registry(events_path: Path) -> dict[str, Any]:
    registry_path = events_path / "active.json"
    info = _lstat_regular(registry_path, "active registry")
    if info.st_size > MAX_REGISTRY_BYTES:
        raise EventCorruptionError("active registry exceeds 128 KiB")
    try:
        value = _strict_json_loads(registry_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EventCorruptionError("active registry is invalid") from exc
    if not isinstance(value, dict) or set(value) != {"capture", "pending_processing"}:
        raise EventCorruptionError("active registry fields do not match schema")
    return value


def _read_pending_ownership(events_path: Path, value: Any, owned: set[str]) -> None:
    if not isinstance(value, list) or len(value) > MAX_PENDING_PROCESSING:
        raise EventCorruptionError("pending processing registry is invalid or unbounded")
    for item in value:
        _read_pending_item_ownership(events_path, item, owned)


def _read_pending_item_ownership(events_path: Path, value: Any, owned: set[str]) -> None:
    expected = {
        "tag",
        "blackout_id",
        "segment_ids",
        "final_path_token",
        "frozen_stage",
        "last_record_hash",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise EventCorruptionError("processing registry fields do not match schema")
    if value["tag"] != "processing":
        raise EventCorruptionError("processing registry tag is invalid")
    blackout_id = value["blackout_id"]
    _validate_uuid4_hex(blackout_id, "blackout_id")
    _validate_event_token(value["final_path_token"])
    owned.add(value["final_path_token"])
    segment_ids = value["segment_ids"]
    if not isinstance(segment_ids, list) or not segment_ids or len(segment_ids) > MAX_SEGMENT_REFS:
        raise EventCorruptionError("processing segment IDs are invalid or unbounded")
    for segment_id in segment_ids:
        _validate_uuid4_hex(segment_id, "segment_id")
    for path in events_path.iterdir():
        match = EVENT_FILENAME_RE.fullmatch(path.name)
        if match is not None and match.group("blackout") == blackout_id:
            if match.group("segment") in segment_ids:
                owned.add(path.name)


def sealed_event_paths(events_path: Path) -> tuple[Path, ...]:
    """Enumerate sealed event files, excluding paths owned by a live writer."""
    owned = owner_approved_event_paths(events_path)
    paths: list[Path] = []
    for path in sorted(events_path.iterdir(), key=lambda item: item.name):
        if not path.name.startswith("evt-"):
            continue
        if EVENT_FILENAME_RE.fullmatch(path.name) is None:
            raise EventPathError(f"event filename is invalid: {path.name}")
        info = _lstat_regular(path, "event path")
        if path.name in owned:
            continue
        if stat.S_IMODE(info.st_mode) != 0o400:
            raise EventCorruptionError(f"sealed event has unexpected permissions: {path.name}")
        paths.append(path)
    return tuple(paths)


def _read_capture_ownership(value: Any, owned: set[str]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise EventCorruptionError("capture registry value is not an object")
    tag = value.get("tag")
    common = {"tag", "blackout_id", "segment_id", "path_token"}
    expected = common | ({"canonical_start_record_utf8"} if tag == "preparing" else set())
    allowed = (expected, expected | {"event_kind"})
    if set(value) not in allowed or tag not in {"preparing", "capturing"}:
        raise EventCorruptionError("capture registry fields do not match its tag")
    _validate_uuid4_hex(value.get("blackout_id"), "blackout_id")
    _validate_uuid4_hex(value.get("segment_id"), "segment_id")
    _validate_event_token(value.get("path_token"))
    if tag == "preparing":
        frozen = value.get("canonical_start_record_utf8")
        if not isinstance(frozen, str) or len(frozen.encode("utf-8")) > MAX_LINE_BYTES:
            raise EventCorruptionError("preparing start bytes are invalid or unbounded")
    event_kind = value.get("event_kind", "blackout")
    if event_kind not in {"blackout", "recharge"}:
        raise EventCorruptionError("capture event kind is invalid")
    owned.add(value["path_token"])


def _validate_event_token(value: Any) -> None:
    if not isinstance(value, str):
        raise EventCorruptionError("event path token is invalid")
    try:
        _validate_path_token(value)
    except EventPathError as exc:
        raise EventCorruptionError("event path token is invalid") from exc
    if EVENT_FILENAME_RE.fullmatch(value) is None:
        raise EventCorruptionError("event path token is invalid")


def _decode_manifest_line(line: bytes, blackout_id: str) -> tuple[str, str | None]:
    try:
        value = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EventCorruptionError("segment manifest is invalid") from exc
    if not isinstance(value, dict) or set(value) != {"path_token", "damaged_sha256"}:
        raise EventCorruptionError("segment manifest fields are invalid")
    token = value["path_token"]
    damaged = value["damaged_sha256"]
    if not isinstance(token, str) or not _event_belongs_to(token, blackout_id):
        raise EventCorruptionError("segment manifest path is invalid")
    if damaged is not None and (not isinstance(damaged, str) or not _is_sha256(damaged)):
        raise EventCorruptionError("segment manifest damaged hash is invalid")
    return token, damaged


def _event_belongs_to(filename: str, blackout_id: str) -> bool:
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


def _blackout_id_from_path(path: Path) -> str:
    _validate_event_token(path.name)
    match = EVENT_FILENAME_RE.fullmatch(path.name)
    assert match is not None
    return match.group("blackout")


def _lstat_regular(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise EventPersistenceError(f"{label} is missing: {path}") from exc
    except OSError as exc:
        raise EventPersistenceError(f"cannot inspect {label}: {_bounded_error(exc)}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise EventPathError(f"{label} is not a regular file")
    return info
