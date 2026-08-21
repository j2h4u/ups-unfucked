"""Bounded, resumable merge and promotion for the JSONL summary index."""

import hashlib
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from src.adapters.jsonl_errors import (
    EventConflictError,
    EventCorruptionError,
    EventPersistenceError,
    ProjectionUnavailableError,
)
from src.adapters.jsonl_filesystem import JsonlFilesystem
from src.adapters.jsonl_record_codec import EMPTY_SHA256, MAX_INDEX_LINE_BYTES, _bounded_error
from src.adapters.jsonl_summary_codec import (
    _bounded_tail_lines,
    _decode_summary_line,
    _summary_key,
)
from src.application.storage_values import EventSummary


class RebuildCursorHost(Protocol):
    """Small persistence boundary required by the merge lane."""

    def _read_cursor_if_present(self) -> Mapping[str, Any] | None: ...

    def _write_rebuild_cursor(self, cursor: Mapping[str, Any]) -> None: ...

    def _clear_rebuild_metadata(self) -> None: ...

    def _unlink_projection_file(self, path: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class IndexMergePaths:
    """Immutable projection paths owned by one index rebuild."""

    events_path: Path
    index_path: Path
    rebuild_path: Path
    merge_path: Path
    delta_path: Path


@dataclass(frozen=True, slots=True)
class _MergeBudget:
    max_files: int
    max_bytes: int
    deadline: float | None


class IndexMergeCoordinator:
    """Merge two ordered summary streams within cooperative tick budgets."""

    def __init__(
        self,
        filesystem: JsonlFilesystem,
        host: RebuildCursorHost,
        paths: IndexMergePaths,
        wall_clock: Callable[[], str],
    ) -> None:
        self._filesystem = filesystem
        self._host = host
        self._events_path = paths.events_path
        self._index_path = paths.index_path
        self._rebuild_path = paths.rebuild_path
        self._merge_path = paths.merge_path
        self._delta_path = paths.delta_path
        self._wall_clock = wall_clock

    def begin(self, cursor: Mapping[str, Any]) -> Mapping[str, Any]:
        """Create an empty merge target and durably enter the merge phase."""
        if cursor["phase"] != "project":
            return cursor
        self._sort_inputs()
        self._host._unlink_projection_file(self._merge_path)
        self._filesystem.atomic_replace(self._merge_path, b"", mode=0o600)
        merged = {
            **cursor,
            "phase": "merge",
            "merge_rebuild_offset": 0,
            "merge_delta_offset": 0,
            "merge_delta_target_offset": 0,
            "merge_output_offset": 0,
            "merge_output_sha256": EMPTY_SHA256,
            "merge_verify_offset": 0,
            "merge_verify_sha256": EMPTY_SHA256,
            "last_progress_utc": self._wall_clock(),
        }
        self._host._write_rebuild_cursor(merged)
        self._filesystem._trip("after_rebuild_merge_cursor")
        return merged

    def _sort_inputs(self) -> None:
        """Normalize both rebuild inputs before any merge cursor is exposed."""
        if self._rebuild_path.exists():
            _sort_summary_file(self._rebuild_path, self._filesystem)
        if self._delta_path.exists() and self._delta_path.stat().st_size:
            _sort_summary_file(self._delta_path, self._filesystem)

    def tick(
        self,
        cursor: Mapping[str, Any],
        *,
        max_files: int,
        max_bytes: int,
        deadline: float | None = None,
    ) -> bool:
        """Copy at most the supplied number of summary records and bytes."""
        if cursor["phase"] == "prepared":
            return True
        budget = _MergeBudget(max_files, max_bytes, deadline)
        cursor = self._repair_output(cursor)
        if cursor["phase"] == "verify":
            return self._verify_output_tick(cursor, budget=budget)
        return self._merge_tick(
            cursor,
            budget=budget,
        )

    def _merge_tick(
        self,
        cursor: Mapping[str, Any],
        *,
        budget: _MergeBudget,
    ) -> bool:
        processed_files = 0
        processed_bytes = 0
        while processed_files < budget.max_files and (
            budget.deadline is None or time.monotonic() < budget.deadline or not processed_files
        ):
            base = self._summary_at(self._rebuild_path, cursor["merge_rebuild_offset"])
            delta = self._summary_at(self._delta_path, cursor["merge_delta_offset"])
            if base is None and delta is None:
                return self._start_output_verification(
                    cursor,
                    processed_files=processed_files,
                    processed_bytes=processed_bytes,
                    budget=budget,
                )
            selected, rebuild_offset, delta_offset = _select_merge_record(base, delta)
            if base is None:
                rebuild_offset = cursor["merge_rebuild_offset"]
            elif rebuild_offset == 0:
                rebuild_offset = cursor["merge_rebuild_offset"]
            if delta is None:
                delta_offset = cursor["merge_delta_offset"]
            elif delta_offset == 0:
                delta_offset = cursor["merge_delta_offset"]
            summary, line = selected
            if processed_bytes + len(line) > budget.max_bytes:
                if processed_files:
                    return False
                raise ProjectionUnavailableError("index merge record exceeds byte bound")
            self._assert_order(summary, cursor)
            fd = self._filesystem._open_append_or_create(self._merge_path, mode=0o600)
            try:
                self._filesystem._append_and_sync_fd(fd, line)
            finally:
                os.close(fd)
            self._filesystem._trip("after_rebuild_merge_append")
            cursor = {
                **cursor,
                "merge_rebuild_offset": rebuild_offset,
                "merge_delta_offset": delta_offset,
                "merge_output_offset": cursor["merge_output_offset"] + len(line),
                "merge_output_sha256": _chain_digest(cursor["merge_output_sha256"], line),
                "last_progress_utc": self._wall_clock(),
            }
            self._host._write_rebuild_cursor(cursor)
            self._filesystem._trip("after_rebuild_merge_cursor")
            processed_files += 1
            processed_bytes += len(line)
        return False

    def _start_output_verification(
        self,
        cursor: Mapping[str, Any],
        *,
        processed_files: int,
        processed_bytes: int,
        budget: _MergeBudget,
    ) -> bool:
        verifying = {
            **cursor,
            "phase": "verify",
            "merge_delta_target_offset": self._path_size(self._delta_path),
            "merge_verify_offset": 0,
            "merge_verify_sha256": EMPTY_SHA256,
            "last_progress_utc": self._wall_clock(),
        }
        self._host._write_rebuild_cursor(verifying)
        self._filesystem._trip("after_rebuild_merge_verify_started")
        remaining_files = budget.max_files - processed_files
        remaining_bytes = budget.max_bytes - processed_bytes
        if remaining_files <= 0 or remaining_bytes <= 0:
            return False
        remaining_budget = _MergeBudget(remaining_files, remaining_bytes, budget.deadline)
        return self._verify_output_tick(
            verifying,
            budget=remaining_budget,
        )

    def _verify_output_tick(
        self,
        cursor: Mapping[str, Any],
        *,
        budget: _MergeBudget,
    ) -> bool:
        """Re-read the merge output cooperatively before exposing it."""
        processed_files = 0
        processed_bytes = 0
        verify_offset = cursor["merge_verify_offset"]
        verify_digest = cursor["merge_verify_sha256"]
        while processed_files < budget.max_files and (
            budget.deadline is None or time.monotonic() < budget.deadline or not processed_files
        ):
            item = self._summary_at(self._merge_path, verify_offset)
            if item is None:
                if verify_offset != cursor["merge_output_offset"]:
                    raise EventCorruptionError("merge verification ended before output cursor")
                if verify_digest != cursor["merge_output_sha256"]:
                    raise ProjectionUnavailableError("merge output cumulative digest mismatch")
                self._seal_verified_output()
                prepared = {
                    **cursor,
                    "phase": "prepared",
                    "merge_verify_offset": verify_offset,
                    "merge_verify_sha256": verify_digest,
                    "last_progress_utc": self._wall_clock(),
                }
                self._host._write_rebuild_cursor(prepared)
                self._filesystem._trip("after_rebuild_merge_prepared")
                return True
            (_summary, line), next_offset = item
            if processed_bytes + len(line) > budget.max_bytes:
                if processed_files:
                    break
                raise ProjectionUnavailableError(
                    "index merge verification record exceeds byte bound"
                )
            verify_offset = next_offset
            verify_digest = _chain_digest(verify_digest, line)
            processed_files += 1
            processed_bytes += len(line)
        updated = {
            **cursor,
            "merge_verify_offset": verify_offset,
            "merge_verify_sha256": verify_digest,
            "last_progress_utc": self._wall_clock(),
        }
        self._host._write_rebuild_cursor(updated)
        self._filesystem._trip("after_rebuild_merge_verify_cursor")
        return False

    def _seal_verified_output(self) -> None:
        try:
            fd = self._filesystem._open_existing(self._merge_path, writable=False)
            try:
                os.fchmod(fd, 0o400)
                os.fdatasync(fd)
            finally:
                os.close(fd)
            self._filesystem.sync_storage_directory(self._events_path)
        except OSError as exc:
            self._filesystem._record_error(exc)
            raise EventPersistenceError(
                f"cannot seal verified merge output: {_bounded_error(exc)}"
            ) from exc

    def promote(self) -> None:
        """Atomically promote prepared output; no history scan is performed."""
        cursor = self._host._read_cursor_if_present()
        if cursor is None:
            return
        if cursor["phase"] != "prepared":
            raise ProjectionUnavailableError("index rebuild merge is not prepared")
        if self._path_size(self._delta_path) != cursor["merge_delta_target_offset"]:
            raise ProjectionUnavailableError("new index delta arrived after merge preparation")
        if self._merge_path.exists():
            if not _output_size_matches(self._merge_path, cursor["merge_output_offset"]):
                raise ProjectionUnavailableError("prepared merge output does not match its cursor")
            source = self._merge_path
        elif self._index_path.exists() and _output_size_matches(
            self._index_path, cursor["merge_output_offset"]
        ):
            self._host._clear_rebuild_metadata()
            return
        else:
            raise ProjectionUnavailableError("prepared merge output is missing before promotion")
        self._filesystem._trip("before_rebuild_rename")
        try:
            os.replace(source, self._index_path)
            self._filesystem.sync_storage_directory(self._events_path)
        except OSError as exc:
            self._filesystem._record_error(exc)
            raise EventPersistenceError(f"index promotion failed: {_bounded_error(exc)}") from exc
        self._filesystem._trip("after_rebuild_rename")
        self._host._clear_rebuild_metadata()
        self._filesystem._trip("after_rebuild_cleanup")

    def matches(self, cursor: Mapping[str, Any], rebuild_matches: bool) -> bool:
        """Validate source and merge metadata without reading full history."""
        if not rebuild_matches:
            return False
        if cursor["phase"] == "project":
            return True
        if cursor["phase"] == "prepared" and self._merge_path.exists():
            return _output_size_matches(self._merge_path, cursor["merge_output_offset"])
        if cursor["phase"] == "prepared" and self._index_path.exists():
            return _output_size_matches(self._index_path, cursor["merge_output_offset"])
        return self._merge_path.exists()

    def truncate_projection(self, path: Path, size: int) -> None:
        fd = self._filesystem._open_existing(path, writable=True)
        try:
            os.ftruncate(fd, size)
            os.fdatasync(fd)
        except OSError as exc:
            raise EventPersistenceError(
                f"cannot repair {path.name}: {_bounded_error(exc)}"
            ) from exc
        finally:
            os.close(fd)
        self._filesystem.sync_storage_directory(self._events_path)

    def _repair_output(self, cursor: Mapping[str, Any]) -> Mapping[str, Any]:
        path = self._merge_path
        expected = cursor["merge_output_offset"]
        if not path.exists():
            if expected == 0:
                self._filesystem.atomic_replace(path, b"", mode=0o600)
                return cursor
            raise ProjectionUnavailableError("merge output disappeared before recovery")
        actual = self._path_size(path)
        if actual < expected:
            raise ProjectionUnavailableError("merge output is shorter than its cursor")
        if actual > expected:
            self.truncate_projection(path, expected)
        return cursor

    def _assert_order(self, summary: EventSummary, cursor: Mapping[str, Any]) -> None:
        if cursor["merge_output_offset"] == 0:
            return
        tail = _bounded_tail_lines(self._merge_path, 1, MAX_INDEX_LINE_BYTES)
        if not tail:
            raise EventCorruptionError("merge cursor has output progress but no output tail")
        previous = _decode_summary_line(tail[-1])
        if _summary_sort_key(summary) < _summary_sort_key(previous):
            raise ProjectionUnavailableError("index rebuild inputs are not ordered")

    @staticmethod
    def _path_size(path: Path) -> int:
        try:
            return path.stat().st_size if path.exists() else 0
        except OSError as exc:
            raise EventPersistenceError(
                f"cannot inspect {path.name}: {_bounded_error(exc)}"
            ) from exc

    @staticmethod
    def _summary_at(
        path: Path,
        offset: int,
    ) -> tuple[tuple[EventSummary, bytes], int] | None:
        if not path.exists():
            if offset:
                raise EventCorruptionError(f"{path.name} disappeared after merge progress")
            return None
        try:
            size = path.stat().st_size
            if offset > size:
                raise EventCorruptionError(f"{path.name} cursor exceeds file size")
            with path.open("rb") as stream:
                stream.seek(offset)
                line = stream.readline(MAX_INDEX_LINE_BYTES + 1)
        except OSError as exc:
            raise EventPersistenceError(f"cannot read {path.name}: {_bounded_error(exc)}") from exc
        if not line:
            return None
        if not line.endswith(b"\n") or len(line) > MAX_INDEX_LINE_BYTES:
            raise EventCorruptionError(f"{path.name} contains a torn or oversized summary")
        return (_decode_summary_line(line), line), offset + len(line)


def _tail_digest(path: Path) -> str:
    lines = _bounded_tail_lines(path, 1, MAX_INDEX_LINE_BYTES)
    if not lines:
        return EMPTY_SHA256
    return hashlib.sha256(lines[-1]).hexdigest()


def _chain_digest(previous: str, line: bytes) -> str:
    return hashlib.sha256(previous.encode("ascii") + line).hexdigest()


def _output_size_matches(path: Path, expected: int) -> bool:
    try:
        return path.stat().st_size == expected
    except OSError:
        return False


def _output_matches(path: Path, *, offset: int, digest: str) -> bool:
    try:
        return path.stat().st_size == offset and _tail_digest(path) == digest
    except OSError:
        return False


def _sort_summary_file(path: Path, filesystem: JsonlFilesystem) -> None:
    """Validate, canonically order, and deduplicate one summary projection."""
    try:
        lines = path.read_bytes().splitlines(keepends=True)
    except OSError as exc:
        raise EventPersistenceError(f"cannot read {path.name}: {_bounded_error(exc)}") from exc

    records: list[tuple[tuple[str, str, str], tuple[str, str], bytes]] = []
    for line in lines:
        summary = _decode_summary_line(line)
        records.append((_summary_sort_key(summary), _summary_key(summary), line))
    records.sort(key=lambda record: record[0])

    unique: list[bytes] = []
    seen: dict[tuple[str, str], bytes] = {}
    for _order, identity, line in records:
        previous = seen.get(identity)
        if previous is None:
            seen[identity] = line
            unique.append(line)
        elif previous != line:
            raise EventConflictError("summary duplicate key has different bytes")
    filesystem.atomic_replace(path, b"".join(unique), mode=0o400)


def _summary_sort_key(summary: EventSummary) -> tuple[str, str, str]:
    return summary.segment_filename, summary.blackout_id, summary.outcome_record_sha256


def _select_merge_record(
    base: tuple[tuple[EventSummary, bytes], int] | None,
    delta: tuple[tuple[EventSummary, bytes], int] | None,
) -> tuple[tuple[EventSummary, bytes], int, int]:
    if base is None and delta is None:
        raise ValueError("cannot select from two empty merge streams")
    if base is None:
        assert delta is not None
        (summary, line), offset = delta
        return (summary, line), 0, offset
    if delta is None:
        (summary, line), offset = base
        return (summary, line), offset, 0
    (base_summary, base_line), base_offset = base
    (delta_summary, delta_line), delta_offset = delta
    base_key = _summary_sort_key(base_summary)
    delta_key = _summary_sort_key(delta_summary)
    if base_key == delta_key:
        if base_line != delta_line:
            raise EventConflictError("rebuild delta conflicts with target summary")
        return (base_summary, base_line), base_offset, delta_offset
    if base_key < delta_key:
        return (base_summary, base_line), base_offset, 0
    return (delta_summary, delta_line), 0, delta_offset
