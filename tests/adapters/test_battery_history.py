import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.adapters.battery_history import BatteryHistory, canonical_timestamp, summarize_episode
from src.adapters.telemetry_jsonl import TelemetryJsonlWriter
from src.domain.values import BlackoutKind, PhysicalObservation


def _observation(status: str, battery_pct: float | None, offset: int) -> PhysicalObservation:
    return PhysicalObservation(
        monotonic_ns=offset * 1_000_000_000,
        wall_time_utc=datetime(2026, 8, 22, tzinfo=timezone.utc) + timedelta(seconds=offset),
        raw_status=status,
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
        "duration_s": 20,
        "depth_pct": 60.0,
        "efc": 0.6,
    }


def test_episode_uses_pre_blackout_charge_as_depth_baseline() -> None:
    rows = [
        {"at": "2026-08-22T00:00:00Z", "status": "OL", "battery_pct": 100.0},
        {"at": "2026-08-22T00:00:01Z", "status": "OB DISCHRG", "battery_pct": 94.0},
        {"at": "2026-08-22T00:00:11Z", "status": "OL CHRG", "battery_pct": 96.0},
    ]

    assert summarize_episode(rows) == {
        "kind": "blackout",
        "at": "2026-08-22T00:00:01Z",
        "duration_s": 10,
        "depth_pct": 6.0,
        "efc": 0.06,
    }


def test_charge_drop_before_ob_is_part_of_blackout_depth() -> None:
    rows = [
        {"at": "2026-09-03T22:43:41Z", "status": "OL", "battery_pct": 100.0},
        {"at": "2026-09-03T22:43:42Z", "status": "OL", "battery_pct": 100.0},
        {"at": "2026-09-03T22:43:43Z", "status": "OL", "battery_pct": 99.0},
        {"at": "2026-09-03T22:43:44Z", "status": "OB DISCHRG", "battery_pct": 99.0},
        {"at": "2026-09-03T22:44:08Z", "status": "OL CHRG", "battery_pct": 99.0},
    ]

    summary = summarize_episode(rows)

    assert summary is not None
    assert summary["depth_pct"] == 1.0
    assert summary["efc"] == 0.01


def test_ordinary_ob_with_mains_input_is_not_a_self_test() -> None:
    rows = [
        {
            "at": "2026-08-15T23:08:21.882803Z",
            "status": "OB DISCHRG",
            "battery_pct": 100.0,
            "input_v": 247.0,
        },
        {
            "at": "2026-08-15T23:34:43.882803Z",
            "status": "OB DISCHRG",
            "battery_pct": 0.0,
            "input_v": 0.0,
        },
        {
            "at": "2026-08-15T23:34:44.882803Z",
            "status": "OL CHRG",
            "battery_pct": 10.0,
            "input_v": 247.0,
        },
    ]

    summary = summarize_episode(rows)

    assert summary is not None
    assert summary["kind"] == "blackout"
    assert summary["duration_s"] == 1583
    assert type(summary["duration_s"]) is int


def test_mixed_ob_cal_episode_defaults_to_blackout_without_provenance() -> None:
    rows = [
        {
            "at": "2026-08-22T00:00:00Z",
            "status": "OB DISCHRG",
            "battery_pct": 100.0,
            "input_v": 0.0,
        },
        {
            "at": "2026-08-22T00:00:01Z",
            "status": "CAL DISCHRG",
            "battery_pct": 99.0,
            "input_v": 230.0,
        },
        {
            "at": "2026-08-22T00:00:02Z",
            "status": "OL CHRG",
            "battery_pct": 100.0,
            "input_v": 230.0,
        },
    ]

    assert summarize_episode(rows) == {
        "kind": "blackout",
        "at": "2026-08-22T00:00:00Z",
        "duration_s": 2,
        "depth_pct": 1.0,
        "efc": 0.01,
    }


def test_episode_summary_uses_explicit_writer_provenance() -> None:
    rows = [
        {"at": "2026-08-22T00:00:00Z", "status": "OB CAL", "battery_pct": 100.0},
        {"at": "2026-08-22T00:00:01Z", "status": "OL", "battery_pct": 99.0},
    ]

    summary = summarize_episode(rows, physical_kind=BlackoutKind.BLACKOUT_TEST)

    assert summary is not None
    assert summary["kind"] == "self_test"


