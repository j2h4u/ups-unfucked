#!/usr/bin/env python3
"""Print natural physical blackouts reconstructed from raw telemetry."""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
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


def _episodes(path: Path, start: datetime, end: datetime) -> tuple[tuple[str, str | None], ...]:
    found: list[tuple[str, str | None]] = []
    active: tuple[str, str | None] | None = None
    for value in read(path).records:
        active, episode = _advance(active, value)
        if episode is not None and start <= _utc(episode[0]) < end:
            found.append(episode)
    if active is not None and start <= _utc(active[0]) < end:
        found.append(active)
    return tuple(found)


def _advance(
    active: tuple[str, str | None] | None, value: dict[str, object]
) -> tuple[tuple[str, str | None] | None, tuple[str, str | None] | None]:
    at = str(value["at"])
    flags = frozenset(str(value["status"]).split())
    if "OB" in flags and "CAL" not in flags:
        if active is not None and active[1] is not None:
            return (at, None), active
        return active or (at, None), None
    if active is not None and "OL" in flags and "OB" not in flags:
        return None, (active[0], active[1] or at)
    return active, None


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
        state_dir = arguments.state_option or arguments.state_dir or Path.home() / ".config" / "ups-battery-monitor"
        start, end = _period(arguments)
        path = state_dir / "events" / "telemetry.jsonl"
        episodes = _episodes(path, start, end) if path.exists() else ()
    except (EventStoreError, OSError, TypeError, ValueError) as error:
        print(f"blackout-history: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    sys.stdout.write(_render(start, end, episodes) + "\n")


if __name__ == "__main__":
    main()
