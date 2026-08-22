"""Validated configuration for the safety-first monitor composition root."""

from __future__ import annotations

import logging
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

from src.battery_math.constants import RATED_CAPACITY_AH
from src.domain.safety_policy import validate_shutdown_threshold_minutes

logger = logging.getLogger("ups-battery-monitor")

CONFIG_DIR = Path.home() / ".config" / "ups-battery-monitor"
REPO_ROOT = Path(__file__).resolve().parent.parent

POLL_INTERVAL_SEC = 1
EMA_WINDOW_SEC = 120
NUT_HOST = "localhost"
NUT_PORT = 3493
NUT_TIMEOUT_SEC = 2.0


class ConfigError(ValueError):
    """The daemon configuration cannot be represented safely."""


@dataclass(frozen=True, slots=True)
class Config:
    """Complete runtime configuration with no scientific model parameters."""

    ups_name: str = "cyberpower"
    shutdown_minutes: int = 5
    capacity_ah: float = RATED_CAPACITY_AH
    model_dir: Path = CONFIG_DIR
    nut_host: str = NUT_HOST
    nut_port: int = NUT_PORT
    nut_timeout: float = NUT_TIMEOUT_SEC
    polling_interval: int = POLL_INTERVAL_SEC
    ema_window_sec: int = EMA_WINDOW_SEC

    def __post_init__(self) -> None:
        if not self.ups_name or any(character.isspace() for character in self.ups_name):
            raise ConfigError("ups_name must be a non-empty NUT identifier")
        try:
            validate_shutdown_threshold_minutes(self.shutdown_minutes)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        if isinstance(self.capacity_ah, bool) or self.capacity_ah <= 0.0:
            raise ConfigError("capacity_ah must be positive")
        if self.polling_interval != POLL_INTERVAL_SEC:
            raise ConfigError("physical polling interval is fixed at one second")
        if self.ema_window_sec <= 0:
            raise ConfigError("ema_window_sec must be positive")
        if not 1 <= self.nut_port <= 65535:
            raise ConfigError("nut_port must be between 1 and 65535")
        if (
            isinstance(self.nut_timeout, bool)
            or not isinstance(self.nut_timeout, (int, float))
            or not math.isfinite(float(self.nut_timeout))
            or self.nut_timeout <= 0.0
        ):
            raise ConfigError("nut_timeout must be positive")


def load_config(*, paths: tuple[Path, ...] | None = None) -> Config:
    """Load the first present TOML file and validate the bounded runtime schema."""
    search_paths = paths or (CONFIG_DIR / "config.toml", REPO_ROOT / "config.toml")
    raw: dict[str, object] = {}
    for path in search_paths:
        if not path.is_file():
            continue
        try:
            with path.open("rb") as stream:
                raw = tomllib.load(stream)
        except OSError as exc:
            raise ConfigError(f"cannot read configuration {path}: {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"malformed configuration {path}: {exc}") from exc
        break

    if "reference_load_percent" in raw:
        raise ConfigError(
            "reference_load_percent is model-owned and must not appear in runtime configuration"
        )

    allowed = {"ups_name", "shutdown_minutes", "capacity_ah"}
    unknown = sorted(set(raw).difference(allowed))
    if unknown:
        logger.warning("Ignoring unknown configuration keys: %s", ", ".join(unknown))

    ups_name = raw.get("ups_name", "cyberpower")
    shutdown_minutes = raw.get("shutdown_minutes", 5)
    capacity_ah = raw.get("capacity_ah", RATED_CAPACITY_AH)
    if not isinstance(ups_name, str):
        raise ConfigError("ups_name must be a string")
    if isinstance(shutdown_minutes, bool) or not isinstance(shutdown_minutes, int):
        raise ConfigError("shutdown_minutes must be a positive integer")
    if isinstance(capacity_ah, bool) or not isinstance(capacity_ah, (int, float)):
        raise ConfigError("capacity_ah must be a positive number")
    return Config(
        ups_name=ups_name,
        shutdown_minutes=shutdown_minutes,
        capacity_ah=float(capacity_ah),
        model_dir=CONFIG_DIR,
    )


def configure_logging() -> None:
    """Install one process logger without making systemd a library dependency."""
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return
    try:
        from systemd.journal import JournalHandler  # pyright: ignore[reportMissingImports]
    except ImportError:
        handler: logging.Handler = logging.StreamHandler()
    else:
        handler = JournalHandler(SYSLOG_IDENTIFIER="ups-battery-monitor")
    handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))
    logger.addHandler(handler)
