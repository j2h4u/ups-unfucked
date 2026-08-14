"""Release A health/export observability contract tests."""

import json
from pathlib import Path
from types import SimpleNamespace

from src.monitor_config import HealthSnapshot, write_health_endpoint
from src.virtual_ups_exporter import VirtualUpsExporter


def test_health_endpoint_exposes_release_a_operator_state(tmp_path: Path):
    path = tmp_path / "health.json"
    snapshot = HealthSnapshot(
        startup_degraded=True,
        model_update_mode="capture_only",
        automatic_dispatch=False,
        scheduler_mode="proposal_only",
        eligible_for_operator_test_at="2026-08-16T08:00:00+00:00",
        last_event_disposition="recorded_only",
    )

    assert write_health_endpoint(snapshot, health_path=path) is True
    health = json.loads(path.read_text())
    assert health["startup_degraded"] is True
    assert health["model_update_mode"] == "capture_only"
    assert health["automatic_dispatch"] is False
    assert health["scheduler_mode"] == "proposal_only"
    assert health["eligible_for_operator_test_at"] == "2026-08-16T08:00:00+00:00"
    assert health["last_event_disposition"] == "recorded_only"


def test_exporter_keeps_release_a_capture_and_dispatch_flags(tmp_path: Path, monkeypatch):
    model = SimpleNamespace(
        soh_threshold=0.8,
        get_convergence_status=lambda: SimpleNamespace(
            latest_ah=None,
            rated_ah=7.2,
            confidence_percent=0.0,
            sample_count=0,
            converged=False,
        ),
    )
    handler = SimpleNamespace(
        last_days_since_deep=None,
        last_ir_trend_rate=None,
        last_cycle_budget_remaining=None,
        last_discharge_timestamp=None,
    )
    scheduler = SimpleNamespace(
        last_scheduling_reason="observing",
        last_next_test_timestamp="2026-08-16T08:00:00+00:00",
        scheduler_mode="proposal_only",
    )
    exporter = VirtualUpsExporter(
        model,
        None,
        handler,
        scheduler,
        journal_health_provider=lambda: {
            "startup_degraded": True,
            "last_event_disposition": "recorded_only",
        },
        virtual_ups_path=tmp_path / "ups.dev",
        health_path=tmp_path / "health.json",
    )
    captured = {}
    monkeypatch.setattr(
        "src.virtual_ups_exporter.write_health_endpoint",
        lambda snapshot, *, health_path: captured.setdefault("snapshot", snapshot) or True,
    )

    exporter.write_health_snapshot(
        1.0,
        SimpleNamespace(
            soc=0.5,
            ups_status_override="OL",
            shutdown_imminent=False,
        ),
        0,
    )
    snapshot = captured["snapshot"]
    assert snapshot.startup_degraded is True
    assert snapshot.model_update_mode == "capture_only"
    assert snapshot.automatic_dispatch is False
    assert snapshot.scheduler_mode == "proposal_only"
    assert snapshot.eligible_for_operator_test_at == scheduler.last_next_test_timestamp
    assert snapshot.last_event_disposition == "recorded_only"


def test_exporter_reports_execute_mode_as_automatic_dispatch(tmp_path: Path, monkeypatch):
    model = SimpleNamespace(
        soh_threshold=0.8,
        get_convergence_status=lambda: SimpleNamespace(
            latest_ah=None,
            rated_ah=7.2,
            confidence_percent=0.0,
            sample_count=0,
            converged=False,
        ),
    )
    handler = SimpleNamespace(
        last_days_since_deep=None,
        last_ir_trend_rate=None,
        last_cycle_budget_remaining=None,
        last_discharge_timestamp=None,
    )
    scheduler = SimpleNamespace(
        last_scheduling_reason="observing",
        last_next_test_timestamp=None,
        scheduler_mode="execute",
    )
    exporter = VirtualUpsExporter(
        model,
        None,
        handler,
        scheduler,
        virtual_ups_path=tmp_path / "ups.dev",
        health_path=tmp_path / "health.json",
    )
    captured = {}
    monkeypatch.setattr(
        "src.virtual_ups_exporter.write_health_endpoint",
        lambda snapshot, *, health_path: captured.setdefault("snapshot", snapshot) or True,
    )

    exporter.write_health_snapshot(
        1.0,
        SimpleNamespace(soc=0.5, ups_status_override="OL", shutdown_imminent=False),
        0,
    )
    snapshot = captured["snapshot"]
    assert snapshot.model_update_mode == "capture_only"
    assert snapshot.scheduler_mode == "execute"
    assert snapshot.automatic_dispatch is True
