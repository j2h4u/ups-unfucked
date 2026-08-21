import ast
import inspect
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from src.adapters.jsonl_errors import EventCorruptionError, EventPathError
from src.adapters.jsonl_event_store import JsonlEventStore
from src.adapters.jsonl_history_store import JsonlHistoryStore
from src.application.history_query import (
    HistoryRange,
    parse_utc,
    query_history,
    utc_day,
    utc_month,
    utc_year,
)
from src.application.storage_values import (
    EventRecord,
    EventStart,
    TerminalOutcomeRecord,
)


def _seal_event(
    store: JsonlEventStore,
    started: str,
    termination: str,
    *,
    commit_ir_k: bool = False,
    gap: bool = False,
) -> str:
    blackout_id = uuid4().hex
    segment_id = uuid4().hex
    handle = store.open(
        EventStart(
            blackout_id,
            segment_id,
            "boot-a",
            started,
            0,
            {"battery_epoch_id": uuid4().hex},
        )
    )
    if gap:
        handle = store.append(
            handle,
            EventRecord(
                "gap",
                "boot-a",
                started,
                1,
                {"reason": "observation_queue_overflow"},
                "system",
            ),
        )
    handle = store.append(
        handle,
        EventRecord(
            "end",
            "boot-a",
            started,
            2,
            {"termination": termination},
            "physical",
        ),
    )
    outcome = {
        "disposition": "learned" if commit_ir_k else "recorded_only",
        "comparison_mode": "none",
        "commit_receipt_id": "a" * 64 if commit_ir_k else None,
        "model_change": (
            {
                "parameter": "ir_k_v_per_pp",
                "value_before": 0.01,
                "measured_estimate": 0.02,
                "value_after": 0.02,
            }
            if commit_ir_k
            else None
        ),
        "terminal_outcome": {
            "disposition": "learned" if commit_ir_k else "recorded_only",
            "learning_decision": {"commit_ir_k": commit_ir_k},
            "reason_codes": [] if commit_ir_k else ["insufficient_coverage"],
        },
    }
    store.seal(
        handle,
        TerminalOutcomeRecord("boot-a", started, 3, outcome),
    )
    return blackout_id


def _seal_recharge(store: JsonlEventStore, blackout_id: str, started: str) -> str:
    episode_id = uuid4().hex
    handle = store.open(
        EventStart(
            episode_id,
            uuid4().hex,
            "boot-a",
            started,
            10,
            {
                "restoration_observation_id": uuid4().hex,
                "preceding_blackout_id": blackout_id,
                "observation": {
                    "boot_id": "boot-a",
                    "monotonic_ns": 10,
                    "wall_time_utc": started,
                    "raw_status": "OL",
                    "battery_voltage_raw": "12.3",
                    "battery_voltage_v": 12.3,
                    "voltage_token_quantum_v": 0.01,
                    "load_percent": 20.0,
                    "input_voltage_v": 230.0,
                },
            },
            "recharge",
        )
    )
    handle = store.append(
        handle,
        EventRecord(
            "end",
            "boot-a",
            started,
            11,
            {
                "termination": "service_stop",
                "assessment": {
                    "kind": "diagnostic",
                    "reason": "voltage stabilization only; full charge not established",
                },
            },
            "physical",
            "recharge",
        ),
    )
    store.seal(
        handle,
        TerminalOutcomeRecord(
            "boot-a",
            started,
            12,
            {
                "termination": "service_stop",
                "assessment": {
                    "kind": "diagnostic",
                    "reason": "voltage stabilization only; full charge not established",
                },
            },
            "recharge",
        ),
    )
    return episode_id


