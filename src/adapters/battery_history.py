"""Small append-only aggregate history derived from raw UPS telemetry."""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HISTORY_FILENAME = "history.jsonl"


class BatteryHistory:
    """Write concise aggregate records without changing raw telemetry."""

    def __init__(self, path: Path) -> None:
        if path.name != HISTORY_FILENAME:
            raise ValueError(f"history must be stored as {HISTORY_FILENAME}")
        self._path = path

    def episode(self, records: list[dict[str, Any]]) -> None:
        summary = summarize_episode(records)
        if summary is not None:
            self._append(summary)

    def model_update(
        self,
        *,
        at: str,
        event_at: str,
        evidence_at: str,
        changes: dict[str, tuple[Any, Any]],
        reason: str,
    ) -> None:
        """Record the exact, human-readable effect of one feedback pass."""
        rendered = {
            field: {"from": before, "to": after, "delta": round(after - before, 12)}
            for field, (before, after) in changes.items()
        }
        self._append(
            {
                "kind": "model_update",
                "at": _timestamp(at),
                "event_at": _timestamp(event_at),
                "evidence_at": _timestamp(evidence_at),
                "changes": rendered,
                "reason": reason,
            }
        )

    def event_kinds(self) -> dict[str, str]:
        """Return classifications and successful feedback applications by event time."""
        if not self._path.exists():
            return {}
        result: dict[str, str] = {}
        for line in self._path.read_text().splitlines():
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("history record must be an object")
            kind = record.get("kind")
            event_at = record.get("event_at") if kind == "model_update" else record.get("at")
            if kind in {"blackout", "self_test", "model_update"} and isinstance(event_at, str):
                result[event_at] = str(kind)
        return result

    def _append(self, record: dict[str, Any]) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n"
        fd = os.open(
            self._path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)


def summarize_episode(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Summarize one closed telemetry episode; never invent unavailable values."""
    bounds = _discharge_bounds(records)
    if bounds is None:
        return None
    start_index, end_index = bounds
    discharge = records[start_index : end_index + 1]
    start_at = _parse_timestamp(str(discharge[0].get("at")))
    end_at = _parse_timestamp(str(discharge[-1].get("at")))
    start = _timestamp(str(discharge[0].get("at")))
    duration = max(0, round((end_at - start_at).total_seconds()))
    discharge_percentages = [
        float(row["battery_pct"]) for row in discharge if _finite_number(row.get("battery_pct"))
    ]
    baseline = _last_percentage(records[:start_index])
    if baseline is None and discharge_percentages:
        baseline = discharge_percentages[0]
    depth = (
        max(0.0, baseline - min(discharge_percentages))
        if baseline is not None and discharge_percentages
        else None
    )
    kind = "self_test" if any(_is_self_test(row) for row in records) else "blackout"
    return {
        "kind": kind,
        "at": start,
        "duration_s": duration,
        "depth_pct": depth,
        "efc": depth / 100.0 if depth is not None else None,
    }


def _discharge_bounds(records: list[dict[str, Any]]) -> tuple[int, int] | None:
    start = next((index for index, row in enumerate(records) if _is_on_battery(row)), None)
    if start is None:
        return None
    for index, row in enumerate(records[start + 1 :], start=start + 1):
        if not _is_on_battery(row):
            return start, index
    return start, len(records) - 1


def _last_percentage(records: list[dict[str, Any]]) -> float | None:
    return next(
        (
            float(row["battery_pct"])
            for row in reversed(records)
            if _finite_number(row.get("battery_pct"))
        ),
        None,
    )


def _is_on_battery(record: dict[str, Any]) -> bool:
    status = str(record.get("status", "")).split()
    return "OB" in status or "CAL" in status


def _is_self_test(record: dict[str, Any]) -> bool:
    status = str(record.get("status", "")).split()
    return "CAL" in status


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _timestamp(value: str) -> str:
    moment = _parse_timestamp(value)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("history timestamp must include a timezone")
    return moment.astimezone(timezone.utc)
