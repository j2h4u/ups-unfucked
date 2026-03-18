"""Pytest configuration and shared fixtures for UPS Battery Monitor tests."""

import tempfile
import socket
from unittest.mock import Mock, patch
import pytest


@pytest.fixture
def mock_socket_ok():
    """
    Fixture that returns valid NUT protocol response string.

    Standard TCP response format (text-based key-value), simulating successful
    communication with NUT upsd daemon. Includes responses for common UPS variables:
    battery.voltage, ups.load, ups.status, input.voltage.
    """
    # NUT protocol response strings for common variables
    response_data = {
        'battery.voltage': 'VAR cyberpower battery.voltage 13.4\n',
        'ups.load': 'VAR cyberpower ups.load 16\n',
        'ups.status': 'VAR cyberpower ups.status OL\n',
        'input.voltage': 'VAR cyberpower input.voltage 230\n',
    }

    # Create a mock socket that returns valid responses
    mock_sock = Mock(spec=socket.socket)

    def mock_recv(bufsize):
        # Return a complete response for a typical NUT protocol query
        # Format: "VAR <ups-name> <variable> <value>\n"
        return b'VAR cyberpower battery.voltage 13.4\n'

    mock_sock.recv = Mock(side_effect=mock_recv)
    mock_sock.sendall = Mock(return_value=None)
    mock_sock.connect = Mock(return_value=None)
    mock_sock.close = Mock(return_value=None)

    return mock_sock


@pytest.fixture
def mock_socket_timeout():
    """
    Fixture that simulates socket.timeout exception on recv().

    Used to test daemon behavior when NUT upsd becomes unresponsive or network
    timeout occurs. Enables verification of graceful error handling without hanging.
    """
    mock_sock = Mock(spec=socket.socket)

    # Socket recv() will raise timeout exception
    mock_sock.recv = Mock(side_effect=socket.timeout("Connection timed out"))
    mock_sock.sendall = Mock(return_value=None)
    mock_sock.connect = Mock(return_value=None)
    mock_sock.close = Mock(return_value=None)

    return mock_sock


@pytest.fixture
def mock_socket_list_var():
    """
    Fixture returning mock socket with proper LIST VAR multi-line response.

    Real NUT upsd format for LIST VAR command returns multi-line response:
    VAR cyberpower battery.voltage "13.4"
    VAR cyberpower battery.charge "85"
    VAR cyberpower ups.status "OL"
    VAR cyberpower ups.load "25"
    VAR cyberpower input.voltage "230"
    END LIST VAR cyberpower

    This fixture ensures get_ups_vars() parsing works correctly.
    """
    response = b"""VAR cyberpower battery.voltage "13.4"
VAR cyberpower battery.charge "85"
VAR cyberpower ups.status "OL"
VAR cyberpower ups.load "25"
VAR cyberpower input.voltage "230"
END LIST VAR cyberpower
"""
    mock_sock = Mock(spec=socket.socket)

    def mock_recv_impl(bufsize):
        return response

    mock_sock.recv = Mock(side_effect=mock_recv_impl)
    mock_sock.sendall = Mock(return_value=None)
    mock_sock.connect = Mock(return_value=None)
    mock_sock.close = Mock(return_value=None)

    return mock_sock


@pytest.fixture
def temporary_model_path():
    """
    Pytest fixture that yields temporary file path for model.json.

    Creates a temporary file with suffix='.json' that can be used for testing
    model persistence. File is automatically cleaned up after test completes.

    Yields:
        str: Path to temporary JSON file
    """
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tmp:
        tmp_path = tmp.name

    yield tmp_path

    # Cleanup: remove temporary file
    import os
    try:
        os.unlink(tmp_path)
    except OSError:
        pass  # File might already be deleted by test


@pytest.fixture
def nut_protocol_samples():
    """Real NUT protocol response samples for parsing tests."""
    return {
        'voltage': 'VAR cyberpower battery.voltage 13.4',
        'load': 'VAR cyberpower ups.load 25',
        'status': 'VAR cyberpower ups.status OL',
        'runtime': 'VAR cyberpower battery.runtime 3600',
        'charge': 'VAR cyberpower battery.charge 100',
        'input_voltage': 'VAR cyberpower input.voltage 230',
    }


@pytest.fixture
def mock_lut_standard():
    """
    Standard VRLA LUT from research (02-RESEARCH.md).

    Returns a list of dictionaries with voltage, SoC, and source tracking.
    Used for SoC prediction tests and interpolation validation.
    """
    return [
        {"v": 13.4, "soc": 1.0, "source": "standard"},
        {"v": 12.8, "soc": 0.9, "source": "standard"},
        {"v": 12.4, "soc": 0.64, "source": "standard"},  # Knee point
        {"v": 12.0, "soc": 0.4, "source": "standard"},
        {"v": 11.5, "soc": 0.2, "source": "standard"},
        {"v": 10.5, "soc": 0.0, "source": "anchor"},
    ]


@pytest.fixture
def mock_lut_measured():
    """
    Measured VRLA LUT for test scenarios.

    Subset of standard LUT with some measured points included.
    Used to test LUT flexibility and measured point handling.
    """
    return [
        {"v": 13.4, "soc": 1.0, "source": "measured"},
        {"v": 12.4, "soc": 0.63, "source": "measured"},
        {"v": 11.0, "soc": 0.15, "source": "measured"},
        {"v": 10.5, "soc": 0.0, "source": "anchor"},
    ]