def test_utc_day_month_year_and_half_open_range(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        first = _seal_event(store, "2026-01-31T23:59:59Z", "power_restored")
        second = _seal_event(store, "2026-02-01T00:00:00Z", "power_restored")
        third = _seal_event(store, "2027-01-01T00:00:00Z", "power_restored")

        assert [item.blackout_id for item in query_history(store, utc_day(2026, 2, 1)).entries] == [
            second
        ]
        assert [item.blackout_id for item in query_history(store, utc_month(2026, 2)).entries] == [
            second
        ]
        assert [item.blackout_id for item in query_history(store, utc_year(2026)).entries] == [
            first,
            second,
        ]
        period = HistoryRange(parse_utc("2026-01-31T23:59:59Z"), parse_utc("2026-02-01T00:00:00Z"))
        assert [item.blackout_id for item in query_history(store, period).entries] == [first]
        assert third not in {
            item.blackout_id for item in query_history(store, utc_year(2026)).entries
        }


@pytest.mark.parametrize(
    ("period", "blackout_started", "recharge_started"),
    [
        (
            utc_day(2026, 1, 31),
            "2026-01-31T23:59:59Z",
            "2026-02-01T00:00:01Z",
        ),
        (
            utc_month(2026, 1),
            "2026-01-31T23:59:59Z",
            "2026-02-01T00:00:01Z",
        ),
        (
            utc_year(2026),
            "2026-12-31T23:59:59Z",
            "2027-01-01T00:00:01Z",
        ),
        (
            HistoryRange(parse_utc("2026-05-31T23:59:00Z"), parse_utc("2026-06-01T00:00:00Z")),
            "2026-05-31T23:59:59Z",
            "2026-06-01T00:00:01Z",
        ),
    ],
)
def test_recharge_is_attached_by_blackout_id_outside_requested_period(
    tmp_path: Path,
    period: HistoryRange,
    blackout_started: str,
    recharge_started: str,
) -> None:
    with JsonlEventStore(tmp_path) as store:
        blackout_id = _seal_event(store, blackout_started, "power_restored")
        episode_id = _seal_recharge(store, blackout_id, recharge_started)
        result = query_history(store, period)

    assert len(result.entries) == 1
    assert result.entries[0].blackout_id == blackout_id
    assert result.entries[0].recharge is not None
    assert result.entries[0].recharge.episode_id == episode_id


def test_read_only_history_uses_path_only_public_composition_seam() -> None:
    source = inspect.getsource(JsonlHistoryStore)
    assert "JsonlEventHistory(events_path)" in source
    assert "JsonlWorkRegistry" not in source
    assert "JsonlEventStream" not in source
    assert "_read_registry" not in source
    history_source = inspect.getsource(
        __import__("src.adapters.jsonl_event_history", fromlist=["JsonlEventHistory"])
    )
    tree = ast.parse(history_source)
    imported_names = {
        alias.name.split(".")[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert imported_names.isdisjoint({"JsonlFilesystem", "JsonlEventStream", "JsonlWorkRegistry"})
    assert "from_read_only_root" not in history_source
    assert "_reject_read_only_commit" not in history_source


def test_linked_recharge_and_learning_status_render_as_history_fields(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        blackout_id = _seal_event(store, "2026-03-10T01:00:00Z", "power_restored", commit_ir_k=True)
        episode_id = _seal_recharge(store, blackout_id, "2026-03-10T01:00:01Z")
        result = query_history(store, utc_month(2026, 3))

    entry = result.entries[0]
    assert entry.learning.status == "used"
    assert entry.recharge is not None
    assert entry.recharge.episode_id == episode_id
    assert entry.recharge.outcome == "diagnostic"
    assert "full charge" in entry.recharge.reason


def test_refused_learning_and_damage_are_explicit(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        blackout_id = _seal_event(
            store,
            "2026-04-10T01:00:00Z",
            "closed_restart_gap",
            gap=True,
        )
        entry = query_history(store, utc_month(2026, 4)).entries[0]

    assert entry.blackout_id == blackout_id
    assert entry.termination == "closed_restart_gap"
    assert entry.restoration_utc is None
    assert entry.learning.status == "refused"
    assert "insufficient coverage" in entry.learning.reasons
    assert entry.evidence_damage == ("gap: observation_queue_overflow",)


def test_corrupt_authoritative_jsonl_fails_visibly(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        _seal_event(store, "2026-05-10T01:00:00Z", "power_restored")
        path = next((tmp_path / "events").glob("evt-*.jsonl"))
        os.chmod(path, 0o600)
        path.write_bytes(path.read_bytes() + b"{not-json}\n")
        os.chmod(path, 0o400)
        with pytest.raises(EventCorruptionError):
            query_history(store, utc_month(2026, 5))


def test_range_requires_aware_utc_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone"):
        HistoryRange(datetime(2026, 1, 1), datetime(2026, 1, 2, tzinfo=timezone.utc))


def test_history_cli_is_a_thin_utc_month_adapter(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        _seal_event(store, "2026-06-10T01:00:00Z", "power_restored", commit_ir_k=True)
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parents[2] / "scripts" / "blackout-history.py"),
                str(tmp_path),
                "--month",
                "2026-06",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    assert "UTC range: [2026-06-01T00:00:00Z, 2026-07-01T00:00:00Z)" in completed.stdout
    assert "mains loss" in completed.stdout
    assert "learning: used" in completed.stdout


def test_read_only_history_skips_active_capture_while_writer_is_held(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path) as store:
        sealed_id = _seal_event(store, "2026-07-10T01:00:00Z", "power_restored")
        store.open(
            EventStart(
                uuid4().hex,
                uuid4().hex,
                "boot-a",
                "2026-07-10T02:00:00Z",
                0,
                {"battery_epoch_id": uuid4().hex},
            )
        )
        before = {
            path.relative_to(tmp_path): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            for path in tmp_path.rglob("*")
            if path.is_file()
        }
        with JsonlHistoryStore(tmp_path) as reader:
            result = query_history(reader, utc_month(2026, 7))
        after = {
            path.relative_to(tmp_path): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            for path in tmp_path.rglob("*")
            if path.is_file()
        }
    assert [entry.blackout_id for entry in result.entries] == [sealed_id]
    assert after == before


def test_read_only_history_skips_0400_pending_processing_until_registry_removal(
    tmp_path: Path,
) -> None:
    pending_query: list[
        tuple[tuple[str, ...], dict[Path, tuple[bytes, int]], dict[Path, tuple[bytes, int]]]
    ] = []

    def inspect_after_event_chmod(stage: str) -> None:
        if stage != "after_event_chmod":
            return
        pending = store.work_registry().pending_processing
        assert len(pending) == 1
        before = {
            path.relative_to(tmp_path): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            for path in tmp_path.rglob("*")
            if path.is_file()
        }
        assert (
            stat.S_IMODE((tmp_path / "events" / pending[0].final_path_token).stat().st_mode)
            == 0o400
        )
        with JsonlHistoryStore(tmp_path) as reader:
            entries = tuple(
                item.blackout_id for item in query_history(reader, utc_month(2026, 7)).entries
            )
        after = {
            path.relative_to(tmp_path): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            for path in tmp_path.rglob("*")
            if path.is_file()
        }
        pending_query.append((entries, before, after))

    with JsonlEventStore(tmp_path, fault_hook=inspect_after_event_chmod) as store:
        blackout_id = _seal_event(store, "2026-07-10T01:00:00Z", "power_restored")
        assert store.work_registry().pending_processing == ()

        with JsonlHistoryStore(tmp_path) as reader:
            visible = tuple(
                item.blackout_id for item in query_history(reader, utc_month(2026, 7)).entries
            )

    assert pending_query and pending_query[0][0] == ()
    assert pending_query[0][1] == pending_query[0][2]
    assert visible == (blackout_id,)


def test_read_only_history_store_requires_private_existing_state(tmp_path: Path) -> None:
    with pytest.raises(EventPathError, match="does not exist"):
        JsonlHistoryStore(tmp_path / "missing")


def test_malformed_active_registry_fails_history_query_visibly(tmp_path: Path) -> None:
    with JsonlEventStore(tmp_path):
        pass
    (tmp_path / "events" / "active.json").write_bytes(b"{}\n")
    with JsonlHistoryStore(tmp_path) as reader:
        with pytest.raises(EventCorruptionError, match="active registry"):
            query_history(reader, utc_month(2026, 8))
