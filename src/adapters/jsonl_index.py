"""Summary projection encoding and bounded index codecs."""

import hashlib
import stat
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from src.adapters.jsonl_errors import (
    EventConflictError,
    EventCorruptionError,
    EventPathError,
    EventPersistenceError,
    EventValidationError,
    ProjectionUnavailableError,
)
from src.adapters.jsonl_event_catalog import (
    CatalogBatch,
    CatalogEntry,
    CatalogSnapshot,
    JsonlEventCatalog,
)
from src.adapters.jsonl_filesystem import JsonlFilesystem, _file_sha256
from src.adapters.jsonl_health_inventory import JsonlHealthInventory
from src.adapters.jsonl_index_merge import (
    IndexMergeCoordinator,
    IndexMergePaths,
    _output_matches,
    _tail_digest,
)
from src.adapters.jsonl_index_metadata import IndexMetadataPaths, IndexMetadataStore
from src.adapters.jsonl_index_rebuild_support import JsonlIndexRebuildSupport
from src.adapters.jsonl_large_event_cursor import (
    LARGE_EVENT_SCAN_BYTES,
    JsonlLargeEventCursor,
)
from src.adapters.jsonl_record_codec import (
    CURSOR_FIELDS,
    EMPTY_SHA256,
    EVENT_FILENAME_RE,
    MAX_DAMAGED_HASHES,
    MAX_INDEX_LINE_BYTES,
    MAX_INDEX_TAIL,
    REBUILD_MAX_BYTES_PER_TICK,
    REBUILD_MAX_FILES_PER_TICK,
    REBUILD_MAX_WALL_SECONDS,
    SCHEMA_VERSION,
    _is_sha256,
    _optional_bool,
    _optional_finite_nonnegative,
    _optional_short_string,
    _parse_utc,
    _required_short_string,
    _validate_path_token,
    _validate_uuid4_hex,
)
from src.adapters.jsonl_report_outbox import JsonlReportOutbox, ReportNotice
from src.adapters.jsonl_summary_codec import (
    _bounded_epoch_summaries,
    _bounded_tail_lines,
    _decode_summary_line,
    _encode_summary,
)
from src.adapters.jsonl_summary_locator import (
    JsonlSummaryLocatorStore,
    locator_from_projection,
)
from src.application.storage_values import (
    EpochIndexScan,
    EpochIndexTail,
    EventProjection,
    EventRef,
    EventSummary,
)

if TYPE_CHECKING:
    from src.adapters.jsonl_event_stream import JsonlEventStream
    from src.adapters.jsonl_work_registry import JsonlWorkRegistry


_CATALOG_CURSOR_FIELDS = frozenset(
    {
        "schema_version",
        "generation_id",
        "target_offset",
        "target_count",
        "offset",
        "next_seq",
        "previous_entry_sha256",
        "complete",
    }
)
_REBUILD_PHASES = frozenset({"project", "merge", "verify", "prepared"})


def _cursor_has_merge_progress(cursor: Mapping[str, Any]) -> bool:
    return any(
        cursor[key]
        for key in (
            "merge_rebuild_offset",
            "merge_delta_offset",
            "merge_delta_target_offset",
            "merge_output_offset",
            "merge_verify_offset",
        )
    ) or any(cursor[key] != EMPTY_SHA256 for key in ("merge_output_sha256", "merge_verify_sha256"))


def _new_catalog_cursor(generation_id: str, snapshot: "CatalogSnapshot") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generation_id": generation_id,
        "target_offset": snapshot.byte_offset,
        "target_count": snapshot.entry_count,
        "offset": 0,
        "next_seq": 0,
        "previous_entry_sha256": EMPTY_SHA256,
        "complete": snapshot.entry_count == 0,
    }


