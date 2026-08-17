"""Bounded, resumable event-file inventory for storage health reporting."""

import stat

from src.adapters.jsonl_event_catalog import EMPTY_SHA256, JsonlEventCatalog
from src.adapters.jsonl_filesystem import JsonlFilesystem

MAX_HEALTH_FILES_PER_TICK = 64


class JsonlHealthInventory:
    """Own the resumable cursor and published counters for one health view."""

    def __init__(self, catalog: JsonlEventCatalog, filesystem: JsonlFilesystem) -> None:
        self._catalog = catalog
        self._filesystem = filesystem
        self._complete = True
        self._catalog_offset = 0
        self._catalog_seq = 0
        self._catalog_prev_hash = EMPTY_SHA256
        self._catalog_target_offset = 0
        self._pending_event_count = 0
        self._pending_total_bytes = 0
        self._published_stats: tuple[int, int] = (0, 0)

    def _tick(self) -> tuple[tuple[str, ...], bool]:
        """Advance one bounded inventory batch without opening event contents."""
        if self._complete:
            snapshot = self._catalog.snapshot()
            self._catalog_offset = 0
            self._catalog_seq = 0
            self._catalog_prev_hash = EMPTY_SHA256
            self._catalog_target_offset = snapshot.byte_offset
            self._pending_event_count = 0
            self._pending_total_bytes = 0
            self._complete = False
        batch = self._catalog.read_batch(
            byte_offset=self._catalog_offset,
            target_offset=self._catalog_target_offset,
            expected_seq=self._catalog_seq,
            previous_entry_sha256=self._catalog_prev_hash,
            max_files=MAX_HEALTH_FILES_PER_TICK,
        )
        paths: list[str] = []
        for entry in batch.entries:
            path = self._filesystem._event_path(entry.path_token)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValueError(f"event path is not a regular file: {path.name}")
            self._pending_event_count += 1
            self._pending_total_bytes += info.st_size
            paths.append(entry.path_token)
        self._catalog_offset = batch.byte_offset
        self._catalog_seq = batch.next_seq
        self._catalog_prev_hash = batch.previous_entry_sha256
        self._published_stats = (self._pending_event_count, self._pending_total_bytes)
        if batch.complete:
            self._complete = True
        return tuple(paths), self._complete

    def _stats(self) -> tuple[int, int]:
        return self._published_stats
