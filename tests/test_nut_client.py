"""Tests for NUT client socket communication and error handling.

This module contains tests for DATA-01 requirement:
- Socket communication and continuous polling
- Timeout handling and recovery
- Connection error handling
- LIST VAR single-connection optimization
"""

import socket
import pytest
from unittest.mock import patch, MagicMock
from src.nut_client import NUTClient


LIST_VAR_RESPONSE = (
    'BEGIN LIST VAR cyberpower\n'
    'VAR cyberpower battery.voltage "13.40"\n'
    'VAR cyberpower ups.load "16"\n'
    'VAR cyberpower ups.status "OL"\n'
    'VAR cyberpower input.voltage "222.0"\n'
    'VAR cyberpower battery.charge "100"\n'
    'VAR cyberpower battery.runtime "1500"\n'
    'END LIST VAR cyberpower\n'
)


@pytest.fixture
def mock_nut_socket():
    """Provide a mocked NUT socket for all tests."""
    with patch('src.nut_client.socket.socket') as mock_socket_class:
        mock_sock = MagicMock()
        mock_socket_class.return_value = mock_sock
        yield mock_sock


class TestNUTClientCommunication:
    """Test suite for NUT socket communication (DATA-01)."""

    def test_continuous_polling(self, mock_nut_socket):
        """100 consecutive polls without dropped samples."""
        mock_nut_socket.recv.return_value = LIST_VAR_RESPONSE.encode()

        client = NUTClient()
        responses = [client.get_ups_vars() for _ in range(100)]

        assert len(responses) == 100
        assert all('battery.voltage' in r for r in responses)

    def test_socket_timeout(self, mock_nut_socket):
        """Socket timeout prevents hanging on NUT upsd crash."""
        mock_nut_socket.recv.side_effect = socket.timeout("Connection timed out")

        client = NUTClient(timeout=2.0)
        with pytest.raises(socket.timeout):
            client.get_ups_var('battery.voltage')

    def test_connection_refused(self, mock_nut_socket):
        """Socket errors are raised, not silently ignored."""
        mock_nut_socket.connect.side_effect = socket.error("Connection refused")

        client = NUTClient()
        with pytest.raises(socket.error):
            client.get_ups_var('battery.voltage')


class TestListVar:
    """Tests for LIST VAR single-connection optimization."""

    def test_list_var_single_connection(self, mock_nut_socket):
        """Only 1 socket.connect() per get_ups_vars()."""
        mock_nut_socket.recv.return_value = LIST_VAR_RESPONSE.encode()

        client = NUTClient()
        client.get_ups_vars()

        mock_nut_socket.connect.assert_called_once()

    def test_list_var_parsing(self, mock_nut_socket):
        """Full LIST VAR response parsed into correct dict with all 6 vars."""
        mock_nut_socket.recv.return_value = LIST_VAR_RESPONSE.encode()

        client = NUTClient()
        result = client.get_ups_vars()

        assert result['battery.voltage'] == 13.40
        assert result['ups.load'] == 16.0
        assert result['ups.status'] == 'OL'
        assert result['input.voltage'] == 222.0
        assert result['battery.charge'] == 100.0
        assert result['battery.runtime'] == 1500.0
        assert len(result) == 6

    def test_list_var_string_values(self, mock_nut_socket):
        """ups.status 'OL' parsed as string, not float."""
        mock_nut_socket.recv.return_value = LIST_VAR_RESPONSE.encode()

        client = NUTClient()
        result = client.get_ups_vars()

        assert isinstance(result['ups.status'], str)

    def test_list_var_timeout(self, mock_nut_socket):
        """socket.timeout raised correctly from LIST VAR."""
        mock_nut_socket.recv.side_effect = socket.timeout("timed out")

        client = NUTClient()
        with pytest.raises(socket.timeout):
            client.get_ups_vars()

    def test_recv_until_multi_chunk(self, mock_nut_socket):
        """Response split across multiple recv calls assembled correctly."""
        full = LIST_VAR_RESPONSE.encode()
        mid = len(full) // 2
        mock_nut_socket.recv.side_effect = [full[:mid], full[mid:]]

        client = NUTClient()
        result = client.get_ups_vars()

        assert len(result) == 6
        assert result['battery.voltage'] == 13.40

    def test_socket_cleanup_on_success(self, mock_nut_socket):
        """Socket is closed after successful LIST VAR."""
        mock_nut_socket.recv.return_value = LIST_VAR_RESPONSE.encode()

        client = NUTClient()
        client.get_ups_vars()

        mock_nut_socket.close.assert_called_once()

    def test_socket_cleanup_on_error(self, mock_nut_socket):
        """Socket is closed even when recv fails."""
        mock_nut_socket.recv.side_effect = socket.timeout("timed out")

        client = NUTClient()
        with pytest.raises(socket.timeout):
            client.get_ups_vars()

        mock_nut_socket.close.assert_called_once()
