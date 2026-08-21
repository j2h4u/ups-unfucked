"""Adversarial checks for the read-side JSONL segment grammar."""

import uuid
from collections.abc import Sequence

import pytest

from src.adapters.jsonl_errors import EventCorruptionError
from src.adapters.jsonl_record_codec import (
    _is_terminal_damage_segment,
    _StoredRecord,
    _validate_gap_link,
    _validate_segment_boundaries,
)

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


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"previous_segment_id": "wrong"}, "wrong previous segment"),
        ({"previous_segment_id": "wrong"}, "wrong previous segment"),
    ],
)
def test_gap_links_fail_closed_on_each_mismatch(payload: dict[str, object], message: str) -> None:
    previous = _record("observation")
    gap = _record("gap", segment="c" * 32, **payload)
    with pytest.raises(EventCorruptionError, match=message):
        _validate_gap_link(gap, previous)


def test_gap_links_accept_matching_and_absent_optional_links() -> None:
    previous = _record("observation")
    matching = _record(
        "gap",
        segment="c" * 32,
        previous_segment_id=previous.segment_id,
    )
    _validate_gap_link(matching, previous)
    _validate_gap_link(_record("gap", segment="c" * 32), previous)


def test_gap_links_reject_a_mismatched_previous_file() -> None:
    previous = _record("observation")
    gap = _record(
        "gap",
        segment="c" * 32,
    )
    _validate_gap_link(gap, previous)


def test_terminal_damage_segment_requires_a_final_singleton_after_end() -> None:
    end = _record("end")
    damaged = _record(
        "outcome",
        segment="c" * 32,
        disposition="rejected",
        reasons=["capture_damaged"],
    )
    assert _is_terminal_damage_segment((damaged,), is_last=True, preceding=((end,),))
    assert not _is_terminal_damage_segment((damaged,), is_last=False, preceding=((end,),))
    assert not _is_terminal_damage_segment((damaged, damaged), is_last=True, preceding=((end,),))
    assert not _is_terminal_damage_segment((damaged,), is_last=True, preceding=())


@pytest.mark.parametrize(
    "record",
    [
        _record("observation"),
        _record("outcome", disposition="accepted", reasons=["capture_damaged"]),
        _record("outcome", disposition="rejected", reasons=["other"]),
    ],
)
def test_terminal_damage_segment_rejects_non_damage_outcomes(record: _StoredRecord) -> None:
    assert not _is_terminal_damage_segment((record,), is_last=True, preceding=((_record("end"),),))


def test_segment_boundaries_validate_gap_links_and_damage_terminal() -> None:
    start = _record("start")
    end = _record("end")
    gap = _record(
        "gap",
        segment="c" * 32,
        previous_segment_id=end.segment_id,
    )
    damaged = _record(
        "outcome",
        segment="c" * 32,
        disposition="rejected",
        reasons=["capture_damaged"],
    )
    _validate_segment_boundaries(((start, end), (gap, damaged)))


def test_segment_boundaries_reject_non_gap_continuation() -> None:
    prefixes: Sequence[Sequence[_StoredRecord]] = (
        (_record("start"),),
        (_record("observation", segment="c" * 32),),
    )
    with pytest.raises(EventCorruptionError, match="begin with a gap"):
        _validate_segment_boundaries(prefixes)
