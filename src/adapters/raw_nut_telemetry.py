"""Inactive raw-preserving NUT acquisition for the future v3 capture lane."""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Callable

from src.adapters.nut_telemetry import (
    BOOT_ID_PATH,
    observation_from_nut_reply,
)
from src.domain.blackout_capture import CapturedTelemetry, RawNutToken
from src.nut_client import StrictNUTEvidencePort


class RawNUTTelemetryError(ValueError):
    """A complete raw NUT capture could not be constructed."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(f"{reason}: {message}")


class RawNutTelemetry:
    """Acquire one strict NUT reply as typed observation plus raw tokens.

    This adapter is intentionally not composed into the active v2 runtime.
    One call to the strict port performs one ``LIST VAR`` query; no fallback
    read is attempted when the reply is incomplete or invalid.
    """

    def __init__(
        self,
        client: StrictNUTEvidencePort,
        *,
        boot_id: str | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic_clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._client = client
        self._boot_id = boot_id if boot_id is not None else self._read_boot_id()
        self._wall_clock = wall_clock or self._wall_time_utc
        self._monotonic_clock_ns = monotonic_clock_ns

    def read(self) -> CapturedTelemetry:
        """Read exactly one complete reply and retain every raw NUT token."""
        try:
            values, tokens, wire_lexemes = _read_strict_evidence(self._client)
            _validate_reply_mappings(values, tokens, wire_lexemes)
            observation = observation_from_nut_reply(
                values,
                tokens,
                boot_id=self._boot_id,
                wall_time_utc=self._wall_clock(),
                monotonic_ns=self._monotonic_clock_ns(),
            )
            raw_tokens = tuple(
                RawNutToken(key, tokens[key], wire_lexemes[key]) for key in sorted(tokens)
            )
        except RawNUTTelemetryError:
            raise
        except ValueError as exc:
            raise RawNUTTelemetryError("malformed_reply", str(exc)) from exc

        try:
            return CapturedTelemetry(observation, raw_tokens)
        except ValueError as exc:
            reason = "raw_map_oversize" if "16 KiB" in str(exc) else "malformed_reply"
            raise RawNUTTelemetryError(reason, str(exc)) from exc

    @staticmethod
    def _read_boot_id() -> str:
        boot_id = BOOT_ID_PATH.read_text(encoding="ascii").strip()
        if not boot_id:
            raise RuntimeError("kernel boot ID is empty")
        return boot_id

    @staticmethod
    def _wall_time_utc() -> datetime:
        return datetime.now(timezone.utc)


def _validate_reply_mappings(
    values: Mapping[str, object],
    tokens: Mapping[str, str],
    wire_lexemes: Mapping[str, str],
) -> None:
    if not all(isinstance(value, Mapping) for value in (values, tokens, wire_lexemes)):
        raise RawNUTTelemetryError("incomplete_reply", "NUT reply must contain mappings")
    if not values or set(values) != set(tokens) or set(values) != set(wire_lexemes):
        raise RawNUTTelemetryError("incomplete_reply", "NUT value/token coverage is incomplete")
    if any(
        not isinstance(key, str) or not isinstance(token, str) or not isinstance(wire, str)
        for key, token in tokens.items()
        for wire in (wire_lexemes.get(key),)
    ):
        raise RawNUTTelemetryError("malformed_reply", "NUT reply contains a non-text token")


def _read_strict_evidence(
    client: StrictNUTEvidencePort,
) -> tuple[Mapping[str, object], Mapping[str, str], Mapping[str, str]]:
    """Use the one-query exact API; no logical-only fallback may become evidence."""
    reply = client.get_ups_vars_with_evidence_strict()
    if not isinstance(reply, tuple) or len(reply) != 3:
        raise RawNUTTelemetryError("malformed_reply", "strict evidence shape is invalid")
    return reply


__all__ = ["RawNUTTelemetryError", "RawNutTelemetry"]
