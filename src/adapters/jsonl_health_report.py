"""Storage-health reporting over the durable event filesystem."""

import shutil
import stat
from typing import TYPE_CHECKING

from src.adapters.jsonl_errors import EventPathError, EventStoreError
from src.adapters.jsonl_record_codec import (
    EVENT_FILENAME_RE,
    _validate_optional_count,
)
from src.application.storage_values import StorageHealth

if TYPE_CHECKING:
    from src.adapters.jsonl_filesystem import JsonlFilesystem
    from src.adapters.jsonl_work_registry import JsonlWorkRegistry


def storage_health(
    filesystem: "JsonlFilesystem",
    registry: "JsonlWorkRegistry",
    *,
    queued_observations: int | None = None,
    consumed_step_budget_remaining: int | None = None,
) -> StorageHealth:
    """Return bounded diagnostics without maintaining historical projection state."""
    _validate_optional_count(queued_observations, "queued_observations")
    _validate_optional_count(
        consumed_step_budget_remaining,
        "consumed_step_budget_remaining",
    )
    try:
        active = registry._read_registry()
        active_phase = active.capture.tag if active.capture is not None else None
        if active_phase is None and active.pending_processing:
            active_phase = "processing"
        event_count = 0
        total_bytes = 0
        for path in filesystem._events_path().iterdir():
            if not path.name.startswith("evt-"):
                continue
            if EVENT_FILENAME_RE.fullmatch(path.name) is None:
                raise EventPathError(f"event filename is invalid: {path.name}")
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise EventPathError(f"event path is not a regular file: {path.name}")
            event_count += 1
            total_bytes += info.st_size
        alarm = filesystem._last_error_value()
        return StorageHealth(
            capture_available=alarm is None,
            active_phase=active_phase,
            queued_observations=queued_observations,
            durability_lag_s=filesystem._durability_lag_s(),
            consumed_step_budget_remaining=consumed_step_budget_remaining,
            event_count=event_count,
            total_bytes=total_bytes,
            free_bytes=shutil.disk_usage(filesystem._events_path()).free,
            alarm=alarm,
            bounded_error=alarm,
        )
    except (EventStoreError, OSError) as exc:
        filesystem._record_error(exc)
        return StorageHealth(
            capture_available=False,
            active_phase=None,
            queued_observations=queued_observations,
            durability_lag_s=filesystem._durability_lag_s(),
            consumed_step_budget_remaining=consumed_step_budget_remaining,
            event_count=0,
            total_bytes=0,
            free_bytes=0,
            alarm="storage_unavailable",
            bounded_error=filesystem._last_error_value(),
        )
