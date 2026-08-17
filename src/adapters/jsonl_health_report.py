"""Storage-health reporting over bounded adapter collaborators."""

import shutil
from typing import TYPE_CHECKING

from src.adapters.jsonl_errors import EventStoreError
from src.adapters.jsonl_record_codec import (
    REBUILD_STALL_SECONDS,
    _cursor_int,
    _cursor_str,
    _parse_utc,
    _validate_optional_count,
)
from src.application.storage_values import StorageHealth

if TYPE_CHECKING:
    from src.adapters.jsonl_index import JsonlIndex


def storage_health(
    index: "JsonlIndex",
    *,
    queued_observations: int | None = None,
    consumed_step_budget_remaining: int | None = None,
) -> StorageHealth:
    """Return bounded diagnostics without opening historical event contents."""
    _validate_optional_count(queued_observations, "queued_observations")
    _validate_optional_count(
        consumed_step_budget_remaining,
        "consumed_step_budget_remaining",
    )
    try:
        registry = index._registry()._read_registry()
        active_phase = registry.capture.tag if registry.capture is not None else None
        if active_phase is None and registry.pending_processing:
            active_phase = "processing"
        cursor = index._read_cursor_if_present()
        _paths, inventory_complete = index._health_inventory_tick()
        index_available = index._index_available(
            allow_partial=not inventory_complete,
        )
        alarm = index._filesystem._last_error_value()
        if alarm is None and not inventory_complete and not index_available:
            alarm = "inventory_in_progress"
        if alarm is None and not index_available:
            alarm = "projection_unavailable"
        rebuild_files_done = _cursor_int(cursor, "files_done")
        rebuild_files_target = _cursor_int(cursor, "target_count")
        last_progress = _cursor_str(cursor, "last_progress_utc")
        event_count, total_bytes = index._health_inventory_stats()
        return StorageHealth(
            capture_available=index._filesystem._last_error_value() is None,
            active_phase=active_phase,
            queued_observations=queued_observations,
            durability_lag_s=index._filesystem._durability_lag_s(),
            index_available=index_available,
            rebuild_generation=_cursor_str(cursor, "generation_id"),
            rebuild_in_progress=cursor is not None,
            rebuild_files_done=rebuild_files_done,
            rebuild_files_target=rebuild_files_target,
            rebuild_files_remaining=max(0, rebuild_files_target - rebuild_files_done),
            rebuild_last_progress_utc=last_progress,
            rebuild_stalled=rebuild_stalled(index, last_progress),
            consumed_step_budget_remaining=consumed_step_budget_remaining,
            event_count=event_count,
            total_bytes=total_bytes,
            free_bytes=shutil.disk_usage(index._events_directory()).free,
            alarm=alarm,
            bounded_error=index._filesystem._last_error_value(),
        )
    except (EventStoreError, OSError) as exc:
        index._filesystem._record_error(exc)
        return StorageHealth(
            capture_available=False,
            active_phase=None,
            queued_observations=queued_observations,
            durability_lag_s=index._filesystem._durability_lag_s(),
            index_available=False,
            rebuild_generation=None,
            rebuild_in_progress=False,
            rebuild_files_done=0,
            rebuild_files_target=0,
            rebuild_files_remaining=0,
            rebuild_last_progress_utc=None,
            rebuild_stalled=False,
            consumed_step_budget_remaining=consumed_step_budget_remaining,
            event_count=0,
            total_bytes=0,
            free_bytes=0,
            alarm="storage_unavailable",
            bounded_error=index._filesystem._last_error_value(),
        )


def rebuild_stalled(index: "JsonlIndex", last_progress_utc: str | None) -> bool:
    if last_progress_utc is None:
        return False
    elapsed = (_parse_utc(index._wall_time_utc()) - _parse_utc(last_progress_utc)).total_seconds()
    return elapsed > REBUILD_STALL_SECONDS
