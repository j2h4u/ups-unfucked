"""Named durability-boundary and strict-decoding evidence for JSONL storage.

The tests in this module keep the durable bytes as the source of truth: a
simulated crash may leave a retryable transaction, but it must never turn a
partial or malformed record into scientific evidence.
"""

import json
import os
from pathlib import Path

import pytest

from src.adapters import model_state_persistence as model_files
from src.adapters.jsonl_errors import (
    EventConflictError,
    EventCorruptionError,
    ProjectionUnavailableError,
)
from src.adapters.jsonl_event_store import JsonlEventStore
from src.adapters.jsonl_record_codec import (
    _decode_record_line,
    canonical_record_line,
)
from src.adapters.jsonl_summary_codec import (
    _bounded_epoch_summaries,
    _bounded_file_suffix,
    _bounded_tail_lines,
    _decode_summary_line,
    _encode_summary,
    _iter_complete_lines,
)
from src.application.storage_values import (
    EventRecord,
    EventStart,
    EventSummary,
    TerminalOutcomeRecord,
)

BLACKOUT_ID = "00000000000040008000000000000021"
SEGMENT_ID = "00000000000040008000000000000022"
EPOCH_ID = "00000000000040008000000000000023"
WALL_START = "2026-08-17T00:00:00.000000Z"
WALL_END = "2026-08-17T00:00:01.000000Z"


class InjectedCrash(RuntimeError):
    """Simulated process death at one named durable boundary."""


def _start() -> EventStart:
    return EventStart(
        BLACKOUT_ID,
        SEGMENT_ID,
        "boot-a",
        WALL_START,
        1_000_000_000,
        {"battery_epoch_id": EPOCH_ID, "frozen_model": {"ir_k_v_per_pp": 0.015}},
    )


def _end() -> EventRecord:
    return EventRecord(
        "end",
        "boot-a",
        WALL_END,
        2_000_000_000,
        {"termination": "power_restored"},
        "physical",
    )


def _outcome() -> TerminalOutcomeRecord:
    return TerminalOutcomeRecord(
        "boot-a",
        WALL_END,
        2_000_000_001,
        {
            "disposition": "recorded_only",
            "evidence_class": "censored_partial",
            "duration_s": 1.0,
            "comparison_available": False,
            "comparison_mode": "none",
            "ir_estimate_available": False,
            "reasons": ["comparison_not_attempted"],
        },
    )


def _open_and_end(store: JsonlEventStore):
    handle = store.open(_start())
    return store.append(handle, _end())


def _retry_pending_seal(store: JsonlEventStore) -> None:
    pending = store.work_registry().pending_processing
    if not pending:
        return
    handle = store._registry._handle_from_processing_ref(pending[0])
    store.seal(handle, _outcome())


@pytest.mark.parametrize("stage", ("after_start_append", "after_registry_capturing"))
def test_open_fault_stages_recover_exact_start_bytes(tmp_path: Path, stage: str) -> None:
    def crash(selected: str) -> None:
        if selected == stage:
            raise InjectedCrash(stage)

    store = JsonlEventStore(tmp_path, fault_hook=crash)
    with pytest.raises(InjectedCrash, match=stage):
        store.open(_start())
    store.close()

    with JsonlEventStore(tmp_path) as restarted:
        recovered = restarted.recover_startup()
        assert recovered is not None
        assert recovered.handle.next_seq == 1
        assert recovered.handle.blackout_id == BLACKOUT_ID
        event_path = tmp_path / "events" / recovered.handle.path_token
        assert event_path.read_bytes().count(b"\n") == 1


@pytest.mark.parametrize("stage", ("after_end_append", "after_end_registry_transition"))
def test_end_transition_faults_converge_to_one_pending_seal(tmp_path: Path, stage: str) -> None:
    def crash(selected: str) -> None:
        if selected == stage:
            raise InjectedCrash(stage)

    store = JsonlEventStore(tmp_path, fault_hook=crash)
    handle = store.open(_start())
    with pytest.raises(InjectedCrash, match=stage):
        store.append(handle, _end())
    store.close()

    with JsonlEventStore(tmp_path) as restarted:
        # Startup either promotes a captured END into processing or observes
        # the already-promoted processing ref.  Both are retryable states.
        assert restarted.recover_startup() is None
        _retry_pending_seal(restarted)
        assert restarted.work_registry().pending_processing == ()
        summaries = restarted.index_tail(1)
        assert len(summaries) == 1
        assert summaries[0].blackout_id == BLACKOUT_ID


