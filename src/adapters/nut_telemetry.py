"""Translate one raw NUT reply into an immutable physical observation."""

import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from src.domain.values import PhysicalObservation
from src.nut_client import NUTTelemetryPort


class NutTelemetry:
    """Physical telemetry adapter."""

    def __init__(
        self,
        client: NUTTelemetryPort,
        *,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic_clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._client = client
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._monotonic_clock_ns = monotonic_clock_ns

    def read(self) -> PhysicalObservation:
        """Read one reply from NUT."""
        values = self._client.get_ups_vars()
        return observation_from_nut_reply(
            values,
            wall_time_utc=self._wall_clock(),
            monotonic_ns=self._monotonic_clock_ns(),
        )


def observation_from_nut_reply(
    values: Mapping[str, object],
    *,
    wall_time_utc: datetime,
    monotonic_ns: int,
) -> PhysicalObservation:
    """Build a physical-only value without inserting derived fallbacks."""
    return PhysicalObservation(
        monotonic_ns=monotonic_ns,
        wall_time_utc=wall_time_utc,
        raw_status=_string_or_empty(values.get("ups.status")),
        battery_voltage_v=_finite_float(values.get("battery.voltage")),
        load_percent=_finite_float(values.get("ups.load")),
        input_voltage_v=_finite_float(values.get("input.voltage")),
        battery_pct=_finite_float(values.get("battery.charge")),
        runtime_s=_finite_float(values.get("battery.runtime")),
        output_v=_finite_float(values.get("output.voltage")),
    )


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _string_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""
