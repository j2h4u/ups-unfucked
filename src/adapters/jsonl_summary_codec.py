"""Canonical summaries and bounded event-record line helpers."""

from pathlib import Path

from src.adapters.jsonl_errors import (
    EventCorruptionError,
    EventPersistenceError,
)
from src.adapters.jsonl_record_codec import (
    _bounded_error,
)


def _iter_complete_lines(path: Path, max_line_bytes: int):
    try:
        with path.open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    raise EventCorruptionError(f"{path.name} has a torn tail")
                if len(line) > max_line_bytes:
                    raise EventCorruptionError(f"{path.name} contains an oversized line")
                yield line
    except OSError as exc:
        raise EventPersistenceError(f"cannot read {path.name}: {_bounded_error(exc)}") from exc


def _bounded_tail_lines(path: Path, limit: int, max_line_bytes: int) -> tuple[bytes, ...]:
    if limit <= 0:
        return ()
    max_bytes = limit * max_line_bytes + 1
    try:
        with path.open("rb") as stream:
            size = path.stat().st_size
            start = max(0, size - max_bytes)
            stream.seek(start)
            data = stream.read(max_bytes)
    except OSError as exc:
        raise EventPersistenceError(f"cannot read bounded tail: {_bounded_error(exc)}") from exc
    if start > 0:
        first_newline = data.find(b"\n")
        if first_newline < 0:
            raise EventCorruptionError("bounded tail contains an oversized line")
        data = data[first_newline + 1 :]
    if data and not data.endswith(b"\n"):
        raise EventCorruptionError("bounded tail is not newline terminated")
    lines = tuple(line + b"\n" for line in data.splitlines())
    if any(len(line) > max_line_bytes for line in lines):
        raise EventCorruptionError("bounded tail contains an oversized line")
    return lines[-limit:]
