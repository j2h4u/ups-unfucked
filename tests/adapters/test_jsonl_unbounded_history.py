"""Durable bounded-index regressions for long histories and damaged evidence."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.adapters.jsonl_errors import EventConflictError
from src.adapters.jsonl_event_catalog import _encode_entry
from src.adapters.jsonl_event_store import JsonlEventStore
from src.adapters.jsonl_filesystem import JsonlFilesystem
from src.adapters.jsonl_index_merge import IndexMergeCoordinator, IndexMergePaths
from src.adapters.jsonl_record_codec import (
    EMPTY_SHA256,
    _decode_record_line,
    canonical_record_line,
)
from src.adapters.jsonl_summary_codec import _encode_summary
from src.application.storage_values import EventSummary

_EPOCH = "epoch-a"
_OTHER_EPOCH = "epoch-b"
_EMPTY_HASH = EMPTY_SHA256


def _token(number: int, blackout_id: str) -> str:
    wall = datetime(2026, 8, 16, tzinfo=timezone.utc) + timedelta(seconds=number)
    stamp = wall.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return f"evt-{stamp.replace('-', '').replace(':', '')}-{blackout_id}.jsonl"


def _summary(number: int, *, epoch: str = _EPOCH, wide: bool = False) -> EventSummary:
    blackout_id = uuid.UUID(int=number + 1, version=4).hex
    token = _token(number, blackout_id)
    wall = datetime(2026, 8, 16, tzinfo=timezone.utc) + timedelta(seconds=number)
    started = wall.isoformat(timespec="microseconds").replace("+00:00", "Z")
    ended = (wall + timedelta(seconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    long_text = "x" * 128 if wide else "evidence"
    return EventSummary(
        schema_version=2,
        blackout_id=blackout_id,
        segment_filename=token,
        started_utc=started,
        ended_utc=ended,
        termination="power_restored",
        evidence_class=long_text,
        disposition="recorded_only",
        duration_s=1.0,
        observation_count=1,
        battery_epoch_id=epoch,
        comparison_available=False,
        comparison_mode="none",
        ir_estimate_available=False,
        commit_receipt_id="c" * 128 if wide else None,
        damaged_segment_hashes=tuple("f" * 64 for _ in range(8 if wide else 0)),
        damaged_segment_overflow=0,
        outcome_record_sha256="a" * 64,
        event_file_sha256="b" * 64,
    )


def _write_index(root: Path, summaries: list[EventSummary]) -> Path:
    events = root / "events"
    events.mkdir(parents=True, exist_ok=True)
    events.chmod(0o700)
    path = events / "index.jsonl"
    if path.exists():
        path.chmod(0o600)
    path.write_bytes(b"".join(_encode_summary(summary) for summary in summaries))
    path.chmod(0o400)
    return path


def test_healthy_index_append_and_restart_stays_bounded_past_4mib_and_10k(
    tmp_path: Path,
) -> None:
    summaries = [_summary(number, wide=True) for number in range(10_001)]
    index_path = _write_index(tmp_path, summaries)
    assert index_path.stat().st_size > 4 * 1024 * 1024

    with JsonlEventStore(tmp_path) as store:
        appended = _summary(10_001, wide=True)
        index_path.chmod(0o600)
        store._index._metadata._rebuild_index_head()
        store._index._metadata._append_summary(index_path, appended, _encode_summary(appended))
        assert tuple(item.blackout_id for item in store.index_tail(3)) == tuple(
            item.blackout_id for item in (*summaries[-2:], appended)
        )
        assert store.index_tail(1)[0].blackout_id == appended.blackout_id
        index_path.chmod(0o400)

    with JsonlEventStore(tmp_path) as restarted:
        tail = restarted.index_tail(1)
        assert len(tail) == 1
        assert tail[0].blackout_id == appended.blackout_id
        assert index_path.stat().st_size > 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _RecordSpec:
    record_type: str
    blackout_id: str
    segment_id: str
    seq: int
    previous: str | None
    payload: dict[str, Any]


def _record_line(spec: _RecordSpec) -> bytes:
    provenance = {
        "gap": "system",
        "outcome": "derived",
    }.get(spec.record_type, "physical")
    return canonical_record_line(
        {
            "schema_version": 2,
            "record_type": spec.record_type,
            "provenance": provenance,
            "blackout_id": spec.blackout_id,
            "segment_id": spec.segment_id,
            "seq": spec.seq,
            "boot_id": "boot-a",
            "wall_time_utc": "2026-08-16T00:00:00.000000Z",
            "monotonic_ns": spec.seq + 1,
            "prev_record_sha256": spec.previous,
            "payload": spec.payload,
        }
    )


def _write_fixture_events(
    root: Path,
    count: int,
    *,
    large: bool = False,
    wide_summary: bool = False,
) -> list[EventSummary]:
    events = root / "events"
    events.mkdir(parents=True, exist_ok=True)
    events.chmod(0o700)
    catalog_lines: list[bytes] = []
    summaries: list[EventSummary] = []
    previous_catalog = _EMPTY_HASH
    for number in range(count):
        blackout_id = uuid.UUID(int=number + 1, version=4).hex
        segment_id = uuid.UUID(int=number + 10_000, version=4).hex
        token = _token(number, blackout_id)
        records = [
            _record_line(
                _RecordSpec(
                    "start",
                    blackout_id,
                    segment_id,
                    0,
                    None,
                    {"battery_epoch_id": "e" * 128 if wide_summary else _EPOCH},
                )
            )
        ]
        previous = _decode_record_line(records[-1]).record_sha256
        if large:
            for seq in range(1, 360):
                records.append(
                    _record_line(
                        _RecordSpec(
                            "observation",
                            blackout_id,
                            segment_id,
                            seq,
                            previous,
                            {"sample": "x" * 2_000, "voltage": 12.0},
                        )
                    )
                )
                previous = _decode_record_line(records[-1]).record_sha256
        records.append(
            _record_line(
                _RecordSpec(
                    "end",
                    blackout_id,
                    segment_id,
                    len(records),
                    previous,
                    {"termination": "power_restored"},
                )
            )
        )
        previous = _decode_record_line(records[-1]).record_sha256
        records.append(
            _record_line(
                _RecordSpec(
                    "outcome",
                    blackout_id,
                    segment_id,
                    len(records),
                    previous,
                    {
                        "disposition": "recorded_only",
                        "comparison_mode": "none",
                        "duration_s": 1.0,
                        "evidence_class": "e" * 128 if wide_summary else None,
                        "commit_receipt_id": "c" * 128 if wide_summary else None,
                    },
                )
            )
        )
        event_path = events / token
        event_path.write_bytes(b"".join(records))
        event_path.chmod(0o400)
        manifest = events / f"segments-{blackout_id}.jsonl"
        manifest.write_bytes(
            json.dumps(
                {"damaged_sha256": None, "path_token": token}, separators=(",", ":")
            ).encode()
            + b"\n"
        )
        manifest.chmod(0o600)
        catalog_line = _encode_entry(len(catalog_lines), token, previous_catalog)
        catalog_lines.append(catalog_line)
        previous_catalog = json.loads(catalog_line)["entry_sha256"]
        summaries.append(_summary(number, wide=True))
    catalog = events / "event-catalog.jsonl"
    catalog.write_bytes(b"".join(catalog_lines))
    catalog.chmod(0o600)
    return summaries


def _rebuild_to_ready(store: JsonlEventStore, *, max_files: int = 32) -> int:
    ticks = 0
    while not store.maintenance.rebuild_index_tick(
        max_files=max_files,
        max_bytes=4 * 1024 * 1024,
        max_wall_s=0.20,
    ):
        ticks += 1
        if ticks > 512:
            raise AssertionError("bounded rebuild did not become ready")
    return ticks + 1


def test_rebuild_promote_restart_handles_index_past_4mib(tmp_path: Path) -> None:
    _write_fixture_events(tmp_path, 300)
    index_path = _write_index(tmp_path, [_summary(number, wide=True) for number in range(3_000)])
    assert index_path.stat().st_size > 4 * 1024 * 1024
    index_path.unlink()

    with JsonlEventStore(tmp_path) as store:
        ticks = _rebuild_to_ready(store)
        assert ticks > 1
        merged = tmp_path / "events" / "index.rebuild.merged.jsonl"
        padding = tmp_path / "events" / "index.rebuild.padding.jsonl"
        padding.write_bytes(
            b"".join(
                _encode_summary(_summary(number, wide=True)) for number in range(10_000, 13_000)
            )
        )
        expanded = tmp_path / "events" / "index.rebuild.expanded.jsonl"
        expanded.write_bytes(merged.read_bytes() + padding.read_bytes())
        merged.unlink()
        expanded.rename(merged)
        merged.chmod(0o400)
        cursor_path = tmp_path / "events" / "index-rebuild.cursor.json"
        cursor = json.loads(cursor_path.read_bytes())
        cursor.update(
            {
                "phase": "prepared",
                "merge_output_offset": merged.stat().st_size,
                "merge_verify_offset": merged.stat().st_size,
            }
        )
        store._index._metadata.write_rebuild_cursor(cursor)
        assert merged.stat().st_size > 4 * 1024 * 1024

    with JsonlEventStore(tmp_path) as restarted:
        restarted.maintenance.promote_index_rebuild()
        assert restarted.index_tail(1)[0].blackout_id == _summary(12_999, wide=True).blackout_id


def test_large_event_scans_across_bounded_ticks_and_promotes(tmp_path: Path) -> None:
    _write_fixture_events(tmp_path, 1, large=True)
    started = time.monotonic()
    with JsonlEventStore(tmp_path) as store:
        ticks = _rebuild_to_ready(store, max_files=1)
        elapsed = time.monotonic() - started
        store.maintenance.promote_index_rebuild()
        assert ticks >= 2
        assert elapsed < 10.0
        assert store.index_tail(1)


def test_merge_owner_sorts_deduplicates_and_rejects_conflicting_summaries(
    tmp_path: Path,
) -> None:
    class Host:
        def __init__(self) -> None:
            self.cursor: dict[str, Any] | None = None

        def _read_cursor_if_present(self) -> dict[str, Any] | None:
            return self.cursor

        def _write_rebuild_cursor(self, cursor: dict[str, Any]) -> None:
            self.cursor = cursor

        def _clear_rebuild_metadata(self) -> None:
            self.cursor = None

        def _unlink_projection_file(self, path: Path) -> None:
            path.unlink(missing_ok=True)

    def coordinator(root: Path) -> tuple[IndexMergeCoordinator, Path]:
        events = root / "events"
        events.mkdir(parents=True)
        events.chmod(0o700)
        rebuild = events / "rebuild.jsonl"
        filesystem = JsonlFilesystem(
            root,
            fault_hook=None,
            monotonic_clock_ns=time.monotonic_ns,
        )
        paths = IndexMergePaths(
            events_path=events,
            index_path=events / "index.jsonl",
            rebuild_path=rebuild,
            merge_path=events / "merged.jsonl",
            delta_path=events / "delta.jsonl",
        )
        return (
            IndexMergeCoordinator(
                filesystem,
                Host(),
                paths,
                lambda: "2026-08-21T00:00:00.000000Z",
            ),
            rebuild,
        )

    first, rebuild = coordinator(tmp_path / "ordered")
    older = _summary(0)
    newer = _summary(1)
    rebuild.write_bytes(_encode_summary(newer) + _encode_summary(older) + _encode_summary(older))
    first.begin({"phase": "project"})
    assert rebuild.read_bytes() == _encode_summary(older) + _encode_summary(newer)

    conflicting, conflict_path = coordinator(tmp_path / "conflict")
    conflict_path.write_bytes(
        _encode_summary(older) + _encode_summary(replace(older, evidence_class="conflicting-bytes"))
    )
    with pytest.raises(EventConflictError, match="summary duplicate key"):
        conflicting.begin({"phase": "project"})


def test_epoch_scan_reports_older_boundary_but_not_current_epoch_truncation(
    tmp_path: Path,
) -> None:
    _write_index(
        tmp_path,
        [
            _summary(0, epoch=_OTHER_EPOCH, wide=True),
            *[_summary(n, wide=True) for n in range(1, 8)],
        ],
    )
    with JsonlEventStore(tmp_path) as store:
        assert store.index_scan_for_decline_epoch(_EPOCH).scan_complete

    truncated = tmp_path / "events" / "index.jsonl"
    truncated.chmod(0o600)
    truncated.write_bytes(
        b"".join(_encode_summary(_summary(number, wide=True)) for number in range(10_000))
    )
    with JsonlEventStore(tmp_path) as store:
        assert not store.index_scan_for_decline_epoch(_EPOCH).scan_complete
