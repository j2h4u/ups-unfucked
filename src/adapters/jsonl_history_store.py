"""Read-only JSONL composition for human history while the daemon is live."""

from __future__ import annotations

import stat
from collections.abc import Collection
from pathlib import Path

from src.adapters.jsonl_errors import EventPathError
from src.adapters.jsonl_event_history import JsonlEventHistory
from src.application.storage_values import EventProjection


class JsonlHistoryStore:
    """Expose only sealed-history reads without creating or taking a writer lock."""

    def __init__(self, root: str | Path) -> None:
        root_path = Path(root)
        events_path = root_path / "events"
        _validate_private_directory(root_path, "state directory")
        _validate_private_directory(events_path, "events directory")
        _validate_private_file(events_path / "active.json", "active registry")
        self._history = JsonlEventHistory(events_path)

    def sealed_event_projections(
        self,
        start_utc: str,
        end_utc: str,
        *,
        event_kind: str = "blackout",
    ) -> tuple[EventProjection, ...]:
        """Read sealed ordinary projections without touching the writer lock."""
        return self._history.sealed_projections(
            start_utc,
            end_utc,
            event_kind=event_kind,
        )

    def sealed_recharge_projections_for_blackouts(
        self,
        blackout_ids: Collection[str],
    ) -> tuple[EventProjection, ...]:
        """Read sealed recharge episodes linked to selected blackout IDs."""
        return self._history.sealed_recharge_projections_for_blackouts(blackout_ids)

    def close(self) -> None:
        """Keep the read-only facade lifecycle explicit; no descriptor is owned."""
        return None

    def __enter__(self) -> "JsonlHistoryStore":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


def _validate_private_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise EventPathError(f"{label} does not exist: {path}") from exc
    except OSError as exc:
        raise EventPathError(f"cannot inspect {label}: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise EventPathError(f"{label} is not a regular directory: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise EventPathError(f"{label} permissions are broader than 0700: {path}")


def _validate_private_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise EventPathError(f"{label} does not exist: {path}") from exc
    except OSError as exc:
        raise EventPathError(f"cannot inspect {label}: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise EventPathError(f"{label} is not a regular file: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise EventPathError(f"{label} permissions are broader than 0600: {path}")
