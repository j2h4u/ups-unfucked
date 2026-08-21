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
)
from src.adapters.jsonl_event_store import JsonlEventStore
from src.adapters.jsonl_record_codec import (
    _decode_record_line,
    canonical_record_line,
)
from src.application.storage_values import (
    EventRecord,
    EventStart,
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
        summaries = restarted.history_tail(1)
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
        outbox_path = tmp_path / "events" / "report-outbox.jsonl"
        outbox_lines = outbox_path.read_bytes().splitlines() if outbox_path.exists() else []
        assert len(outbox_lines) == 1
        assert restarted.history_tail(1)[0].blackout_id == BLACKOUT_ID


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
        assert len(restarted.history_tail(1)) == 1
        assert event_path.read_bytes() == durable_event_bytes


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