def test_writer_emits_one_summary_for_natural_and_cal_episodes(tmp_path: Path) -> None:
    writer = TelemetryJsonlWriter(tmp_path)
    writer.write(_observation("OB DISCHRG", 100.0, 0), BlackoutKind.BLACKOUT_REAL)
    writer.write(_observation("OB DISCHRG LB", 40.0, 10), BlackoutKind.BLACKOUT_REAL)
    writer.write(_observation("OL CHRG", 80.0, 20), BlackoutKind.ONLINE)
    writer.write(_observation("OL", 100.0, 30), BlackoutKind.ONLINE)
    writer.write(_observation("OL", 100.0, 31), BlackoutKind.ONLINE)
    writer.write(_observation("CAL DISCHRG", 90.0, 40), BlackoutKind.BLACKOUT_TEST)
    writer.write(_observation("OL CHRG", 100.0, 50), BlackoutKind.ONLINE)

    rows = _rows(tmp_path / "history.jsonl")
    history_lines = (tmp_path / "history.jsonl").read_text().splitlines()
    assert '"duration_s":20,' in history_lines[0]
    assert '"duration_s":10,' in history_lines[1]
    assert rows == [
        {
            "kind": "blackout",
            "at": "2026-08-22T00:00:00Z",
            "duration_s": 20,
            "depth_pct": 60.0,
            "efc": 0.6,
            "load_pct": 20.0,
            "early_v": 13.3,
            "min_v": 13.3,
            "min_at_s": 0,
        },
        {
            "kind": "self_test",
            "at": "2026-08-22T00:00:40Z",
            "duration_s": 10,
            "depth_pct": 10.0,
            "efc": 0.1,
            "load_pct": 20.0,
            "pre_v": 13.3,
            "early_v": 13.3,
            "sag_v": 0.0,
            "min_v": 13.3,
            "min_at_s": 0,
        },
    ]


def test_history_records_typed_voltage_response_and_comparable_delta(tmp_path: Path) -> None:
    history = BatteryHistory(tmp_path / "history.jsonl")

    def episode(start: int, *, early_v: float, load_pct: float) -> list[dict[str, object]]:
        base = datetime(2026, 8, 22, tzinfo=timezone.utc) + timedelta(seconds=start)

        def row(offset: int, status: str, voltage: float) -> dict[str, object]:
            return {
                "at": (base + timedelta(seconds=offset))
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "status": status,
                "battery_pct": 100.0,
                "battery_v": voltage,
                "load_pct": load_pct,
            }

        return [
            *(row(offset, "OL", 13.6) for offset in range(-30, 0)),
            row(0, "OB DISCHRG", 13.4),
            row(1, "OB DISCHRG", 13.1),
            *(row(offset, "OB DISCHRG", early_v) for offset in range(2, 7)),
            row(7, "OB DISCHRG", early_v - 0.1),
            row(8, "OL", 13.0),
        ]

    history.episode(episode(0, early_v=12.9, load_pct=15.0))
    history.episode(episode(60, early_v=12.7, load_pct=18.0))

    first, second = _rows(tmp_path / "history.jsonl")
    assert first == {
        "kind": "blackout",
        "at": "2026-08-22T00:00:00Z",
        "duration_s": 8,
        "depth_pct": 0.0,
        "efc": 0.0,
        "load_pct": 15.0,
        "pre_v": 13.6,
        "early_v": 12.8,
        "sag_v": 0.8,
        "min_v": 12.8,
        "min_at_s": 7,
    }
    assert second["sag_delta_v"] == 0.2


