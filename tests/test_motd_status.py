"""Tests for the MOTD status renderer (src/motd_status.py).

The renderer is pure read-only: given a model.json and a health endpoint path it
emits a flat ``key=value`` block. Tests drive it with temp files and assert on the
parsed fields rather than the raw string.
"""

import json
from pathlib import Path

from src.model import BatteryModel
from src.motd_status import render_motd


def _render(model_path: Path, health_path: Path) -> dict[str, str]:
    """Render and parse the key=value block into a dict for assertions."""
    output = render_motd(model_path=model_path, health_path=health_path)
    fields = {}
    for line in output.splitlines():
        key, _, value = line.partition("=")
        fields[key] = value
    return fields


def _write_model(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


def _write_health(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


def test_missing_files_yield_empty_unknown_state(tmp_path):
    """No model.json and no health file → unknown capacity, no samples, no next test."""
    fields = _render(tmp_path / "absent-model.json", tmp_path / "absent-health.json")

    assert fields["capacity_status"] == "unknown"
    assert fields["capacity_samples"] == "0"
    assert fields["capacity_measured_ah"] == ""
    assert fields["next_test_timestamp"] == ""
    assert fields["new_battery_detected"] == "false"


def test_two_samples_report_measuring(tmp_path):
    """Fewer than 3 estimates → measuring, zero confidence, latest Ah surfaced."""
    model_path = tmp_path / "model.json"
    _write_model(model_path, {"capacity_estimates": [{"ah_estimate": 6.0}, {"ah_estimate": 6.1}]})

    fields = _render(model_path, tmp_path / "absent-health.json")

    assert fields["capacity_status"] == "measuring"
    assert fields["capacity_samples"] == "2"
    assert fields["capacity_measured_ah"] == "6.1"
    assert fields["capacity_confidence_pct"] == "0"


def test_three_tight_samples_report_locked(tmp_path):
    """Three low-variance estimates (CoV < 0.10) → locked with positive confidence."""
    model_path = tmp_path / "model.json"
    _write_model(
        model_path,
        {
            "capacity_estimates": [
                {"ah_estimate": 6.0},
                {"ah_estimate": 6.05},
                {"ah_estimate": 5.95},
            ]
        },
    )

    fields = _render(model_path, tmp_path / "absent-health.json")

    assert fields["capacity_status"] == "locked"
    assert fields["capacity_samples"] == "3"
    assert fields["capacity_measured_ah"] == "5.95"
    assert int(fields["capacity_confidence_pct"]) > 0


def test_soh_and_replacement_due_surface(tmp_path):
    """SoH fraction renders as integer percent; replacement_due is computed live (not persisted).

    replacement_due is now computed via compute_replacement_due() — it requires both
    converged capacity_estimates (>=3, CoV<0.10) and a regression-quality soh_history
    (>=3 points, negative slope, R²>=0.5). The MOTD surfaces a non-empty date string
    when both conditions are met. This tests that the live-compute path surfaces in the
    rendered output.
    """
    model_path = tmp_path / "model.json"
    # Converged capacity estimates (CoV well below 0.10)
    capacity_estimates = [
        {"ah_estimate": 7.15, "timestamp": "2025-06-01T00:00:00Z", "confidence": 0.95, "metadata": {}},
        {"ah_estimate": 7.18, "timestamp": "2025-07-01T00:00:00Z", "confidence": 0.95, "metadata": {}},
        {"ah_estimate": 7.20, "timestamp": "2025-08-01T00:00:00Z", "confidence": 0.95, "metadata": {}},
    ]
    # Regression-quality soh_history: negative slope, R²≥0.5, all same capacity_ah_ref
    soh_history = [
        {"date": "2025-06-01", "soh": 0.98, "capacity_ah_ref": 7.2},
        {"date": "2025-09-01", "soh": 0.95, "capacity_ah_ref": 7.2},
        {"date": "2025-12-01", "soh": 0.92, "capacity_ah_ref": 7.2},
        {"date": "2026-03-01", "soh": 0.89, "capacity_ah_ref": 7.2},
    ]
    _write_model(model_path, {"soh": 0.85, "capacity_estimates": capacity_estimates, "soh_history": soh_history})

    fields = _render(model_path, tmp_path / "absent-health.json")

    assert fields["soh_pct"] == "85"
    # Live compute_replacement_due() must return a non-empty date for this converged fixture
    assert fields["replacement_due"] != ""


def test_replacement_due_uses_threshold_from_health_endpoint(tmp_path):
    """WR-01: the MOTD replacement date tracks the daemon's configured soh_alert_threshold
    (read from health.json), not a hardcoded 0.80 — so the two surfaces never disagree."""
    model_path = tmp_path / "model.json"
    capacity_estimates = [
        {"ah_estimate": 7.15, "timestamp": "2025-06-01T00:00:00Z", "confidence": 0.95, "metadata": {}},
        {"ah_estimate": 7.18, "timestamp": "2025-07-01T00:00:00Z", "confidence": 0.95, "metadata": {}},
        {"ah_estimate": 7.20, "timestamp": "2025-08-01T00:00:00Z", "confidence": 0.95, "metadata": {}},
    ]
    soh_history = [
        {"date": "2025-06-01", "soh": 0.98, "capacity_ah_ref": 7.2},
        {"date": "2025-09-01", "soh": 0.95, "capacity_ah_ref": 7.2},
        {"date": "2025-12-01", "soh": 0.92, "capacity_ah_ref": 7.2},
        {"date": "2026-03-01", "soh": 0.89, "capacity_ah_ref": 7.2},
    ]
    _write_model(model_path, {"soh": 0.85, "capacity_estimates": capacity_estimates, "soh_history": soh_history})

    health_path = tmp_path / "health.json"
    _write_health(health_path, {"soh_alert_threshold": 0.70})
    fields = _render(model_path, health_path)

    # MOTD reproduces the daemon's value for THIS configured threshold, byte-for-byte.
    daemon_value = BatteryModel(model_path, soh_threshold=0.70).compute_replacement_due()
    assert fields["replacement_due"] == (daemon_value or "")

    # A different configured threshold yields a different date — proving the value is
    # sourced from health.json, not hardcoded to 0.80.
    _write_health(health_path, {"soh_alert_threshold": 0.90})
    fields_high = _render(model_path, health_path)
    assert fields_high["replacement_due"] != fields["replacement_due"]


def test_new_battery_flag_surfaces(tmp_path):
    """new_battery_detected + timestamp surface for the MOTD alert line."""
    model_path = tmp_path / "model.json"
    _write_model(
        model_path,
        {
            "new_battery_detected": True,
            "new_battery_detected_timestamp": "2026-06-03T10:00:00+00:00",
        },
    )

    fields = _render(model_path, tmp_path / "absent-health.json")

    assert fields["new_battery_detected"] == "true"
    assert fields["new_battery_timestamp"] == "2026-06-03T10:00:00+00:00"


def test_next_test_comes_from_health(tmp_path):
    """Next-test timestamp is read from the health endpoint file."""
    model_path = tmp_path / "model.json"
    _write_model(model_path, {})
    health_path = tmp_path / "health.json"
    _write_health(
        health_path,
        {"next_test_timestamp": "2026-07-01T00:00:00+00:00"},
    )

    fields = _render(model_path, health_path)

    assert fields["next_test_timestamp"] == "2026-07-01T00:00:00+00:00"


def test_null_next_test_in_health_renders_empty(tmp_path):
    """A health file with null next_test_timestamp → empty string (no banner line)."""
    model_path = tmp_path / "model.json"
    _write_model(model_path, {})
    health_path = tmp_path / "health.json"
    _write_health(health_path, {"next_test_timestamp": None})

    fields = _render(model_path, health_path)

    assert fields["next_test_timestamp"] == ""