@pytest.mark.parametrize(
    "stage",
    (
        "before_event_chmod",
        "after_event_chmod",
        "before_summary_append",
        "after_summary_append",
        "after_registry_remove",
    ),
)
def test_seal_fault_stages_preserve_durable_outcome_for_retry(tmp_path: Path, stage: str) -> None:
    def crash(selected: str) -> None:
        if selected == stage:
            raise InjectedCrash(stage)

    store = JsonlEventStore(tmp_path, fault_hook=crash)
    handle = _open_and_end(store)
    with pytest.raises(InjectedCrash, match=stage):
        store.seal(handle, _outcome())
    event_path = tmp_path / "events" / handle.path_token
    durable_event_bytes = event_path.read_bytes()
    assert len(durable_event_bytes.splitlines()) == 3
    store.close()

    with JsonlEventStore(tmp_path) as restarted:
        assert restarted.recover_startup() is None
        if stage == "after_registry_remove":
            assert restarted.work_registry().pending_processing == ()
        else:
            _retry_pending_seal(restarted)
            assert restarted.work_registry().pending_processing == ()
        assert event_path.read_bytes() == durable_event_bytes
        index_path = tmp_path / "events" / "index.jsonl"
        index_lines = index_path.read_bytes().splitlines() if index_path.exists() else []
        assert len(index_lines) == 1
        assert restarted.index_tail(1)[0].blackout_id == BLACKOUT_ID


def test_seal_retry_rejects_different_outcome_without_changing_durable_bytes(
    tmp_path: Path,
) -> None:
    def crash(selected: str) -> None:
        if selected == "before_event_chmod":
            raise InjectedCrash(selected)

    store = JsonlEventStore(tmp_path, fault_hook=crash)
    handle = _open_and_end(store)
    with pytest.raises(InjectedCrash, match="before_event_chmod"):
        store.seal(handle, _outcome())
    event_path = tmp_path / "events" / handle.path_token
    durable_event_bytes = event_path.read_bytes()
    store.close()

    mismatched = TerminalOutcomeRecord(
        "boot-a",
        WALL_END,
        2_000_000_001,
        {
            **_outcome().payload,
            "disposition": "rejected",
        },
    )
    with JsonlEventStore(tmp_path) as restarted:
        with pytest.raises(EventConflictError, match="idempotency conflict"):
            pending = restarted.work_registry().pending_processing[0]
            restarted.seal(
                restarted._registry._handle_from_processing_ref(pending),
                mismatched,
            )
        assert len(restarted.work_registry().pending_processing) == 1
        assert event_path.read_bytes() == durable_event_bytes
        _retry_pending_seal(restarted)
        assert restarted.work_registry().pending_processing == ()
        assert len(restarted.index_tail(1)) == 1
        assert event_path.read_bytes() == durable_event_bytes


@pytest.mark.parametrize(
    "stage",
    (
        "after_rebuild_merge_cursor",
        "after_rebuild_merge_append",
        "after_rebuild_merge_verify_started",
        "after_rebuild_merge_prepared",
    ),
)
def test_merge_fault_stages_resume_and_promote_exact_projection(tmp_path: Path, stage: str) -> None:
    def crash(selected: str) -> None:
        if selected == stage:
            raise InjectedCrash(stage)

    store = JsonlEventStore(tmp_path, fault_hook=crash)
    _open_and_end(store)
    store.seal(
        store._registry._handle_from_processing_ref(store.work_registry().pending_processing[0]),
        _outcome(),
    )
    (tmp_path / "events" / "index.jsonl").unlink()
    with pytest.raises(InjectedCrash, match=stage):
        store.maintenance.rebuild_index_tick(max_files=16, max_bytes=4 * 1024 * 1024)
    store.close()

    with JsonlEventStore(tmp_path) as restarted:
        ready = False
        for _ in range(8):
            if restarted.maintenance.rebuild_index_tick(max_files=16, max_bytes=4 * 1024 * 1024):
                ready = True
                break
        assert ready
        restarted.maintenance.promote_index_rebuild()
        assert restarted.index_tail(1)[0].blackout_id == BLACKOUT_ID
        assert len((tmp_path / "events" / "index.jsonl").read_bytes().splitlines()) == 1


def test_merge_before_rename_crash_retains_prepared_output_for_retry(tmp_path: Path) -> None:
    armed = False

    def crash(stage: str) -> None:
        if armed and stage == "before_rebuild_rename":
            raise InjectedCrash(stage)

    store = JsonlEventStore(tmp_path, fault_hook=crash)
    _open_and_end(store)
    store.seal(
        store._registry._handle_from_processing_ref(store.work_registry().pending_processing[0]),
        _outcome(),
    )
    (tmp_path / "events" / "index.jsonl").unlink()
    assert store.maintenance.rebuild_index_tick(max_files=16, max_bytes=4 * 1024 * 1024)
    armed = True
    with pytest.raises(InjectedCrash, match="before_rebuild_rename"):
        store.maintenance.promote_index_rebuild()
    store.close()

    with JsonlEventStore(tmp_path) as restarted:
        restarted.maintenance.promote_index_rebuild()
        assert restarted.index_tail(1)[0].blackout_id == BLACKOUT_ID


