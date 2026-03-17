"""MOTD integration tests for UPS battery monitor.

Tests for MOTD module scripts that display UPS status, capacity estimates,
and new battery detection alerts.
"""

import subprocess
import json
import tempfile
from pathlib import Path
import pytest
import os
import math


@pytest.fixture
def temp_model_json(tmp_path):
    """Create a temporary model.json for MOTD testing in proper config directory."""
    config_dir = tmp_path / '.config' / 'ups-battery-monitor'
    config_dir.mkdir(parents=True)
    model_path = config_dir / 'model.json'
    return tmp_path, model_path


@pytest.fixture
def model_json_with_capacity(tmp_path):
    """Create temporary model.json with capacity_estimates in tmpdir config."""
    config_dir = tmp_path / ".config" / "ups-battery-monitor"
    config_dir.mkdir(parents=True)
    model_file = config_dir / "model.json"
    return tmp_path, model_file


def test_motd_capacity_displays(model_json_with_capacity):
    """MOTD integration: displays measured capacity, rated capacity, sample count, and confidence."""

    tmp_path, model_file = model_json_with_capacity

    # Setup: Create temporary model.json with 3 capacity_estimates entries
    model_data = {
        "full_capacity_ah_ref": 7.2,
        "capacity_estimates": [
            {
                "timestamp": "2026-03-16T10:00:00Z",
                "ah_estimate": 6.9,
                "confidence": 0.88,
                "delta_soc_percent": 52.3,
                "duration_sec": 2850
            },
            {
                "timestamp": "2026-03-16T11:00:00Z",
                "ah_estimate": 7.0,
                "confidence": 0.90,
                "delta_soc_percent": 55.8,
                "duration_sec": 3120
            },
            {
                "timestamp": "2026-03-16T12:00:00Z",
                "ah_estimate": 6.95,
                "confidence": 0.92,
                "delta_soc_percent": 53.5,
                "duration_sec": 2990
            }
        ],
        "soh_history": [],
        "lut": []
    }

    with open(model_file, 'w') as f:
        json.dump(model_data, f)

    # Execute: Run 51-ups.sh as subprocess with HOME env pointing to temp directory
    motd_script = Path('scripts/motd/51-ups.sh')

    result = subprocess.run(
        ['bash', str(motd_script)],
        env={'HOME': str(tmp_path)},
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=5
    )

    # Assert: Exit code is 0 (no crash)
    assert result.returncode == 0, f"MOTD script failed: {result.stderr}"

    # Assert: Output contains required elements
    assert "Capacity:" in result.stdout, f"'Capacity:' not found in output: {result.stdout}"
    assert "6.95Ah" in result.stdout or "6.95" in result.stdout, f"Measured capacity not found in output: {result.stdout}"
    assert "7.2Ah" in result.stdout or "7.2" in result.stdout, f"Rated capacity not found in output: {result.stdout}"
    assert "3/3 samples" in result.stdout or "3/3" in result.stdout, f"Sample count format not found in output: {result.stdout}"

    # Assert: Status badge is present (LOCKED expected since CoV should be < 0.10)
    assert "LOCKED" in result.stdout or "MEASURING" in result.stdout or "UNKNOWN" in result.stdout, \
           f"Status badge not found in output: {result.stdout}"

    # Assert: Confidence percentage is displayed (should be 92% for this data)
    assert "%" in result.stdout, f"Confidence percentage not found in output: {result.stdout}"


def test_motd_handles_empty_estimates(model_json_with_capacity):
    """MOTD integration: gracefully handles missing or empty capacity_estimates array."""

    tmp_path, model_file = model_json_with_capacity

    # Setup: Create temporary model.json with empty capacity_estimates array
    model_data = {
        "full_capacity_ah_ref": 7.2,
        "capacity_estimates": [],
        "soh_history": [],
        "lut": []
    }

    with open(model_file, 'w') as f:
        json.dump(model_data, f)

    # Execute: Run 51-ups.sh with HOME override
    motd_script = Path('scripts/motd/51-ups.sh')

    result = subprocess.run(
        ['bash', str(motd_script)],
        env={'HOME': str(tmp_path)},
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=5
    )

    # Assert: Exit code is 0 (no crash on graceful fallback)
    assert result.returncode == 0, f"MOTD script should not crash on empty estimates: {result.stderr}"

    # Assert: No error messages in output
    assert "error" not in result.stdout.lower() and "error" not in result.stderr.lower(), \
           f"Unexpected error output: stdout={result.stdout}, stderr={result.stderr}"

    # Assert: Output is empty or contains no capacity line (graceful degradation)
    # The script should exit 0 and output nothing when capacity_estimates is empty
    assert "Capacity:" not in result.stdout, \
           f"Capacity line should not appear with empty estimates, but got: {result.stdout}"

    # Test with completely missing model.json
    # Remove the config directory
    import shutil
    config_dir = tmp_path / ".config"
    if config_dir.exists():
        shutil.rmtree(config_dir)

    result = subprocess.run(
        ['bash', str(motd_script)],
        env={'HOME': str(tmp_path)},
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=5
    )

    # Assert: Exit code still 0 when model.json missing
    assert result.returncode == 0, f"MOTD script should gracefully handle missing model.json: {result.stderr}"
    assert "Capacity:" not in result.stdout, f"No capacity line should appear when model.json missing"


