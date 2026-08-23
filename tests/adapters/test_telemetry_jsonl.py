import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.adapters.minimal_event_file as minimal_event_file
import src.adapters.telemetry_jsonl as telemetry_jsonl
from src.adapters.telemetry_jsonl import TelemetryJsonlWriter
from src.domain.values import BlackoutKind, PhysicalObservation


def _observation(
    status: str, battery_pct: float | None, *, offset_sec: int = 0
) -> PhysicalObservation:
    return PhysicalObservation(
        boot_id="boot",
        monotonic_ns=1,
        wall_time_utc=datetime(2026, 8, 22, tzinfo=timezone.utc) + timedelta(seconds=offset_sec),
        raw_status=status,
        battery_voltage_raw="13.3",
        battery_voltage_v=13.3,
        load_percent=20.0,
        input_voltage_v=0.0 if status.startswith("OB") else 230.0,
        battery_pct=battery_pct,
        runtime_s=600.0,
        output_v=230.0,
    )


def _lines(root: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in (root / "telemetry.jsonl").read_text().splitlines()]


def test_writer_records_blackout_recharge_and_one_terminal_sample(tmp_path: Path) -> None:
    writer = TelemetryJsonlWriter(tmp_path)
    blackout = _observation("OB DISCHRG", 40.0)
    online = _observation("OL CHRG", 80.0)
    full = _observation("OL", 100.0)

    assert writer.write(blackout, BlackoutKind.BLACKOUT_REAL)
    assert writer.write(blackout, BlackoutKind.BLACKOUT_TEST)
    assert writer.write(online, BlackoutKind.ONLINE)
    assert writer.write(full, BlackoutKind.ONLINE)
    assert not writer.write(full, BlackoutKind.ONLINE)

    rows = _lines(tmp_path)
    assert len(rows) == 4
    assert all(len(row) == 8 for row in rows)
    assert rows[-1]["battery_pct"] == 100.0
    completed = writer.take_completed_episode()
    assert completed is not None
    assert completed[-1]["status"] == "OL"
    assert writer.take_completed_episode() is None


def test_full_online_start_is_silent_but_recharge_restart_is_recorded(tmp_path: Path) -> None:
    writer = TelemetryJsonlWriter(tmp_path)
    assert not writer.write(_observation("OL", 100.0), BlackoutKind.ONLINE)
    assert writer.write(_observation("OL CHRG", 99.0), BlackoutKind.ONLINE)
    assert writer.write(_observation("OL", 100.0), BlackoutKind.ONLINE)


def test_silent_online_samples_are_time_bounded_and_flush_before_event(tmp_path: Path) -> None:
    writer = TelemetryJsonlWriter(tmp_path, silent_window_sec=3)

    for offset_sec in (2, 0, 1, 3, 4, 5):
        assert not writer.write(
            _observation("OL", 100.0, offset_sec=offset_sec), BlackoutKind.ONLINE
        )

    event = _observation("OB DISCHRG", 90.0, offset_sec=6)
    assert writer.write(event, BlackoutKind.BLACKOUT_REAL)

    rows = _lines(tmp_path)
    assert [row["at"] for row in rows] == [
        f"2026-08-22T00:00:0{offset_sec}Z" for offset_sec in range(2, 7)
    ]


def test_recorded_recharge_clears_silent_samples_without_duplicates(tmp_path: Path) -> None:
    writer = TelemetryJsonlWriter(tmp_path, silent_window_sec=120)
    assert not writer.write(_observation("OL", 100.0, offset_sec=0), BlackoutKind.ONLINE)
    charging = _observation("OL CHRG", 80.0, offset_sec=1)
    full = _observation("OL", 100.0, offset_sec=2)
    event = _observation("OB DISCHRG", 90.0, offset_sec=3)

    assert writer.write(charging, BlackoutKind.ONLINE)
    assert writer.write(full, BlackoutKind.ONLINE)
    assert writer.write(event, BlackoutKind.BLACKOUT_REAL)

    rows = _lines(tmp_path)
    assert [row["at"] for row in rows] == [
        "2026-08-22T00:00:00Z",
        "2026-08-22T00:00:01Z",
        "2026-08-22T00:00:02Z",
        "2026-08-22T00:00:03Z",
    ]


def test_silent_context_flushes_before_recharge_precursor_and_cal(tmp_path: Path) -> None:
    writer = TelemetryJsonlWriter(tmp_path, silent_window_sec=120)
    for offset_sec in (2, 0, 1):
        assert not writer.write(
            _observation("OL", 100.0, offset_sec=offset_sec), BlackoutKind.ONLINE
        )

    precursor = _observation("OL", 99.0, offset_sec=3)
    calibration = _observation("CAL", 99.0, offset_sec=4)
    assert writer.write(precursor, BlackoutKind.ONLINE)
    assert writer.write(calibration, BlackoutKind.BLACKOUT_TEST)

    assert [row["at"] for row in _lines(tmp_path)] == [
        "2026-08-22T00:00:00Z",
        "2026-08-22T00:00:01Z",
        "2026-08-22T00:00:02Z",
        "2026-08-22T00:00:03Z",
        "2026-08-22T00:00:04Z",
    ]


