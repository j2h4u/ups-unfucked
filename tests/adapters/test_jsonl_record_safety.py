"""Focused safety regressions for durable JSONL boundaries."""

import uuid
from pathlib import Path

import pytest

from src.adapters.jsonl_errors import EventCorruptionError, EventValidationError
from src.adapters.jsonl_event_store import JsonlEventStore
from src.adapters.jsonl_record_codec import (
    _bounded_start_payload,
    _is_terminal_damage_segment,
    _StoredRecord,
    _validate_gap_link,
    _validate_segment_boundaries,
    _validate_terminal_record_order,
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
        payload,
        canonical_line=b"{}\n",
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
        {"previous_segment_id": "wrong"},
    ],
)
def test_gap_links_fail_closed_on_mismatch(payload: dict[str, str]) -> None:
    previous = _record("observation")
    gap = _record("gap", segment="c" * 32, **payload)
    with pytest.raises(EventCorruptionError):
        _validate_gap_link(gap, previous)


def test_segment_boundaries_accept_gap_and_recovered_damage_terminal() -> None:
    start = _record("start")
    end = _record("end")
    gap = _record(
        "gap",
        segment="c" * 32,
        previous_segment_id=end.segment_id,
    )
    damaged = _record(
        "outcome", segment="c" * 32, disposition="rejected", reasons=["capture_damaged"]
    )
    _validate_segment_boundaries(((start, end), (gap, damaged)))
    assert _is_terminal_damage_segment((damaged,), is_last=True, preceding=((start, end),))


def test_segment_boundaries_reject_non_gap_continuation() -> None:
    with pytest.raises(EventCorruptionError, match="begin with a gap"):
        _validate_segment_boundaries(((_record("start"),), (_record("observation"),)))


@pytest.mark.parametrize("record_type", ["start", "end", "outcome"])
def test_terminal_records_reject_duplicate_terminal_types(record_type: str) -> None:
    start = _record("start")
    end = _record("end")
    outcome = _record("outcome", disposition="recorded_only")
    records = [start, end, outcome]
    duplicate = _record(record_type, disposition="recorded_only")
    records.insert(1, duplicate)

    with pytest.raises(EventCorruptionError, match=f"multiple {record_type} records"):
        _validate_terminal_record_order(tuple(records), end=end, outcome=outcome)


def test_terminal_outcome_must_be_last_record() -> None:
    start = _record("start")
    end = _record("end")
    outcome = _record("outcome", disposition="recorded_only")

    with pytest.raises(EventCorruptionError, match="follow terminal outcome"):
        _validate_terminal_record_order(
            (start, end, outcome, _record("observation")),
            end=end,
            outcome=outcome,
        )


def test_non_rejected_outcome_requires_end_but_rejected_outcome_does_not() -> None:
    start = _record("start")
    non_rejected = _record("outcome", disposition="recorded_only")
    with pytest.raises(EventCorruptionError, match="requires an end record"):
        _validate_terminal_record_order((start, non_rejected), end=None, outcome=non_rejected)

    rejected = _record("outcome", disposition="rejected")
    _validate_terminal_record_order((start, rejected), end=None, outcome=rejected)


def test_physical_records_cannot_follow_end() -> None:
    start = _record("start")
    end = _record("end")

    with pytest.raises(EventCorruptionError, match="follow end"):
        _validate_terminal_record_order(
            (start, end, _record("observation")),
            end=end,
            outcome=None,
        )


def test_segment_manifest_reservation_is_idempotent(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        token = "evt-20260801T000000.000Z-" + BLACKOUT + ".jsonl"
        store._stream._reserve_segment_manifest(token)
        store._stream._reserve_segment_manifest(token)
        entries = store._stream._capacity.manifest_entries(BLACKOUT)
    assert entries == ((token, None),)


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
