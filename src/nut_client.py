"""
Socket-based NUT (Network UPS Tools) client for reliable UPS telemetry collection.

Implements stateless polling pattern: connect → send → receive → close on each poll
to enable automatic recovery from NUT service restarts.
"""

import socket
import logging
import time
from contextlib import contextmanager
from typing import Tuple, Optional


logger = logging.getLogger('ups-battery-monitor')


class NUTClient:
    """
    NUT upsd client using raw TCP socket communication.

    Features:
    - Stateless polling (reconnect on each call for automatic recovery)
    - Socket timeout prevents hanging if NUT service crashes
    - Error handling logs issues but doesn't crash
    - Returns dict with all requested variables; None for failed reads
    """

    def __init__(self, host='localhost', port=3493, timeout=2.0, ups_name='cyberpower'):
        """
        Initialize NUT client.

        Args:
            host: NUT upsd hostname or IP (default: localhost)
            port: NUT upsd port (default: 3493)
            timeout: Socket timeout in seconds (prevents hanging, default: 2.0)
            ups_name: UPS device name in NUT (typically 'cyberpower')
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ups_name = ups_name
        self.sock = None

    def _close_socket(self):
        """Close socket, swallowing errors."""
        try:
            if self.sock:
                self.sock.close()
        except Exception as e:
            logger.debug(f"Socket close error (ignored): {e}")

    @staticmethod
    def _parse_var_line(line):
        """
        Parse a NUT VAR response line into (var_name, value).

        Returns (var_name, float_or_str) or None if line is not a VAR line.
        """
        if not line.startswith('VAR '):
            return None
        words = line.split()
        if len(words) < 3:
            return None
        var_name = words[2]
        parts = line.split('"')
        if len(parts) < 2:
            return None
        raw_value = parts[1]
        try:
            return (var_name, float(raw_value))
        except ValueError:
            return (var_name, raw_value)

    def connect(self):
        """Establish TCP connection to NUT upsd."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.host, self.port))

    @contextmanager
    def _socket_session(self):
        """Connect, yield, close — handles cleanup on success and error."""
        self.connect()
        try:
            yield
        finally:
            self._close_socket()

    def send_command(self, command):
        """
        Send command to NUT, receive response (one line).

        Args:
            command: NUT protocol command string (without newline)

        Returns:
            Response string (stripped)
        """
        if '\n' in command:
            raise ValueError(f"NUT protocol injection: newline in command: {command!r}")
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
        with self._socket_session():
            response = self.send_command(f'GET VAR {self.ups_name} {var_name}')
            parsed = self._parse_var_line(response)
            if parsed is not None:
                return parsed[1]
            logger.error(f"Unexpected NUT response: {response}")
            return None

    _MAX_RECV_BYTES = 64 * 1024  # 64 KB — NUT LIST VAR is typically ~1 KB

    def _recv_until(self, delimiter):
        """
        Read from socket until delimiter string is found in response.

        Guards against infinite loops: socket timeout covers idle connections,
        wall-clock deadline covers slow-drip data, buffer cap covers runaway responses.

        Args:
            delimiter: String to look for (e.g., 'END LIST VAR cyberpower')

        Returns:
            Decoded response string

        Raises:
            socket.timeout: If wall-clock deadline exceeded or individual recv times out
        """
        buf = b''
        delim_bytes = delimiter.encode()
        deadline = time.monotonic() + self.timeout
        while delim_bytes not in buf:
            if time.monotonic() > deadline:
                raise socket.timeout("LIST VAR response deadline exceeded")
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > self._MAX_RECV_BYTES:
                raise socket.timeout("LIST VAR response too large")
        return buf.decode()

    def get_ups_vars(self):
        """
        Fetch all UPS variables in a single connection using LIST VAR.

        Uses NUT protocol's LIST VAR command to get all variables in one
        TCP connection instead of 6 separate connections.

        Returns:
            Dict {var_name: value} — float where possible, string otherwise

        Raises:
            socket.timeout: If socket communication times out (caller should retry)
            socket.error: If socket communication fails (caller should retry)
        """
        with self._socket_session():
            self.sock.sendall(f'LIST VAR {self.ups_name}\n'.encode())
            raw = self._recv_until(f'END LIST VAR {self.ups_name}')

            result = {}
            for line in raw.splitlines():
                parsed = self._parse_var_line(line)
                if parsed is not None:
                    result[parsed[0]] = parsed[1]
            return result

    def send_instcmd(self, cmd_name: str, cmd_param: Optional[str] = None) -> Tuple[bool, str]:
        """
        Send instant command via NUT RFC 9271 INSTCMD protocol.

        Dispatches immediate commands to the UPS via authenticated NUT protocol.
        Implements full RFC 9271 authentication handshake: USERNAME → PASSWORD → LOGIN → INSTCMD.

        Args:
            cmd_name: Command name in NUT format (e.g., 'test.battery.start.quick')
            cmd_param: Optional parameter value (not typically used for battery tests)

        Returns:
            Tuple[bool, str] with success flag and message where:
            - (True, response_text): Command accepted by upsd (e.g., 'OK' or 'OK TRACKING <id>')
            - (False, error_text): Command failed or auth failed (e.g., 'ERR CMD-NOT-SUPPORTED')

        Raises:
            socket.timeout: Connection timeout (caller should retry)
            socket.error: Socket communication failed (caller should retry)

        Protocol flow (RFC 9271):
            1. USERNAME upsmon    → OK
            2. PASSWORD           → OK  (v3.0 assumes upsd.users permits upsmon without password)
            3. LOGIN <upsname>    → OK
            4. INSTCMD <upsname> <cmd> [param] → OK or ERR
            5. LOGOUT (implicit via socket close)

        Example:
            success, msg = client.send_instcmd('test.battery.start.quick')
            if success:
                print(f"Test started: {msg}")
            else:
                print(f"Error: {msg}")
        """
        with self._socket_session():
            try:
                # Step 1: Authenticate as 'upsmon' user
                response = self.send_command('USERNAME upsmon')
                if not response.startswith('OK'):
                    return (False, f"USERNAME failed: {response}")

                # Step 2: Send password (empty for upsmon in standard upsd.users)
                response = self.send_command('PASSWORD')
                if not response.startswith('OK'):
                    return (False, f"PASSWORD failed: {response}")

                # Step 3: Login to UPS
                response = self.send_command(f'LOGIN {self.ups_name}')
                if not response.startswith('OK'):
                    return (False, f"LOGIN failed: {response}")

                # Step 4: Send the actual INSTCMD
                if cmd_param is not None:
                    cmd = f'INSTCMD {self.ups_name} {cmd_name} {cmd_param}'
                else:
                    cmd = f'INSTCMD {self.ups_name} {cmd_name}'

                response = self.send_command(cmd)

                # Step 5: Parse INSTCMD response
                if response.startswith('OK'):
                    return (True, response)
                elif response.startswith('ERR'):
                    return (False, response)
                else:
                    return (False, f"Unexpected response: {response}")

            except (socket.timeout, socket.error):
                raise  # Propagate for caller retry logic
