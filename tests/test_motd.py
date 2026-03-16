"""MOTD integration tests for UPS battery monitor.

Tests for MOTD module scripts that display UPS status, capacity estimates,
and new battery detection alerts.
"""

import subprocess
import json
import tempfile
from pathlib import Path
import pytest


@pytest.fixture
def temp_model_json():
    """Create a temporary model.json for MOTD testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        model_path = Path(tmpdir) / 'model.json'
        yield model_path


def test_motd_shows_new_battery_alert(temp_model_json):
    """MOTD integration: new_battery_detected flag triggers alert display."""

    # Setup: model.json with new_battery_detected flag set
    model_data = {
        'capacity_estimates': [],
        'new_battery_detected': True,
        'new_battery_detected_timestamp': '2026-03-16T10:30:00',
        'soh_history': [],
        'lut': [],
    }

    with open(temp_model_json, 'w') as f:
        json.dump(model_data, f)

    # Run MOTD module (scripts/motd/51-ups.sh)
    motd_script = Path('scripts/motd/51-ups.sh')

    result = subprocess.run(
        ['bash', str(motd_script)],
        env={'HOME': str(temp_model_json.parent)},
        capture_output=True,
        text=True,
        cwd='/home/j2h4u/repos/j2h4u/ups-battery-monitor'
    )

    # Verify: MOTD output contains alert text
    assert '⚠️  Possible new battery detected' in result.stdout or \
           '⚠️' in result.stdout, \
           f"Alert not found in output: {result.stdout}"
    assert 'ups-battery-monitor --new-battery' in result.stdout, \
           f"Command not found in output: {result.stdout}"
    assert '2026-03-16T10:30:00' in result.stdout, \
           f"Timestamp not found in output: {result.stdout}"

    # Setup: model.json with flag NOT set
    model_data['new_battery_detected'] = False

    with open(temp_model_json, 'w') as f:
        json.dump(model_data, f)

    result = subprocess.run(
        ['bash', str(motd_script)],
        env={'HOME': str(temp_model_json.parent)},
        capture_output=True,
        text=True,
        cwd='/home/j2h4u/repos/j2h4u/ups-battery-monitor'
    )

    # Verify: MOTD output does NOT contain alert when flag is false
    assert '⚠️  Possible new battery detected' not in result.stdout or \
           'ups-battery-monitor --new-battery' not in result.stdout, \
           f"Alert should not be shown when flag=false: {result.stdout}"