def test_full_online_keeps_recording_for_tail_then_buffers_silently(tmp_path: Path) -> None:
    writer = TelemetryJsonlWriter(tmp_path, silent_window_sec=3)
    assert writer.write(_observation("OB DISCHRG", 90.0, offset_sec=0), BlackoutKind.BLACKOUT_REAL)
    assert writer.write(_observation("OL", 100.0, offset_sec=1), BlackoutKind.ONLINE)
    assert writer.write(_observation("OL", 100.0, offset_sec=2), BlackoutKind.ONLINE)
    assert writer.write(_observation("OL", 100.0, offset_sec=4), BlackoutKind.ONLINE)
    assert not writer.write(_observation("OL", 100.0, offset_sec=5), BlackoutKind.ONLINE)
    assert not writer.write(_observation("OL", 100.0, offset_sec=6), BlackoutKind.ONLINE)

    assert writer.write(_observation("OB DISCHRG", 90.0, offset_sec=7), BlackoutKind.BLACKOUT_REAL)
    assert [row["at"] for row in _lines(tmp_path)] == [
        f"2026-08-22T00:00:0{offset_sec}Z" for offset_sec in (0, 1, 2, 4, 5, 6, 7)
    ]


def test_below_full_relapse_restarts_post_full_tail(tmp_path: Path) -> None:
    writer = TelemetryJsonlWriter(tmp_path, silent_window_sec=3)
    assert writer.write(_observation("OB DISCHRG", 90.0, offset_sec=0), BlackoutKind.BLACKOUT_REAL)
    assert writer.write(_observation("OL", 100.0, offset_sec=1), BlackoutKind.ONLINE)
    assert writer.write(_observation("OL", 100.0, offset_sec=2), BlackoutKind.ONLINE)
    assert writer.write(_observation("OL", 99.0, offset_sec=3), BlackoutKind.ONLINE)
    assert writer.write(_observation("OL", 100.0, offset_sec=4), BlackoutKind.ONLINE)
    assert writer.write(_observation("OL", 100.0, offset_sec=6), BlackoutKind.ONLINE)
    assert not writer.write(_observation("OL", 100.0, offset_sec=8), BlackoutKind.ONLINE)

    assert writer.write(_observation("OB DISCHRG", 90.0, offset_sec=9), BlackoutKind.BLACKOUT_REAL)
    assert [row["at"] for row in _lines(tmp_path)] == [
        f"2026-08-22T00:00:0{offset_sec}Z" for offset_sec in (0, 1, 2, 3, 4, 6, 8, 9)
    ]


def test_event_during_post_full_tail_resets_episode_without_duplicates(tmp_path: Path) -> None:
    writer = TelemetryJsonlWriter(tmp_path, silent_window_sec=3)
    assert writer.write(_observation("OB DISCHRG", 90.0, offset_sec=0), BlackoutKind.BLACKOUT_REAL)
    assert writer.write(_observation("OL", 100.0, offset_sec=1), BlackoutKind.ONLINE)
    assert writer.write(_observation("OL", 100.0, offset_sec=2), BlackoutKind.ONLINE)
    assert writer.write(_observation("CAL", 100.0, offset_sec=3), BlackoutKind.BLACKOUT_TEST)
    assert writer.write(_observation("OL", 100.0, offset_sec=4), BlackoutKind.ONLINE)
    assert writer.write(_observation("OL", 100.0, offset_sec=6), BlackoutKind.ONLINE)
    assert not writer.write(_observation("OL", 100.0, offset_sec=8), BlackoutKind.ONLINE)

    assert writer.write(_observation("OB DISCHRG", 90.0, offset_sec=9), BlackoutKind.BLACKOUT_REAL)
    assert [row["at"] for row in _lines(tmp_path)] == [
        f"2026-08-22T00:00:0{offset_sec}Z" for offset_sec in (0, 1, 2, 3, 4, 6, 8, 9)
    ]


def test_silent_online_remains_disk_silent_without_event(tmp_path: Path) -> None:
    writer = TelemetryJsonlWriter(tmp_path, silent_window_sec=120)
    for offset_sec in range(4):
        assert not writer.write(
            _observation("OL", 100.0, offset_sec=offset_sec), BlackoutKind.ONLINE
        )

    assert not (tmp_path / "telemetry.jsonl").exists()


