"""Strict runtime configuration invariants."""

from pathlib import Path

import pytest

from src.application.publication_freshness import telemetry_loss_grace_s
from src.monitor_config import Config, ConfigError


@pytest.mark.parametrize(
    ("overrides", "error_fragment"),
    (
        ({"ups_name": ""}, "non-empty NUT identifier"),
        ({"ups_name": "two names"}, "non-empty NUT identifier"),
        ({"shutdown_minutes": True}, "positive integer"),
        ({"shutdown_minutes": 0}, "positive integer"),
        ({"shutdown_minutes": 1}, "greater than the 2-minute safety floor"),
        ({"shutdown_minutes": 2}, "greater than the 2-minute safety floor"),
        ({"capacity_ah": True}, "capacity_ah must be positive"),
        ({"capacity_ah": 0.0}, "capacity_ah must be positive"),
        ({"polling_interval": 2}, "fixed at one second"),
        ({"ema_window_sec": 0}, "ema_window_sec must be positive"),
        ({"nut_port": 0}, "nut_port must be between 1 and 65535"),
        ({"nut_port": 65536}, "nut_port must be between 1 and 65535"),
        ({"nut_timeout": 0.0}, "nut_timeout must be positive"),
    ),
)
def test_config_rejects_unsafe_runtime_values(
    overrides: dict[str, object],
    error_fragment: str,
) -> None:
    with pytest.raises(ConfigError) as error:
        Config(**overrides)

    assert error_fragment in str(error.value)


def test_default_config_derives_thirty_second_telemetry_grace() -> None:
    config = Config()

    assert (
        telemetry_loss_grace_s(
            shutdown_minutes=config.shutdown_minutes,
            nut_timeout_s=config.nut_timeout,
            polling_interval_s=config.polling_interval,
        )
        == 30.0
    )


@pytest.mark.parametrize("shutdown_minutes", (1, 2))
def test_telemetry_grace_rejects_threshold_at_or_below_safety_floor(
    shutdown_minutes: int,
) -> None:
    with pytest.raises(ValueError, match="greater than"):
        telemetry_loss_grace_s(
            shutdown_minutes=shutdown_minutes,
            nut_timeout_s=2.0,
            polling_interval_s=1.0,
        )


def test_runtime_config_defaults_are_explicit_code_owned_values() -> None:
    config = Config()

    assert config.ups_name == "cyberpower"
    assert config.shutdown_minutes == 5
    assert config.capacity_ah == 7.2
    assert config.model_dir == Path.home() / ".local" / "state" / "ups-battery-monitor"