def _validate_catalog_cursor(cursor: Mapping[str, Any]) -> None:
    if set(cursor) != _CATALOG_CURSOR_FIELDS or cursor.get("schema_version") != 1:
        raise EventCorruptionError("index rebuild catalog cursor fields do not match schema")
    _validate_uuid4_hex(cursor.get("generation_id"), "generation_id")
    for key in ("target_offset", "target_count", "offset", "next_seq"):
        value = cursor.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EventCorruptionError(f"catalog cursor {key} is invalid")
    if cursor["offset"] > cursor["target_offset"] or cursor["next_seq"] > cursor["target_count"]:
        raise EventCorruptionError("catalog cursor advanced beyond its target")
    if not _is_sha256(cursor.get("previous_entry_sha256")):
        raise EventCorruptionError("catalog cursor previous hash is invalid")
    if not isinstance(cursor.get("complete"), bool):
        raise EventCorruptionError("catalog cursor completion flag is invalid")
    if cursor["complete"] != (cursor["offset"] == cursor["target_offset"]):
        raise EventCorruptionError("catalog cursor completion disagrees with its offset")


def _catalog_cursor_after(
    cursor: Mapping[str, Any], batch: CatalogBatch, consumed: int
) -> dict[str, Any]:
    if not 1 <= consumed <= len(batch.entries):
        raise ValueError("catalog cursor must consume one bounded entry")
    return {
        **cursor,
        "offset": batch.entry_offsets[consumed - 1],
        "next_seq": cursor["next_seq"] + consumed,
        "previous_entry_sha256": batch.entries[consumed - 1].entry_sha256,
        "complete": batch.entry_offsets[consumed - 1] == cursor["target_offset"],
    }


def _rebuild_output_matches(path: Path, cursor: Mapping[str, Any]) -> bool:
    return _output_matches(
        path,
        offset=cursor["rebuild_output_offset"],
        digest=cursor["rebuild_output_sha256"],
    )


def _blackout_id_from_event_filename(filename: str) -> str:
    _validate_path_token(filename)
    match = EVENT_FILENAME_RE.fullmatch(filename)
    if match is None:
        raise EventPathError("event filename does not match the version 2 layout")
    blackout_id = match.group("blackout")
    _validate_uuid4_hex(blackout_id, "blackout_id")
    return blackout_id


def _validate_rebuild_cursor(cursor: Mapping[str, Any]) -> None:
    if set(cursor) != CURSOR_FIELDS or cursor.get("schema_version") != 1:
        raise EventCorruptionError("index rebuild cursor fields do not match schema")
    phase = cursor.get("phase")
    if phase not in _REBUILD_PHASES:
        raise EventCorruptionError("index rebuild cursor phase is invalid")
    _validate_uuid4_hex(cursor.get("generation_id"), "generation_id")
    _validate_rebuild_cursor_filenames(cursor)
    _validate_rebuild_cursor_progress(cursor)
    _parse_utc(cursor.get("last_progress_utc"))


def _validate_rebuild_cursor_filenames(cursor: Mapping[str, Any]) -> None:
    target_last = cursor.get("target_last_filename")
    last_projected = cursor.get("last_projected_filename")
    if target_last is not None:
        if not isinstance(target_last, str):
            raise EventCorruptionError("rebuild target-last filename is invalid")
        _validate_path_token(target_last)
    if last_projected is not None:
        if not isinstance(last_projected, str):
            raise EventCorruptionError("rebuild last-projected filename is invalid")
        _validate_path_token(last_projected)
    last_hash = cursor.get("last_projected_sha256")
    if (last_projected is None) != (last_hash is None):
        raise EventCorruptionError("rebuild last filename/hash must be present together")
    if last_hash is not None and not _is_sha256(last_hash):
        raise EventCorruptionError("rebuild last projected SHA-256 is invalid")


def _validate_rebuild_cursor_progress(cursor: Mapping[str, Any]) -> None:
    _validate_rebuild_cursor_numeric_progress(cursor)
    _validate_rebuild_cursor_file_progress(cursor)
    _validate_rebuild_cursor_merge_progress(cursor)


