#!/usr/bin/env python3
"""One-time removal of integrity-hash fields from domain event JSONL.

The command is dry-run by default.  Pass ``--apply`` only after reviewing the
listed files.  Each changed file is replaced atomically in place; no backup
copy containing the old hash fields is created.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

HASH_FIELDS = frozenset(
    {
        "prev_record_sha256",
        "record_sha256",
        "previous_final_record_sha256",
        "previous_segment_file_sha256",
        "damaged_segment_sha256",
        "damaged_segment_hashes",
        "outcome_record_sha256",
        "event_file_sha256",
        "summary_sha256",
        "index_head_sha256",
        "previous_notice_sha256",
        "notice_sha256",
        "original_sha256",
        "last_record_hash",
        "damaged_sha256",
    }
)
HASHED_JSONL_NAMES = frozenset({"report-outbox.jsonl"})


def _targets(events_dir: Path) -> tuple[Path, ...]:
    candidates = [
        *events_dir.glob("evt-*.jsonl"),
        *events_dir.glob("corrupt-evt-*.jsonl"),
        *events_dir.glob("segments-*.jsonl"),
        *(events_dir / name for name in HASHED_JSONL_NAMES),
        events_dir / "active.json",
        events_dir / "report-outbox.cursor.json",
    ]
    return tuple(sorted({path for path in candidates if path.is_file()}))


def _remove_hash_fields(value: Any) -> tuple[Any, int]:
    if isinstance(value, dict):
        removed = 0
        dict_result: dict[str, Any] = {}
        for key, item in value.items():
            if key in HASH_FIELDS:
                removed += 1
                continue
            clean, nested_removed = _remove_hash_fields(item)
            dict_result[key] = clean
            removed += nested_removed
        return dict_result, removed
    if isinstance(value, list):
        list_result: list[Any] = []
        removed = 0
        for item in value:
            clean, nested_removed = _remove_hash_fields(item)
            list_result.append(clean)
            removed += nested_removed
        return list_result, removed
    return value, 0


def _transform(path: Path) -> tuple[bytes, int]:
    raw = path.read_bytes()
    output: list[bytes] = []
    removed = 0
    for line_number, line in enumerate(raw.splitlines(keepends=True), start=1):
        if not line.endswith(b"\n"):
            raise ValueError(f"{path}: line {line_number} is not newline terminated")
        value = json.loads(line[:-1])
        clean, count = _remove_hash_fields(value)
        output.append(
            json.dumps(clean, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        removed += count
    return b"".join(output), removed


def _replace(path: Path, data: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("events_dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    events_dir = args.events_dir.resolve()
    if not events_dir.is_dir():
        parser.error(f"events directory is missing: {events_dir}")
    changed = 0
    removed_total = 0
    for path in _targets(events_dir):
        transformed, removed = _transform(path)
        if removed == 0:
            continue
        changed += 1
        removed_total += removed
        print(f"{path}: remove {removed} hash field(s)")
        if args.apply:
            _replace(path, transformed)
    action = "updated" if args.apply else "would update"
    print(f"{action} {changed} file(s), removed {removed_total} field(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
