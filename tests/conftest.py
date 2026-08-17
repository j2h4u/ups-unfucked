"""Shared physical-NUT fixtures retained after the runtime cutover."""

import socket
from unittest.mock import Mock

import pytest


@pytest.fixture
def mock_socket_timeout():
    mock_sock = Mock(spec=socket.socket)
    mock_sock.recv = Mock(side_effect=socket.timeout("Connection timed out"))
    mock_sock.sendall = Mock(return_value=None)
    mock_sock.connect = Mock(return_value=None)
    mock_sock.close = Mock(return_value=None)
    return mock_sock


@pytest.fixture
def mock_socket_list_var():
    response = b"""VAR cyberpower battery.voltage "13.4"
VAR cyberpower battery.charge "85"
VAR cyberpower ups.status "OL"
VAR cyberpower ups.load "25"
VAR cyberpower input.voltage "230"
END LIST VAR cyberpower
"""
    mock_sock = Mock(spec=socket.socket)
    mock_sock.recv = Mock(return_value=response)
    mock_sock.sendall = Mock(return_value=None)
    mock_sock.connect = Mock(return_value=None)
    mock_sock.close = Mock(return_value=None)
    return mock_sock
