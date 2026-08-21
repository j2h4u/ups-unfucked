"""Direct sealed-event history projection for the JSONL event store."""

import hashlib
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from src.adapters.jsonl_errors import EventCorruptionError, EventPathError, EventValidationError
from src.adapters.jsonl_event_stream import JsonlEventStream
from src.adapters.jsonl_filesystem import JsonlFilesystem, _file_sha256
from src.adapters.jsonl_record_codec import (
    EVENT_FILENAME_RE,
    MAX_DAMAGED_HASHES,
    SCHEMA_VERSION,
    _optional_bool,
    _optional_finite_nonnegative,
    _optional_short_string,
    _required_short_string,
)
from src.adapters.jsonl_summary_codec import _encode_summary
from src.application.storage_values import (
    EpochHistoryScan,
    EpochHistoryTail,
    EventProjection,
    EventRef,
    EventSummary,
)


class JsonlEventHistory:
    """Enumerate sealed event files and derive terminal summaries in memory."""

    def __init__(
        self,
        events_path: Path,
        filesystem: JsonlFilesystem,
        stream: JsonlEventStream,
        owned_paths: Callable[[], frozenset[str]],
    ) -> None:
        self._events_path = events_path
        self._filesystem = filesystem
        self._stream = stream
        self._owned_paths = owned_paths

    def tail(self, limit: int) -> tuple[EventSummary, ...]:
        return self._history()[:limit] if limit else ()

    def tail_for_epoch(self, battery_epoch_id: str, limit: int) -> EpochHistoryTail:
        summaries = tuple(
            item for item in self._history() if item.battery_epoch_id == battery_epoch_id
        )
        return EpochHistoryTail(summaries[:limit], max(0, len(summaries) - limit), True)

    def scan_for_epoch(self, battery_epoch_id: str) -> EpochHistoryScan:
        return EpochHistoryScan(
            tuple(item for item in self._history() if item.battery_epoch_id == battery_epoch_id),
            True,
        )

    def commit_notice(
        self, path_token: str, projection: EventProjection, append: Callable[..., object]
    ) -> None:
        summary = self._summary_for(path_token, projection)
        summary_hash = hashlib.sha256(_encode_summary(summary)).hexdigest()
        append(
            blackout_id=summary.blackout_id,
            segment_filename=summary.segment_filename,
            summary_sha256=summary_hash,
            index_head_sha256=summary_hash,
        )

    def _history(self) -> tuple[EventSummary, ...]:
        owned_paths = self._owned_paths()
        summaries = [
            summary
            for path in self._events_path.iterdir()
            if path.name.startswith("evt-")
            for summary in (self._summary_for_path(path, owned_paths),)
            if summary is not None
        ]
        summaries.sort(key=lambda item: (item.started_utc, item.segment_filename), reverse=True)
        return tuple(summaries)

    def _summary_for_path(self, path: Path, owned_paths: frozenset[str]) -> EventSummary | None:
        match = EVENT_FILENAME_RE.fullmatch(path.name)
        if match is None:
            raise EventPathError(f"event filename is invalid: {path.name}")
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise EventPathError(f"event path is not a regular file: {path.name}")
        if stat.S_IMODE(info.st_mode) != 0o400:
            if path.name in owned_paths:
                return None
            raise EventCorruptionError(f"sealed event has unexpected permissions: {path.name}")
        projection = self._stream.project(EventRef(match.group("blackout"), path.name))
        if projection.outcome is None:
            raise EventCorruptionError(f"sealed event has no terminal outcome: {path.name}")
        terminal_segment = projection.outcome.segment_id
        filename_segment = match.group("segment")
        if filename_segment is None and (
            projection.start is None or projection.start.segment_id != terminal_segment
        ):
            return None
        if filename_segment is not None and filename_segment != terminal_segment:
            return None
        return self._summary_for(path.name, projection)

    def _summary_for(self, path_token: str, projection: EventProjection) -> EventSummary:
        start = projection.start
        outcome = projection.outcome
        if start is None or outcome is None:
            raise EventValidationError("summary requires start and outcome records")
        end = projection.end
        outcome_payload = outcome.payload
        comparison_mode: Literal["full", "short_window", "none"] = "none"
        raw_comparison_mode = _optional_short_string(outcome_payload, "comparison_mode")
        if raw_comparison_mode == "full":
            comparison_mode = "full"
        elif raw_comparison_mode == "short_window":
            comparison_mode = "short_window"
        elif raw_comparison_mode not in {None, "none"}:
            raise EventValidationError("invalid comparison mode in outcome")
        damaged = self._stream._damaged_hashes(start.blackout_id)
        summary = EventSummary(
            schema_version=SCHEMA_VERSION,
            blackout_id=start.blackout_id,
            segment_filename=path_token,
            started_utc=start.wall_time_utc,
            ended_utc=end.wall_time_utc if end is not None else outcome.wall_time_utc,
            termination=_optional_short_string(end.payload, "termination") if end else None,
            evidence_class=_optional_short_string(outcome_payload, "evidence_class"),
            disposition=_required_short_string(outcome_payload, "disposition"),
            duration_s=_optional_finite_nonnegative(outcome_payload, "duration_s"),
            observation_count=len(projection.observations),
            battery_epoch_id=_optional_short_string(start.payload, "battery_epoch_id"),
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