def test_failed_pre_event_flush_preserves_unwritten_samples_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = TelemetryJsonlWriter(tmp_path, silent_window_sec=120)
    for offset_sec in range(2):
        assert not writer.write(
            _observation("OL", 100.0, offset_sec=offset_sec), BlackoutKind.ONLINE
        )

    real_append = telemetry_jsonl.append
    calls = 0

    def append_once_then_fail(path: Path, record: dict[str, object]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("full disk")
        real_append(path, record)

    monkeypatch.setattr(telemetry_jsonl, "append", append_once_then_fail)
    event = _observation("OB DISCHRG", 90.0, offset_sec=2)
    with pytest.raises(OSError, match="full disk"):
        writer.write(event, BlackoutKind.BLACKOUT_REAL)

    monkeypatch.setattr(telemetry_jsonl, "append", real_append)
    assert writer.write(event, BlackoutKind.BLACKOUT_REAL)
    assert [row["at"] for row in _lines(tmp_path)] == [
        "2026-08-22T00:00:00Z",
        "2026-08-22T00:00:01Z",
        "2026-08-22T00:00:02Z",
    ]


def test_rebooted_writer_closes_old_ob_tail_on_full_online_observation(tmp_path: Path) -> None:
    first = TelemetryJsonlWriter(tmp_path)
    assert first.write(_observation("OB DISCHRG", 40.0), BlackoutKind.BLACKOUT_REAL)

    rebooted = TelemetryJsonlWriter(tmp_path)
    assert rebooted.write(_observation("OL", 100.0, offset_sec=1), BlackoutKind.ONLINE)

    rows = _lines(tmp_path)
    assert [row["status"] for row in rows] == ["OB DISCHRG", "OL"]
    completed = rebooted.take_completed_episode()
    assert completed is not None
    assert [row["status"] for row in completed] == ["OB DISCHRG", "OL"]
    assert rebooted.take_completed_episode() is None


def test_rebooted_writer_continues_ob_tail_without_duplicate_lines(tmp_path: Path) -> None:
    first = TelemetryJsonlWriter(tmp_path)
    assert first.write(_observation("OB DISCHRG", 40.0), BlackoutKind.BLACKOUT_REAL)

    rebooted = TelemetryJsonlWriter(tmp_path)
    assert rebooted.write(
        _observation("OB DISCHRG", 39.0, offset_sec=1), BlackoutKind.BLACKOUT_REAL
    )
    assert rebooted.write(_observation("OL", 100.0, offset_sec=2), BlackoutKind.ONLINE)

    rows = _lines(tmp_path)
    assert [row["status"] for row in rows] == ["OB DISCHRG", "OB DISCHRG", "OL"]
    assert len(rows) == 3


def test_historical_fractional_timestamp_is_read_and_new_sample_is_whole_second(
    tmp_path: Path,
) -> None:
    path = tmp_path / "telemetry.jsonl"
    historical = {
        "at": "2026-08-22T00:00:00.123456Z",
        "battery_v": 13.3,
        "battery_pct": 40.0,
        "runtime_s": 600.0,
        "load_pct": 20.0,
        "input_v": 0.0,
        "output_v": 230.0,
        "status": "OB DISCHRG",
    }
    path.write_text(json.dumps(historical) + "\n")

    writer = TelemetryJsonlWriter(tmp_path)
    assert writer.write(_observation("OL", 100.0, offset_sec=1), BlackoutKind.ONLINE)

    rows = _lines(tmp_path)
    assert rows[0]["at"] == "2026-08-22T00:00:00.123456Z"
    assert rows[1]["at"] == "2026-08-22T00:00:01Z"


def test_append_handles_short_os_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "telemetry.jsonl"
    record = {
        "at": "2026-08-22T00:00:00Z",
        "battery_v": 13.3,
        "battery_pct": 40.0,
        "runtime_s": 600.0,
        "load_pct": 20.0,
        "input_v": 0.0,
        "output_v": 230.0,
        "status": "OB DISCHRG",
    }
    real_write = minimal_event_file.os.write
    calls = 0

    def short_write(fd: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        return real_write(fd, data[: max(1, len(data) // 2)])

    monkeypatch.setattr(minimal_event_file.os, "write", short_write)
    minimal_event_file.append(path, record)

    assert calls > 1
    assert minimal_event_file.read(path).records == (record,)


def test_writer_exposes_ir_observation_and_idempotent_model_receipt(tmp_path: Path) -> None:
    writer = TelemetryJsonlWriter(tmp_path)
    observation = {
        "event_at": "2026-08-22T00:00:00.900Z",
        "estimate": 0.023,
        "evidence_at": "2026-08-22T00:00:20.9Z",
        "uncertainty": 0.001,
        "reason": "stable load step",
    }
    assert writer.record_ir_observation(observation)
    assert not writer.record_ir_observation(observation)
    receipt = {
        "event_at": "2026-08-22T00:00:00Z",
        "evidence_at": "2026-08-22T00:00:20Z",
        "changes": {
            "physics.ir_compensation.k_volts_per_percent": {
                "from": 0.025,
                "to": 0.023,
                "delta": -0.002,
                "evidence_at": "2026-08-22T00:00:20Z",
                "reason": "cohort",
            }
        },
        "reason": "cohort",
    }
    assert writer.upsert_model_update(receipt)
    assert not writer.upsert_model_update(receipt)
    assert writer.ir_observations() == []
