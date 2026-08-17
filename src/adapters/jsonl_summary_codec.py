"""Canonical summary and bounded index-line codecs."""

import json
import math
import os
import stat
from pathlib import Path

from src.adapters.jsonl_errors import (
    EventCorruptionError,
    EventPathError,
    EventPersistenceError,
    EventValidationError,
)
from src.adapters.jsonl_record_codec import (
    MAX_DAMAGED_HASHES,
    MAX_EPOCH_INDEX_SCAN_BYTES,
    MAX_INDEX_LINE_BYTES,
    SCHEMA_VERSION,
    SUMMARY_FIELDS,
    _bounded_error,
    _is_sha256,
    _parse_utc,
    _strict_json_loads,
    _validate_path_token,
    _validate_short_ascii,
    _validate_uuid4_hex,
    canonical_json_bytes,
)
from src.application.storage_values import EventSummary


def _encode_summary(summary: EventSummary) -> bytes:
    obj = {
        "schema_version": summary.schema_version,
        "blackout_id": summary.blackout_id,
        "segment_filename": summary.segment_filename,
        "started_utc": summary.started_utc,
        "ended_utc": summary.ended_utc,
        "termination": summary.termination,
        "evidence_class": summary.evidence_class,
        "disposition": summary.disposition,
        "duration_s": summary.duration_s,
        "observation_count": summary.observation_count,
        "battery_epoch_id": summary.battery_epoch_id,
        "comparison_available": summary.comparison_available,
        "comparison_mode": summary.comparison_mode,
        "ir_estimate_available": summary.ir_estimate_available,
        "commit_receipt_id": summary.commit_receipt_id,
        "damaged_segment_hashes": list(summary.damaged_segment_hashes),
        "damaged_segment_overflow": summary.damaged_segment_overflow,
        "outcome_record_sha256": summary.outcome_record_sha256,
        "event_file_sha256": summary.event_file_sha256,
    }
    line = canonical_json_bytes(obj) + b"\n"
    if len(line) > MAX_INDEX_LINE_BYTES:
        raise EventValidationError("fixed event summary exceeds 4 KiB")
    return line


def _decode_summary_line(line: bytes) -> EventSummary:
    if not line.endswith(b"\n") or len(line) > MAX_INDEX_LINE_BYTES:
        raise EventCorruptionError("index summary line is incomplete or exceeds 4 KiB")
    try:
        value = _strict_json_loads(line[:-1])
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EventCorruptionError("index summary is not strict JSON") from exc
    if not isinstance(value, dict) or set(value) != SUMMARY_FIELDS:
        raise EventCorruptionError("index summary fields do not match fixed schema")
    if canonical_json_bytes(value) + b"\n" != line:
        raise EventCorruptionError("index summary is not canonical JSON")
    try:
        summary = EventSummary(
            schema_version=value["schema_version"],
            blackout_id=value["blackout_id"],
            segment_filename=value["segment_filename"],
            started_utc=value["started_utc"],
            ended_utc=value["ended_utc"],
            termination=value["termination"],
            evidence_class=value["evidence_class"],
            disposition=value["disposition"],
            duration_s=value["duration_s"],
            observation_count=value["observation_count"],
            battery_epoch_id=value["battery_epoch_id"],
            comparison_available=value["comparison_available"],
            comparison_mode=value["comparison_mode"],
            ir_estimate_available=value["ir_estimate_available"],
            commit_receipt_id=value["commit_receipt_id"],
            damaged_segment_hashes=tuple(value["damaged_segment_hashes"]),
            damaged_segment_overflow=value["damaged_segment_overflow"],
            outcome_record_sha256=value["outcome_record_sha256"],
            event_file_sha256=value["event_file_sha256"],
        )
    except (KeyError, TypeError) as exc:
        raise EventCorruptionError("index summary values are invalid") from exc
    _validate_summary(summary)
    return summary


