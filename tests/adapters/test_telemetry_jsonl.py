import json
from datetime import datetime, timezone
from pathlib import Path

from src.adapters.telemetry_jsonl import TelemetryJsonlWriter
from src.domain.values import BlackoutKind, PhysicalObservation


def _observation(status: str, battery_pct: float | None) -> PhysicalObservation:
    return PhysicalObservation(
        boot_id="boot",
        monotonic_ns=1,
        wall_time_utc=datetime(2026, 8, 22, tzinfo=timezone.utc),
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
    return [
        json.loads(line) for line in (root / "events" / "telemetry.jsonl").read_text().splitlines()
    ]


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


def test_full_online_start_is_silent_but_recharge_restart_is_recorded(tmp_path: Path) -> None:
    writer = TelemetryJsonlWriter(tmp_path)
    assert not writer.write(_observation("OL", 100.0), BlackoutKind.ONLINE)
    assert writer.write(_observation("OL CHRG", 99.0), BlackoutKind.ONLINE)
    assert writer.write(_observation("OL", 100.0), BlackoutKind.ONLINE)
