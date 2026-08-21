"""Canonical summaries and bounded event-record line helpers."""

from pathlib import Path

from src.adapters.jsonl_errors import (
    EventCorruptionError,
    EventPersistenceError,
    EventValidationError,
)
from src.adapters.jsonl_record_codec import (
    MAX_FIXED_LINE_BYTES,
    _bounded_error,
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
    if len(line) > MAX_FIXED_LINE_BYTES:
        raise EventValidationError("fixed event summary exceeds 4 KiB")
    return line


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
