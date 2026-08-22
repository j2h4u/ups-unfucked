#!/usr/bin/env python3
"""Print natural physical blackouts reconstructed from raw telemetry."""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters.jsonl_errors import EventStoreError
from src.adapters.minimal_event_file import read


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path, nargs="?")
    parser.add_argument("--state-dir", dest="state_option", type=Path)
    ranges = parser.add_mutually_exclusive_group(required=True)
    ranges.add_argument("--day")
    ranges.add_argument("--month")
    ranges.add_argument("--year", type=int)
    ranges.add_argument("--start")
    parser.add_argument("--end")
    return parser


def _utc(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return moment.astimezone(timezone.utc)


def _period(arguments: argparse.Namespace) -> tuple[datetime, datetime]:
    if arguments.day:
        start = datetime.strptime(arguments.day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return start, start + timedelta(days=1)
    if arguments.month:
        start = datetime.strptime(arguments.month, "%Y-%m").replace(tzinfo=timezone.utc, day=1)
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
        return start, end
    if arguments.year:
        start = datetime(arguments.year, 1, 1, tzinfo=timezone.utc)
        return start, start.replace(year=start.year + 1)
    if not arguments.start or not arguments.end:
        raise ValueError("--start requires --end")
    start, end = _utc(arguments.start), _utc(arguments.end)
    if end <= start:
        raise ValueError("range end must be after start")
    return start, end


def _episodes(
    path: Path,
    start: datetime,
    end: datetime,
    classifications: dict[str, str] | None = None,
) -> tuple[tuple[str, str | None], ...]:
    found: list[tuple[str, str | None]] = []
    active: str | None = None
    saw_cal = False
    for value in read(path).records:
        at = str(value["at"])
        flags = frozenset(str(value["status"]).split())
        if "OB" in flags or "CAL" in flags:
            active = active or at
            saw_cal = saw_cal or "CAL" in flags
        elif active is not None and "OL" in flags:
            kind = (classifications or {}).get(active)
            if kind == "blackout" or (kind is None and not saw_cal):
                if start <= _utc(active) < end:
                    found.append((active, at))
            active, saw_cal = None, False
    if active is not None and start <= _utc(active) < end:
        kind = (classifications or {}).get(active)
        if kind == "blackout" or (kind is None and not saw_cal):
            found.append((active, None))
    return tuple(found)


def _classifications(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"history line {line_number} is not an object")
        if value.get("kind") in {"blackout", "self_test"}:
            result[str(value["at"])] = str(value["kind"])
    return result


def _render(start: datetime, end: datetime, episodes: tuple[tuple[str, str | None], ...]) -> str:
    lines = [
        f"UTC range: [{start.isoformat(timespec='seconds').replace('+00:00', 'Z')}, "
        f"{end.isoformat(timespec='seconds').replace('+00:00', 'Z')})",
        f"Natural blackouts: {len(episodes)}",
    ]
    lines.extend(
        f"- {loss} mains loss; restoration: {restoration or 'not observed'}"
        for loss, restoration in episodes
    )
    if not episodes:
        lines.append("No natural blackouts in range.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        state_dir = (
            arguments.state_option
            or arguments.state_dir
            or Path.home() / ".config" / "ups-battery-monitor"
        )
        start, end = _period(arguments)
        path = state_dir / "events" / "telemetry.jsonl"
        history = _classifications(state_dir / "events" / "history.jsonl")
        episodes = _episodes(path, start, end, history) if path.exists() else ()
    except (EventStoreError, json.JSONDecodeError, OSError, TypeError, ValueError) as error:
        print(f"blackout-history: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    sys.stdout.write(_render(start, end, episodes) + "\n")


if __name__ == "__main__":
    main()
