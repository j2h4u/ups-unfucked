"""State seam for resumable index rebuild metadata and restart."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from src.adapters.jsonl_errors import EventStoreError
from src.adapters.jsonl_event_catalog import CatalogSnapshot, JsonlEventCatalog
from src.adapters.jsonl_filesystem import JsonlFilesystem, _file_sha256
from src.adapters.jsonl_index_merge import IndexMergeCoordinator, _output_matches
from src.adapters.jsonl_index_metadata import IndexMetadataStore
from src.adapters.jsonl_record_codec import EMPTY_SHA256


def _catalog_cursor(generation_id: str, snapshot: CatalogSnapshot) -> dict[str, Any]:
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


class JsonlIndexRebuildSupport:
    """Provide the rebuild host protocol without bloating the index facade."""

    _rebuild_path: Path
    _merge_path: Path
    _delta_path: Path
    _filesystem: JsonlFilesystem
    _catalog: JsonlEventCatalog
    _wall_clock: Callable[[], str]
    _metadata: IndexMetadataStore
    _merge: IndexMergeCoordinator

    def _cursor_matches_rebuild(self, cursor: Mapping[str, Any]) -> bool:
        try:
            source_matches = self._rebuild_path.exists() and _output_matches(
                self._rebuild_path,
                offset=cursor["rebuild_output_offset"],
                digest=cursor["rebuild_output_sha256"],
            )
            source_matches = source_matches and self._last_projected_matches(cursor)
            return self._merge.matches(cursor, source_matches)
        except (EventStoreError, OSError, KeyError):
            return False

    def _last_projected_matches(self, cursor: Mapping[str, Any]) -> bool:
        last_name = cursor["last_projected_filename"]
        if last_name is None:
            return cursor["files_done"] == 0
        last_path = self._filesystem._event_path(last_name)
        if cursor["last_projected_sha256"] == EMPTY_SHA256 and not last_path.exists():
            return True
        return last_path.exists() and _file_sha256(last_path) == cursor["last_projected_sha256"]

    def _restart_rebuild_generation(self) -> Mapping[str, Any]:
        self._unlink_projection_file(self._merge_path)
        self._unlink_projection_file(self._delta_path)
        self._filesystem.atomic_replace(self._rebuild_path, b"", mode=0o600)
        snapshot = self._catalog.snapshot()
        cursor = {
            "schema_version": 1,
            "phase": "project",
            "generation_id": uuid.uuid4().hex,
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
        self._write_catalog_cursor(_catalog_cursor(cursor["generation_id"], snapshot))
        self._write_rebuild_cursor(cursor)
        return cursor

    def _write_rebuild_cursor(self, cursor: Mapping[str, Any]) -> None:
        self._metadata.write_rebuild_cursor(cursor)

    def _read_catalog_cursor(self) -> Mapping[str, Any]:
        return self._metadata.read_catalog_cursor()

    def _write_catalog_cursor(self, cursor: Mapping[str, Any]) -> None:
        self._metadata.write_catalog_cursor(cursor)

    def _clear_rebuild_metadata(self) -> None:
        self._metadata.clear_rebuild_metadata()

    def _clear_orphan_rebuild_metadata(self) -> None:
        self._metadata.clear_orphan_rebuild_metadata()

    def _unlink_projection_file(self, path: Path) -> None:
        self._metadata.unlink_projection_file(path)

    def _projection_destination(self) -> Path:
        return self._metadata.projection_destination()

    def _index_available(self, *, allow_partial: bool = False) -> bool:
        return self._metadata.index_available(allow_partial=allow_partial)

    def _read_cursor_if_present(self) -> Mapping[str, Any] | None:
        return self._metadata.read_rebuild_cursor()
