"""Focused checks for the operator-facing battery-health journal section."""

import importlib.util
from pathlib import Path


def _load_report_module():
    path = Path(__file__).parents[1] / "scripts" / "battery-health.py"
    spec = importlib.util.spec_from_file_location("battery_health_report", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_journal_report_distinguishes_degraded_recovery_from_authoritative_model(capsys):
    report = _load_report_module()

    report.print_journal_status(
        {
            "journal_healthy": False,
            "active_event_id": "event-123",
            "journal_last_synced_seq": 9,
            "journal_last_error": "write failed\n/path=private",
            "pending_replay": True,
            "recovered_partial_events": 2,
        }
    )

    output = capsys.readouterr().out
    assert "Journal:           DEGRADED" in output
    assert "Journal open event: event-123" in output
    assert "Journal replay:    pending" in output
    assert "Journal sync seq:  9" in output
    assert "Journal error:     write failed /path=private" in output
    assert "Operational partial/recovered: 2" in output
    assert "excluded from authoritative capacity/SoH" in output


def test_health_counters_override_model_counters():
    report = _load_report_module()

    cycle_count, cumulative_sec = report.get_operational_counters(
        {"cycle_count": 21, "cumulative_on_battery_sec": 490.0},
        {"cycle_count": 24, "cumulative_on_battery_sec": 812.5},
    )

    assert cycle_count == 24
    assert cumulative_sec == 812.5


def test_invalid_health_counters_fall_back_to_model():
    report = _load_report_module()

    cycle_count, cumulative_sec = report.get_operational_counters(
        {"cycle_count": 21, "cumulative_on_battery_sec": 490.0},
        {"cycle_count": -1, "cumulative_on_battery_sec": "not-a-number"},
    )

    assert cycle_count == 21
    assert cumulative_sec == 490.0