def _validate_summary(summary: EventSummary) -> None:
    if summary.schema_version != SCHEMA_VERSION:
        raise EventCorruptionError("index summary schema version is not 2")
    _validate_uuid4_hex(summary.blackout_id, "blackout_id")
    _validate_path_token(summary.segment_filename)
    _parse_utc(summary.started_utc)
    if summary.ended_utc is not None:
        _parse_utc(summary.ended_utc)
    for value, name in (
        (summary.termination, "termination"),
        (summary.evidence_class, "evidence_class"),
        (summary.disposition, "disposition"),
        (summary.battery_epoch_id, "battery_epoch_id"),
        (summary.commit_receipt_id, "commit_receipt_id"),
    ):
        if value is not None:
            _validate_short_ascii(value, name, 128)
    if summary.comparison_mode not in {"full", "short_window", "none"}:
        raise EventCorruptionError("index comparison mode is invalid")
    _validate_summary_counts_and_hashes(summary)


def _validate_summary_counts_and_hashes(summary: EventSummary) -> None:
    if (
        isinstance(summary.observation_count, bool)
        or not isinstance(summary.observation_count, int)
        or summary.observation_count < 0
    ):
        raise EventCorruptionError("index observation count is invalid")
    if summary.duration_s is not None and (
        isinstance(summary.duration_s, bool)
        or not isinstance(summary.duration_s, (int, float))
        or not math.isfinite(summary.duration_s)
        or summary.duration_s < 0
    ):
        raise EventCorruptionError("index duration is invalid")
    if len(summary.damaged_segment_hashes) > MAX_DAMAGED_HASHES:
        raise EventCorruptionError("index damaged hashes exceed fixed bound")
    if not all(_is_sha256(value) for value in summary.damaged_segment_hashes):
        raise EventCorruptionError("index damaged segment hash is invalid")
    if (
        isinstance(summary.damaged_segment_overflow, bool)
        or not isinstance(summary.damaged_segment_overflow, int)
        or summary.damaged_segment_overflow < 0
    ):
        raise EventCorruptionError("index damaged overflow is invalid")
    if not _is_sha256(summary.outcome_record_sha256) or not _is_sha256(summary.event_file_sha256):
        raise EventCorruptionError("index evidence hash is invalid")


def _summary_key(summary: EventSummary) -> tuple[str, str]:
    return summary.blackout_id, summary.outcome_record_sha256


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


def _bounded_file_suffix(path: Path, max_bytes: int) -> tuple[bytes, bool]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise EventPathError("summary index is not a regular file")
        start = max(0, info.st_size - max_bytes)
        os.lseek(descriptor, start, os.SEEK_SET)
        data = os.read(descriptor, max_bytes)
    except OSError as exc:
        raise EventPersistenceError(
            f"cannot read bounded index suffix: {_bounded_error(exc)}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if data and not data.endswith(b"\n"):
        raise EventCorruptionError("summary index has a torn tail")
    return data, start == 0


def _bounded_epoch_summaries(
    path: Path,
    battery_epoch_id: str,
) -> tuple[tuple[EventSummary, ...], bool]:
    raw, reached_start = _bounded_file_suffix(path, MAX_EPOCH_INDEX_SCAN_BYTES)
    lines = raw.splitlines(keepends=True)
    if not reached_start and lines:
        lines = lines[1:]
    matches: list[EventSummary] = []
    crossed_older_boundary = False
    seen_current = False
    for line in reversed(lines):
        summary = _decode_summary_line(line)
        if summary.battery_epoch_id == battery_epoch_id:
            seen_current = True
            matches.append(summary)
            continue
        if seen_current:
            crossed_older_boundary = True
            # Other epochs are not an ordering contract: a valid index can
            # contain entries from more than one writer/rebuild generation.
            # Keep scanning the bounded suffix so filtering is independent of
            # interleaving, while the first older different epoch still gives
            # us the reset barrier needed to declare the scan complete.
    return tuple(reversed(matches)), reached_start or crossed_older_boundary
