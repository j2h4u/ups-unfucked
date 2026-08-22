from datetime import datetime, timezone

import pytest

from src.adapters.nut_telemetry import NutTelemetry, observation_from_nut_reply


def test_observation_retains_voltage_token() -> None:
    observed = observation_from_nut_reply(
        {
            "ups.status": "OB DISCHRG",
            "battery.voltage": 12.3,
            "ups.load": 24.0,
        },
        {"battery.voltage": "12.30"},
        boot_id="boot-a",
        wall_time_utc=datetime(2026, 8, 16, tzinfo=timezone.utc),
        monotonic_ns=123,
    )

    assert observed.battery_voltage_raw == "12.30"
    assert observed.battery_voltage_v == pytest.approx(12.3)
    assert observed.input_voltage_v is None


def test_telemetry_freezes_boot_id_and_samples_clocks() -> None:
    class Client:
        def get_ups_vars_with_tokens(self):
            return (
                {"ups.status": "OL", "battery.voltage": 13.4, "ups.load": 10.0},
                {"battery.voltage": "13.4"},
            )

    observed = NutTelemetry(
        Client(),
        boot_id="boot-a",
        wall_clock=lambda: datetime(2026, 8, 16, tzinfo=timezone.utc),
        monotonic_clock_ns=lambda: 456,
    ).read()

    assert observed.boot_id == "boot-a"
    assert observed.monotonic_ns == 456