def test_writer_preserves_response_metrics_with_default_online_context(tmp_path: Path) -> None:
    writer = TelemetryJsonlWriter(tmp_path)

    def observation(
        offset: int,
        status: str,
        battery_pct: float,
        voltage: float,
        load_pct: float,
    ) -> PhysicalObservation:
        return replace(
            _observation(status, battery_pct, offset),
            battery_voltage_v=voltage,
            load_percent=load_pct,
        )

    for offset in range(11):
        assert not writer.write(observation(offset, "OL", 100.0, 13.6, 15.0), BlackoutKind.ONLINE)
    assert writer.write(observation(11, "OB DISCHRG", 99.0, 13.4, 15.0), BlackoutKind.BLACKOUT_REAL)
    assert writer.write(observation(12, "OB DISCHRG", 99.0, 13.1, 15.0), BlackoutKind.BLACKOUT_REAL)
    assert writer.write(observation(13, "OB DISCHRG", 99.0, 12.9, 15.0), BlackoutKind.BLACKOUT_REAL)
    assert writer.write(observation(14, "OL", 100.0, 12.9, 15.0), BlackoutKind.ONLINE)

    assert _rows(tmp_path / "history.jsonl") == [
        {
            "kind": "blackout",
            "at": "2026-08-22T00:00:11Z",
            "duration_s": 3,
            "depth_pct": 1.0,
            "efc": 0.01,
            "load_pct": 15.0,
            "pre_v": 13.6,
            "early_v": 12.9,
            "sag_v": 0.7,
            "min_v": 12.9,
            "min_at_s": 2,
        }
    ]


def test_response_includes_voltage_change_after_status_transition() -> None:
    base = datetime(2026, 8, 22, tzinfo=timezone.utc)

    def row(offset: int, status: str, voltage: float) -> dict[str, object]:
        return {
            "at": (base + timedelta(seconds=offset))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "status": status,
            "battery_pct": 100.0,
            "battery_v": voltage,
            "load_pct": 15.0,
        }

    rows = [
        *(row(offset, "OL", 13.6) for offset in range(-30, 0)),
        *(row(offset, "OB DISCHRG", 13.6) for offset in range(0, 12)),
        *(row(offset, "OB DISCHRG", 12.3) for offset in range(12, 16)),
        row(16, "OL", 12.3),
    ]

    summary = summarize_episode(rows)

    assert summary is not None
    assert summary["pre_v"] == 13.6
    assert summary["early_v"] == 12.3
    assert summary["sag_v"] == 1.3
    assert summary["min_at_s"] == 12


def test_response_uses_single_pre_event_voltage_sample() -> None:
    base = datetime(2026, 8, 22, tzinfo=timezone.utc)

    def row(
        offset: int,
        status: str,
        battery_pct: float,
        *,
        voltage: float | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "at": (base + timedelta(seconds=offset))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "status": status,
            "battery_pct": battery_pct,
            "load_pct": 15.0,
        }
        if voltage is not None:
            result["battery_v"] = voltage
        return result

    summary = summarize_episode(
        [
            row(-1, "OL", 100.0, voltage=13.54),
            row(0, "OB DISCHRG", 99.0, voltage=13.4),
            row(1, "OB DISCHRG", 98.0, voltage=13.2),
            row(2, "OB DISCHRG", 97.0, voltage=13.1),
            row(3, "OL", 100.0, voltage=13.1),
        ]
    )

    assert summary is not None
    assert summary["pre_v"] == 13.5
    assert summary["early_v"] == 13.1
    assert summary["sag_v"] == 0.4
    assert summary["min_v"] == 13.1
    assert summary["min_at_s"] == 2


def test_response_metrics_remain_independent_without_pre_event_voltage() -> None:
    base = datetime(2026, 8, 22, tzinfo=timezone.utc)

    def row(
        offset: int, status: str, battery_pct: float, voltage: float | None
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "at": (base + timedelta(seconds=offset))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "status": status,
            "battery_pct": battery_pct,
            "load_pct": 15.0,
        }
        if voltage is not None:
            result["battery_v"] = voltage
        return result

    summary = summarize_episode(
        [
            row(-1, "OL", 100.0, None),
            row(0, "OB DISCHRG", 99.0, 13.4),
            row(1, "OB DISCHRG", 98.0, 13.2),
            row(2, "OB DISCHRG", 97.0, 13.1),
            row(3, "OL", 100.0, None),
        ]
    )

    assert summary is not None
    assert summary["load_pct"] == 15.0
    assert summary["early_v"] == 13.1
    assert summary["min_v"] == 13.1
    assert summary["min_at_s"] == 2
    assert "pre_v" not in summary
    assert "sag_v" not in summary


