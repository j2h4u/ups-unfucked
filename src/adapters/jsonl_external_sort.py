"""Bounded external sorting for summary JSONL streams.

Neither the event catalog nor a delta file is an ordering contract.  This
module turns arbitrary input streams into bounded sorted runs and performs a
stable k-way merge.  Duplicate keys are accepted only when their canonical
bytes are identical.
"""

from __future__ import annotations

import heapq
import json
import os
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.adapters.jsonl_errors import (
    EventConflictError,
    EventCorruptionError,
    EventPersistenceError,
)
from src.adapters.jsonl_record_codec import MAX_INDEX_LINE_BYTES, canonical_json_bytes

MAX_SORT_RUN_BYTES = 512 * 1024
MAX_SORT_RUN_RECORDS = 256


def terminal_summary_key(
    value: Mapping[str, Any], *, fallback_order: int = 0
) -> tuple[int, str, str]:
    """Return the stable terminal sequence, blackout, outcome key."""
    sequence = value.get("terminal_catalog_seq", fallback_order)
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise EventCorruptionError("summary terminal catalog sequence is invalid")
    blackout_id = value.get("blackout_id")
    outcome_hash = value.get("outcome_record_sha256")
    if not isinstance(blackout_id, str) or not isinstance(outcome_hash, str):
        raise EventCorruptionError("summary sort identity is invalid")
    return sequence, blackout_id, outcome_hash


def _decode_line(line: bytes) -> Mapping[str, Any]:
    if not line.endswith(b"\n") or len(line) > MAX_INDEX_LINE_BYTES:
        raise EventCorruptionError("external-sort input has a torn or oversized line")
    try:
        value = json.loads(line[:-1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EventCorruptionError("external-sort input is not strict JSON") from exc
    if not isinstance(value, dict):
        raise EventCorruptionError("external-sort input is not an object")
    if canonical_json_bytes(value) + b"\n" != line:
        raise EventCorruptionError("external-sort input is not canonical JSON")
    return value


@dataclass(frozen=True, slots=True)
class SortItem:
    key: tuple[int, str, str]
    line: bytes
    ordinal: int


class JsonlExternalSorter:
    """Spill arbitrary summary lines into bounded sorted runs."""

    def __init__(
        self,
        work_directory: Path,
        *,
        max_run_bytes: int = MAX_SORT_RUN_BYTES,
        max_run_records: int = MAX_SORT_RUN_RECORDS,
    ) -> None:
        if max_run_bytes < 1 or max_run_records < 1:
            raise ValueError("external-sort bounds must be positive")
        self._work_directory = work_directory
        self._max_run_bytes = max_run_bytes
        self._max_run_records = max_run_records
        self._items: list[SortItem] = []
        self._bytes = 0
        self._ordinal = 0
        self._runs: list[Path] = []

    def add(self, line: bytes) -> None:
        value = _decode_line(line)
        item = SortItem(
            terminal_summary_key(value, fallback_order=self._ordinal), line, self._ordinal
        )
        self._items.append(item)
        self._bytes += len(line)
        self._ordinal += 1
        if len(self._items) >= self._max_run_records or self._bytes >= self._max_run_bytes:
            self._spill()

    def add_file(self, path: Path) -> None:
        try:
            with path.open("rb") as stream:
                for line in stream:
                    self.add(line)
        except OSError as exc:
            raise EventPersistenceError(f"cannot read external-sort input {path.name}") from exc

    def finish(self) -> tuple[Path, ...]:
        self._spill()
        return tuple(self._runs)

    def cleanup(self) -> None:
        for path in self._runs:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise EventPersistenceError("cannot clean external-sort run") from exc
        self._runs.clear()

    def _spill(self) -> None:
        if not self._items:
            return
        self._work_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._items.sort(key=lambda item: (item.key, item.ordinal))
        fd, raw_path = tempfile.mkstemp(
            prefix="index-sort-", suffix=".run", dir=self._work_directory
        )
        path = Path(raw_path)
        try:
            with os.fdopen(fd, "wb") as stream:
                for item in self._items:
                    stream.write(item.line)
                stream.flush()
                os.fdatasync(stream.fileno())
            os.chmod(path, 0o400)
        except OSError as exc:
            try:
                path.unlink()
            except OSError:
                pass
            raise EventPersistenceError("cannot write external-sort run") from exc
        self._runs.append(path)
        self._items.clear()
        self._bytes = 0


def _run_items(path: Path, run_number: int) -> Iterator[SortItem]:
    try:
        with path.open("rb") as stream:
            ordinal = 0
            for line in stream:
                value = _decode_line(line)
                yield SortItem(terminal_summary_key(value), line, (run_number << 32) + ordinal)
                ordinal += 1
    except OSError as exc:
        raise EventPersistenceError(f"cannot read external-sort run {path.name}") from exc


def merge_sorted_runs(runs: Sequence[Path]) -> Iterator[bytes]:
    """Yield stable unique lines from sorted runs, rejecting conflicts."""
    iterators = [iter(_run_items(path, index)) for index, path in enumerate(runs)]
    heap: list[tuple[tuple[int, str, str], int, int, SortItem]] = []
    for index, iterator in enumerate(iterators):
        try:
            item = next(iterator)
        except StopIteration:
            continue
        heapq.heappush(heap, (item.key, item.ordinal, index, item))
    previous_key: tuple[int, str, str] | None = None
    previous_line: bytes | None = None
    while heap:
        _key, _ordinal, index, item = heapq.heappop(heap)
        if previous_key == item.key:
            if previous_line != item.line:
                raise EventConflictError("external-sort duplicate key has different bytes")
        else:
            yield item.line
            previous_key, previous_line = item.key, item.line
        try:
            next_item = next(iterators[index])
        except StopIteration:
            continue
        heapq.heappush(heap, (next_item.key, next_item.ordinal, index, next_item))


def sort_summary_files(
    inputs: Iterable[Path],
    output: Path,
    *,
    work_directory: Path | None = None,
    max_run_bytes: int = MAX_SORT_RUN_BYTES,
    max_run_records: int = MAX_SORT_RUN_RECORDS,
) -> int:
    """Sort and deduplicate one or more arbitrary summary files."""
    workspace = work_directory or output.parent
    sorter = JsonlExternalSorter(
        workspace,
        max_run_bytes=max_run_bytes,
        max_run_records=max_run_records,
    )
    try:
        for path in inputs:
            if path.exists():
                sorter.add_file(path)
        runs = sorter.finish()
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.sort-tmp")
        count = 0
        try:
            with temporary.open("wb") as stream:
                for line in merge_sorted_runs(runs):
                    stream.write(line)
                    count += 1
                stream.flush()
                os.fdatasync(stream.fileno())
            os.chmod(temporary, 0o400)
            os.replace(temporary, output)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return count
    finally:
        sorter.cleanup()


__all__ = [
    "JsonlExternalSorter",
    "MAX_SORT_RUN_BYTES",
    "MAX_SORT_RUN_RECORDS",
    "merge_sorted_runs",
    "sort_summary_files",
    "terminal_summary_key",
]
