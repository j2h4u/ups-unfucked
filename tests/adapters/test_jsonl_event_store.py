"""Focused fault-boundary tests for the per-blackout JSONL adapter."""

import errno
import hashlib
import json
import os
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import pytest

from src.adapters import jsonl_event_capacity, jsonl_event_stream
from src.adapters.jsonl_errors import (
    EventConflictError,
    EventCorruptionError,
    EventPathError,
    EventPersistenceError,
    EventValidationError,
    ProcessingBacklogFullError,
)
from src.adapters.jsonl_event_store import JsonlEventStore
from src.adapters.jsonl_event_stream import JsonlEventStream
from src.adapters.jsonl_filesystem import JsonlFilesystem
from src.adapters.jsonl_index import JsonlIndex
from src.adapters.jsonl_record_codec import (
    MAX_EPOCH_INDEX_SCAN_BYTES,
    MAX_LINE_BYTES,
    _StoredRecord,
    _validate_segment_boundaries,
    canonical_json_bytes,
    canonical_record_line,
)
from src.adapters.jsonl_summary_codec import _encode_summary
from src.adapters.jsonl_work_registry import JsonlWorkRegistry
from src.application.storage_values import (
    CapturingEventRef,
    EventRecord,
    EventRef,
    EventStart,
    EventSummary,
    PreparingCaptureRef,
    TerminalOutcomeRecord,
)
from src.domain.reasons import InfrastructureReason

BLACKOUT_ID = "00000000000040008000000000000001"
SEGMENT_ID = "00000000000040008000000000000002"
OTHER_SEGMENT_ID = "00000000000040008000000000000004"
WALL_START = "2026-08-16T09:41:07.123000Z"


class InjectedCrash(RuntimeError):
    """Simulated process death after a selected durable boundary."""


def test_store_uses_explicit_lanes_without_writer_authority(tmp_path: Path) -> None:
    assert JsonlEventStore.__bases__ == (object,)
    with JsonlEventStore(tmp_path) as store:
        assert isinstance(store._filesystem, JsonlFilesystem)
        assert isinstance(store._stream, JsonlEventStream)
        assert isinstance(store._registry, JsonlWorkRegistry)
        assert isinstance(store._index, JsonlIndex)
    for lane in (JsonlFilesystem, JsonlEventStream, JsonlWorkRegistry, JsonlIndex):
        assert not {"open", "append", "seal", "close"}.intersection(vars(lane))
    assert {name for name in vars(JsonlFilesystem) if not name.startswith("_")} == {
        "atomic_replace",
        "sync_storage_directory",
    }
    assert {name for name in vars(JsonlEventStream) if not name.startswith("_")} == {"project"}
    assert {name for name in vars(JsonlWorkRegistry) if not name.startswith("_")} == set()
    assert {name for name in vars(JsonlIndex) if not name.startswith("_")} == {
        "acknowledge_report_notice",
        "index_tail",
        "index_tail_for_epoch",
        "index_scan_for_decline_epoch",
        "report_outbox_head",
        "report_outbox_pending",
    }


def test_report_lanes_use_public_facade_seams() -> None:
    event_store_source = (
        Path(__file__).parents[2] / "src" / "adapters" / "jsonl_event_store.py"
    ).read_text(encoding="utf-8")
    report_source = (
        Path(__file__).parents[2] / "src" / "adapters" / "jsonl_report_outbox.py"
    ).read_text(encoding="utf-8")
    filesystem_source = (
        Path(__file__).parents[2] / "src" / "adapters" / "jsonl_filesystem.py"
    ).read_text(encoding="utf-8")
    assert "self._index._report_outbox" not in event_store_source
    assert "_atomic_replace" not in report_source
    assert "_sync_storage_directory" not in report_source
    assert "def _atomic_replace" not in filesystem_source
    assert "def _sync_storage_directory" not in filesystem_source
    assert "_atomic_replace" not in filesystem_source
    assert "_sync_storage_directory" not in filesystem_source
    assert "def atomic_replace" in filesystem_source
    assert "def sync_storage_directory" in filesystem_source
    assert "adapter-family friend primitives" in filesystem_source


def test_event_ownership_accepts_only_current_exact_filename_grammar() -> None:
    initial = f"evt-20260816T094107.123Z-{BLACKOUT_ID}.jsonl"
    continuation = f"evt-20260816T094107.123Z-{BLACKOUT_ID}-seg-000001-{SEGMENT_ID}.jsonl"
    ordinal_less = f"evt-20260816T094107.123Z-{BLACKOUT_ID}-seg-{SEGMENT_ID}.jsonl"
    suffix_decoy = f"not-an-event-{BLACKOUT_ID}.jsonl"

    assert jsonl_event_capacity._event_filename_belongs_to(initial, BLACKOUT_ID)
    assert jsonl_event_capacity._event_filename_belongs_to(continuation, BLACKOUT_ID)
    assert not jsonl_event_capacity._event_filename_belongs_to(ordinal_less, BLACKOUT_ID)
    assert not jsonl_event_capacity._event_filename_belongs_to(suffix_decoy, BLACKOUT_ID)


def _start(
    *,
    blackout_id: str = BLACKOUT_ID,
    segment_id: str = SEGMENT_ID,
    payload: dict | None = None,
) -> EventStart:
    return EventStart(
        blackout_id=blackout_id,
        segment_id=segment_id,
        boot_id="boot-a",
        wall_time_utc=WALL_START,
        monotonic_ns=1_000_000_000,
        payload=payload
        or {
            "battery_epoch_id": "00000000000040008000000000000003",
            "raw_status": "OB DISCHRG",
            "frozen_model": {"ir_k_v_per_pp": 0.015},
        },
    )


def _record(record_type: str, seq: int, payload: dict | None = None) -> EventRecord:
    provenance_by_type: dict[str, Literal["physical", "system", "derived"]] = {
        "observation": "physical",
        "end": "physical",
        "gap": "system",
        "assessment": "derived",
    }
    return EventRecord(
        record_type=record_type,
        boot_id="boot-a",
        wall_time_utc=f"2026-08-16T09:41:{7 + seq:02d}.123000Z",
        monotonic_ns=(seq + 1) * 1_000_000_000,
        payload=payload or {},
        provenance=provenance_by_type[record_type],
    )


