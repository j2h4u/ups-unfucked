#!/usr/bin/env python3
"""Print bounded human history from sealed blackout and recharge JSONL."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapters.minimal_jsonl import MinimalJsonlEventStore
from src.application.history_query import (
    HistoryRange,
    HistoryResult,
    parse_utc,
    query_history,
    utc_day,
    utc_month,
    utc_year,
)


def render_history(result: HistoryResult) -> str:
    """Render deterministic oldest-first operator lines."""
    lines = [
        f"UTC range: [{result.period.start_utc}, {result.period.end_utc})",
        "Order: oldest first",
        f"Blackouts: {len(result.entries)}",
    ]
    for entry in result.entries:
        lines.append(f"- {entry.loss_utc} mains loss {entry.blackout_id}")
        lines.append(f"  restoration: {entry.restoration_utc or 'not observed'}")
        lines.append(f"  termination: {entry.termination or 'not recorded'}")
        lines.append(f"  outcome: {entry.disposition}")
        lines.append(
            f"  learning: {entry.learning.status}; reason: {'; '.join(entry.learning.reasons)}"
        )
        if entry.recharge is not None:
            lines.append(
                f"  recharge: {entry.recharge.outcome} {entry.recharge.episode_id}; "
                f"reason: {entry.recharge.reason}"
            )
        if entry.evidence_damage:
            lines.append(f"  evidence: {'; '.join(entry.evidence_damage)}")
        else:
            lines.append("  evidence: no recorded damage or loss")
    if not result.entries:
        lines.append("No sealed blackouts in range.")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path, nargs="?", help="daemon model/state directory")
    parser.add_argument("--state-dir", dest="state_option", type=Path)
    ranges = parser.add_mutually_exclusive_group(required=True)
    ranges.add_argument("--day", help="UTC calendar day YYYY-MM-DD")
    ranges.add_argument("--month", help="UTC calendar month YYYY-MM")
    ranges.add_argument("--year", type=int, help="UTC calendar year YYYY")
    ranges.add_argument("--start", help="UTC range start; pair with --end")
    parser.add_argument("--end", help="UTC range end for --start")
    return parser


def _period(arguments: argparse.Namespace) -> HistoryRange:
    if arguments.day is not None:
        try:
            year, month, day = (int(item) for item in arguments.day.split("-"))
            return utc_day(year, month, day)
        except (TypeError, ValueError) as exc:
            raise ValueError("--day must be YYYY-MM-DD") from exc
    if arguments.month is not None:
        try:
            year, month = (int(item) for item in arguments.month.split("-"))
            return utc_month(year, month)
        except (TypeError, ValueError) as exc:
            raise ValueError("--month must be YYYY-MM") from exc
    if arguments.year is not None:
        return utc_year(arguments.year)
    if arguments.start is None or arguments.end is None:
        raise ValueError("--start requires --end")
    return HistoryRange(parse_utc(arguments.start), parse_utc(arguments.end))


def main(argv: list[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    state_dir = (
        arguments.state_option
        or arguments.state_dir
        or Path.home() / ".config" / "ups-battery-monitor"
    )
    period = _period(arguments)
    store = MinimalJsonlEventStore(state_dir)
    try:
        result = query_history(store, period)
    finally:
        store.close()
    sys.stdout.write(render_history(result) + "\n")


if __name__ == "__main__":
    main()
