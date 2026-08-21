"""Direct sealed-event history projection for the JSONL event store."""

from collections.abc import Callable, Collection
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from src.adapters.jsonl_errors import EventCorruptionError, EventPathError, EventValidationError
from src.adapters.jsonl_event_read_codec import (
    project_event,
    sealed_event_paths,
)
from src.adapters.jsonl_record_codec import (
    EVENT_FILENAME_RE,
    SCHEMA_VERSION,
    _optional_bool,
    _optional_finite_nonnegative,
    _optional_short_string,
    _required_short_string,
)
from src.application.storage_values import (
    EpochHistoryScan,
    EpochHistoryTail,
    EventProjection,
    EventSummary,
)


class JsonlEventHistory:
    """Enumerate sealed event files and derive terminal summaries in memory."""

    def __init__(self, events_path: Path) -> None:
        self._events_path = events_path

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

    def sealed_projections(
        self,
        start_utc: str,
        end_utc: str,
        *,
        event_kind: str = "blackout",
    ) -> tuple[EventProjection, ...]:
        """Project sealed ordinary events whose start is in ``[start,end)``."""
        if event_kind not in {"blackout", "recharge"}:
            raise EventValidationError("event kind must be blackout or recharge")
        start = _parse_range_bound(start_utc, "start_utc")
        end = _parse_range_bound(end_utc, "end_utc")
        if start >= end:
            raise ValueError("history range must be non-empty and ordered")
        projections: list[EventProjection] = []
        for path in sealed_event_paths(self._events_path):
            projection = self._sealed_projection(path, event_kind)
            if projection is None or projection.start is None:
                continue
            event_start = _parse_range_bound(projection.start.wall_time_utc, "event start")
            if start <= event_start < end:
                projections.append(projection)
        return tuple(projections)

    def sealed_recharge_projections_for_blackouts(
        self,
        blackout_ids: Collection[str],
    ) -> tuple[EventProjection, ...]:
        """Read sealed recharge episodes linked to selected blackout IDs."""
        linked_ids = frozenset(blackout_ids)
        if not linked_ids:
            return ()
        projections: list[EventProjection] = []
        for path in sealed_event_paths(self._events_path):
            projection = self._sealed_projection(path, "recharge")
            if projection is None or projection.start is None:
                continue
            preceding = projection.start.payload.get("preceding_blackout_id")
            if isinstance(preceding, str) and preceding in linked_ids:
                projections.append(projection)
        return tuple(projections)

    def _sealed_projection(
        self,
        path: Path,
        event_kind: str,
    ) -> EventProjection | None:
        projection = project_event(self._events_path, _blackout_id(path))
        if projection.event_kind != event_kind or projection.start is None:
            return None
        if projection.outcome is None:
            raise EventCorruptionError(f"sealed event has no terminal outcome: {path.name}")
        return projection

    def commit_notice(
        self, path_token: str, projection: EventProjection, append: Callable[..., object]
    ) -> None:
        summary = self._summary_for(path_token, projection)
        append(
            blackout_id=summary.blackout_id,
            segment_filename=summary.segment_filename,
        )

    def _history(self) -> tuple[EventSummary, ...]:
        summaries = [
            summary
            for path in sealed_event_paths(self._events_path)
            for summary in (self._summary_for_path(path),)
            if summary is not None
        ]
        summaries.sort(key=lambda item: (item.started_utc, item.segment_filename), reverse=True)
        return tuple(summaries)

    def _summary_for_path(self, path: Path) -> EventSummary | None:
        projection = project_event(self._events_path, _blackout_id(path))
        if projection.event_kind != "blackout":
            return None
        if projection.outcome is None:
            raise EventCorruptionError(f"sealed event has no terminal outcome: {path.name}")
        terminal_segment = projection.outcome.segment_id
        filename_segment = _event_segment(path)
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
        )
        return summary


def _parse_range_bound(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventValidationError(f"{label} is not an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EventValidationError(f"{label} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _blackout_id(path: Path) -> str:
    match = EVENT_FILENAME_RE.fullmatch(path.name)
    if match is None:
        raise EventPathError(f"event filename is invalid: {path.name}")
    return match.group("blackout")


def _event_segment(path: Path) -> str | None:
    match = EVENT_FILENAME_RE.fullmatch(path.name)
    if match is None:
        raise EventPathError(f"event filename is invalid: {path.name}")
    return match.group("segment")
