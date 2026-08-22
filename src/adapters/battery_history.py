"""Small append-only aggregate history derived from raw UPS telemetry."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.domain.time import utc_second
from src.domain.values import BlackoutKind

HISTORY_FILENAME = "history.jsonl"


class BatteryHistory:
    """Write concise aggregate records without changing raw telemetry."""

    def __init__(self, path: Path) -> None:
        if path.name != HISTORY_FILENAME:
            raise ValueError(f"history must be stored as {HISTORY_FILENAME}")
        self._path = path

    def episode(
        self,
        records: list[dict[str, Any]],
        *,
        physical_kind: BlackoutKind | str | None = None,
    ) -> None:
        summary = summarize_episode(records, physical_kind=physical_kind)
        if summary is not None:
            self._append(summary)

    def ir_observation(
        self,
        *,
        event_at: str,
        estimate: float,
        evidence_at: str,
        uncertainty: float,
        reason: str,
    ) -> bool:
        """Persist one compact, repeat-safe IR observation."""
        event_key = canonical_timestamp(event_at)
        if self._has_kind("ir_observation", event_key):
            return False
        self._append(
            {
                "kind": "ir_observation",
                "event_at": event_key,
                "estimate": float(estimate),
                "evidence_at": canonical_timestamp(evidence_at),
                "uncertainty": float(uncertainty),
                "reason": reason,
            }
        )
        return True

    def ir_observations(self) -> list[dict[str, Any]]:
        """Return distinct observations newer than the last IR model update."""
        if not self._path.exists():
            return []
        last_update: str | None = None
        rows: list[dict[str, Any]] = []
        for line in self._path.read_text().splitlines():
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("history record must be an object")
            if record.get("kind") == "model_update":
                changes = record.get("changes")
                if (
                    isinstance(changes, Mapping)
                    and "physics.ir_compensation.k_volts_per_percent" in changes
                ):
                    last_update = canonical_timestamp(str(record["event_at"]))
            elif record.get("kind") == "ir_observation":
                rows.append(_canonical_observation(record))
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            event_at = str(row["event_at"])
            if last_update is None or event_at > last_update:
                unique.setdefault(event_at, row)
        return list(unique.values())

    def upsert_model_update(self, receipt: Mapping[str, Any]) -> bool:
        """Append one model receipt unless its canonical event key is present.

        The model receipt is deliberately a complete history payload.  This
        makes recovery after a successful model rename and a failed history
        append a simple, repeat-safe operation.
        """
        record = _canonical_receipt(receipt)
        if self._has_kind("model_update", record["event_at"]):
            return False
        self._append(record)
        return True

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
            event_at = (
                record.get("event_at")
                if kind in {"model_update", "ir_observation"}
                else record.get("at")
            )
            if kind in {"blackout", "self_test", "model_update", "ir_observation"} and isinstance(
                event_at, str
            ):
                result[canonical_timestamp(event_at)] = str(kind)
        return result

    def _has_kind(self, kind: str, event_at: str) -> bool:
        if not self._path.exists():
            return False
        for line in self._path.read_text().splitlines():
            record = json.loads(line)
            if isinstance(record, dict) and record.get("kind") == kind:
                value = record.get("event_at", record.get("at"))
                if isinstance(value, str) and canonical_timestamp(value) == event_at:
                    return True
        return False

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


def summarize_episode(
    records: list[dict[str, Any]],
    *,
    physical_kind: BlackoutKind | str | None = None,
) -> dict[str, Any] | None:
    """Summarize one closed episode using explicit daemon provenance.

    Raw ``CAL`` is deliberately ignored for classification.  Missing or
    unknown provenance is a real blackout, including episodes recovered after
    a daemon restart.
    """
    bounds = _discharge_bounds(records)
    if bounds is None:
        return None
    start_index, end_index = bounds
    discharge = records[start_index : end_index + 1]
    start_at = _parse_timestamp(str(discharge[0].get("at")))
    end_at = _parse_timestamp(str(discharge[-1].get("at")))
    start = canonical_timestamp(str(discharge[0].get("at")))
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
    kind = "self_test" if _is_test_kind(physical_kind) else "blackout"
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


def _is_test_kind(physical_kind: BlackoutKind | str | None) -> bool:
    return physical_kind in {BlackoutKind.BLACKOUT_TEST, BlackoutKind.BLACKOUT_TEST.value}


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def canonical_timestamp(value: str) -> str:
    """Return the canonical UTC event key, with exactly one-second precision."""
    return utc_second(value)


def _parse_timestamp(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("history timestamp must include a timezone")
    return moment.astimezone(timezone.utc)


def _canonical_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    if set(receipt) == {"event_at", "evidence_at", "changes", "reason"}:
        receipt = {
            "kind": "model_update",
            "at": receipt["evidence_at"],
            **receipt,
        }
    required = {"kind", "at", "event_at", "evidence_at", "changes", "reason"}
    if set(receipt) != required or receipt.get("kind") != "model_update":
        raise ValueError("model receipt must contain exactly the model_update fields")
    changes = receipt["changes"]
    if not isinstance(changes, Mapping) or not changes:
        raise ValueError("model receipt changes must be a non-empty object")
    return {
        "kind": "model_update",
        "at": canonical_timestamp(str(receipt["at"])),
        "event_at": canonical_timestamp(str(receipt["event_at"])),
        "evidence_at": canonical_timestamp(str(receipt["evidence_at"])),
        "changes": {str(field): dict(change) for field, change in changes.items()},
        "reason": receipt["reason"],
    }


def _canonical_observation(record: Mapping[str, Any]) -> dict[str, Any]:
    required = {"kind", "event_at", "estimate", "evidence_at", "uncertainty", "reason"}
    if set(record) != required or record.get("kind") != "ir_observation":
        raise ValueError("IR observation has invalid fields")
    estimate = record["estimate"]
    uncertainty = record["uncertainty"]
    if not _finite_number(estimate) or not _finite_number(uncertainty):
        raise ValueError("IR observation values must be finite")
    reason = record["reason"]
    if not isinstance(reason, str) or not reason:
        raise ValueError("IR observation reason must be non-empty text")
    return {
        "kind": "ir_observation",
        "event_at": canonical_timestamp(str(record["event_at"])),
        "estimate": float(estimate),
        "evidence_at": canonical_timestamp(str(record["evidence_at"])),
        "uncertainty": float(uncertainty),
        "reason": reason,
    }
