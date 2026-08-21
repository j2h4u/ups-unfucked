#!/usr/bin/env python3
"""Backfill the legacy event journal into one strict telemetry JSONL file.

The command is dry-run by default. ``--apply`` stages and validates the one
output before replacing anything in ``events/``. Legacy event envelopes,
identities, derived records, and sidecars are never copied to the output.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TELEMETRY_NAME = "telemetry.jsonl"
TELEMETRY_FIELDS = frozenset(
    {"at", "battery_v", "battery_pct", "runtime_s", "load_pct", "input_v", "output_v", "status"}
)
SIDECAR_GLOBS = (
    "active.json",
    "segments-*.jsonl",
    "report-outbox.jsonl",
    "report-outbox.cursor.json",
)


def _event_sources(events_dir: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in events_dir.glob("evt-*.jsonl") if path.is_file()))


def _legacy_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for number, raw in enumerate(path.read_bytes().splitlines(keepends=True), start=1):
        if not raw.endswith(b"\n"):
            raise ValueError(f"{path}: line {number} is not newline terminated")
        try:
            value = json.loads(raw[:-1])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path}: line {number} is invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path}: line {number} is not an object")
        records.append(value)
    if not records:
        raise ValueError(f"{path}: event is empty")
    return records


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("payload", record)
    if not isinstance(value, dict):
        raise ValueError("legacy payload is not an object")
    return value


def _timestamp(value: Any, path: Path) -> tuple[str, datetime]:
    if not isinstance(value, str):
        raise ValueError(f"{path}: sample timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{path}: sample timestamp is invalid: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{path}: sample timestamp is not UTC: {value!r}")
    return value, parsed.astimezone(timezone.utc)


def _number(value: Any, field: str, path: Path) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path}: {field} is not numeric")
    return value


def _sample_from_legacy(
    record: dict[str, Any], path: Path
) -> tuple[dict[str, Any], datetime] | None:
    if record.get("record_type") not in {"start", "observation"}:
        return None
    payload = _payload(record)
    value = payload.get("observation", payload)
    if not isinstance(value, dict):
        return None
    if "battery_voltage_v" not in value and "battery_v" not in value:
        return None
    at, parsed = _timestamp(value.get("wall_time_utc", record.get("wall_time_utc")), path)
    status = value.get("raw_status", value.get("status"))
    if not isinstance(status, str):
        raise ValueError(f"{path}: sample status is missing or not a string")
    row = {
        "at": at,
        "battery_v": _number(
            value.get("battery_voltage_v", value.get("battery_v")), "battery_v", path
        ),
        "battery_pct": _number(
            value.get("battery_percent", value.get("battery_pct")), "battery_pct", path
        ),
        "runtime_s": _number(
            value.get("runtime_seconds", value.get("runtime_s")), "runtime_s", path
        ),
        "load_pct": _number(value.get("load_percent", value.get("load_pct")), "load_pct", path),
        "input_v": _number(value.get("input_voltage_v", value.get("input_v")), "input_v", path),
        "output_v": _number(value.get("output_voltage_v", value.get("output_v")), "output_v", path),
        "status": status,
    }
    return row, parsed


def _collect_samples(sources: tuple[Path, ...]) -> list[dict[str, Any]]:
    collected: list[tuple[datetime, int, int, dict[str, Any]]] = []
    for source_index, path in enumerate(sources):
        for line_number, record in enumerate(_legacy_records(path)):
            converted = _sample_from_legacy(record, path)
            if converted is not None:
                row, parsed = converted
                collected.append((parsed, source_index, line_number, row))
    collected.sort(key=lambda item: item[:3])
    return [row for _parsed, _source, _line, row in collected]


def _encode(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(
        json.dumps(row, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        + b"\n"
        for row in rows
    )


def _validate_telemetry(path: Path) -> int:
    previous: datetime | None = None
    count = 0
    for number, raw in enumerate(path.read_bytes().splitlines(keepends=True), start=1):
        if not raw.endswith(b"\n"):
            raise ValueError(f"{path}: line {number} is not newline terminated")
        try:
            row = json.loads(raw[:-1])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path}: line {number} is invalid JSON") from exc
        if not isinstance(row, dict) or set(row) != TELEMETRY_FIELDS:
            raise ValueError(f"{path}: line {number} does not have the telemetry schema")
        _at, parsed = _timestamp(row["at"], path)
        if previous is not None and parsed < previous:
            raise ValueError(f"{path}: samples are not chronological at line {number}")
        previous = parsed
        for field in TELEMETRY_FIELDS - {"at", "status"}:
            _number(row[field], field, path)
        if not isinstance(row["status"], str):
            raise ValueError(f"{path}: line {number} status is not a string")
        count += 1
    if count == 0:
        raise ValueError(f"{path}: telemetry output is empty")
    return count


def _write_stage(events_dir: Path, data: bytes) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix=f".{TELEMETRY_NAME}.", dir=events_dir)
    temporary = Path(raw_path)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _sidecars(events_dir: Path) -> tuple[Path, ...]:
    return tuple(
        path for pattern in SIDECAR_GLOBS for path in events_dir.glob(pattern) if path.is_file()
    )


def _cleanup(path: Path) -> None:
    path.unlink(missing_ok=True)


def _apply(events_dir: Path, sources: tuple[Path, ...], rows: list[dict[str, Any]]) -> None:
    target = events_dir / TELEMETRY_NAME
    stage = _write_stage(events_dir, _encode(rows))
    try:
        _validate_telemetry(stage)
        os.replace(stage, target)
        stage = None
        for source in sources:
            source.unlink()
        for sidecar in _sidecars(events_dir):
            sidecar.unlink()
        print(f"migrated {len(rows)} samples into {target.name}; removed legacy files and sidecars")
    finally:
        if stage is not None:
            _cleanup(stage)


def _dry_run(events_dir: Path, sources: tuple[Path, ...], rows: list[dict[str, Any]]) -> None:
    data = _encode(rows)
    with tempfile.NamedTemporaryFile(
        dir=events_dir, prefix=".validate-", suffix=".jsonl"
    ) as validation:
        validation.write(data)
        validation.flush()
        _validate_telemetry(Path(validation.name))
    for source in sources:
        print(f"would remove {source.name}")
    print(f"would migrate {len(rows)} samples into {TELEMETRY_NAME}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("events_dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    events_dir = args.events_dir.resolve()
    if not events_dir.is_dir():
        parser.error(f"events directory is missing: {events_dir}")
    sources = _event_sources(events_dir)
    target = events_dir / TELEMETRY_NAME
    if not sources:
        if target.is_file():
            print(
                f"telemetry already present: {target.name} ({_validate_telemetry(target)} samples)"
            )
        else:
            print("nothing to migrate")
        return 0
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing {target}")
    rows = _collect_samples(sources)
    if not rows:
        raise ValueError("legacy events contain no observation samples")
    if args.apply:
        _apply(events_dir, sources, rows)
    else:
        _dry_run(events_dir, sources, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
