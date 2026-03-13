"""
Socket-based NUT (Network UPS Tools) client for reliable UPS telemetry collection.

Implements stateless polling pattern: connect → send → receive → close on each poll
to enable automatic recovery from NUT service restarts.
"""

import socket
import logging


class NUTClient:
    """
    NUT upsd client using raw TCP socket communication.

    Features:
    - Stateless polling (reconnect on each call for automatic recovery)
    - Socket timeout prevents hanging if NUT service crashes
    - Error handling logs issues but doesn't crash
    - Returns dict with all requested variables; None for failed reads
    """

    def __init__(self, host='localhost', port=3493, timeout=2.0, ups_name='cyberpower', socket=None):
        """
        Initialize NUT client.

        Args:
            host: NUT upsd hostname or IP (default: localhost)
            port: NUT upsd port (default: 3493)
            timeout: Socket timeout in seconds (prevents hanging, default: 2.0)
            ups_name: UPS device name in NUT (typically 'cyberpower')
            socket: Optional mock socket for testing (default: None, uses stdlib socket)
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ups_name = ups_name
        self.logger = logging.getLogger(__name__)
        self.sock = None
        self._mock_socket = socket  # For testing purposes

    def connect(self):
        """Establish TCP connection to NUT upsd."""
        if self._mock_socket:
            self.sock = self._mock_socket
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect((self.host, self.port))

    def send_command(self, command):
        """
        Send command to NUT, receive response (one line).

        Args:
            command: NUT protocol command string (without newline)

        Returns:
            Response string (stripped)
        """
        self.sock.sendall((command + '\n').encode())
        response = self.sock.recv(4096).decode().strip()
        return response

    def get_ups_var(self, var_name):
        """
        Fetch single UPS variable (e.g., 'battery.voltage').

        Args:
            var_name: Variable name in NUT format (e.g., 'battery.voltage', 'ups.load')

        Returns:
            Float value if successful, None if parsing failed

        Raises:
            socket.timeout: If connection times out
            socket.error: If socket communication fails
        """
        try:
            self.connect()
            response = self.send_command(f'GET VAR {self.ups_name} {var_name}')

            # Parse response: "VAR cyberpower battery.voltage 13.4"
            parts = response.split()
            if len(parts) >= 3 and parts[0] == 'VAR':
                return float(parts[-1])
            else:
                self.logger.error(f"Unexpected NUT response: {response}")
                return None
        except socket.timeout:
            self.logger.error(f"Socket timeout reading {var_name}")
            raise
        except socket.error as e:
            self.logger.error(f"Socket error reading {var_name}: {e}")
            raise
        except ValueError as e:
            self.logger.error(f"Failed to parse {var_name} as float: {response}")
            return None
        finally:
            try:
                if self.sock:
                    self.sock.close()
            except Exception:
                pass

    def get_ups_vars(self):
        """
        Fetch all relevant UPS variables as dict.

        Returns:
            Dict {var_name: float_value or None} for all requested variables

        Raises:
            socket.timeout: If socket communication times out (caller should retry)
            socket.error: If socket communication fails (caller should retry)
        """
        result = {}
        vars_to_fetch = [
            'battery.voltage',
            'ups.load',
            'ups.status',
            'input.voltage',
            'battery.charge',
            'battery.runtime',
        ]

        for var_name in vars_to_fetch:
            try:
                value = self.get_ups_var(var_name)
                result[var_name] = value
            except (socket.timeout, socket.error):
                # Re-raise to allow daemon-level retry logic
                raise
            except Exception:
                # Log other exceptions but continue (e.g., ValueError from parsing)
                result[var_name] = None

        return result
