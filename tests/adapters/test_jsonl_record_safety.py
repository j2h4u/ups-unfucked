"""Focused safety regressions for durable JSONL boundaries."""

import hashlib
import uuid
from pathlib import Path

import pytest

from src.adapters.jsonl_errors import EventCorruptionError, EventValidationError
from src.adapters.jsonl_event_store import JsonlEventStore
from src.adapters.jsonl_event_stream import _corrupt_original_filename
from src.adapters.jsonl_record_codec import (
    _bounded_start_payload,
    _is_terminal_damage_segment,
    _StoredRecord,
    _validate_gap_link,
    _validate_segment_boundaries,
)
from src.application.storage_values import EventStart

BLACKOUT = uuid.UUID(int=101, version=4).hex


def _record(record_type: str, segment: str = "a" * 32, **payload: object) -> _StoredRecord:
    return _StoredRecord(
        2,
        record_type,
        "system" if record_type == "gap" else "physical",
        BLACKOUT,
        segment,
        0,
        "boot",
        "2026-08-01T00:00:00.000000Z",
        0,
        None,
        payload,
        "b" * 64,
        b"{}\n",
    )


def test_start_payload_overflow_is_bounded_and_disables_comparison() -> None:
    payload = {
        "frozen_model": {"blob": "x" * 70_000},
        "reasons": [f"reason-{number}" for number in range(8)],
    }
    bounded = _bounded_start_payload(payload)
    assert bounded["comparison_allowed"] is False
    assert bounded["snapshot_budget_exceeded"] is True
    assert bounded["reason_overflow"] == 1


def test_start_payload_rejects_non_object_snapshot() -> None:
    with pytest.raises(EventValidationError, match="must be an object"):
        _bounded_start_payload({"frozen_model": "not-an-object"})


@pytest.mark.parametrize(
    "payload",
    [
        {"previous_segment_id": "wrong"},
        {"previous_final_record_sha256": "wrong"},
        {"previous_segment_file_sha256": "wrong"},
    ],
)
def test_gap_links_fail_closed_on_mismatch(payload: dict[str, str]) -> None:
    previous = _record("observation")
    gap = _record("gap", segment="c" * 32, **payload)
    with pytest.raises(EventCorruptionError):
        _validate_gap_link(gap, previous, "d" * 64)


def test_segment_boundaries_accept_gap_and_recovered_damage_terminal() -> None:
    start = _record("start")
    end = _record("end")
    gap = _record(
        "gap",
        segment="c" * 32,
        previous_segment_id=end.segment_id,
        previous_final_record_sha256=end.record_sha256,
    )
    damaged = _record(
        "outcome", segment="c" * 32, disposition="rejected", reasons=["capture_damaged"]
    )
    _validate_segment_boundaries(((start, end), (gap, damaged)))
    assert _is_terminal_damage_segment((damaged,), is_last=True, preceding=((start, end),))


def test_segment_boundaries_reject_non_gap_continuation() -> None:
    with pytest.raises(EventCorruptionError, match="begin with a gap"):
        _validate_segment_boundaries(((_record("start"),), (_record("observation"),)))


@pytest.mark.parametrize(
    "filename", ["not-corrupt.jsonl", "corrupt-zz.jsonl", "corrupt-" + "a" * 64 + "-bad.txt"]
)
def test_corrupt_filename_parser_rejects_non_event_names(filename: str) -> None:
    assert _corrupt_original_filename(filename) is None


def test_corrupt_filename_parser_recovers_original_event_name() -> None:
    original = "evt-20260801T000000.000Z-" + BLACKOUT + ".jsonl"
    digest = hashlib.sha256(b"damage").hexdigest()
    assert _corrupt_original_filename(f"corrupt-{digest}-{original}") == original


def test_segment_manifest_reservation_is_idempotent_and_preserves_damage(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        token = "evt-20260801T000000.000Z-" + BLACKOUT + ".jsonl"
        store._stream._reserve_segment_manifest(token)
        store._stream._reserve_segment_manifest(token, "a" * 64)
        entries = store._stream._capacity.manifest_entries(BLACKOUT)
    assert entries == ((token, "a" * 64),)


def test_trusted_prefix_stops_at_malformed_or_torn_tail(tmp_path: Path) -> None:
    blackout_id = uuid.UUID(int=202, version=4).hex
    segment_id = uuid.UUID(int=303, version=4).hex
    start = EventStart(
        blackout_id,
        segment_id,
        "boot",
        "2026-08-01T00:00:00.000000Z",
        1,
        {},
    )
    with JsonlEventStore(tmp_path) as store:
        handle = store.open(start)
        path = tmp_path / "events" / handle.path_token
        assert len(store._stream._trusted_prefix(path, blackout_id)) == 1
        with path.open("ab") as stream:
            stream.write(b"not-json\n")
        assert len(store._stream._trusted_prefix(path, blackout_id)) == 1
        with path.open("ab") as stream:
            stream.write(b"torn")
        assert len(store._stream._trusted_prefix(path, blackout_id)) == 1