@pytest.fixture
def sample_model_data(mock_lut_standard):
    """
    Complete battery model dict with standard LUT and metadata.

    Provides realistic model data for integration tests. Includes capacity,
    state of health, LUT, and SoH history tracking.
    """
    return {
        "capacity_ah": 7.2,
        "soh": 1.0,
        "lut": mock_lut_standard,
        "soh_history": [
            {"date": "2026-03-12", "soh": 1.0}
        ]
    }


@pytest.fixture
def current_metrics_fixture():
    """
    Fixture returning a CurrentMetrics dataclass instance with default test values.

    Provides reusable metric state across test suite. Tests will import CurrentMetrics
    from src.monitor and use this fixture to get populated instances.

    Returns:
        CurrentMetrics: Current metrics with typed field values for SoC, charge, runtime, event state.
    """
    from src.monitor_config import CurrentMetrics
    from src.event_classifier import EventType
    from datetime import datetime, timezone

    return CurrentMetrics(
        soc=0.75,
        battery_charge=75.0,
        time_rem_minutes=30.0,
        event_type=EventType.ONLINE,
        transition_occurred=False,
        shutdown_imminent=False,
        ups_status_override=None,
        previous_event_type=EventType.ONLINE,
        timestamp=datetime(2026, 3, 12, tzinfo=timezone.utc),
    )


@pytest.fixture
def config_fixture(tmp_path):
    """
    Fixture returning a Config dataclass instance with typical test values.

    Provides reusable configuration across test suite. Tests will import Config
    from src.monitor and use this fixture to get populated instances.

    Args:
        tmp_path: pytest's temporary directory fixture for test isolation.

    Returns:
        Config: Configuration dataclass with UPS name, intervals, hosts, paths, thresholds, model parameters.
    """
    from src.monitor_config import Config
    from pathlib import Path

    return Config(
        ups_name="test-cyberpower",
        polling_interval=10,
        reporting_interval=60,
        nut_host="localhost",
        nut_port=3493,
        nut_timeout=2.0,
        shutdown_minutes=5,
        soh_alert_threshold=0.80,
        model_dir=tmp_path / "test_model",
        runtime_threshold_minutes=20,
        reference_load_percent=20.0,
        ema_window_sec=120,
        capacity_ah=7.2,
    )


@pytest.fixture
def synthetic_discharge_fixture(mock_lut_standard):
    """
    Synthetic discharge data for capacity estimator testing.

    Represents a 100-point discharge event over ~990 seconds:
    - Voltage: drops from 13.2V to 10.5V (50% ΔSoC)
    - Current: constant 35A (load ~27%)
    - Duration: 990 seconds (~16.5 minutes)
    - Expected capacity: ~5.8Ah via coulomb counting

    Returns:
        tuple: (voltage_series, time_series, current_series, lut)
    """
    # 100 time points over 990 seconds (every ~10 seconds)
    time_series = [float(i * 10) for i in range(100)]

    # Voltage drops linearly from 13.2V to 10.5V (50% ΔSoC)
    voltage_series = [13.2 - (i * 0.027) for i in range(100)]

    # Constant load: 35A at 12V = (35 * 12 / 425) * 100 ≈ 99% load
    # For realistic testing, use 30% load: (30 * 425 / 100 / 12) ≈ 10.6A
    current_percent_series = [30.0] * 100

    return voltage_series, time_series, current_percent_series, mock_lut_standard


@pytest.fixture
def synthetic_discharge_47min_fixture():
    """
    Synthetic discharge data modeled after a 47-minute blackout scenario.

    Synthetic but realistic parameters for a CyberPower UT850EG discharge:
    - Duration: ~2820 seconds (47 minutes)
    - Voltage drop: 13.2V → 10.5V (50% ΔSoC)
    - Load: ~26% average (normalized to UPS rating)
    - Expected capacity: ~7.2Ah

    Returns:
        tuple: (voltage_series, time_series, current_series, lut)
    """
    # Simulated real discharge over 2820 seconds (47 minutes)
    # Sample every 10 seconds, so 282 samples
    num_samples = 282
    time_series = [float(i * 10) for i in range(num_samples)]

    # Realistic voltage curve with slight variations (not perfectly linear)
    voltage_series = []
    for i in range(num_samples):
        progress = i / num_samples  # 0 to 1
        # Voltage drop with slight non-linearity
        v = 13.2 - (progress * 2.7) - (0.1 * (progress ** 2))
        voltage_series.append(v)

    # Variable load: ~26% average with some realistic variations
    # For 7.2Ah over 2800s: I_avg = 7.2 * 3600 / 2800 ≈ 9.26A
    # In load percent: (9.26A * 12V / 425W) * 100 ≈ 26%
    # Add variation: ±3% to simulate server load fluctuations
    current_percent_series = []
    for i in range(num_samples):
        # Base load ~26% with ±3% variation
        base = 26.0
        variation = 3.0 * (0.5 + 0.5 * (i % 10) / 10)  # Sinusoidal-ish variation
        current_percent_series.append(base + variation)

    lut = [
        {"v": 13.4, "soc": 1.0, "source": "standard"},
        {"v": 12.8, "soc": 0.9, "source": "standard"},
        {"v": 12.4, "soc": 0.64, "source": "standard"},
        {"v": 12.0, "soc": 0.4, "source": "standard"},
        {"v": 11.5, "soc": 0.2, "source": "standard"},
        {"v": 10.5, "soc": 0.0, "source": "anchor"},
    ]

    return voltage_series, time_series, current_percent_series, lut