def _validate_rebuild_cursor_numeric_progress(cursor: Mapping[str, Any]) -> None:
    output_hash = cursor.get("rebuild_output_sha256")
    if not _is_sha256(output_hash):
        raise EventCorruptionError("rebuild output SHA-256 is invalid")
    for key in (
        "rebuild_output_offset",
        "files_done",
        "target_count",
        "merge_rebuild_offset",
        "merge_delta_offset",
        "merge_delta_target_offset",
        "merge_output_offset",
        "merge_verify_offset",
    ):
        value = cursor.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EventCorruptionError(f"rebuild cursor {key} is invalid")


def _validate_rebuild_cursor_file_progress(cursor: Mapping[str, Any]) -> None:
    last_projected = cursor.get("last_projected_filename")
    files_done = cursor["files_done"]
    target_count = cursor["target_count"]
    if files_done > target_count:
        raise EventCorruptionError("rebuild files-done exceeds target count")
    if (files_done == 0) != (last_projected is None):
        raise EventCorruptionError("rebuild cursor progress fields disagree")


def _validate_rebuild_cursor_merge_progress(cursor: Mapping[str, Any]) -> None:
    _validate_merge_digests(cursor)
    _validate_merge_phase_shape(cursor)
    _validate_merge_verification(cursor)


def _validate_merge_digests(cursor: Mapping[str, Any]) -> None:
    if not _is_sha256(cursor.get("merge_output_sha256")):
        raise EventCorruptionError("rebuild merge output SHA-256 is invalid")
    if not _is_sha256(cursor.get("merge_verify_sha256")):
        raise EventCorruptionError("rebuild merge verification SHA-256 is invalid")


def _validate_merge_phase_shape(cursor: Mapping[str, Any]) -> None:
    phase = cursor["phase"]
    if phase == "project" and _cursor_has_merge_progress(cursor):
        raise EventCorruptionError("project cursor contains merge progress")
    if phase == "prepared" and cursor["merge_delta_target_offset"] < cursor["merge_delta_offset"]:
        raise EventCorruptionError("prepared merge cursor delta target moved backwards")


def _validate_merge_verification(cursor: Mapping[str, Any]) -> None:
    phase = cursor["phase"]
    if cursor["merge_verify_offset"] > cursor["merge_output_offset"]:
        raise EventCorruptionError("merge verification cursor exceeds output offset")
    if phase in {"project", "merge"} and cursor["merge_verify_offset"] != 0:
        raise EventCorruptionError("merge verification progressed before verify phase")
    if phase == "project" and cursor["merge_verify_sha256"] != EMPTY_SHA256:
        raise EventCorruptionError("project cursor contains merge verification state")
    if phase == "prepared" and (
        cursor["merge_verify_offset"] != cursor["merge_output_offset"]
        or cursor["merge_verify_sha256"] != cursor["merge_output_sha256"]
    ):
        raise EventCorruptionError("prepared cursor is not fully verified")


def _validate_rebuild_tick_bounds(*, max_files: int, max_bytes: int) -> None:
    if not 1 <= max_files <= REBUILD_MAX_FILES_PER_TICK:
        raise ValueError(f"max_files must be between 1 and {REBUILD_MAX_FILES_PER_TICK}")
    if not 1 <= max_bytes <= REBUILD_MAX_BYTES_PER_TICK:
        raise ValueError(f"max_bytes must be between 1 and {REBUILD_MAX_BYTES_PER_TICK}")


def _validate_rebuild_wall_bound(max_wall_s: float) -> None:
    if not 0.0 < max_wall_s <= REBUILD_MAX_WALL_SECONDS:
        raise ValueError(f"max_wall_s must be between 0 and {REBUILD_MAX_WALL_SECONDS} seconds")


@dataclass(frozen=True, slots=True)
class JsonlIndexPaths:
    """Immutable filesystem and clock dependencies owned by one index."""

    events_path: Path
    index_path: Path
    cursor_path: Path
    rebuild_path: Path
    merge_path: Path
    delta_path: Path
    catalog_cursor_path: Path
    wall_clock: Callable[[], str]