def _summary() -> EventSummary:
    return EventSummary(
        2,
        BLACKOUT_ID,
        f"evt-20260817T000000.000Z-{BLACKOUT_ID}.jsonl",
        WALL_START,
        WALL_END,
        "power_restored",
        "censored_partial",
        "recorded_only",
        1.0,
        0,
        EPOCH_ID,
        False,
        "none",
        False,
        None,
        (),
        0,
        "a" * 64,
        "b" * 64,
    )


@pytest.mark.parametrize(
    "raw",
    (
        b"",
        b"{}\n",
        b'{"schema_version":2}\n',
        b'{"schema_version":2',
        _encode_summary(_summary())[:-1],
        _encode_summary(_summary()).replace(b'"schema_version":2', b'"schema_version":1'),
    ),
)
def test_summary_decoder_rejects_empty_short_torn_and_corrupt_lines(raw: bytes) -> None:
    with pytest.raises(EventCorruptionError):
        _decode_summary_line(raw)


def test_summary_stream_decoders_reject_torn_and_short_eof(tmp_path: Path) -> None:
    valid = _encode_summary(_summary())
    path = tmp_path / "index.jsonl"
    path.write_bytes(valid + b"torn")
    with pytest.raises(EventCorruptionError, match="torn"):
        tuple(_iter_complete_lines(path, 4096))
    with pytest.raises(EventCorruptionError, match="newline"):
        _bounded_tail_lines(path, 1, 4096)
    with pytest.raises(EventCorruptionError, match="torn"):
        _bounded_file_suffix(path, 4096)
    with pytest.raises(EventCorruptionError):
        _bounded_epoch_summaries(path, EPOCH_ID)


def test_index_and_merge_cursors_fail_closed_on_eof_or_short_output(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        events = tmp_path / "events"
        cursor = events / "index-rebuild.cursor.json"
        cursor.write_bytes(b'{"phase":')
        with pytest.raises(EventCorruptionError):
            store._index._read_cursor_if_present()

        catalog_cursor = events / "index-rebuild.catalog.cursor.json"
        catalog_cursor.write_bytes(b'{"phase":')
        with pytest.raises(EventCorruptionError):
            store._index._read_catalog_cursor()

        merge_path = store._index._merge._merge_path
        assert store._index._merge._summary_at(merge_path, 0) is None
        with pytest.raises(EventCorruptionError, match="disappeared"):
            store._index._merge._summary_at(merge_path, 1)
        merge_path.write_bytes(b"short")
        with pytest.raises(EventCorruptionError, match="torn"):
            store._index._merge._summary_at(merge_path, 0)
        with pytest.raises(ProjectionUnavailableError, match="shorter"):
            store._index._merge._repair_output({"merge_output_offset": len(b"short") + 1})


def test_registry_empty_processing_file_is_not_fabricated_on_restart(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        handle = _open_and_end(store)
        pending = store.work_registry().pending_processing[0]
        path = tmp_path / "events" / pending.final_path_token
        path.write_bytes(b"")
        with pytest.raises(EventCorruptionError, match="no durable record"):
            store._registry._handle_from_processing_ref(pending)
        assert path.read_bytes() == b""
        assert handle.path_token == pending.final_path_token


def _fd_name(fd: int) -> str:
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}")).name
    except OSError:
        return ""


def test_model_read_short_eof_is_a_model_file_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "model.json"
    path.write_bytes(b'{"schema_version":2}\n')
    real_read = os.read

    def eof(fd: int, size: int) -> bytes:
        if _fd_name(fd) == path.name:
            return b""
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", eof)
    with pytest.raises(model_files.ModelStateFileError, match="short read"):
        model_files.read_model_file(path)


def _record_line() -> bytes:
    return canonical_record_line(
        {
            "schema_version": 2,
            "record_type": "start",
            "provenance": "physical",
            "blackout_id": BLACKOUT_ID,
            "segment_id": SEGMENT_ID,
            "seq": 0,
            "boot_id": "boot-a",
            "wall_time_utc": WALL_START,
            "monotonic_ns": 1_000_000_000,
            "prev_record_sha256": None,
            "payload": {"battery_epoch_id": EPOCH_ID},
        }
    )


@pytest.mark.parametrize("raw", (b"", b"{}\n", b'{"schema_version":2', b"NaN\n"))
def test_record_decoder_rejects_eof_short_and_non_strict_json(raw: bytes) -> None:
    with pytest.raises(EventCorruptionError):
        _decode_record_line(raw)


def test_record_decoder_rejects_hash_and_canonicality_damage() -> None:
    line = _record_line()
    obj = json.loads(line)
    obj["record_sha256"] = "f" * 64
    with pytest.raises(EventCorruptionError, match="SHA-256"):
        _decode_record_line(json.dumps(obj, separators=(",", ":")).encode() + b"\n")
