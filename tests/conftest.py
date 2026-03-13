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