def test_fractional_event_key_is_canonical_and_duplicate_is_suppressed(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    path.write_text(
        '{"kind":"model_update","at":"2026-08-22T00:01:00.900Z",'
        '"event_at":"2026-08-22T00:00:00.900Z","evidence_at":"2026-08-22T00:00:30.1Z",'
        '"changes":{"physics.ir_compensation.k_volts_per_percent":'
        '{"from":0.015,"to":0.014,"delta":-0.001}},"reason":"old"}\n'
    )
    history = BatteryHistory(path)

    assert canonical_timestamp("2026-08-22T05:00:00.999+05:00") == "2026-08-22T00:00:00Z"
    assert history.event_kinds() == {"2026-08-22T00:00:00Z": "model_update"}
    assert not history.upsert_model_update(
        {
            "event_at": "2026-08-22T00:00:00.2Z",
            "evidence_at": "2026-08-22T00:00:31.2Z",
            "changes": {
                "physics.ir_compensation.k_volts_per_percent": {
                    "from": 0.015,
                    "to": 0.013,
                    "delta": -0.002,
                }
            },
            "reason": "new",
        }
    )
    assert len(path.read_text().splitlines()) == 1


def test_history_recovers_minimal_model_receipt_exactly_once(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    history = BatteryHistory(path)
    receipt = {
        "event_at": "2026-08-22T00:00:00Z",
        "evidence_at": "2026-08-22T00:00:30Z",
        "changes": {
            "physics.ir_compensation.k_volts_per_percent": {
                "from": 0.015,
                "to": 0.012,
                "delta": -0.003,
                "evidence_at": "2026-08-22T00:00:31Z",
                "reason": "curve evidence",
            }
        },
        "reason": "natural blackout",
    }

    assert history.upsert_model_update(receipt)
    assert not history.upsert_model_update(receipt)
    assert _rows(path) == [
        {
            "kind": "model_update",
            "at": "2026-08-22T00:00:30Z",
            "event_at": "2026-08-22T00:00:00Z",
            "evidence_at": "2026-08-22T00:00:30Z",
            "changes": receipt["changes"],
            "reason": "natural blackout",
        }
    ]


def test_ir_observations_are_compact_canonical_and_distinct(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    history = BatteryHistory(path)

    assert history.ir_observation(
        event_at="2026-08-22T05:00:00.900+05:00",
        estimate=0.023,
        evidence_at="2026-08-22T00:00:20.1Z",
        uncertainty=0.001,
        reason="stable load step",
    )
    assert not history.ir_observation(
        event_at="2026-08-22T00:00:00.2Z",
        estimate=0.024,
        evidence_at="2026-08-22T00:00:21Z",
        uncertainty=0.001,
        reason="replay",
    )
    assert history.ir_observations() == [
        {
            "kind": "ir_observation",
            "event_at": "2026-08-22T00:00:00Z",
            "estimate": 0.023,
            "evidence_at": "2026-08-22T00:00:20Z",
            "uncertainty": 0.001,
            "reason": "stable load step",
        }
    ]


def test_ir_observations_after_last_ir_model_update_only(tmp_path: Path) -> None:
    history = BatteryHistory(tmp_path / "history.jsonl")
    for second in (0, 1, 2):
        history.ir_observation(
            event_at=f"2026-08-22T00:00:0{second}Z",
            estimate=0.023,
            evidence_at=f"2026-08-22T00:00:1{second}Z",
            uncertainty=0.001,
            reason="stable load step",
        )
    history.upsert_model_update(
        {
            "kind": "model_update",
            "at": "2026-08-22T00:00:30Z",
            "event_at": "2026-08-22T00:00:02Z",
            "evidence_at": "2026-08-22T00:00:20Z",
            "changes": {
                "physics.ir_compensation.k_volts_per_percent": {
                    "from": 0.025,
                    "to": 0.023,
                    "delta": -0.002,
                }
            },
            "reason": "cohort",
        }
    )
    history.ir_observation(
        event_at="2026-08-22T00:00:03Z",
        estimate=0.022,
        evidence_at="2026-08-22T00:00:31Z",
        uncertainty=0.001,
        reason="stable load step",
    )

    assert [row["event_at"] for row in history.ir_observations()] == ["2026-08-22T00:00:03Z"]
