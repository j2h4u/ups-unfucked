import os
import stat
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from src.adapters.jsonl_errors import EventCorruptionError
from src.adapters.minimal_jsonl import MinimalJsonlEventStore
from src.application.history_query import (
    HistoryRange,
    RechargeHistory,
    _recharge_entry,
    parse_utc,
    query_history,
    utc_day,
    utc_month,
    utc_year,
)
from src.application.storage_values import (
    EventProjection,
    EventRecord,
    EventStart,
    ProjectedEventRecord,
    TerminalOutcomeRecord,
)


def _seal_event(
    store: MinimalJsonlEventStore,
    started: str,
    termination: str,
    *,
    commit_ir_k: bool = False,
    gap: bool = False,
) -> str:
    blackout_id = f"blackout-{len(store.history_tail(2**31))}"
    segment_id = uuid4().hex
    end_at = _plus(started, 2)
    handle = store.open(
        EventStart(
            blackout_id,
            segment_id,
            "boot-a",
            started,
            0,
            {
                "battery_epoch_id": uuid4().hex,
                "observation": _observation(started, "OB DISCHRG", battery_pct=None),
            },
        )
    )
    if gap:
        handle = store.append(
            handle,
            EventRecord(
                "gap",
                "boot-a",
                end_at,
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
            end_at,
            2,
            {
                "termination": termination,
                "observation": _observation(end_at, "OL", battery_pct=100.0),
            },
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
        TerminalOutcomeRecord("boot-a", end_at, 3, outcome),
    )
    return blackout_id


def _plus(value: str, seconds: int) -> str:
    return (
        (datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(seconds=seconds))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _observation(at: str, status: str, *, battery_pct: float | None) -> dict[str, object]:
    return {
        "boot_id": "boot-a",
        "monotonic_ns": 0,
        "wall_time_utc": at,
        "raw_status": status,
        "battery_voltage_raw": "12.5",
        "battery_voltage_v": 12.5,
        "voltage_token_quantum_v": 0.01,
        "battery_pct": battery_pct,
        "load_percent": 20.0,
        "input_voltage_v": 230.0 if status.startswith("OL") else 0.0,
    }


def _seal_recharge(store: MinimalJsonlEventStore, blackout_id: str, started: str) -> str:
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
                "observation": _observation(_plus(started, 2), "OL", battery_pct=100.0),
            },
            "physical",
            "recharge",
        ),
    )
    store.seal(
        handle,
        TerminalOutcomeRecord(
            "boot-a",
            _plus(started, 2),
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


def _recharge_record(
    record_type: str,
    payload: dict[str, object],
) -> ProjectedEventRecord:
    return ProjectedEventRecord(
        record_type,
        "physical",
        "recharge-episode",
        "recharge-episode",
        1,
        "boot-a",
        "2026-07-10T01:00:01Z",
        1,
        payload,
        "recharge",
    )


def _recharge_projection(
    start: ProjectedEventRecord | None,
    end: ProjectedEventRecord | None,
) -> EventProjection:
    return EventProjection(start, (), (), end, (), None, (), ())


def test_utc_day_month_year_and_half_open_range(tmp_path: Path) -> None:
    with nullcontext(MinimalJsonlEventStore(tmp_path)) as store:
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


def test_refused_learning_and_damage_are_explicit(tmp_path: Path) -> None:
    with nullcontext(MinimalJsonlEventStore(tmp_path)) as store:
        blackout_id = _seal_event(
            store,
            "2026-04-10T01:00:00Z",
            "closed_restart_gap",
            gap=True,
        )
        entry = query_history(store, utc_month(2026, 4)).entries[0]

    assert entry.blackout_id == blackout_id
    assert entry.termination == "power_restored"
    assert entry.restoration_utc is not None
    assert entry.learning.status == "refused"
    assert entry.disposition == "recorded_only"
    assert entry.learning.reasons == ("terminal outcome unavailable",)
    assert entry.evidence_damage == ()


def test_recharge_history_maps_missing_open_and_terminal_boundaries() -> None:
    assert _recharge_entry(None) is None

    missing_start = _recharge_projection(None, None)
    with pytest.raises(ValueError, match="sealed recharge projection has no start"):
        _recharge_entry(missing_start)

    start = _recharge_record("start", {})
    open_recharge = _recharge_entry(_recharge_projection(start, None))
    assert open_recharge == RechargeHistory(
        "recharge-episode", None, "incomplete", "recharge is still open"
    )

    terminal = _recharge_record(
        "end",
        {
            "assessment": {
                "kind": "diagnostic",
                "reason": "voltage stabilization only",
            }
        },
    )
    assert _recharge_entry(_recharge_projection(start, terminal)) == RechargeHistory(
        "recharge-episode",
        "2026-07-10T01:00:01Z",
        "diagnostic",
        "voltage stabilization only",
    )

    fallback_terminal = _recharge_record("end", {"reason": "service_stop"})
    assert _recharge_entry(_recharge_projection(start, fallback_terminal)) == RechargeHistory(
        "recharge-episode",
        "2026-07-10T01:00:01Z",
        "unknown",
        "service_stop",
    )


def test_corrupt_authoritative_jsonl_fails_visibly(tmp_path: Path) -> None:
    with nullcontext(MinimalJsonlEventStore(tmp_path)) as store:
        _seal_event(store, "2026-05-10T01:00:00Z", "power_restored")
        path = tmp_path / "events" / "telemetry.jsonl"
        os.chmod(path, 0o600)
        path.write_bytes(path.read_bytes() + b"{not-json}\n")
        os.chmod(path, 0o400)
        with pytest.raises(EventCorruptionError):
            query_history(store, utc_month(2026, 5))


def test_range_requires_aware_utc_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone"):
        HistoryRange(datetime(2026, 1, 1), datetime(2026, 1, 2, tzinfo=timezone.utc))


def test_read_only_history_skips_active_capture_while_writer_is_held(tmp_path: Path) -> None:
    with nullcontext(MinimalJsonlEventStore(tmp_path)) as store:
        sealed_id = _seal_event(store, "2026-07-10T01:00:00Z", "power_restored")
        store.open(
            EventStart(
                uuid4().hex,
                uuid4().hex,
                "boot-a",
                "2026-07-10T02:00:00Z",
                0,
                {
                    "battery_epoch_id": uuid4().hex,
                    "observation": _observation(
                        "2026-07-10T02:00:00Z", "OB DISCHRG", battery_pct=None
                    ),
                },
            )
        )
        before = {
            path.relative_to(tmp_path): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            for path in tmp_path.rglob("*")
            if path.is_file()
        }
        result = query_history(store, utc_month(2026, 7))
        after = {
            path.relative_to(tmp_path): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            for path in tmp_path.rglob("*")
            if path.is_file()
        }
    assert [entry.blackout_id for entry in result.entries] == [sealed_id]
    assert after == before


def test_history_query_ignores_active_capture(tmp_path: Path) -> None:
    store = MinimalJsonlEventStore(tmp_path)
    sealed_id = _seal_event(store, "2026-07-10T01:00:00Z", "power_restored")
    store.open(
        EventStart(
            uuid4().hex,
            uuid4().hex,
            "boot-a",
            "2026-07-10T02:00:00Z",
            0,
            {"observation": _observation("2026-07-10T02:00:00Z", "OB DISCHRG", battery_pct=None)},
        )
    )
    result = query_history(store, utc_month(2026, 7))
    assert [entry.blackout_id for entry in result.entries] == [sealed_id]