def test_motd_convergence_status_badge(model_json_with_capacity):
    """MOTD integration: convergence status badge changes from MEASURING to LOCKED as samples increase."""

    tmp_path, model_file = model_json_with_capacity

    motd_script = Path('scripts/motd/51-ups.sh')

    # Setup: Create model.json with 2 estimates (count < 3, state = "measuring")
    model_data = {
        "full_capacity_ah_ref": 7.2,
        "capacity_estimates": [
            {
                "timestamp": "2026-03-16T10:00:00Z",
                "ah_estimate": 6.8,
                "confidence": 0.45,
                "delta_soc_percent": 50.0,
                "duration_sec": 2800
            },
            {
                "timestamp": "2026-03-16T11:00:00Z",
                "ah_estimate": 7.1,
                "confidence": 0.50,
                "delta_soc_percent": 55.0,
                "duration_sec": 3100
            }
        ],
        "soh_history": [],
        "lut": []
    }

    with open(model_file, 'w') as f:
        json.dump(model_data, f)

    # Execute: Run 51-ups.sh with HOME override
    result = subprocess.run(
        ['bash', str(motd_script)],
        env={'HOME': str(tmp_path)},
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=5
    )

    # Assert: Exit code is 0
    assert result.returncode == 0, f"MOTD script failed: {result.stderr}"

    # Assert: Output contains "2/3 samples" (count format correct)
    assert "2/3 samples" in result.stdout or "2/3" in result.stdout, \
           f"Sample count 2/3 not found in output: {result.stdout}"

    # Assert: With 2 samples, status badge should be MEASURING (not locked)
    # Note: output might contain color codes, so check for plain text
    output_clean = result.stdout.replace('\033[0;32m', '').replace('\033[1;33m', '').replace('\033[2m', '').replace('\033[0m', '')
    assert "MEASURING" in output_clean, \
           f"MEASURING badge not found with 2 samples in output: {result.stdout}"
    assert "LOCKED" not in output_clean, \
           f"LOCKED badge should not appear with 2 samples in output: {result.stdout}"

    # Setup: Add 3rd estimate with low variance (should trigger LOCKED status)
    model_data["capacity_estimates"].append({
        "timestamp": "2026-03-16T12:00:00Z",
        "ah_estimate": 6.95,
        "confidence": 0.92,
        "delta_soc_percent": 53.5,
        "duration_sec": 2990
    })

    with open(model_file, 'w') as f:
        json.dump(model_data, f)

    # Execute: Run 51-ups.sh again with 3 samples
    result = subprocess.run(
        ['bash', str(motd_script)],
        env={'HOME': str(tmp_path)},
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=5
    )

    # Assert: Exit code is 0
    assert result.returncode == 0, f"MOTD script failed on 3 samples: {result.stderr}"

    # Assert: Output contains "3/3 samples"
    assert "3/3 samples" in result.stdout or "3/3" in result.stdout, \
           f"Sample count 3/3 not found in output: {result.stdout}"

    # Assert: With 3 samples and low variance, status badge should be LOCKED
    output_clean = result.stdout.replace('\033[0;32m', '').replace('\033[1;33m', '').replace('\033[2m', '').replace('\033[0m', '')
    assert "LOCKED" in output_clean, \
           f"LOCKED badge not found with 3 samples and low CoV in output: {result.stdout}"


def test_motd_shows_new_battery_alert(temp_model_json):
    """MOTD integration: new_battery_detected flag triggers alert display."""

    tmp_path, model_file = temp_model_json

    # Setup: model.json with new_battery_detected flag set and at least one capacity estimate
    model_data = {
        'capacity_estimates': [
            {
                'timestamp': '2026-03-16T09:30:00Z',
                'ah_estimate': 6.9,
                'confidence': 0.88,
                'delta_soc_percent': 52.3,
                'duration_sec': 2850
            }
        ],
        'new_battery_detected': True,
        'new_battery_detected_timestamp': '2026-03-16T10:30:00',
        'soh_history': [],
        'lut': [],
    }

    with open(model_file, 'w') as f:
        json.dump(model_data, f)

    # Run MOTD module (scripts/motd/51-ups.sh)
    motd_script = Path('scripts/motd/51-ups.sh')

    result = subprocess.run(
        ['bash', str(motd_script)],
        env={'HOME': str(tmp_path)},
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=5
    )

    # Verify: Exit code is 0
    assert result.returncode == 0, f"MOTD script failed: {result.stderr}"

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

    with open(model_file, 'w') as f:
        json.dump(model_data, f)

    result = subprocess.run(
        ['bash', str(motd_script)],
        env={'HOME': str(tmp_path)},
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=5
    )

    # Verify: Exit code is 0
    assert result.returncode == 0, f"MOTD script failed: {result.stderr}"

    # Verify: MOTD output does NOT contain alert when flag is false
    assert '⚠️  Possible new battery detected' not in result.stdout or \
           'ups-battery-monitor --new-battery' not in result.stdout, \
           f"Alert should not be shown when flag=false: {result.stdout}"
