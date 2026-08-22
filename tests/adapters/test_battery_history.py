import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.adapters.battery_history import BatteryHistory, summarize_episode
from src.adapters.telemetry_jsonl import TelemetryJsonlWriter
from src.domain.values import BlackoutKind, PhysicalObservation


def _observation(status: str, battery_pct: float | None, offset: int) -> PhysicalObservation:
    return PhysicalObservation(
        boot_id="boot",
        monotonic_ns=offset * 1_000_000_000,
        wall_time_utc=datetime(2026, 8, 22, tzinfo=timezone.utc) + timedelta(seconds=offset),
        raw_status=status,
        battery_voltage_raw="13.3",
        battery_voltage_v=13.3,
        load_percent=20.0,
        input_voltage_v=230.0 if "CAL" in status else 0.0,
        battery_pct=battery_pct,
        runtime_s=600.0,
        output_v=230.0,
    )


def _rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_episode_summary_classifies_natural_depth_and_efc() -> None:
    rows = [
        {"at": "2026-08-22T00:00:00Z", "status": "OB DISCHRG", "battery_pct": 100.0},
        {"at": "2026-08-22T00:00:10Z", "status": "OB DISCHRG LB", "battery_pct": 40.0},
        {"at": "2026-08-22T00:00:20Z", "status": "OL CHRG", "battery_pct": 80.0},
    ]

    assert summarize_episode(rows) == {
        "kind": "blackout",
        "at": "2026-08-22T00:00:00Z",
        "duration_s": 20.0,
        "depth_pct": 60.0,
        "efc": 0.6,
    }


def test_episode_uses_last_pre_blackout_charge_as_depth_baseline() -> None:
    rows = [
        {"at": "2026-08-22T00:00:00Z", "status": "OL", "battery_pct": 100.0},
        {"at": "2026-08-22T00:00:01Z", "status": "OB DISCHRG", "battery_pct": 94.0},
        {"at": "2026-08-22T00:00:11Z", "status": "OL CHRG", "battery_pct": 96.0},
    ]

    assert summarize_episode(rows) == {
        "kind": "blackout",
        "at": "2026-08-22T00:00:01Z",
        "duration_s": 10.0,
        "depth_pct": 6.0,
        "efc": 0.06,
    }


def test_writer_emits_one_summary_for_natural_and_cal_episodes(tmp_path: Path) -> None:
    writer = TelemetryJsonlWriter(tmp_path)
    writer.write(_observation("OB DISCHRG", 100.0, 0), BlackoutKind.BLACKOUT_REAL)
    writer.write(_observation("OB DISCHRG LB", 40.0, 10), BlackoutKind.BLACKOUT_REAL)
    writer.write(_observation("OL CHRG", 80.0, 20), BlackoutKind.ONLINE)
    writer.write(_observation("OL", 100.0, 30), BlackoutKind.ONLINE)
    writer.write(_observation("OL", 100.0, 31), BlackoutKind.ONLINE)
    writer.write(_observation("CAL DISCHRG", 90.0, 40), BlackoutKind.BLACKOUT_TEST)
    writer.write(_observation("OL CHRG", 100.0, 50), BlackoutKind.ONLINE)

    rows = _rows(tmp_path / "events" / "history.jsonl")
    assert rows == [
        {
            "kind": "blackout",
            "at": "2026-08-22T00:00:00Z",
            "duration_s": 20.0,
            "depth_pct": 60.0,
            "efc": 0.6,
        },
        {
            "kind": "self_test",
            "at": "2026-08-22T00:00:40Z",
            "duration_s": 10.0,
            "depth_pct": 0.0,
            "efc": 0.0,
        },
    ]


def test_model_update_records_exact_change_reason_and_event(tmp_path: Path) -> None:
    path = tmp_path / "events" / "history.jsonl"
    history = BatteryHistory(path)

    history.model_update(
        at="2026-08-22T00:01:00Z",
        event_at="2026-08-22T00:00:00Z",
        evidence_at="2026-08-22T00:00:30Z",
        changes={"physics.ir_compensation.k_volts_per_percent": (0.025, 0.023)},
        reason="stable natural-blackout load step",
    )

    assert _rows(path) == [
        {
            "kind": "model_update",
            "at": "2026-08-22T00:01:00Z",
            "event_at": "2026-08-22T00:00:00Z",
            "evidence_at": "2026-08-22T00:00:30Z",
            "changes": {
                "physics.ir_compensation.k_volts_per_percent": {
                    "from": 0.025,
                    "to": 0.023,
                    "delta": -0.002,
                }
            },
            "reason": "stable natural-blackout load step",
        }
    ]
    assert history.event_kinds() == {"2026-08-22T00:00:00Z": "model_update"}