class JsonlIndex(JsonlIndexRebuildSupport):
    """Cohesive Index lane used by the transactional facade."""

    def __init__(
        self,
        paths: "JsonlIndexPaths",
        filesystem: JsonlFilesystem,
        stream: Callable[[], "JsonlEventStream"],
        registry: Callable[[], "JsonlWorkRegistry"],
        catalog: JsonlEventCatalog,
    ) -> None:
        self._events_path = paths.events_path
        self._index_path = paths.index_path
        self._cursor_path = paths.cursor_path
        self._rebuild_path = paths.rebuild_path
        self._merge_path = paths.merge_path
        self._delta_path = paths.delta_path
        self._catalog_cursor_path = paths.catalog_cursor_path
        self._wall_clock = paths.wall_clock
        self._filesystem = filesystem
        self._stream = stream
        self._registry = registry
        self._catalog = catalog
        self._locator_store = JsonlSummaryLocatorStore(self._events_path, filesystem)
        self._report_outbox = JsonlReportOutbox(self._events_path, filesystem)
        self._large_event_cursor = JsonlLargeEventCursor(self._events_path, filesystem)
        self._metadata = IndexMetadataStore(
            IndexMetadataPaths(
                events_path=self._events_path,
                index_path=self._index_path,
                cursor_path=self._cursor_path,
                rebuild_path=self._rebuild_path,
                merge_path=self._merge_path,
                delta_path=self._delta_path,
                catalog_cursor_path=self._catalog_cursor_path,
            ),
            filesystem,
            catalog,
            validate_rebuild_cursor=_validate_rebuild_cursor,
            validate_catalog_cursor=_validate_catalog_cursor,
        )
        self._health_inventory = JsonlHealthInventory(catalog, filesystem)
        self._merge = IndexMergeCoordinator(
            filesystem,
            self,
            IndexMergePaths(
                events_path=self._events_path,
                index_path=self._index_path,
                rebuild_path=self._rebuild_path,
                merge_path=self._merge_path,
                delta_path=self._delta_path,
            ),
            self._wall_clock,
        )

    def _health_inventory_tick(self) -> tuple[tuple[str, ...], bool]:
        return self._health_inventory._tick()

    def _health_inventory_stats(self) -> tuple[int, int]:
        return self._health_inventory._stats()

    def _events_directory(self) -> Path:
        return self._events_path

    def _wall_time_utc(self) -> str:
        return self._wall_clock()

    def index_tail(self, limit: int) -> tuple[EventSummary, ...]:
        """Return a bounded, newest-first-independent tail of fixed summaries."""
        if limit < 0 or limit > MAX_INDEX_TAIL:
            raise ValueError(f"limit must be between 0 and {MAX_INDEX_TAIL}")
        if limit == 0:
            return ()
        if not self._index_path.exists():
            if self._catalog.snapshot().entry_count == 0:
                return ()
            raise ProjectionUnavailableError("summary index is unavailable")
        lines = _bounded_tail_lines(self._index_path, limit, MAX_INDEX_LINE_BYTES)
        return tuple(_decode_summary_line(line) for line in lines)

    def index_tail_for_epoch(self, battery_epoch_id: str, limit: int) -> EpochIndexTail:
        if limit < 0 or limit > MAX_INDEX_TAIL:
            raise ValueError(f"limit must be between 0 and {MAX_INDEX_TAIL}")
        if limit == 0:
            return EpochIndexTail((), 0, True)
        summaries, scan_complete = self._epoch_summaries(battery_epoch_id)
        selected = summaries[-limit:]
        return EpochIndexTail(selected, max(0, len(summaries) - limit), scan_complete)

    def index_scan_for_decline_epoch(self, battery_epoch_id: str) -> EpochIndexScan:
        summaries, scan_complete = self._epoch_summaries(battery_epoch_id)
        return EpochIndexScan(summaries, scan_complete)

    def report_outbox_pending(self, limit: int) -> tuple[ReportNotice, ...]:
        """Return the bounded durable report notices through the index facade."""
        return self._report_outbox.pending(limit=limit)

    def report_outbox_head(self) -> ReportNotice | None:
        """Return the exact next report notice through the index facade."""
        return self._report_outbox.head()

    def acknowledge_report_notice(self, notice: ReportNotice) -> None:
        """Acknowledge one exact FIFO report notice through the index facade."""
        self._report_outbox.acknowledge(notice)

    def _epoch_summaries(self, battery_epoch_id: str) -> tuple[tuple[EventSummary, ...], bool]:
        if not battery_epoch_id or len(battery_epoch_id.encode("utf-8")) > 128:
            raise ValueError("battery_epoch_id must be a non-empty bounded string")
        if not self._index_path.exists():
            if self._catalog.snapshot().entry_count == 0:
                return (), True
            raise ProjectionUnavailableError("summary index is unavailable")
        return _bounded_epoch_summaries(
            self._index_path,
            battery_epoch_id,
        )

    def _begin_index_rebuild(self) -> str:
        """Snapshot the catalog and start one bounded projection generation."""
        cursor = self._read_cursor_if_present()
        if cursor is not None:
            return cursor["generation_id"]
        generation_id = uuid.uuid4().hex
        snapshot = self._catalog.snapshot()
        self._unlink_projection_file(self._merge_path)
        self._unlink_projection_file(self._delta_path)
        self._filesystem.atomic_replace(self._rebuild_path, b"", mode=0o600)
        new_cursor = {
            "schema_version": 1,
            "phase": "project",
            "generation_id": generation_id,
            "target_last_filename": None,
            "last_projected_filename": None,
            "last_projected_sha256": None,
            "rebuild_output_offset": 0,
            "rebuild_output_sha256": EMPTY_SHA256,
            "files_done": 0,
            "target_count": snapshot.entry_count,
            "merge_rebuild_offset": 0,
            "merge_delta_offset": 0,
            "merge_delta_target_offset": 0,
            "merge_output_offset": 0,
            "merge_output_sha256": EMPTY_SHA256,
            "merge_verify_offset": 0,
            "merge_verify_sha256": EMPTY_SHA256,
            "last_progress_utc": self._wall_clock(),
        }
        self._write_catalog_cursor(_new_catalog_cursor(generation_id, snapshot))
        self._write_rebuild_cursor(new_cursor)
        return generation_id

    def _rebuild_index_tick(
        self,
        *,
        max_files: int = REBUILD_MAX_FILES_PER_TICK,
        max_bytes: int = REBUILD_MAX_BYTES_PER_TICK,
        max_wall_s: float = REBUILD_MAX_WALL_SECONDS,
    ) -> bool:
        """Project at most the supplied bounded work; return whether promotion is ready."""
        _validate_rebuild_tick_bounds(max_files=max_files, max_bytes=max_bytes)
        _validate_rebuild_wall_bound(max_wall_s)
        deadline = time.monotonic() + max_wall_s
        cursor = self._active_rebuild_cursor()
        if cursor is None:
            return False
        if cursor["phase"] == "prepared":
            return True
        catalog_cursor = self._read_catalog_cursor()
        if catalog_cursor["generation_id"] != cursor["generation_id"]:
            cursor = self._restart_rebuild_generation()
            catalog_cursor = self._read_catalog_cursor()
        elif catalog_cursor["next_seq"] < cursor["files_done"]:
            cursor = self._restart_rebuild_generation()
            catalog_cursor = self._read_catalog_cursor()
        if cursor["phase"] == "project":
            if not catalog_cursor["complete"]:
                complete = self._project_rebuild_tick(
                    cursor,
                    catalog_cursor,
                    max_files=max_files,
                    max_bytes=max_bytes,
                    deadline=deadline,
                )
                if not complete:
                    return False
                cursor = self._read_cursor_if_present()
                if cursor is None:
                    raise EventPersistenceError("rebuild cursor disappeared after projection")
            if cursor["files_done"] != cursor["target_count"]:
                raise ProjectionUnavailableError("catalog completed before all rebuild files")
            cursor = self._begin_merge(cursor)
        return self._merge_rebuild_tick(
            cursor,
            max_files=max_files,
            max_bytes=max_bytes,
            deadline=deadline,
        )

    def _active_rebuild_cursor(self) -> Mapping[str, Any] | None:
        cursor = self._read_cursor_if_present()
        if cursor is None and self._index_available():
            self._clear_orphan_rebuild_metadata()
            return None
        if cursor is None:
            self._begin_index_rebuild()
            cursor = self._read_cursor_if_present()
        if cursor is None:
            raise EventPersistenceError("index rebuild cursor was not created")
        return (
            cursor if self._cursor_matches_rebuild(cursor) else self._restart_rebuild_generation()
        )

    def _project_catalog_entry(
        self,
        entry: CatalogEntry,
        *,
        processed_bytes: int,
        max_bytes: int,
    ) -> tuple[bool, int]:
        path = self._filesystem._event_path(entry.path_token)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return True, 0
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise EventPathError(f"event path is not a regular file: {path.name}")
        size = info.st_size
        if stat.S_IMODE(info.st_mode) != 0o400:
            return True, 0
        if size > min(max_bytes, LARGE_EVENT_SCAN_BYTES):
            if self._advance_large_event(path, size, max_bytes=max_bytes):
                return False, -1
        if processed_bytes + size > max_bytes:
            if processed_bytes:
                return False, 0
            raise ProjectionUnavailableError("rebuild target exceeds byte bound")
        projection = self._stream().project(
            EventRef(_blackout_id_from_event_filename(entry.path_token), entry.path_token)
        )
        if projection.outcome is None:
            raise ProjectionUnavailableError(f"rebuild target is not terminal: {entry.path_token}")
        summary = self._summary_for(entry.path_token, projection)
        self._write_or_verify_locator(summary, projection, entry.catalog_seq)
        self._metadata._append_summary(self._rebuild_path, summary, _encode_summary(summary))
        return True, size

    def _advance_large_event(self, path: Path, size: int, *, max_bytes: int) -> bool:
        cursor = self._large_event_cursor.read()
        if cursor is not None:
            if cursor.path_token == path.name and not self._large_event_cursor.matches(
                path, cursor
            ):
                raise EventCorruptionError("large event changed during bounded rebuild")
            if cursor.path_token != path.name:
                self._large_event_cursor.clear()
                cursor = None
        offset = cursor.offset if cursor is not None else 0
        if offset >= size:
            self._large_event_cursor.clear()
            return False
        self._large_event_cursor.advance(
            path,
            offset=offset,
            budget=min(max_bytes, LARGE_EVENT_SCAN_BYTES),
        )
        return True

    def _project_rebuild_tick(
        self,
        cursor: Mapping[str, Any],
        catalog_cursor: Mapping[str, Any],
        *,
        max_files: int,
        max_bytes: int,
        deadline: float,
    ) -> bool:
        batch = self._catalog.read_batch(
            byte_offset=catalog_cursor["offset"],
            target_offset=catalog_cursor["target_offset"],
            expected_seq=catalog_cursor["next_seq"],
            previous_entry_sha256=catalog_cursor["previous_entry_sha256"],
            max_files=max_files,
        )
        if not batch.entries:
            return True
        processed_bytes = 0
        updated = dict(cursor)
        consumed = 0
        for entry in batch.entries:
            if time.monotonic() >= deadline and consumed:
                break
            accepted, size = self._project_catalog_entry(
                entry,
                processed_bytes=processed_bytes,
                max_bytes=max_bytes,
            )
            if size == -1:
                updated["last_progress_utc"] = self._wall_clock()
                self._write_rebuild_cursor(updated)
                return False
            if not accepted:
                break
            processed_bytes += size
            consumed += 1
            updated["files_done"] += 1
            updated["last_projected_filename"] = entry.path_token
            path = self._filesystem._event_path(entry.path_token)
            updated["last_projected_sha256"] = _file_sha256(path) if path.exists() else EMPTY_SHA256
            if consumed >= max_files:
                break
        if consumed == 0:
            raise ProjectionUnavailableError("rebuild tick cannot make progress within byte bound")
        updated["rebuild_output_offset"] = self._rebuild_path.stat().st_size
        updated["rebuild_output_sha256"] = _tail_digest(self._rebuild_path)
        updated["last_progress_utc"] = self._wall_clock()
        self._write_rebuild_cursor(updated)
        self._filesystem._trip("after_rebuild_cursor")
        self._write_catalog_cursor(_catalog_cursor_after(catalog_cursor, batch, consumed))
        return updated["files_done"] == updated["target_count"]

    def _begin_merge(self, cursor: Mapping[str, Any]) -> Mapping[str, Any]:
        """Create an empty merge target and durably enter the merge phase."""
        if not _rebuild_output_matches(self._rebuild_path, cursor):
            raise ProjectionUnavailableError("rebuild output changed before merge")
        return self._merge.begin(cursor)

    def _merge_rebuild_tick(
        self,
        cursor: Mapping[str, Any],
        *,
        max_files: int,
        max_bytes: int,
        deadline: float,
    ) -> bool:
        """Merge two ordered summary streams with bounded durable progress."""
        return self._merge.tick(
            cursor,
            max_files=max_files,
            max_bytes=max_bytes,
            deadline=deadline,
        )

    def _promote_index_rebuild(self) -> None:
        """Atomically promote a prepared bounded merge and clear repair state."""
        self._merge.promote()
        self._metadata._rebuild_index_head()

    def _summary_for(self, path_token: str, projection: EventProjection) -> EventSummary:
        start = projection.start
        outcome = projection.outcome
        if start is None or outcome is None:
            raise EventValidationError("summary requires start and outcome records")
        end = projection.end
        outcome_payload = outcome.payload
        start_payload = start.payload
        disposition = _required_short_string(outcome_payload, "disposition")
        comparison_mode: Literal["full", "short_window", "none"] = "none"
        raw_comparison_mode = _optional_short_string(outcome_payload, "comparison_mode")
        if raw_comparison_mode == "full":
            comparison_mode = "full"
        elif raw_comparison_mode == "short_window":
            comparison_mode = "short_window"
        elif raw_comparison_mode not in {None, "none"}:
            raise EventValidationError("invalid comparison mode in outcome")
        evidence_class = _optional_short_string(outcome_payload, "evidence_class")
        damaged = self._stream()._damaged_hashes(start.blackout_id)
        summary = EventSummary(
            schema_version=SCHEMA_VERSION,
            blackout_id=start.blackout_id,
            segment_filename=path_token,
            started_utc=start.wall_time_utc,
            ended_utc=end.wall_time_utc if end is not None else outcome.wall_time_utc,
            termination=(
                _optional_short_string(end.payload, "termination") if end is not None else None
            ),
            evidence_class=evidence_class,
            disposition=disposition,
            duration_s=_optional_finite_nonnegative(outcome_payload, "duration_s"),
            observation_count=len(projection.observations),
            battery_epoch_id=_optional_short_string(start_payload, "battery_epoch_id"),
            comparison_available=_optional_bool(outcome_payload, "comparison_available", False),
            comparison_mode=comparison_mode,
            ir_estimate_available=_optional_bool(outcome_payload, "ir_estimate_available", False),
            commit_receipt_id=_optional_short_string(outcome_payload, "commit_receipt_id"),
            damaged_segment_hashes=damaged[:MAX_DAMAGED_HASHES],
            damaged_segment_overflow=max(0, len(damaged) - MAX_DAMAGED_HASHES),
            outcome_record_sha256=outcome.record_sha256,
            event_file_sha256=_file_sha256(self._filesystem._event_path(path_token)),
        )
        _encode_summary(summary)
        return summary

    def _append_summary_idempotent(self, summary: EventSummary) -> None:
        destination = self._projection_destination()
        line = _encode_summary(summary)
        if destination == self._index_path and destination.exists():
            try:
                locator = self._locator_store.read(summary.blackout_id)
            except EventCorruptionError:
                locator = None
            if locator is not None:
                self._locator_store.verify_segments(locator)
                head = self._metadata._read_index_head()
                catalog_count = self._catalog.snapshot().entry_count
                if locator.summary_line != line:
                    raise EventConflictError("summary locator conflicts with summary bytes")
                if head["count"] >= catalog_count:
                    return
        self._metadata._append_summary(destination, summary, line)

    def _commit_summary(self, summary: EventSummary, projection: EventProjection) -> None:
        """Commit locator, summary, head/outbox receipts as one retryable lane."""
        line = _encode_summary(summary)
        snapshot = self._catalog.snapshot()
        terminal_seq = max(0, snapshot.entry_count - 1)
        locator_hash = self._write_or_verify_locator(summary, projection, terminal_seq)
        destination = self._projection_destination()
        offset = destination.stat().st_size if destination.exists() else 0
        cursor = self._read_cursor_if_present()
        intent = {
            "schema_version": 1,
            "destination": destination.name,
            "generation": cursor["generation_id"] if cursor is not None else "none",
            "offset": offset,
            "summary_line": line.decode("utf-8"),
            "summary_sha256": hashlib.sha256(line).hexdigest(),
            "outbox_identity": [summary.blackout_id, summary.segment_filename, locator_hash],
            "phase": "prepared",
        }
        self._metadata._write_append_intent(intent)
        self._append_summary_idempotent(summary)
        self._metadata._write_append_intent({**intent, "phase": "summary_durable"})
        self._report_outbox.append(
            blackout_id=summary.blackout_id,
            segment_filename=summary.segment_filename,
            locator_sha256=locator_hash,
            index_head_sha256=self._index_head_hash(),
        )
        self._metadata._clear_append_intent()

    def _write_or_verify_locator(
        self,
        summary: EventSummary,
        projection: EventProjection,
        terminal_seq: int,
    ) -> str:
        """Persist one immutable logical-event root or verify its exact retry."""
        line = _encode_summary(summary)
        sources = self._stream()._capacity.segment_sources(summary.blackout_id)
        locator = locator_from_projection(
            events_path=self._events_path,
            final_path_token=summary.segment_filename,
            outcome_record_sha256=summary.outcome_record_sha256,
            summary_line=line,
            terminal_catalog_seq=terminal_seq,
            projection=projection,
            segment_sources=sources,
        )
        locator_hash = self._locator_store.write(locator)
        self._locator_store.verify_segments(locator)
        return locator_hash

    def _index_head_hash(self) -> str:
        """Return the durable head digest used to bind an outbox notice."""
        head = self._metadata._read_index_head()
        return head.get("cumulative_sha256", EMPTY_SHA256)

    def _recover_append_intent(self) -> None:
        """Converge an append interrupted after any durable boundary."""
        intent = self._metadata._read_append_intent()
        if intent is None:
            return
        destination = self._events_path / intent["destination"]
        if destination not in {self._index_path, self._delta_path}:
            raise EventPathError("index append intent destination is outside the index lane")
        line = self._metadata._recover_intent_line(destination, intent)
        self._metadata._update_index_head(line, destination=destination)
        identity = self._metadata._intent_outbox_identity(intent)
        self._report_outbox.append(
            blackout_id=identity[0],
            segment_filename=identity[1],
            locator_sha256=identity[2],
            index_head_sha256=self._index_head_hash(),
        )
        self._metadata._clear_append_intent()
