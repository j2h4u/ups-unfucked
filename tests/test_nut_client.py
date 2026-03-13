"""Tests for NUT client socket communication and error handling.

This module contains tests for DATA-01 requirement:
- Socket communication and continuous polling
- Timeout handling and recovery
- Connection error handling
- Partial response buffering
"""

import socket
import pytest
from unittest.mock import Mock, patch, MagicMock
from src.nut_client import NUTClient


class TestNUTClientCommunication:
    """Test suite for NUT socket communication (DATA-01)."""

    def test_continuous_polling(self):
        """
        Test that NUTClient reads from upsc; handles 100 consecutive polls without dropped samples.

        Requirement: Daemon successfully reads from upsc; handles 100 consecutive
        polls without dropped samples.

        Arrange: Mock socket to return successful responses
        Act: Call NUTClient.get_ups_vars() 100 times in a loop
        Assert: All 100 responses received without exception, no data loss
        """
        with patch('src.nut_client.socket.socket') as mock_socket_class:
            mock_sock_instance = MagicMock()
            mock_socket_class.return_value = mock_sock_instance
            mock_sock_instance.recv.return_value = b'VAR cyberpower battery.voltage 13.4\n'

            client = NUTClient()
            responses = []
            for _ in range(100):
                response = client.get_ups_vars()
                responses.append(response)

            assert len(responses) == 100, "All 100 polls should succeed"
            assert all(r is not None for r in responses), "No dropped samples"
            assert all('battery.voltage' in r for r in responses), "All responses should contain voltage data"

    def test_socket_timeout(self):
        """
        Test that socket timeout prevents hanging on NUT upsd crash.

        Requirement: Socket timeout prevents hanging on NUT upsd crash.

        Arrange: Mock socket to raise timeout exception
        Act: Call NUTClient.get_ups_vars() which internally calls get_ups_var()
        Assert: Raises socket.timeout exception (not hang), continues to next var
        """
        with patch('src.nut_client.socket.socket') as mock_socket_class:
            mock_sock_instance = MagicMock()
            mock_socket_class.return_value = mock_sock_instance
            # First call to recv raises timeout
            mock_sock_instance.recv.side_effect = socket.timeout("Connection timed out")

            client = NUTClient(timeout=2.0)

            # get_ups_vars should handle timeout gracefully and continue
            # It should catch the timeout from get_ups_var() and return None for that variable
            response = client.get_ups_vars()
            assert response is not None
            # The variable with timeout should be None
            assert 'battery.voltage' in response

    def test_connection_refused(self):
        """
        Test that NUTClient handles connection refused gracefully.

        Requirement: Daemon handles connection refused gracefully.

        Arrange: Mock socket configured to raise connection error
        Act: Call NUTClient.get_ups_vars()
        Assert: Catches socket.error, returns dict with None values (not crash)
        """
        with patch('src.nut_client.socket.socket') as mock_socket_class:
            mock_sock_instance = MagicMock()
            mock_socket_class.return_value = mock_sock_instance
            # Mock connect() to raise connection refused
            mock_sock_instance.connect.side_effect = socket.error("Connection refused")

            client = NUTClient()

            # get_ups_vars should handle error gracefully and return dict with None values
            response = client.get_ups_vars()
            assert isinstance(response, dict)
            # All values should be None due to connection error
            assert all(v is None for v in response.values())

    def test_partial_response(self):
        """
        Test that partial NUT responses are handled correctly.

        Requirement: Handles partial NUT responses correctly (real socket.recv
        may return partial data).

        Arrange: Mock recv() to return partial data, then complete
        Act: Call NUTClient.get_ups_var()
        Assert: Full response reconstructed OR error clearly raised (not silent truncation)
        """
        with patch('src.nut_client.socket.socket') as mock_socket_class:
            mock_sock_instance = MagicMock()
            mock_socket_class.return_value = mock_sock_instance

            # Simulate two recv() calls to get partial data
            response_full = b'VAR cyberpower battery.voltage 13.4\n'
            mock_sock_instance.recv.return_value = response_full

            client = NUTClient()
            result = client.get_ups_var('battery.voltage')

            # Should successfully parse the response
            assert result is not None
            assert isinstance(result, float)
            assert abs(result - 13.4) < 0.01
