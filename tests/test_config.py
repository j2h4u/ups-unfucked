"""Strict runtime configuration invariants."""

import pytest

from src.application.publication_freshness import telemetry_loss_grace_s
from src.monitor_config import Config, ConfigError, load_config


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


def test_load_config_uses_defaults_when_no_candidate_exists(tmp_path) -> None:
    config = load_config(paths=(tmp_path / "missing.toml",))

    assert config.ups_name == "cyberpower"
    assert config.shutdown_minutes == 5


def test_load_config_rejects_malformed_toml(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[broken", encoding="utf-8")

    with pytest.raises(ConfigError, match="malformed configuration"):
        load_config(paths=(path,))


@pytest.mark.parametrize("shutdown_minutes", (1, 2))
def test_load_config_rejects_shutdown_at_or_below_safety_floor(
    tmp_path,
    shutdown_minutes: int,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"shutdown_minutes = {shutdown_minutes}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="greater than the 2-minute safety floor"):
        load_config(paths=(path,))


@pytest.mark.parametrize(
    ("contents", "error_fragment"),
    (
        ("ups_name = 7\n", "ups_name must be a string"),
        ('shutdown_minutes = "5"\n', "shutdown_minutes must be a positive integer"),
        ("capacity_ah = true\n", "capacity_ah must be a positive number"),
    ),
)
def test_load_config_rejects_unsafe_toml_types(
    tmp_path,
    contents: str,
    error_fragment: str,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(ConfigError, match=error_fragment):
        load_config(paths=(path,))


def test_load_config_accepts_known_values_and_warns_for_unknown(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'ups_name = "cyberpower"\nshutdown_minutes = 7\ncapacity_ah = 9.5\nextra = true\n',
        encoding="utf-8",
    )

    config = load_config(paths=(path,))

    assert config.shutdown_minutes == 7
    assert config.capacity_ah == 9.5
    assert "extra" in caplog.text
