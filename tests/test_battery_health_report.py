"""Focused checks for the health-only operator CLI."""

import importlib.util
import json
from pathlib import Path


def _load_report_module():
    path = Path(__file__).parents[1] / "scripts" / "battery-health.py"
    spec = importlib.util.spec_from_file_location("battery_health_report", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_reads_only_bounded_health_projection(tmp_path: Path) -> None:
    report = _load_report_module()
    health_path = tmp_path / "health.json"
    health_path.write_text(
        json.dumps(
            {
                "physical_status": "OB DISCHRG LB",
                "virtual_status": "OB DISCHRG",
                "raw_lb_observed": True,
                "virtual_lb": False,
                "storage": {
                    "queued_observations": 3,
                    "consumed_step_budget_remaining": 252,
                },
            }
        )
    )

    rendered = report.render_report(health_path)

    assert "physical_status=OB DISCHRG LB" in rendered
    assert "virtual_status=OB DISCHRG" in rendered
    assert "raw_lb_observed=true" in rendered
    assert "queued_observations=3" in rendered
    assert "consumed_evidence_budget_remaining=252" in rendered


def test_cli_no_longer_imports_or_reads_mutable_model(tmp_path: Path, monkeypatch, capsys) -> None:
    report = _load_report_module()
    health_path = tmp_path / "health.json"
    health_path.write_text("{}")
    monkeypatch.setenv("UPS_HEALTH_PATH", str(health_path))
    monkeypatch.setenv("UPS_MODEL_PATH", str(tmp_path / "must-not-be-read.json"))

    report.main()

    assert "physical_status=" in capsys.readouterr().out
