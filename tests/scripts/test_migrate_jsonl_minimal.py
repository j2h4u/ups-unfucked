import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_migrator():
    path = Path(__file__).parents[2] / "scripts/migrate-jsonl-remove-hashes.py"
    spec = importlib.util.spec_from_file_location("migrate_jsonl", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_event(path: Path, kind: str, samples: list[dict[str, object]]) -> None:
    records = []
    for index, sample in enumerate(samples):
        records.append(
            {
                "record_type": "start" if index == 0 else "observation",
                "event_kind": kind,
                "blackout_id": f"{kind}-secret-id",
                "seq": index,
                "wall_time_utc": sample["at"],
                "payload": {
                    "observation": {
                        "wall_time_utc": sample["at"],
                        "battery_voltage_v": sample["battery_v"],
                        "load_percent": sample.get("load_pct"),
                        "input_voltage_v": sample.get("input_v"),
                        "raw_status": sample["status"],
                        "battery_percent": sample.get("battery_pct"),
                        "runtime_seconds": sample.get("runtime_s"),
                        "output_voltage_v": sample.get("output_v"),
                    }
                },
            }
        )
    records.append({"record_type": "end", "payload": {"termination": "done"}})
    path.write_bytes(b"".join(json.dumps(record).encode() + b"\n" for record in records))


def test_conversion_adds_missing_fields_as_null_and_keeps_full_precision(tmp_path: Path) -> None:
    migrator = _load_migrator()
    events = tmp_path / "events"
    events.mkdir()
    source = events / "evt-blackout.jsonl"
    _write_event(
        source,
        "blackout",
        [
            {
                "at": "2026-08-21T14:37:42.519563Z",
                "battery_v": 13.6,
                "load_pct": 19.0,
                "input_v": 236.0,
                "status": "OB DISCHRG",
            }
        ],
    )

    rows = migrator._collect_samples((source,))
    assert rows == [
        {
            "at": "2026-08-21T14:37:42.519563Z",
            "battery_v": 13.6,
            "battery_pct": None,
            "runtime_s": None,
            "load_pct": 19.0,
            "input_v": 236.0,
            "output_v": None,
            "status": "OB DISCHRG",
        }
    ]


def test_apply_stages_one_sorted_output_and_removes_legacy_files_and_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrator = _load_migrator()
    events = tmp_path / "events"
    events.mkdir()
    _write_event(
        events / "evt-blackout.jsonl",
        "blackout",
        [
            {"at": "2026-08-21T14:40:00Z", "battery_v": 12.8, "status": "OB"},
            {"at": "2026-08-21T14:41:00Z", "battery_v": 12.7, "status": "OB"},
        ],
    )
    _write_event(
        events / "evt-recharge.jsonl",
        "recharge",
        [{"at": "2026-08-21T14:39:00Z", "battery_v": 13.0, "status": "OL CHRG"}],
    )
    for name in (
        "active.json",
        "segments-blackout.jsonl",
        "segments-recharge.jsonl",
        "report-outbox.jsonl",
        "report-outbox.cursor.json",
    ):
        (events / name).write_text("legacy")

    monkeypatch.setattr(sys, "argv", ["migrate-jsonl-remove-hashes.py", str(events), "--apply"])
    assert migrator.main() == 0

    assert sorted(path.name for path in events.iterdir()) == ["telemetry.jsonl"]
    rows = [json.loads(line) for line in (events / "telemetry.jsonl").read_text().splitlines()]
    assert len(rows) == 3
    assert [row["at"] for row in rows] == [
        "2026-08-21T14:39:00Z",
        "2026-08-21T14:40:00Z",
        "2026-08-21T14:41:00Z",
    ]
    assert all(set(row) == migrator.TELEMETRY_FIELDS for row in rows)
    assert "blackout_id" not in (events / "telemetry.jsonl").read_text()


def test_late_validation_failure_leaves_sources_and_sidecars_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrator = _load_migrator()
    events = tmp_path / "events"
    events.mkdir()
    source = events / "evt-blackout.jsonl"
    _write_event(
        source,
        "blackout",
        [{"at": "2026-08-21T14:40:00Z", "battery_v": 12.8, "status": "OB"}],
    )
    sidecar = events / "active.json"
    sidecar.write_text("keep")
    before = source.read_bytes()

    def fail_late(path: Path) -> int:
        if path.name.startswith(".telemetry.jsonl."):
            raise ValueError("synthetic late validation failure")
        return migrator._validate_telemetry(path)

    monkeypatch.setattr(migrator, "_validate_telemetry", fail_late)
    monkeypatch.setattr(sys, "argv", ["migrate-jsonl-remove-hashes.py", str(events), "--apply"])
    with pytest.raises(ValueError, match="synthetic late validation failure"):
        migrator.main()

    assert source.read_bytes() == before
    assert sidecar.read_text() == "keep"
    assert not (events / "telemetry.jsonl").exists()
    assert sorted(path.name for path in events.iterdir()) == ["active.json", "evt-blackout.jsonl"]
