"""Focused tests for optional discharge-journal health export."""

from types import SimpleNamespace
from unittest.mock import patch

from src.monitor_config import CurrentMetrics
from src.virtual_ups_exporter import VirtualUpsExporter


class _Model:
    soh_threshold = 0.8

    def get_convergence_status(self):
        return SimpleNamespace(
            latest_ah=None,
            rated_ah=7.2,
            confidence_percent=0.0,
            sample_count=0,
            converged=False,
        )

    def get_soh(self):
        return 1.0

    def get_battery_install_date(self):
        return None

    def get_cycle_count(self):
        return 0

    def get_cumulative_on_battery_sec(self):
        return 0

    def compute_replacement_due(self):
        return None

    def get_r_internal_history(self):
        return []


def _exporter(tmp_path, provider=None):
    return VirtualUpsExporter(
        _Model(),
        SimpleNamespace(transition_occurred=False, state=None),
        SimpleNamespace(
            last_days_since_deep=None,
            last_ir_trend_rate=None,
            last_cycle_budget_remaining=None,
            last_discharge_timestamp=None,
        ),
        SimpleNamespace(last_scheduling_reason="observing", last_next_test_timestamp=None),
        provider,
        virtual_ups_path=tmp_path / "ups-virtual.dev",
        health_path=tmp_path / "health.json",
    )


def _metrics():
    return CurrentMetrics(soc=0.75, ups_status_override="OL")


def test_exporter_reads_optional_journal_state_and_extends_health_snapshot(tmp_path):
    def provider():
        return {
            "journal_healthy": False,
            "active_event_id": "evt-123",
            "journal_last_synced_seq": 9,
            "journal_last_error": "/private/journal/events.jsonl\nwrite failed",
            "pending_replay": True,
            "recovered_partial_events": 2,
        }

    exporter = _exporter(tmp_path, provider)

    with patch("src.virtual_ups_exporter.write_health_endpoint") as write_health:
        exporter.write_health_snapshot(1.5, _metrics(), 0)

    snapshot = write_health.call_args.args[0]
    assert snapshot.journal_healthy is False
    assert snapshot.active_event_id == "evt-123"
    assert snapshot.journal_last_synced_seq == 9
    assert snapshot.pending_replay is True
    assert snapshot.recovered_partial_events == 2
    assert "/private/journal" not in snapshot.journal_last_error
    assert "\n" not in snapshot.journal_last_error


def test_exporter_provider_failure_does_not_block_virtual_ups_or_lb(tmp_path):
    def broken_provider():
        raise OSError("/secret/ups/discharge.jsonl\npermission denied")

    exporter = _exporter(tmp_path, broken_provider)
    metrics = _metrics()
    metrics.ups_status_override = "OB DISCHRG LB"

    with patch("src.virtual_ups_exporter.write_virtual_ups_dev") as write_virtual:
        exporter.write_virtual_ups({"ups.status": "OL", "battery.runtime": "1"}, 20, 1, metrics)

    assert write_virtual.call_count == 1
    assert write_virtual.call_args.args[0]["ups.status"] == "OB DISCHRG LB"

    with patch("src.virtual_ups_exporter.write_health_endpoint") as write_health:
        exporter.write_health_snapshot(1.5, metrics, 0)
    snapshot = write_health.call_args.args[0]
    assert snapshot.journal_healthy is False
    assert "/secret/ups" not in snapshot.journal_last_error
    assert "\n" not in snapshot.journal_last_error


def test_exporter_missing_physical_status_preserves_existing_virtual_file(tmp_path):
    """A partial reply cannot synthesize OL or overwrite a preserved OB/LB state."""
    exporter = _exporter(tmp_path)
    metrics = CurrentMetrics(soc=0.75, ups_status_override=None)
    virtual_path = tmp_path / "ups-virtual.dev"
    prior_bytes = b"ups.status: OB DISCHRG LB\nbattery.runtime: 1\n"
    virtual_path.write_bytes(prior_bytes)

    with patch("src.virtual_ups_exporter.write_virtual_ups_dev") as write_virtual:
        result = exporter.write_virtual_ups(
            {"battery.voltage": 13.0, "ups.load": 20.0},
            75,
            10,
            metrics,
        )

    assert result is False
    write_virtual.assert_not_called()
    assert virtual_path.read_bytes() == prior_bytes


def test_exporter_constructor_without_provider_remains_healthy_by_default(tmp_path):
    exporter = _exporter(tmp_path)
    fields = exporter._journal_health_fields()
    assert fields == {
        "journal_healthy": True,
        "active_event_id": None,
        "journal_last_synced_seq": None,
        "journal_last_error": None,
        "pending_replay": False,
        "recovered_partial_events": 0,
        "cycle_count": None,
        "cumulative_on_battery_sec": None,
    }
