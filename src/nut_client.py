"""
Socket-based NUT (Network UPS Tools) client for reliable UPS telemetry collection.

Implements stateless polling pattern: connect → send → receive → close on each poll
to enable automatic recovery from NUT service restarts.
"""

import logging
import re
import socket
import time
from contextlib import contextmanager
from typing import Protocol

logger = logging.getLogger("ups-battery-monitor")

_NUT_SAFE_NAME = re.compile(r"^[a-zA-Z0-9._-]+$")


class NUTTelemetryPort(Protocol):
    """Read-only NUT contract exposed to the telemetry adapter."""

    def get_ups_vars_with_tokens(self) -> tuple[dict[str, float | str], dict[str, str]]: ...


def _validate_nut_identifier(value: str, label: str) -> None:
    """Validate a NUT protocol identifier (ups_name, var_name, cmd_name).

    Raises ValueError if the identifier contains characters that could
    alter NUT protocol parsing (spaces, quotes, newlines, etc.).
    """
    if not _NUT_SAFE_NAME.match(value):
        raise ValueError(f"Invalid NUT {label}: {value!r} (must match [a-zA-Z0-9._-]+)")


class NUTClient:
    """
    NUT upsd client using raw TCP socket communication.

    Features:
    - Stateless polling (reconnect on each call for automatic recovery)
    - Socket timeout prevents hanging if NUT service crashes
    - Error handling leaves socket failures visible to the read-only caller
    - Returns parsed variables plus exact raw value tokens for provenance
    """

    def __init__(self, host="localhost", port=3493, timeout=2.0, ups_name="cyberpower"):
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
        _validate_nut_identifier(ups_name, "ups_name")
        self.sock: socket.socket | None = None

    def _close_socket(self):
        """Close socket, swallowing I/O errors."""
        try:
            if self.sock:
                self.sock.close()
        except OSError as e:
            logger.debug(f"Socket close error (ignored): {e}")

    @staticmethod
    def _parse_var_line_with_token(line):
        """Parse one VAR line while retaining the exact quoted value token."""
        if not line.startswith("VAR "):
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
            value = float(raw_value)
        except ValueError:
            value = raw_value
        return var_name, value, raw_value

    def connect(self):
        """Establish TCP connection to NUT upsd (called by _socket_session context manager)."""
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
        assert self.sock is not None
        buf = b""
        deadline = time.monotonic() + self.timeout
        while not self._contains_complete_line(buf, delimiter):
            if time.monotonic() > deadline:
                raise socket.timeout("LIST VAR response deadline exceeded")
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("LIST VAR connection closed before END sentinel")
            buf += chunk
            if len(buf) > self._MAX_RECV_BYTES:
                raise ConnectionError(
                    "LIST VAR response too large (>64KB) — possible protocol violation"
                )
        return buf.decode()

    @staticmethod
    def _contains_complete_line(payload: bytes, expected_line: str) -> bool:
        """Match the protocol sentinel as a complete terminated line only."""
        expected = expected_line.encode()
        return any(
            line.rstrip(b"\r\n") == expected and line.endswith((b"\n", b"\r"))
            for line in payload.splitlines(keepends=True)
        )

    def get_ups_vars_with_tokens(self) -> tuple[dict[str, float | str], dict[str, str]]:
        """Fetch UPS variables and retain exact NUT value tokens for provenance.

        The first mapping contains parsed values; the second mapping contains
        the unmodified text between NUT's quotes. This lets scientific capture
        derive voltage quantization without changing the values used by the
        established safety path.
        """
        with self._socket_session():
            assert self.sock is not None
            self.sock.sendall(f"LIST VAR {self.ups_name}\n".encode())
            raw = self._recv_until(f"END LIST VAR {self.ups_name}")

            values: dict[str, float | str] = {}
            tokens: dict[str, str] = {}
            for line in raw.splitlines():
                parsed = self._parse_var_line_with_token(line)
                if parsed is None:
                    continue
                var_name, value, raw_value = parsed
                values[var_name] = value
                tokens[var_name] = raw_value
            return values, tokens
