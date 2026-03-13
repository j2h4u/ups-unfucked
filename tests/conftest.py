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
