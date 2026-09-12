from datetime import datetime, timezone

import pytest

from src.adapters.nut_telemetry import NutTelemetry, observation_from_nut_reply


def test_observation_parses_available_values() -> None:
    observed = observation_from_nut_reply(
        {
            "ups.status": "OB DISCHRG",
            "battery.voltage": 12.3,
            "ups.load": 24.0,
        },
        wall_time_utc=datetime(2026, 8, 16, tzinfo=timezone.utc),
        monotonic_ns=123,
    )

    assert observed.battery_voltage_v == pytest.approx(12.3)
    assert observed.input_voltage_v is None


def test_telemetry_samples_clocks() -> None:
    class Client:
        def get_ups_vars(self):
            return {"ups.status": "OL", "battery.voltage": 13.4, "ups.load": 10.0}

    observed = NutTelemetry(
        Client(),
        wall_clock=lambda: datetime(2026, 8, 16, tzinfo=timezone.utc),
        monotonic_clock_ns=lambda: 456,
    ).read()

    assert observed.monotonic_ns == 456
