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
NUT_MAX_REPLY_BYTES = 64 * 1024
NUT_MAX_LINE_BYTES = 20 * 1024


class NUTTelemetryPort(Protocol):
    """Read-only NUT contract exposed to the telemetry adapter."""

    def get_ups_vars_with_tokens(self) -> tuple[dict[str, float | str], dict[str, str]]: ...


class StrictNUTTelemetryPort(Protocol):
    """Least-authority two-map read port for capability-baseline production."""

    def get_ups_vars_with_tokens_strict(
        self,
    ) -> tuple[dict[str, float | str], dict[str, str]]: ...


class StrictNUTEvidencePort(Protocol):
    """One-method exact-evidence port for the inactive raw capture adapter."""

    def get_ups_vars_with_evidence_strict(
        self,
    ) -> tuple[dict[str, float | str], dict[str, str], dict[str, str]]: ...


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

    _MAX_RECV_BYTES = NUT_MAX_REPLY_BYTES  # NUT LIST VAR is typically ~1 KB

    def _recv_until_bytes(self, delimiter: str) -> bytes:
        """Receive one complete sentinel-delimited reply without decoding it."""
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
        if len(buf) > self._MAX_RECV_BYTES:
            raise ConnectionError(
                "LIST VAR response too large (>64KB) — possible protocol violation"
            )
        return buf

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
        return self._recv_until_bytes(delimiter).decode()

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

    def get_ups_vars_with_tokens_strict(self) -> tuple[dict[str, float | str], dict[str, str]]:
        """Fetch one complete ordinary LIST VAR reply with strict line validation.

        The established method intentionally ignores unrecognised protocol lines for
        compatibility with the daemon's safety path.  The Slice-0 producer needs a
        stronger boundary: every line in the ordinary reply must be a well-formed
        ``VAR`` line, variable names may not repeat, and the BEGIN/END envelope must
        identify this client.  This method still sends only ``LIST VAR``.
        """
        lines = self._strict_list_var_lines()
        values: dict[str, float | str] = {}
        tokens: dict[str, str] = {}
        for line in lines[1:-1]:
            var_name, value, raw_value = self._parse_strict_var_line(line)
            if var_name in values:
                raise ValueError(f"duplicate LIST VAR key: {var_name}")
            values[var_name] = value
            tokens[var_name] = raw_value
        if not values:
            raise ValueError("LIST VAR reply contains no variables")
        return values, tokens

    def get_ups_vars_with_evidence_strict(
        self,
    ) -> tuple[dict[str, float | str], dict[str, str], dict[str, str]]:
        """Fetch one reply with typed values, logical tokens, and wire lexemes.

        The third mapping retains the exact UTF-8 spelling between the outer
        NUT quotes.  This method issues exactly one read-only ``LIST VAR``
        request, independently of the two-map capability-baseline method.
        """
        lines = self._strict_list_var_lines()
        values: dict[str, float | str] = {}
        tokens: dict[str, str] = {}
        wire_lexemes: dict[str, str] = {}
        for line in lines[1:-1]:
            var_name, value, raw_value, wire = self._parse_strict_var_line_with_wire(line)
            if var_name in values:
                raise ValueError(f"duplicate LIST VAR key: {var_name}")
            values[var_name] = value
            tokens[var_name] = raw_value
            wire_lexemes[var_name] = wire
        if not values:
            raise ValueError("LIST VAR reply contains no variables")
        return values, tokens, wire_lexemes

    def _strict_list_var_lines(self) -> list[str]:
        with self._socket_session():
            assert self.sock is not None
            self.sock.sendall(f"LIST VAR {self.ups_name}\n".encode())
            try:
                raw = self._recv_until_bytes(f"END LIST VAR {self.ups_name}")
                lines = _strict_reply_lines(raw)
            except UnicodeDecodeError as exc:
                raise ValueError("LIST VAR reply is not valid UTF-8") from exc
            except ConnectionError as exc:
                if "response too large" in str(exc):
                    raise ValueError(f"LIST VAR reply exceeds {NUT_MAX_REPLY_BYTES} bytes") from exc
                raise
            expected_begin = f"BEGIN LIST VAR {self.ups_name}"
            expected_end = f"END LIST VAR {self.ups_name}"
            if not lines or lines[0] != expected_begin or lines[-1] != expected_end:
                raise ValueError("incomplete LIST VAR envelope")
            return lines

    def _parse_strict_var_line(self, line: str) -> tuple[str, float | str, str]:
        name, value, raw, _ = self._parse_strict_var_line_with_wire(line)
        return name, value, raw

    def _parse_strict_var_line_with_wire(self, line: str) -> tuple[str, float | str, str, str]:
        if len(line.encode("utf-8")) > NUT_MAX_LINE_BYTES:
            raise ValueError(f"LIST VAR line exceeds {NUT_MAX_LINE_BYTES} bytes")
        prefix = f"VAR {self.ups_name} "
        if not line.startswith(prefix):
            raise ValueError("LIST VAR reply contains a non-VAR line")
        remainder = line[len(prefix) :]
        separator_index = remainder.find(' "')
        if separator_index < 0:
            raise ValueError("malformed LIST VAR value")
        name = remainder[:separator_index]
        quoted = remainder[separator_index + 1 :]
        if not name or not _NUT_SAFE_NAME.fullmatch(name):
            raise ValueError("malformed LIST VAR key or value")
        raw_value, wire = self._unquote_strict_value(quoted)
        if raw_value is None or wire is None:
            message = (
                "malformed LIST VAR value"
                if quoted.count('"') == 1
                else "malformed LIST VAR key or value"
            )
            raise ValueError(message)
        try:
            value: float | str = float(raw_value)
        except ValueError:
            value = raw_value
        return name, value, raw_value, wire

    @staticmethod
    def _unquote_strict_value(quoted: str) -> tuple[str | None, str | None]:
        """Unquote one NUT value and reject trailing protocol text.

        NUT frames values in double quotes and escapes embedded quotes and
        backslashes with a backslash.  The returned token is the value after
        that framing has been removed, matching what the client exposes to
        callers.  ``None`` denotes malformed quoting.
        """
        if not quoted.startswith('"'):
            return None, None
        value: list[str] = []
        index = 1
        while index < len(quoted):
            char = quoted[index]
            if char == '"':
                if index == len(quoted) - 1:
                    return "".join(value), quoted[1:index]
                return None, None
            if char == "\\":
                index += 1
                if index >= len(quoted) or quoted[index] not in {'"', "\\"}:
                    return None, None
                value.append(quoted[index])
            else:
                value.append(char)
            index += 1
        return None, None


def _strict_reply_lines(raw: bytes) -> list[str]:
    """Decode complete physical lines, counting CRLF before terminator strip."""
    if not isinstance(raw, bytes):
        raise TypeError("LIST VAR reply must be bytes")
    physical_lines = raw.splitlines(keepends=True)
    if not physical_lines or b"".join(physical_lines) != raw:
        raise ValueError("incomplete LIST VAR envelope")
    lines: list[str] = []
    for physical in physical_lines:
        if physical.endswith(b"\r"):
            raise ValueError("LIST VAR reply contains a bare carriage return")
        if len(physical) > NUT_MAX_LINE_BYTES:
            raise ValueError(f"LIST VAR line exceeds {NUT_MAX_LINE_BYTES} bytes")
        if not physical.endswith(b"\n"):
            raise ValueError("incomplete LIST VAR envelope")
        content = physical[:-2] if physical.endswith(b"\r\n") else physical[:-1]
        if b"\r" in content:
            raise ValueError("LIST VAR reply contains a bare carriage return")
        lines.append(content.decode("utf-8"))
    return lines