def test_reject_processing_seals_deterministic_assessment_corruption(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        handle = store.open(_start())
        store.append(handle, _record("end", 1, {"termination": "power_restored"}))
        processing = store.work_registry().pending_processing[0]

        sealed = store.reject_processing(processing, InfrastructureReason.CAPTURE_DAMAGED)

        projection = store.project(sealed)
        assert projection.outcome is not None
        assert projection.outcome.payload["disposition"] == "rejected"
        assert projection.outcome.payload["reasons"] == ["capture_damaged"]
        assert store.work_registry().pending_processing == ()


def _outcome(seq: int = 4, disposition: str = "recorded_only") -> TerminalOutcomeRecord:
    return TerminalOutcomeRecord(
        boot_id="boot-a",
        wall_time_utc=f"2026-08-16T09:41:{7 + seq:02d}.123000Z",
        monotonic_ns=(seq + 1) * 1_000_000_000,
        payload={
            "disposition": disposition,
            "evidence_class": "censored_partial",
            "duration_s": float(seq),
            "comparison_available": False,
            "comparison_mode": "none",
            "ir_estimate_available": False,
            "reasons": ["comparison_not_attempted"],
        },
    )


def _append_hash_valid_records(
    path: Path,
    records: tuple[tuple[str, str, dict], ...],
) -> None:
    previous = json.loads(path.read_bytes().splitlines()[-1])
    lines = []
    for record_type, provenance, payload in records:
        envelope = {
            "schema_version": 2,
            "record_type": record_type,
            "provenance": provenance,
            "blackout_id": previous["blackout_id"],
            "segment_id": previous["segment_id"],
            "seq": previous["seq"] + 1,
            "boot_id": previous["boot_id"],
            "wall_time_utc": "2026-08-16T09:42:00.000000Z",
            "monotonic_ns": previous["monotonic_ns"] + 1_000_000_000,
            "prev_record_sha256": previous["record_sha256"],
            "payload": payload,
        }
        line = canonical_record_line(envelope)
        lines.append(line)
        previous = json.loads(line)
    with path.open("ab") as stream:
        stream.write(b"".join(lines))


def _boundary_record(
    record_type: str,
    *,
    segment_id: str = SEGMENT_ID,
    payload: dict | None = None,
) -> _StoredRecord:
    provenance = {
        "start": "physical",
        "observation": "physical",
        "end": "physical",
        "gap": "system",
        "outcome": "derived",
    }[record_type]
    return _StoredRecord(
        2,
        record_type,
        provenance,
        BLACKOUT_ID,
        segment_id,
        0,
        "boot-a",
        WALL_START,
        0,
        None,
        payload or {},
        "f" * 64,
        b"",
    )


def _open_and_end(store: JsonlEventStore):
    handle = store.open(_start())
    handle = store.append(handle, _record("observation", 1, {"voltage": 12.3}))
    return store.append(handle, _record("end", 2, {"termination": "power_restored"}))


def _seal_numbered_event(
    store: JsonlEventStore,
    number: int,
    *,
    battery_epoch_id: str | None = None,
    evidence_class: str | None = None,
    decline_evidence_eligible: bool = False,
) -> str:
    blackout_id = uuid.UUID(int=number + 100, version=4).hex
    segment_id = uuid.UUID(int=number + 10_000, version=4).hex
    started = datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(minutes=number)
    wall_start = started.isoformat(timespec="microseconds").replace("+00:00", "Z")
    handle = store.open(
        EventStart(
            blackout_id,
            segment_id,
            "boot-a",
            wall_start,
            number * 10_000_000_000,
            {"battery_epoch_id": battery_epoch_id or uuid.UUID(int=3, version=4).hex},
        )
    )
    ended = (
        (started + timedelta(seconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    handle = store.append(
        handle,
        EventRecord(
            "end",
            "boot-a",
            ended,
            number * 10_000_000_000 + 1_000_000_000,
            {"termination": "power_restored"},
            "physical",
        ),
    )
    outcome_payload = {
        "disposition": "recorded_only",
        "comparison_mode": "none",
        "duration_s": 1.0,
    }
    if evidence_class is not None:
        outcome_payload.update(
            {
                "evidence_class": evidence_class,
                "decline_evidence_eligible": decline_evidence_eligible,
            }
        )
    store.seal(
        handle,
        TerminalOutcomeRecord(
            "boot-a",
            ended,
            number * 10_000_000_000 + 1_000_000_001,
            outcome_payload,
        ),
    )
    return blackout_id


def test_epoch_tail_filters_before_limit_and_reports_true_overflow(tmp_path: Path) -> None:
    selected_epoch = uuid.UUID(int=3, version=4).hex
    other_epoch = uuid.UUID(int=4, version=4).hex
    expected = []
    with JsonlEventStore(tmp_path) as store:
        for number in range(68):
            epoch = selected_epoch if number % 2 == 0 else other_epoch
            blackout_id = _seal_numbered_event(
                store,
                number,
                battery_epoch_id=epoch,
            )
            if epoch == selected_epoch:
                expected.append(blackout_id)

        result = store.index_tail_for_epoch(selected_epoch, 32)

    assert tuple(item.blackout_id for item in result.summaries) == tuple(expected[-32:])
    assert result.overflow_count == len(expected) - 32
    assert result.scan_complete is True


def test_decline_epoch_scan_returns_all_same_epoch_summaries(tmp_path: Path) -> None:
    selected_epoch = uuid.UUID(int=3, version=4).hex
    expected_ids = []
    with JsonlEventStore(tmp_path) as store:
        for number in range(6):
            expected_ids.append(
                _seal_numbered_event(
                    store,
                    number,
                    battery_epoch_id=selected_epoch,
                    evidence_class="qualifying",
                    decline_evidence_eligible=True,
                )
            )
        for number in range(6, 33):
            expected_ids.append(
                _seal_numbered_event(
                    store,
                    number,
                    battery_epoch_id=selected_epoch,
                    evidence_class="operational_only",
                )
            )

        damaged_id = uuid.UUID(int=99_999, version=4).hex
        damaged_summary = EventSummary(
            2,
            damaged_id,
            f"evt-20260816T094107.123Z-{damaged_id}.jsonl",
            "2026-08-16T09:41:07.123000Z",
            "2026-08-16T09:41:08.123000Z",
            "power_restored",
            "qualifying",
            "recorded_only",
            1.0,
            1,
            selected_epoch,
            False,
            "none",
            False,
            None,
            ("c" * 64,),
            0,
            "a" * 64,
            "b" * 64,
        )
        index_path = tmp_path / "events" / "index.jsonl"
        index_path.write_bytes(index_path.read_bytes() + _encode_summary(damaged_summary))
        expected_ids.append(damaged_id)

        result = store.index_scan_for_decline_epoch(selected_epoch)

    assert tuple(item.blackout_id for item in result.summaries) == tuple(expected_ids)
    assert result.scan_complete is True


def test_decline_epoch_scan_fails_closed_when_bounded_scan_is_incomplete(
    tmp_path: Path,
) -> None:
    epoch = uuid.UUID(int=3, version=4).hex
    lines = []
    for number in range((MAX_EPOCH_INDEX_SCAN_BYTES // 777) + 1000):
        blackout_id = uuid.UUID(int=number + 100, version=4).hex
        lines.append(
            _encode_summary(
                EventSummary(
                    2,
                    blackout_id,
                    f"evt-20260816T094107.123Z-{blackout_id}.jsonl",
                    "2026-08-16T09:41:07.123000Z",
                    "2026-08-16T09:41:08.123000Z",
                    "power_restored",
                    "qualifying",
                    "recorded_only",
                    1.0,
                    1,
                    epoch,
                    False,
                    "none",
                    False,
                    None,
                    (),
                    0,
                    "a" * 64,
                    "b" * 64,
                )
            )
        )

    with JsonlEventStore(tmp_path) as store:
        (tmp_path / "events" / "index.jsonl").write_bytes(b"".join(lines))
        result = store.index_scan_for_decline_epoch(epoch)

    assert result.summaries
    assert result.scan_complete is False


def test_canonical_record_golden_bytes_and_hash() -> None:
    envelope = {
        "schema_version": 2,
        "record_type": "start",
        "provenance": "physical",
        "blackout_id": BLACKOUT_ID,
        "segment_id": SEGMENT_ID,
        "seq": 0,
        "boot_id": "boot-a",
        "wall_time_utc": WALL_START,
        "monotonic_ns": 1,
        "prev_record_sha256": None,
        "payload": {"raw_status": "OB"},
    }

    line = canonical_record_line(envelope)
    parsed = json.loads(line)
    digest = parsed.pop("record_sha256")

    assert line.endswith(b"\n")
    assert digest == hashlib.sha256(canonical_json_bytes(envelope)).hexdigest()
    assert canonical_json_bytes({**envelope, "record_sha256": digest}) + b"\n" == line


def test_registry_first_open_append_end_seal_and_project(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        handle = store.open(_start())
        registry = store.work_registry()
        assert isinstance(registry.capture, CapturingEventRef)
        assert registry.pending_processing == ()
        event_path = tmp_path / "events" / handle.path_token
        assert handle.path_token == f"evt-20260816T094107.123Z-{BLACKOUT_ID}.jsonl"
        assert stat.S_IMODE(event_path.stat().st_mode) == 0o600

        handle = store.append(handle, _record("observation", 1, {"voltage": 12.3}))
        handle = store.append(handle, _record("end", 2, {"termination": "power_restored"}))
        registry = store.work_registry()
        assert registry.capture is None
        assert [ref.blackout_id for ref in registry.pending_processing] == [BLACKOUT_ID]

        sealed = store.seal(handle, _outcome(3))
        assert stat.S_IMODE(event_path.stat().st_mode) == 0o400
        assert store.work_registry().pending_processing == ()
        assert sealed.outcome_record_sha256 == store.index_tail(1)[0].outcome_record_sha256
        projection = store.project(sealed)
        assert [record.seq for record in projection.records] == [0, 1, 2, 3]
        assert len(projection.observations) == 1
        assert projection.outcome is not None
        assert projection.outcome.payload["disposition"] == "recorded_only"
        assert store.storage_health().index_available


def test_registry_first_crash_recreates_exact_frozen_start(tmp_path: Path) -> None:
    def crash(stage: str) -> None:
        if stage == "after_registry_prepare":
            raise InjectedCrash(stage)

    store = JsonlEventStore(tmp_path, fault_hook=crash)
    with pytest.raises(InjectedCrash):
        store.open(_start())
    registry_bytes = (tmp_path / "events" / "active.json").read_bytes()
    assert b'"tag":"preparing"' in registry_bytes
    store.close()

    with JsonlEventStore(tmp_path) as recovered:
        handle = recovered.recover_startup()
        assert handle is not None
        assert isinstance(recovered.work_registry().capture, CapturingEventRef)
        event_bytes = (tmp_path / "events" / handle.path_token).read_bytes()
        preparing = json.loads(registry_bytes)["capture"]
        assert event_bytes == preparing["canonical_start_record_utf8"].encode("utf-8")
        assert event_bytes.count(b"\n") == 1


def test_crash_after_event_create_repairs_empty_file_from_preparing_ref(tmp_path: Path) -> None:
    def crash(stage: str) -> None:
        if stage == "after_event_create":
            raise InjectedCrash(stage)

    store = JsonlEventStore(tmp_path, fault_hook=crash)
    with pytest.raises(InjectedCrash):
        store.open(_start())
    event_path = next((tmp_path / "events").glob("evt-*.jsonl"))
    assert event_path.read_bytes() == b""
    store.close()

    with JsonlEventStore(tmp_path) as recovered:
        handle = recovered.recover_startup()
        assert handle is not None
        assert event_path.read_bytes().count(b"\n") == 1


def test_duplicate_delivery_is_idempotent_and_differing_bytes_conflict(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        handle = store.open(_start())
        observation = _record("observation", 1, {"voltage": 12.3})
        advanced = store.append(handle, observation)
        retried = store.append(handle, observation)

        assert retried == advanced
        event_path = tmp_path / "events" / handle.path_token
        assert event_path.read_bytes().count(b"\n") == 2
        with pytest.raises(EventConflictError, match="different canonical bytes"):
            store.append(handle, _record("observation", 1, {"voltage": 12.2}))


@pytest.mark.parametrize(
    ("records", "message"),
    (
        ((("start", "physical", {}),), "multiple start records"),
        (
            (
                ("end", "physical", {"termination": "power_restored"}),
                ("end", "physical", {"termination": "power_restored"}),
            ),
            "multiple end records",
        ),
        (
            (
                ("outcome", "derived", {"disposition": "rejected"}),
                ("assessment", "derived", {}),
            ),
            "records follow terminal outcome",
        ),
        (
            (("outcome", "derived", {"disposition": "recorded_only"}),),
            "non-rejected outcome requires an end record",
        ),
        (
            (
                ("end", "physical", {"termination": "power_restored"}),
                ("observation", "physical", {}),
            ),
            "physical/system records follow end",
        ),
    ),
)
def test_project_rejects_hash_valid_terminal_order_corruption(
    tmp_path: Path,
    records: tuple[tuple[str, str, dict], ...],
    message: str,
) -> None:
    with JsonlEventStore(tmp_path) as store:
        handle = store.open(_start())
        _append_hash_valid_records(tmp_path / "events" / handle.path_token, records)

        with pytest.raises(EventCorruptionError, match=message):
            store.project(handle)


def test_segment_boundaries_accept_gap_and_recovered_end_segments() -> None:
    _validate_segment_boundaries(
        (
            (_boundary_record("start"),),
            (_boundary_record("gap", segment_id=OTHER_SEGMENT_ID),),
        )
    )
    _validate_segment_boundaries(
        (
            (_boundary_record("start"), _boundary_record("end")),
            (
                _boundary_record(
                    "outcome",
                    segment_id=OTHER_SEGMENT_ID,
                    payload={"disposition": "rejected", "reasons": ["capture_damaged"]},
                ),
            ),
        )
    )


@pytest.mark.parametrize(
    ("prefixes", "message"),
    (
        (
            (
                (_boundary_record("start"),),
                (
                    _boundary_record(
                        "outcome",
                        segment_id=OTHER_SEGMENT_ID,
                        payload={"disposition": "rejected", "reasons": ["capture_damaged"]},
                    ),
                ),
            ),
            "continuation segment must begin with a gap record",
        ),
        (
            (
                (
                    _boundary_record("start"),
                    _boundary_record("observation", segment_id=OTHER_SEGMENT_ID),
                ),
            ),
            "one file contains records from multiple segments",
        ),
    ),
)
def test_segment_boundaries_reject_invalid_continuation_shapes(
    prefixes: tuple[tuple[_StoredRecord, ...], ...], message: str
) -> None:
    with pytest.raises(EventCorruptionError, match=message):
        _validate_segment_boundaries(prefixes)


def test_registry_rejects_noncanonical_bytes_and_duplicate_pending_ids(tmp_path: Path) -> None:
    noncanonical_root = tmp_path / "noncanonical"
    with JsonlEventStore(noncanonical_root) as store:
        registry_path = noncanonical_root / "events" / "active.json"
        registry_path.write_bytes(b'{"pending_processing":[],"capture":null}\n')
        with pytest.raises(EventCorruptionError, match="not canonical JSON"):
            store.work_registry()

    duplicate_root = tmp_path / "duplicate"
    with JsonlEventStore(duplicate_root) as store:
        _open_and_end(store)
        registry_path = duplicate_root / "events" / "active.json"
        registry = json.loads(registry_path.read_bytes())
        registry["pending_processing"].append(registry["pending_processing"][0])
        registry_path.write_bytes(canonical_json_bytes(registry) + b"\n")

        with pytest.raises(EventCorruptionError, match="duplicate blackout IDs"):
            store.work_registry()


def test_torn_non_newline_tail_is_truncated_before_capture_resume(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path)
    handle = store.open(_start())
    handle = store.append(handle, _record("observation", 1, {"voltage": 12.3}))
    event_path = tmp_path / "events" / handle.path_token
    with event_path.open("ab") as stream:
        stream.write(b'{"schema_version":2')
    store.close()

    with JsonlEventStore(tmp_path) as recovered:
        resumed = recovered.recover_startup()
        assert resumed is not None
        assert resumed.handle == handle
        assert resumed.last_boot_id == "boot-a"
        assert event_path.read_bytes().endswith(b"\n")
        assert event_path.read_bytes().count(b"\n") == 2


def test_newline_middle_corruption_rotates_segment_and_seals_aggregate_rejection(
    tmp_path: Path,
) -> None:
    with JsonlEventStore(tmp_path) as store:
        handle = store.open(_start())
        handle = store.append(handle, _record("observation", 1, {"voltage": 12.3}))
        event_path = tmp_path / "events" / handle.path_token
        with event_path.open("ab") as stream:
            stream.write(b"{}\n")
        before = event_path.read_bytes()

        resumed = store.recover_startup()
        assert resumed is not None
        assert resumed.blackout_id == handle.blackout_id
        assert resumed.segment_id != handle.segment_id
        assert resumed.path_token != handle.path_token
        corrupt_path = next((tmp_path / "events").glob("corrupt-*.jsonl"))
        assert corrupt_path.read_bytes() == before
        assert not event_path.exists()
        damaged_sha256 = hashlib.sha256(before).hexdigest()
        writable_path = tmp_path / "events" / resumed.path_token
        assert stat.S_IMODE(writable_path.stat().st_mode) == 0o600

        resumed = store.append(resumed, _record("observation", 2, {"voltage": 12.1}))
        resumed = store.append(
            resumed,
            _record("end", 3, {"termination": "power_restored"}),
        )
        sealed = store.seal(resumed, _outcome(4))
        projection = store.project(sealed)

        assert len(projection.trusted_prefixes) == 2
        assert [record.record_type for record in projection.records] == [
            "start",
            "observation",
            "gap",
            "observation",
            "end",
            "outcome",
        ]
        assert projection.damaged_segment_hashes == (damaged_sha256,)
        assert projection.damaged_segment_overflow == 0
        assert projection.outcome is not None
        assert projection.outcome.payload["disposition"] == "rejected"
        assert projection.outcome.payload["reasons"] == ["capture_damaged"]
        assert projection.outcome.payload["damaged_segment_hashes"] == [damaged_sha256]
        assert projection.outcome.payload["damaged_segment_overflow"] == 0
        assert stat.S_IMODE(writable_path.stat().st_mode) == 0o400
        summary = store.index_tail(1)[0]
        assert summary.damaged_segment_hashes == (damaged_sha256,)
        assert summary.disposition == "rejected"


def test_startup_rotates_hash_valid_middle_chain_corruption_instead_of_retrying_forever(
    tmp_path: Path,
) -> None:
    with JsonlEventStore(tmp_path) as store:
        handle = store.open(_start())
        handle = store.append(handle, _record("observation", 1, {"voltage": 12.3}))
        handle = store.append(handle, _record("observation", 2, {"voltage": 12.2}))
        event_path = tmp_path / "events" / handle.path_token
        lines = event_path.read_bytes().splitlines(keepends=True)
        event_path.write_bytes(b"".join((lines[0], lines[2], lines[1])))
        damaged_bytes = event_path.read_bytes()

        resumed = store.recover_startup()

        assert resumed is not None
        assert resumed.blackout_id == handle.blackout_id
        assert resumed.path_token != handle.path_token
        assert not event_path.exists()
        corrupt_path = next((tmp_path / "events").glob("corrupt-*.jsonl"))
        assert corrupt_path.read_bytes() == damaged_bytes
        projection = store.project(resumed.handle)
        assert [record.record_type for record in projection.records] == [
            "start",
            "gap",
        ]
        assert projection.damaged_segment_hashes == (hashlib.sha256(damaged_bytes).hexdigest(),)


def test_seal_recovers_registered_processing_with_terminal_damage_segment(
    tmp_path: Path,
) -> None:
    with JsonlEventStore(tmp_path) as store:
        handle = _open_and_end(store)
        event_path = tmp_path / "events" / handle.path_token
        with event_path.open("ab") as stream:
            stream.write(b"{}\n")
        corrupt_bytes = event_path.read_bytes()
        damaged_sha256 = hashlib.sha256(corrupt_bytes).hexdigest()

        sealed = store.seal(handle, _outcome(3))
        projection = store.project(sealed)

        assert [record.record_type for record in projection.records] == [
            "start",
            "observation",
            "end",
            "outcome",
        ]
        assert projection.damaged_segment_hashes == (damaged_sha256,)
        assert projection.outcome is not None
        assert projection.outcome.payload["reasons"] == ["capture_damaged"]
        assert len(projection.trusted_prefixes) == 2
        assert [record.record_type for record in projection.trusted_prefixes[-1]] == ["outcome"]
        assert not event_path.exists()
        assert next((tmp_path / "events").glob("corrupt-*.jsonl")).read_bytes() == corrupt_bytes


def test_seal_recovers_registered_processing_with_non_end_continuation(
    tmp_path: Path,
) -> None:
    with JsonlEventStore(tmp_path) as store:
        handle = _open_and_end(store)
        event_path = tmp_path / "events" / handle.path_token
        durable_lines = event_path.read_bytes().splitlines(keepends=True)
        event_path.write_bytes(durable_lines[0] + durable_lines[1] + b"{}\n")
        corrupt_bytes = event_path.read_bytes()
        damaged_sha256 = hashlib.sha256(corrupt_bytes).hexdigest()

        sealed = store.seal(handle, _outcome(3))
        projection = store.project(sealed)

        assert [record.record_type for record in projection.records] == [
            "start",
            "observation",
            "gap",
            "outcome",
        ]
        assert projection.damaged_segment_hashes == (damaged_sha256,)
        assert projection.gaps[0].payload["previous_segment_id"] == SEGMENT_ID
        assert projection.gaps[0].payload["damaged_segment_sha256"] == damaged_sha256
        assert projection.outcome is not None
        assert projection.outcome.payload["reasons"] == ["capture_damaged"]
        assert not event_path.exists()
        assert next((tmp_path / "events").glob("corrupt-*.jsonl")).read_bytes() == corrupt_bytes


def test_rejects_symlink_event_directory_and_second_writer(tmp_path: Path) -> None:
    unsafe_root = tmp_path / "unsafe"
    unsafe_root.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (unsafe_root / "events").symlink_to(target, target_is_directory=True)
    with pytest.raises(EventPathError):
        JsonlEventStore(unsafe_root)

    store = JsonlEventStore(tmp_path / "safe")
    with pytest.raises(EventConflictError, match="another event-store writer"):
        JsonlEventStore(tmp_path / "safe")
    store.close()


def test_strict_bounds_reject_nan_snapshot_and_reason_overflow(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        with pytest.raises(EventValidationError, match="non-finite"):
            store.open(_start(payload={"voltage": float("nan")}))
        snapshot = {"blob": "x" * (65 * 1024)}
        handle = store.open(
            _start(
                payload={
                    "raw_status": "OB DISCHRG",
                    "first_observation": {"voltage": 12.2},
                    "frozen_model": snapshot,
                }
            )
        )
        projected = store.project(handle)
        assert projected.start is not None
        payload = projected.start.payload
        assert payload["raw_status"] == "OB DISCHRG"
        assert payload["first_observation"] == {"voltage": 12.2}
        assert payload["snapshot_budget_exceeded"] is True
        assert payload["comparison_allowed"] is False
        assert payload["comparison_available"] is False
        assert payload["commit_allowed"] is False
        assert payload["reasons"] == ["snapshot_budget_exceeded"]
        bounded_snapshot = payload["frozen_model"]
        assert isinstance(bounded_snapshot, dict)
        encoded_snapshot = canonical_json_bytes(snapshot)
        assert bounded_snapshot["original_bytes"] == len(encoded_snapshot)
        assert bounded_snapshot["original_sha256"] == hashlib.sha256(encoded_snapshot).hexdigest()
        handle = store.append(handle, _record("end", 1, {"termination": "power_restored"}))
        store.seal(handle, _outcome(2))

        handle = store.open(_start(blackout_id=uuid.uuid4().hex, segment_id=uuid.uuid4().hex))
        with pytest.raises(EventValidationError, match="at most eight"):
            store.append(
                handle,
                _record("assessment", 1, {"reasons": [f"reason_{index}" for index in range(9)]}),
            )


@pytest.mark.parametrize("damaged_count", [15, 16, 17])
def test_summary_bounds_damaged_hashes_and_reports_exact_overflow(
    tmp_path: Path, damaged_count: int
) -> None:
    with JsonlEventStore(
        tmp_path,
        wall_clock=lambda: "2026-08-16T09:41:08.000000Z",
    ) as store:
        handle = store.open(_start())
        expected_hashes = []
        for number in range(damaged_count):
            path = tmp_path / "events" / handle.path_token
            with path.open("ab") as stream:
                stream.write(canonical_json_bytes({"damaged": number}) + b"\n")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            expected_hashes.append(digest)
            recovered = store.recover_startup()
            assert recovered is not None
            handle = recovered

        handle = store.append(handle, _record("end", 1, {"termination": "capture_damaged"}))
        sealed = store.seal(handle, _outcome(2, disposition="rejected"))
        summary = store.index_tail(1)[0]

        assert summary.damaged_segment_hashes == tuple(expected_hashes[:16])
        assert summary.damaged_segment_overflow == max(0, damaged_count - 16)
        projection = store.project(sealed)
        assert projection.outcome is not None
        assert projection.outcome.payload["damaged_segment_hashes"] == expected_hashes[:16]
        assert projection.outcome.payload["damaged_segment_overflow"] == max(0, damaged_count - 16)
        assert len((tmp_path / "events" / "index.jsonl").read_bytes().splitlines()[0]) <= 4096
        assert len(tuple((tmp_path / "events").glob("corrupt-*.jsonl"))) == damaged_count
        assert len(sealed.segment_ids) == damaged_count + 1


# --- explicit single-blackout capacity bounds ---


def test_projection_accepts_exact_event_bytes_and_rejects_one_byte_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with JsonlEventStore(tmp_path) as store:
        handle = store.open(_start())
        event_path = tmp_path / "events" / handle.path_token
        exact_size = event_path.stat().st_size
        monkeypatch.setattr(jsonl_event_capacity, "MAX_EVENT_BYTES", exact_size + 1)
        assert store.project(handle).start is not None
        monkeypatch.setattr(jsonl_event_capacity, "MAX_EVENT_BYTES", exact_size)
        assert store.project(handle).start is not None

        monkeypatch.setattr(jsonl_event_capacity, "MAX_EVENT_BYTES", exact_size - 1)
        with pytest.raises(EventCorruptionError, match="exceeds 64 MiB"):
            store.project(handle)
        assert event_path.stat().st_size == exact_size


def test_capture_limit_rejects_observation_then_recovers_gap_and_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with JsonlEventStore(tmp_path) as store:
        handle = store.open(_start())
        event_path = tmp_path / "events" / handle.path_token
        capture_limit = event_path.stat().st_size
        monkeypatch.setattr(jsonl_event_capacity, "CAPTURE_APPEND_LIMIT", capture_limit)
        monkeypatch.setattr(
            jsonl_event_capacity, "MAX_EVENT_BYTES", capture_limit + 2 * 1024 * 1024
        )

        before = event_path.read_bytes()
        with pytest.raises(EventCorruptionError, match="durable bound"):
            store.append(handle, _record("observation", 1, {"voltage": 12.3}))
        assert event_path.read_bytes() == before

        gap = store.append(handle, _record("gap", 1, {"reason": "capture_damaged"}))
        ended = store.append(gap, _record("end", 2, {"termination": "capture_damaged"}))
        store.checkpoint_processing(ended, "capture_damaged")
        sealed = store.seal(ended, _outcome(3, disposition="rejected"))
        projection = store.project(sealed)

        assert projection.outcome is not None
        assert projection.outcome.payload["reasons"] == ["capture_damaged"]
        assert event_path.read_bytes() == before


def test_capture_limit_marker_survives_restart_before_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with JsonlEventStore(tmp_path) as store:
        handle = store.open(_start())
        event_path = tmp_path / "events" / handle.path_token
        capture_limit = event_path.stat().st_size
        monkeypatch.setattr(jsonl_event_capacity, "CAPTURE_APPEND_LIMIT", capture_limit)
        monkeypatch.setattr(
            jsonl_event_capacity, "MAX_EVENT_BYTES", capture_limit + 2 * 1024 * 1024
        )
        with pytest.raises(EventCorruptionError):
            store.append(handle, _record("observation", 1, {"voltage": 12.3}))

    with JsonlEventStore(tmp_path) as recovered:
        resumed = recovered.recover_startup()
        assert resumed is not None
        gap = recovered.append(resumed.handle, _record("gap", 1, {"reason": "capture_damaged"}))
        ended = recovered.append(gap, _record("end", 2, {"termination": "capture_damaged"}))
        recovered.checkpoint_processing(ended, "capture_damaged")
        sealed = recovered.seal(ended, _outcome(3, disposition="rejected"))
        assert recovered.project(sealed).outcome is not None


@pytest.mark.parametrize("fault_stage", ("before_outcome_append", "after_outcome_append"))
def test_capture_damaged_outcome_retry_preserves_store_owned_bytes(
    tmp_path: Path, fault_stage: str
) -> None:
    armed = True

    def crash(stage: str) -> None:
        if armed and stage == fault_stage:
            raise InjectedCrash(stage)

    with JsonlEventStore(tmp_path, fault_hook=crash) as store:
        handle = _open_and_end(store)
        store.checkpoint_processing(handle, "capture_damaged")
        event_path = tmp_path / "events" / handle.path_token
        with pytest.raises(InjectedCrash):
            store.seal(
                handle,
                TerminalOutcomeRecord(
                    "caller-boot",
                    "2026-08-16T23:59:59.000000Z",
                    99,
                    {"caller_payload": "ignored"},
                ),
            )
        bytes_after_fault = event_path.read_bytes()
        armed = False

        sealed = store.seal(
            handle,
            TerminalOutcomeRecord(
                "different-boot",
                "2027-01-01T00:00:00.000000Z",
                123_456,
                {"another_payload": True},
            ),
        )

        assert fault_stage == "after_outcome_append" or event_path.read_bytes() != bytes_after_fault
        if fault_stage == "after_outcome_append":
            assert event_path.read_bytes() == bytes_after_fault
        projection = store.project(sealed)
        assert projection.outcome is not None
        assert projection.outcome.boot_id == "boot-a"
        assert projection.outcome.wall_time_utc == "2026-08-16T09:41:09.123000Z"
        assert projection.outcome.monotonic_ns == 3_000_000_000
        assert projection.outcome.payload["reasons"] == ["capture_damaged"]
        assert "caller_payload" not in projection.outcome.payload
        assert "another_payload" not in projection.outcome.payload


def test_continuation_reserves_terminal_budget_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with JsonlEventStore(tmp_path) as store:
        handle = store.open(_start())
        event_path = tmp_path / "events" / handle.path_token
        capture_limit = event_path.stat().st_size
        monkeypatch.setattr(jsonl_event_capacity, "CAPTURE_APPEND_LIMIT", capture_limit)
        monkeypatch.setattr(
            jsonl_event_capacity,
            "MAX_EVENT_BYTES",
            capture_limit + 8 * MAX_LINE_BYTES,
        )
        with pytest.raises(EventCorruptionError, match="durable bound"):
            store.append(handle, _record("observation", 1, {"voltage": 12.3}))

    with JsonlEventStore(tmp_path) as recovered:
        resumed = recovered.recover_startup()
        assert resumed is not None
        gap = recovered.append(resumed.handle, _record("gap", 1, {"reason": "event_size_limit"}))
        total = recovered._stream._capacity.event_total_bytes(BLACKOUT_ID)
        monkeypatch.setattr(
            jsonl_event_capacity,
            "CAPTURE_APPEND_LIMIT",
            total + MAX_LINE_BYTES,
        )
        observed = recovered.append(gap, _record("observation", 2, {"voltage": 12.3}))
        monkeypatch.setattr(
            jsonl_event_capacity,
            "CAPTURE_APPEND_LIMIT",
            recovered._stream._capacity.event_total_bytes(BLACKOUT_ID),
        )
        with pytest.raises(EventCorruptionError, match="durable reserve"):
            recovered.append(observed, _record("observation", 3, {"voltage": 12.2}))

        failed_gap = recovered.append(
            observed,
            _record("gap", 3, {"reason": "observation_execution_failure"}),
        )
        ended = recovered.append(
            failed_gap,
            _record("end", 4, {"termination": "capture_damaged"}),
        )
        recovered.checkpoint_processing(ended, "capture_damaged")
        sealed = recovered.seal(ended, _outcome(5, disposition="rejected"))
        assert recovered.project(sealed).outcome is not None
        assert recovered._stream._capacity.event_total_bytes(BLACKOUT_ID) <= (
            capture_limit + 8 * MAX_LINE_BYTES
        )


def test_missing_committed_manifest_segment_fails_closed(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        handle = store.open(_start())
        missing = f"evt-20260816T094107.123Z-{BLACKOUT_ID}-seg-000001-{OTHER_SEGMENT_ID}.jsonl"
        store._stream._reserve_segment_manifest(missing)
        with pytest.raises(EventCorruptionError, match="manifest-referenced event segment"):
            store.project(handle)


def test_oversized_pending_file_is_rejected_before_full_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with JsonlEventStore(tmp_path) as store:
        handle = store.open(_start())
        event_path = tmp_path / "events" / handle.path_token
        original_size = event_path.stat().st_size
        monkeypatch.setattr(jsonl_event_capacity, "MAX_EVENT_BYTES", original_size)
        with event_path.open("ab") as stream:
            stream.truncate(original_size + 1)

        def fail_if_read(_fd: int, _length: int) -> bytes:
            raise AssertionError("oversized pending file was read before validation")

        monkeypatch.setattr(jsonl_event_stream, "_read_exact_fd", fail_if_read)
        with pytest.raises(EventCorruptionError, match="exceeds 64 MiB"):
            store._stream._repair_torn_tail(event_path)


def test_projection_rejects_aggregate_segment_bytes_before_record_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with JsonlEventStore(tmp_path) as store:
        handle = store.open(_start())
        event_path = tmp_path / "events" / handle.path_token
        event_path.write_bytes(event_path.read_bytes() + b"x" * 32)
        monkeypatch.setattr(jsonl_event_capacity, "MAX_EVENT_BYTES", event_path.stat().st_size - 1)

        with pytest.raises(EventCorruptionError, match="exceeds 64 MiB"):
            store.project(handle)
        assert event_path.read_bytes().endswith(b"x" * 32)


def test_files_and_registry_have_private_exact_modes(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        handle = store.open(_start())
        assert stat.S_IMODE((tmp_path / "events").stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "events" / "active.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / "events" / handle.path_token).stat().st_mode) == 0o600
        assert not os.path.islink(tmp_path / "events" / handle.path_token)


def test_full_processing_fifo_durably_rejects_ninth_event_and_frees_capture(
    tmp_path: Path,
) -> None:
    with JsonlEventStore(tmp_path) as store:
        for number in range(8):
            blackout_id = uuid.UUID(int=number + 500, version=4).hex
            segment_id = uuid.UUID(int=number + 600, version=4).hex
            handle = store.open(
                EventStart(
                    blackout_id,
                    segment_id,
                    "boot-a",
                    f"2026-08-16T10:{number:02d}:00.000000Z",
                    number * 2,
                    {},
                )
            )
            store.append(
                handle,
                EventRecord(
                    "end",
                    "boot-a",
                    f"2026-08-16T10:{number:02d}:01.000000Z",
                    number * 2 + 1,
                    {"termination": "power_restored"},
                    "physical",
                ),
            )
        assert len(store.work_registry().pending_processing) == 8

        ninth = store.open(
            EventStart(
                uuid.UUID(int=999, version=4).hex,
                uuid.UUID(int=1_999, version=4).hex,
                "boot-a",
                "2026-08-16T11:00:00.000000Z",
                100,
                {},
            )
        )
        with pytest.raises(ProcessingBacklogFullError):
            store.append(
                ninth,
                EventRecord(
                    "end",
                    "boot-a",
                    "2026-08-16T11:00:01.000000Z",
                    101,
                    {"termination": "power_restored"},
                    "physical",
                ),
            )

        registry = store.work_registry()
        assert registry.capture is None
        assert len(registry.pending_processing) == 8
        summary = store.index_tail(1)[0]
        assert summary.blackout_id == ninth.blackout_id
        assert summary.disposition == "rejected"
        projection = store.project(EventRef(ninth.blackout_id, ninth.path_token))
        assert projection.outcome is not None
        assert projection.outcome.payload["reasons"] == ["processing_backlog_full"]


def test_lazy_rebuild_is_bounded_resumable_and_merges_new_sealed_delta(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path)
    expected_ids = {_seal_numbered_event(store, number) for number in range(100)}
    index_path = tmp_path / "events" / "index.jsonl"
    index_path.unlink()

    assert not store.storage_health().index_available
    assert not store.maintenance.rebuild_index_tick(max_files=32, max_bytes=4 * 1024 * 1024)
    cursor = json.loads((tmp_path / "events" / "index-rebuild.cursor.json").read_bytes())
    assert cursor["files_done"] == 32
    assert cursor["target_count"] == 100
    catalog_cursor = tmp_path / "events" / "index-rebuild.catalog.cursor.json"
    assert catalog_cursor.exists()
    assert json.loads(catalog_cursor.read_bytes())["offset"] > 0
    store.close()

    store = JsonlEventStore(tmp_path)
    expected_ids.add(_seal_numbered_event(store, 101))
    delta_path = tmp_path / "events" / "index-rebuild.delta.jsonl"
    assert delta_path.read_bytes().count(b"\n") == 1
    ready = False
    progress: list[int] = []
    for _ in range(12):
        ready = store.maintenance.rebuild_index_tick(max_files=32, max_bytes=4 * 1024 * 1024)
        progress.append(
            json.loads((tmp_path / "events" / "index-rebuild.cursor.json").read_bytes())[
                "files_done"
            ]
        )
        if ready:
            break
    assert ready
    assert all(right - left <= 32 for left, right in zip(progress, progress[1:], strict=False))
    store.maintenance.promote_index_rebuild()

    summaries = [json.loads(line) for line in index_path.read_bytes().splitlines()]
    assert {summary["blackout_id"] for summary in summaries} == expected_ids
    assert len(summaries) == 101
    assert [summary["segment_filename"] for summary in summaries] == sorted(
        summary["segment_filename"] for summary in summaries
    )
    assert not (tmp_path / "events" / "index-rebuild.cursor.json").exists()
    assert not catalog_cursor.exists()
    assert not delta_path.exists()
    assert store.storage_health().index_available
    store.close()


def test_storage_health_inventory_is_bounded_and_resumable(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        for number in range(65):
            _seal_numbered_event(store, number)

        first = store.storage_health()
        assert first.event_count == 64
        assert first.alarm is None

        second = store.storage_health()
        assert second.event_count == 65
        assert second.total_bytes > first.total_bytes


def test_catalog_reservation_before_create_is_missing_and_restart_idempotent(
    tmp_path: Path,
) -> None:
    def crash(stage: str) -> None:
        if stage == "after_catalog_reserve":
            raise InjectedCrash(stage)

    token = "evt-20260816T094107.123Z-00000000000040008000000000000009.jsonl"
    with JsonlEventStore(tmp_path, fault_hook=crash) as store:
        with pytest.raises(InjectedCrash):
            store.open(_start(blackout_id="00000000000040008000000000000009"))
        assert not (tmp_path / "events" / token).exists()
        assert store._stream._capacity.segment_sources("00000000000040008000000000000009") == ()
        assert (tmp_path / "events" / "event-catalog.jsonl").read_bytes().count(b"\n") == 1
    with JsonlEventStore(tmp_path) as restarted:
        assert restarted.recover_startup() is not None
        assert (tmp_path / "events" / token).read_bytes().count(b"\n") == 1
        restarted._catalog.reserve(token)
        assert (tmp_path / "events" / "event-catalog.jsonl").read_bytes().count(b"\n") == 1


def test_missing_catalog_reservation_is_skipped_by_rebuild(tmp_path: Path) -> None:
    token = "evt-20260816T094107.123Z-00000000000040008000000000000009.jsonl"
    with JsonlEventStore(tmp_path) as store:
        store._catalog.reserve(token)
        assert not (tmp_path / "events" / token).exists()
        assert store.maintenance.rebuild_index_tick(max_files=1, max_bytes=4 * 1024 * 1024)
        store.maintenance.promote_index_rebuild()
        assert (tmp_path / "events" / "index.jsonl").read_bytes() == b""


def test_rebuild_restart_after_cursor_write_is_idempotent(tmp_path: Path) -> None:
    def crash(stage: str) -> None:
        if stage == "after_rebuild_cursor":
            raise InjectedCrash(stage)

    with JsonlEventStore(tmp_path, fault_hook=crash) as store:
        expected = {_seal_numbered_event(store, number) for number in range(2)}
        (tmp_path / "events" / "index.jsonl").unlink()
        with pytest.raises(InjectedCrash):
            store.maintenance.rebuild_index_tick(max_files=1, max_bytes=4 * 1024 * 1024)

    with JsonlEventStore(tmp_path) as restarted:
        ready = False
        for _ in range(8):
            ready = restarted.maintenance.rebuild_index_tick(max_files=1, max_bytes=4 * 1024 * 1024)
            if ready:
                break
        assert ready
        restarted.maintenance.promote_index_rebuild()
        assert {item.blackout_id for item in restarted.index_tail(2)} == expected


def test_rebuild_catalog_batch_is_bounded_without_directory_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with JsonlEventStore(tmp_path) as store:
        for number in range(101):
            _seal_numbered_event(store, number)
        (tmp_path / "events" / "index.jsonl").unlink()

        def forbidden_glob(_self, _pattern):
            raise AssertionError("rebuild must not enumerate event directory")

        def forbidden_scandir(_path):
            raise AssertionError("rebuild must not call scandir")

        monkeypatch.setattr(Path, "glob", forbidden_glob)
        monkeypatch.setattr(os, "scandir", forbidden_scandir)
        assert not store.maintenance.rebuild_index_tick(max_files=16, max_bytes=4 * 1024 * 1024)
        cursor = json.loads(
            (tmp_path / "events" / "index-rebuild.catalog.cursor.json").read_bytes()
        )
        assert cursor["next_seq"] == 16
        assert cursor["offset"] < cursor["target_offset"]


def test_catalog_corruption_fails_closed_during_rebuild(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        _seal_numbered_event(store, 1)
        (tmp_path / "events" / "index.jsonl").unlink()
        catalog = tmp_path / "events" / "event-catalog.jsonl"
        raw = catalog.read_bytes()
        catalog.write_bytes(b"X" + raw[1:])
        with pytest.raises(EventCorruptionError):
            store.maintenance.rebuild_index_tick(max_files=1, max_bytes=4 * 1024 * 1024)


def test_durability_lag_is_zero_when_idle_and_after_durable_append(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        assert store.storage_health().durability_lag_s == 0.0
        handle = store.open(_start())
        assert store.storage_health().durability_lag_s == 0.0
        store.append(handle, _record("end", 1, {"termination": "power_restored"}))
        assert store.storage_health().durability_lag_s == 0.0


def test_rebuild_cursor_output_mismatch_discards_only_projection_attempt(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        expected_ids = {_seal_numbered_event(store, number) for number in range(3)}
        (tmp_path / "events" / "index.jsonl").unlink()
        assert not store.maintenance.rebuild_index_tick(max_files=1, max_bytes=4 * 1024 * 1024)
        first_cursor = json.loads((tmp_path / "events" / "index-rebuild.cursor.json").read_bytes())
        with (tmp_path / "events" / "index.rebuild.in-progress.jsonl").open("ab") as stream:
            stream.write(b"torn")

        assert not store.maintenance.rebuild_index_tick(max_files=1, max_bytes=4 * 1024 * 1024)
        restarted = json.loads((tmp_path / "events" / "index-rebuild.cursor.json").read_bytes())
        assert restarted["generation_id"] != first_cursor["generation_id"]
        while not store.maintenance.rebuild_index_tick(max_files=1, max_bytes=4 * 1024 * 1024):
            pass
        store.maintenance.promote_index_rebuild()
        assert {summary.blackout_id for summary in store.index_tail(3)} == expected_ids


def test_crash_after_rebuild_rename_recovers_without_losing_promoted_index(tmp_path: Path) -> None:
    armed = False

    def crash(stage: str) -> None:
        if armed and stage == "after_rebuild_rename":
            raise InjectedCrash(stage)

    store = JsonlEventStore(tmp_path, fault_hook=crash)
    expected_id = _seal_numbered_event(store, 1)
    (tmp_path / "events" / "index.jsonl").unlink()
    assert store.maintenance.rebuild_index_tick(max_files=32, max_bytes=4 * 1024 * 1024)
    armed = True
    with pytest.raises(InjectedCrash):
        store.maintenance.promote_index_rebuild()
    assert (tmp_path / "events" / "index.jsonl").exists()
    assert (tmp_path / "events" / "index-rebuild.cursor.json").exists()
    store.close()

    with JsonlEventStore(tmp_path) as recovered:
        recovered.maintenance.promote_index_rebuild()
        assert recovered.index_tail(1)[0].blackout_id == expected_id
        assert not (tmp_path / "events" / "index-rebuild.cursor.json").exists()


def test_storage_health_uses_injected_clock_and_explicit_composed_counters(
    tmp_path: Path,
) -> None:
    now = ["2026-08-16T12:00:00.000000Z"]
    with JsonlEventStore(tmp_path, wall_clock=lambda: now[0]) as store:
        _seal_numbered_event(store, 1)
        (tmp_path / "events" / "index.jsonl").unlink()
        store.maintenance.begin_index_rebuild()

        health = store.storage_health(
            queued_observations=7,
            consumed_step_budget_remaining=3,
        )
        assert health.queued_observations == 7
        assert health.consumed_step_budget_remaining == 3
        assert not health.rebuild_stalled

        now[0] = "2026-08-16T12:02:00.000000Z"
        assert not store.storage_health().rebuild_stalled
        now[0] = "2026-08-16T12:02:00.000001Z"
        stalled = store.storage_health()
        assert stalled.rebuild_stalled
        assert stalled.queued_observations is None
        assert stalled.consumed_step_budget_remaining is None

        with pytest.raises(ValueError, match="queued_observations"):
            store.storage_health(queued_observations=-1)


def _fd_name(fd: int) -> str:
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}")).name
    except OSError:
        return ""


def _assert_preparing_failure_recovers(
    store: JsonlEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_name: str,
    original,
) -> None:
    with pytest.raises(EventPersistenceError):
        store.open(_start())
    registry = store.work_registry()
    assert isinstance(registry.capture, PreparingCaptureRef)
    assert not store.storage_health().capture_available

    monkeypatch.setattr(os, patched_name, original)
    recovered = store.recover_startup()
    assert recovered is not None
    event_path = tmp_path / "events" / recovered.path_token
    assert event_path.read_bytes().count(b"\n") == 1
    store.close()


def test_event_create_enospc_keeps_preparing_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonlEventStore(tmp_path)
    real_open = os.open

    def failing_open(path, flags, mode=0o777):
        if Path(path).name.startswith("evt-") and flags & os.O_EXCL:
            raise OSError(errno.ENOSPC, "injected event create exhaustion")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", failing_open)
    _assert_preparing_failure_recovers(store, tmp_path, monkeypatch, "open", real_open)


def test_event_write_enospc_keeps_preparing_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonlEventStore(tmp_path)
    real_write = os.write

    def failing_write(fd: int, data: bytes) -> int:
        if _fd_name(fd).startswith("evt-"):
            raise OSError(errno.ENOSPC, "injected event write exhaustion")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", failing_write)
    _assert_preparing_failure_recovers(store, tmp_path, monkeypatch, "write", real_write)


def test_event_fdatasync_errno_keeps_preparing_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonlEventStore(tmp_path)
    real_fdatasync = os.fdatasync

    def failing_fdatasync(fd: int) -> None:
        if _fd_name(fd).startswith("evt-"):
            raise OSError(errno.EIO, "injected event fdatasync failure")
        real_fdatasync(fd)

    monkeypatch.setattr(os, "fdatasync", failing_fdatasync)
    _assert_preparing_failure_recovers(
        store,
        tmp_path,
        monkeypatch,
        "fdatasync",
        real_fdatasync,
    )


def test_event_parent_fsync_errno_keeps_preparing_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    armed = False

    def arm_after_registry(stage: str) -> None:
        nonlocal armed
        if stage == "after_registry_prepare":
            armed = True

    store = JsonlEventStore(tmp_path, fault_hook=arm_after_registry)
    real_fsync = os.fsync

    def failing_fsync(fd: int) -> None:
        if armed and _fd_name(fd) == "events":
            raise OSError(errno.ENOSPC, "injected event parent fsync exhaustion")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", failing_fsync)
    _assert_preparing_failure_recovers(store, tmp_path, monkeypatch, "fsync", real_fsync)


def test_registry_replace_enospc_leaves_previous_registry_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonlEventStore(tmp_path)
    active_path = tmp_path / "events" / "active.json"
    before = active_path.read_bytes()
    real_replace = os.replace

    def failing_replace(source, destination) -> None:
        if Path(destination) == active_path:
            raise OSError(errno.ENOSPC, "injected registry replace exhaustion")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(EventPersistenceError, match="atomic replacement"):
        store.open(_start())
    assert active_path.read_bytes() == before
    assert store.work_registry().capture is None
    assert not tuple((tmp_path / "events").glob("evt-*.jsonl"))
    assert not store.storage_health().capture_available
    store.close()


def test_corrupt_evidence_rename_errno_keeps_original_and_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonlEventStore(tmp_path)
    handle = store.open(_start())
    event_path = tmp_path / "events" / handle.path_token
    with event_path.open("ab") as stream:
        stream.write(b"{}\n")
    before = event_path.read_bytes()
    real_replace = os.replace

    def failing_replace(source, destination) -> None:
        if Path(source) == event_path and Path(destination).name.startswith("corrupt-"):
            raise OSError(errno.EIO, "injected corrupt evidence rename failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(EventPersistenceError, match="preserve corrupt event"):
        store.recover_startup()
    assert event_path.read_bytes() == before
    assert isinstance(store.work_registry().capture, CapturingEventRef)
    assert not tuple((tmp_path / "events").glob("corrupt-*.jsonl"))
    assert not store.storage_health().capture_available

    monkeypatch.setattr(os, "replace", real_replace)
    assert store.recover_startup() is not None
    store.close()


def test_index_promotion_rename_errno_preserves_rebuild_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonlEventStore(tmp_path)
    expected_id = _seal_numbered_event(store, 1)
    index_path = tmp_path / "events" / "index.jsonl"
    rebuild_path = tmp_path / "events" / "index.rebuild.merged.jsonl"
    index_path.unlink()
    assert store.maintenance.rebuild_index_tick(max_files=32, max_bytes=4 * 1024 * 1024)
    real_replace = os.replace

    def failing_replace(source, destination) -> None:
        if Path(source) == rebuild_path and Path(destination) == index_path:
            raise OSError(errno.EIO, "injected index promotion rename failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(EventPersistenceError, match="index promotion failed"):
        store.maintenance.promote_index_rebuild()
    assert rebuild_path.exists()
    assert (tmp_path / "events" / "index-rebuild.cursor.json").exists()

    monkeypatch.setattr(os, "replace", real_replace)
    store.maintenance.promote_index_rebuild()
    assert store.index_tail(1)[0].blackout_id == expected_id
    store.close()
