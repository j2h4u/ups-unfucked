"""Tests for the MOTD status renderer (src/motd_status.py).

The renderer is pure read-only: given a model.json and a health endpoint path it
emits a flat ``key=value`` block. Tests drive it with temp files and assert on the
parsed fields rather than the raw string.
"""

import json
from pathlib import Path

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
    """No model.json and no health file → unknown capacity, no samples, no sulfation."""
    fields = _render(tmp_path / "absent-model.json", tmp_path / "absent-health.json")

    assert fields["capacity_status"] == "unknown"
    assert fields["capacity_samples"] == "0"
    assert fields["capacity_measured_ah"] == ""
    assert fields["sulfation_pct"] == ""
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
    """SoH fraction renders as integer percent; replacement date passes through."""
    model_path = tmp_path / "model.json"
    _write_model(model_path, {"soh": 0.85, "replacement_due": "2027-01-01"})

    fields = _render(model_path, tmp_path / "absent-health.json")

    assert fields["soh_pct"] == "85"
    assert fields["replacement_due"] == "2027-01-01"


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


def test_sulfation_and_next_test_come_from_health(tmp_path):
    """Sulfation score (fraction→percent) and next-test timestamp read from health file."""
    model_path = tmp_path / "model.json"
    _write_model(model_path, {})
    health_path = tmp_path / "health.json"
    _write_health(
        health_path,
        {"sulfation_score": 0.65, "next_test_timestamp": "2026-07-01T00:00:00+00:00"},
    )

    fields = _render(model_path, health_path)

    assert fields["sulfation_pct"] == "65"
    assert fields["next_test_timestamp"] == "2026-07-01T00:00:00+00:00"


def test_null_sulfation_in_health_renders_empty(tmp_path):
    """A health file with null sulfation_score → empty sulfation_pct (no banner line)."""
    model_path = tmp_path / "model.json"
    _write_model(model_path, {})
    health_path = tmp_path / "health.json"
    _write_health(health_path, {"sulfation_score": None, "next_test_timestamp": None})

    fields = _render(model_path, health_path)

    assert fields["sulfation_pct"] == ""
    assert fields["next_test_timestamp"] == ""
