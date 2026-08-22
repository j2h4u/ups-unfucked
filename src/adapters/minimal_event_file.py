"""The deliberately tiny telemetry wire format.

``events/telemetry.jsonl`` is an append-only stream of UPS samples.  A sample
is the complete wire record; lifecycle markers, IDs, learning state and
sidecars do not belong in this file.  Event boundaries are reconstructed by
the application adapter from the status/time sequence.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.adapters.jsonl_errors import EventCorruptionError, EventPathError, EventValidationError

TELEMETRY_FILENAME = "telemetry.jsonl"
SAMPLE_FIELDS = frozenset(
    {"at", "battery_v", "battery_pct", "runtime_s", "load_pct", "input_v", "output_v", "status"}
)


@dataclass(frozen=True, slots=True)
class MinimalEvent:
    """Validated stream contents (the name is retained for port compatibility)."""

    path: Path
    records: tuple[dict[str, Any], ...]

    @property
    def kind(self) -> str:
        return "blackout"


def sample(at: str, battery_v: float | None, *fields: Any, **named: Any) -> dict[str, Any]:
    """Build the fixed eight-field ordinary sample record.

    Every encoded record has the new exact schema, including null values for
    metrics unavailable from a particular NUT driver.  The compact variadic
    boundary keeps the public helper below the repository's argument-count
    limit while accepting the natural six positional metrics or keywords.
    """
    if fields and named:
        raise TypeError("sample metrics must be positional or keyword arguments")
    if fields:
        if len(fields) != 6:
            raise TypeError("sample requires six metrics after battery_v")
        battery_pct, runtime_s, load_pct, input_v, output_v, status = fields
    else:
        expected = {"battery_pct", "runtime_s", "load_pct", "input_v", "output_v", "status"}
        if set(named) != expected:
            raise TypeError("sample keyword metrics are incomplete")
        battery_pct = named["battery_pct"]
        runtime_s = named["runtime_s"]
        load_pct = named["load_pct"]
        input_v = named["input_v"]
        output_v = named["output_v"]
        status = named["status"]
    value: dict[str, Any] = {
        "at": _utc_text(at),
        "battery_v": battery_v,
        "battery_pct": battery_pct,
        "runtime_s": runtime_s,
        "load_pct": load_pct,
        "input_v": input_v,
        "output_v": output_v,
        "status": status,
    }
    _validate_sample(value)
    return value


def encode(record: Mapping[str, Any]) -> bytes:
    _validate_sample(record)
    return (
        json.dumps(record, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )


def append(path: Path, record: Mapping[str, Any]) -> None:
    line = encode(record)
    if path.name != TELEMETRY_FILENAME:
        raise EventPathError(f"telemetry must be stored as {TELEMETRY_FILENAME}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def read(path: Path) -> MinimalEvent:
    if path.name != TELEMETRY_FILENAME:
        raise EventCorruptionError(f"invalid telemetry filename: {path.name}")
    try:
        lines = path.read_bytes().splitlines(keepends=True)
    except OSError as exc:
        raise EventCorruptionError(f"cannot read telemetry: {path}") from exc
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.endswith(b"\n"):
            raise EventCorruptionError("telemetry has a torn tail")
        try:
            value = json.loads(line[:-1])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EventCorruptionError("telemetry contains invalid JSON") from exc
        if not isinstance(value, dict):
            raise EventCorruptionError("telemetry record is not an object")
        try:
            _validate_sample(value)
        except EventValidationError as exc:
            raise EventCorruptionError(f"invalid telemetry sample: {exc}") from exc
        records.append(dict(value))
    return MinimalEvent(path, tuple(records))


def _validate_sample(value: Mapping[str, Any]) -> None:
    if set(value) != SAMPLE_FIELDS:
        raise EventValidationError(
            "sample fields must be at,battery_v,battery_pct,runtime_s,load_pct,input_v,output_v,status"
        )
    _validate_timestamp(value["at"])
    for field in ("battery_v", "battery_pct", "runtime_s", "load_pct", "input_v", "output_v"):
        number = value[field]
        if number is not None and (
            isinstance(number, bool) or not isinstance(number, (int, float))
        ):
            raise EventValidationError(f"sample {field} must be a number or null")
    if not isinstance(value["status"], str) or not value["status"]:
        raise EventValidationError("sample status must be a non-empty string")


def _validate_timestamp(value: Any) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EventValidationError("timestamp must be UTC with Z suffix")
    try:
        moment = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise EventValidationError("timestamp is not ISO-8601") from exc
    if moment.tzinfo != timezone.utc:
        raise EventValidationError("timestamp must be UTC")


def _utc_text(value: str) -> str:
    _validate_timestamp(value)
    moment = datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")
