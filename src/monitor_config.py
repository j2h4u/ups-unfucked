"""UPS Battery Monitor — configuration, dataclasses, constants, and helpers.

Separates configuration/infrastructure from daemon orchestration logic.
"""

import time
import logging
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field, fields


def _get_version() -> str:
    """Derive version from git tag (e.g. 'v3.1-2-gabcdef1'). Falls back to 'unknown'."""
    try:
        return subprocess.check_output(
            ['git', 'describe', '--tags', '--always'],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return "unknown"


DAEMON_VERSION = _get_version()
from enum import Enum
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from src.model import BatteryModel, atomic_write_json
from src.event_classifier import EventType


# Precedence: config.toml > code defaults. Physics params (IR_K, etc.) live in model.json.

CONFIG_DIR = Path.home() / '.config' / 'ups-battery-monitor'
REPO_ROOT = Path(__file__).resolve().parent.parent

# Hardcoded internals (not user-facing)
POLL_INTERVAL = 10           # seconds between polls
EMA_WINDOW = 120             # EMA smoothing window (seconds)
NUT_HOST = 'localhost'
NUT_PORT = 3493
NUT_TIMEOUT = 2.0            # socket timeout (seconds)
RUNTIME_THRESHOLD_MINUTES = 20
REFERENCE_LOAD_PERCENT = 20.0
REPORTING_INTERVAL_POLLS = 6  # Log metrics every N polls
HEALTH_ENDPOINT_PATH = Path("/run/ups-battery-monitor/ups-health.json")

# User-configurable (from config.toml)
_CONFIGURABLE_DEFAULTS = {
    'ups_name': 'cyberpower',
    'shutdown_minutes': 5,
    'soh_alert': 0.80,
    'capacity_ah': 7.2,
}

# Internal constants (not user-configurable)
SAG_SAMPLES_REQUIRED = 5             # Collect 5 voltage samples, take median of last 3 for noise rejection
DISCHARGE_BUFFER_MAX_SAMPLES = 1000  # Prevents unbounded memory growth (~2.8h at default poll rate)
ERROR_LOG_BURST = 10                 # Full traceback for first N errors, then summary every REPORTING_INTERVAL_POLLS
from src.battery_math.constants import MIN_DISCHARGE_DURATION_SEC  # Re-exported for existing consumers


@dataclass
class SchedulingConfig:
    """User-configurable scheduling knobs.

    Algorithmic constants (SoH floor, rate limit, ROI threshold, sulfation
    thresholds, cycle budget, blackout credit window) live as named constants
    in their respective modules (scheduler.py constants, discharge_handler.py inline constants).
    """
    grid_stability_cooldown_hours: float = 4.0
    scheduler_eval_hour_utc: int = 8
    verbose_scheduling: bool = False

    def validate(self) -> list[str]:
        """Return list of validation errors, empty if valid."""
        errors = []
        if self.grid_stability_cooldown_hours < 0:
            errors.append("grid_stability_cooldown_hours must be ≥0 (0 disables gate)")
        if not (0 <= self.scheduler_eval_hour_utc <= 23):
            errors.append("scheduler_eval_hour_utc must be in [0, 23]")
        return errors


@dataclass(frozen=True)
class Config:
    """Immutable UPS daemon configuration.

    Created at startup, passed to MonitorDaemon.__init__.
    """
    ups_name: str
    polling_interval: int                   # seconds between NUT polls
    reporting_interval: int                 # seconds between status log lines (REPORTING_INTERVAL_POLLS * POLL_INTERVAL)
    nut_host: str
    nut_port: int
    nut_timeout: float                      # NUT socket timeout (seconds)
    shutdown_minutes: int
    soh_alert_threshold: float              # SoH fraction [0.0, 1.0] below which to alert
    model_dir: Path
    runtime_threshold_minutes: int
    reference_load_percent: float           # UPS load percentage used in Peukert/IR calculations (0-100)
    ema_window_sec: int                     # EMA smoothing window for voltage (seconds)
    capacity_ah: float                      # Rated battery capacity (Ah)
    scheduling: Optional[SchedulingConfig] = None


def load_config() -> Config:
    """Load user config from TOML, falling back to defaults for missing keys.

    Search order: CONFIG_DIR/config.toml (~/.config/ups-battery-monitor/)
    then REPO_ROOT/config.toml. First found wins.

    Returns: Config dataclass instance with all fields populated.
    Raises: ValueError if scheduling configuration is invalid.
    """
    cfg_dict = {}
    for path in [CONFIG_DIR / 'config.toml', REPO_ROOT / 'config.toml']:
        if path.is_file():
            try:
                with open(path, 'rb') as f:
                    cfg_dict = tomllib.load(f)
                break
            except OSError as e:
                logger.error(f"Cannot read config {path}: {e}")
                raise SystemExit(f"Config file error: {path}: {e}") from e
            except tomllib.TOMLDecodeError as e:
                logger.error(f"Malformed config {path}: {e}")
                raise SystemExit(f"Config parse error: {path}: {e}") from e

    config_values = {k: cfg_dict.get(k, v) for k, v in _CONFIGURABLE_DEFAULTS.items()}

    # Validate scheduling configuration
    sched_config = get_scheduling_config(cfg_dict)
    if 'scheduling' in cfg_dict:
        logger.info(
            "Scheduling configuration loaded and validated",
            extra={
                'event_type': 'config_loaded',
                'grid_stability_cooldown_hours': sched_config.grid_stability_cooldown_hours,
                'scheduler_eval_hour_utc': sched_config.scheduler_eval_hour_utc,
                'verbose_scheduling': sched_config.verbose_scheduling,
            }
        )

    return Config(
        ups_name=config_values['ups_name'],
        polling_interval=POLL_INTERVAL,
        reporting_interval=REPORTING_INTERVAL_POLLS * POLL_INTERVAL,
        nut_host=NUT_HOST,
        nut_port=NUT_PORT,
        nut_timeout=NUT_TIMEOUT,
        shutdown_minutes=config_values['shutdown_minutes'],
        soh_alert_threshold=config_values['soh_alert'],
        model_dir=CONFIG_DIR,
        runtime_threshold_minutes=RUNTIME_THRESHOLD_MINUTES,
        capacity_ah=config_values['capacity_ah'],
        reference_load_percent=REFERENCE_LOAD_PERCENT,
        ema_window_sec=EMA_WINDOW,
        scheduling=sched_config,
    )


def get_scheduling_config(config_dict: dict) -> SchedulingConfig:
    """Extract and validate scheduling configuration from TOML dict.

    Args:
        config_dict: Parsed TOML dictionary (from tomllib.load)

    Returns:
        SchedulingConfig with all parameters (defaults applied for missing keys)

    Raises:
        ValueError: If any parameter is invalid
    """
    scheduling_section = config_dict.get('scheduling', {})
    known_fields = {f.name for f in fields(SchedulingConfig)}
    unknown_keys = set(scheduling_section) - known_fields
    if unknown_keys:
        logger.warning("Unknown scheduling config keys ignored: %s", ', '.join(sorted(unknown_keys)))
    scheduling_params = {k: v for k, v in scheduling_section.items() if k in known_fields}
    sched_config = SchedulingConfig(**scheduling_params)
    errors = sched_config.validate()
    if errors:
        raise ValueError(f"Invalid scheduling configuration: {'; '.join(errors)}")
    return sched_config


@dataclass
class CurrentMetrics:
    """Current UPS battery state snapshot, updated every poll.

    Type hints enable IDE autocomplete and mypy validation.
    """
    soc: Optional[float] = None                      # State of Charge, 0-1
    battery_charge: Optional[float] = None           # NUT battery.charge, 0-100
    time_rem_minutes: Optional[float] = None         # Estimated runtime, minutes
    event_type: Optional[EventType] = None           # From EventClassifier
    transition_occurred: bool = False                # True if state changed this poll
    shutdown_imminent: bool = False                  # True if runtime < threshold
    ups_status_override: Optional[str] = None        # Computed status string
    previous_event_type: EventType = EventType.ONLINE  # Last event_type value
    timestamp: Optional[datetime] = None             # When snapshot was taken


@dataclass
class DischargeBuffer:
    """Buffer for collecting voltage/time/load samples during discharge events."""
    voltages: list = field(default_factory=list)
    times: list = field(default_factory=list)
    loads: list = field(default_factory=list)
    collecting: bool = False


class SagState(Enum):
    """Voltage sag measurement state machine: IDLE → MEASURING → COMPLETE."""
    IDLE = "idle"            # Waiting for OL→OB transition
    MEASURING = "measuring"  # Collecting samples (fast poll active)
    COMPLETE = "complete"    # Sag recorded, back to normal polling


# Logging: JournalHandler when running under systemd, stderr fallback otherwise.
# SyslogIdentifier in service file provides the prefix — no need to repeat in formatter.
logger = logging.getLogger('ups-battery-monitor')
logger.setLevel(logging.INFO)
if logger.handlers:
    logger.handlers.clear()

try:
    from systemd.journal import JournalHandler
    log_handler = JournalHandler(identifier='ups-battery-monitor')
    log_handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
    logger.addHandler(log_handler)
except (ImportError, OSError, ValueError) as e:
    log_handler = logging.StreamHandler(sys.stderr)
    log_handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
    logger.addHandler(log_handler)
    logger.info(f"JournalHandler unavailable, using stderr: {e}")


def safe_save(model: BatteryModel) -> None:
    """Save model to disk; log and swallow errors so daemon continues."""
    try:
        model.save()
    except (OSError, TypeError, ValueError) as e:
        logger.error(
            "Failed to persist model: %s", e,
            extra={'event_type': 'model_save_failed'}
        )


@dataclass
class HealthSnapshot:
    """Aggregated health state for external monitoring tools.

    Groups the 16+ metrics written to health.json every poll.
    Construct in monitor.py, pass to write_health_endpoint().
    """
    soc_percent: float = 0.0                          # [0, 100]
    is_online: bool = False
    poll_latency_ms: Optional[float] = None            # NUT TCP round-trip (ms)
    capacity_ah_measured: Optional[float] = None       # Latest single measurement (Ah)
    capacity_ah_rated: float = 7.2                     # Firmware rated capacity (Ah)
    capacity_confidence: float = 0.0                   # [0.0, 1.0] — derived from 1-CoV
    capacity_samples_count: int = 0
    capacity_converged: bool = False                   # count >= 3 AND CoV < 0.10
    sulfation_score: Optional[float] = None            # [0.0, 1.0]
    sulfation_confidence: Optional[str] = None         # 'high' | 'medium' | 'low'
    days_since_deep: Optional[float] = None            # Days since last >70% DoD discharge
    ir_trend_rate: Optional[float] = None              # Ω/day (positive = degrading)
    recovery_delta: Optional[float] = None             # SoH change this discharge [0, 1]
    cycle_roi: Optional[float] = None                  # [0.0, 1.0]
    cycle_budget_remaining: Optional[int] = None       # Estimated remaining cycles
    scheduling_reason: str = 'observing'
    next_test_timestamp: Optional[str] = None
    last_discharge_timestamp: Optional[str] = None
    consecutive_errors: int = 0


def _opt_round(v: Optional[float], n: int) -> Optional[float]:
    """Round v to n decimal places, or return None if v is None."""
    return round(v, n) if v is not None else None


def write_health_endpoint(snapshot: HealthSnapshot) -> None:
    """Write daemon health state to file for external monitoring tools.

    Updates every poll (10s) with current daemon metrics. Uses atomic_write_json
    for crash-safe writes. Refuses to write through symlinks (security guard).
    Silently swallows OSError on write failure (logs warning).

    Monitored by: Grafana Alloy, custom scripts (liveness: last_poll < 30s).
    """
    health_data = {
        "last_poll": datetime.now(timezone.utc).isoformat(),
        "last_poll_unix": int(time.time()),
        "current_soc_percent": round(snapshot.soc_percent, 1),
        "online": snapshot.is_online,
        "daemon_version": DAEMON_VERSION,
        "poll_latency_ms": _opt_round(snapshot.poll_latency_ms, 1),
        "capacity_ah_measured": _opt_round(snapshot.capacity_ah_measured, 2),
        "capacity_ah_rated": round(snapshot.capacity_ah_rated, 2),
        "capacity_confidence": round(snapshot.capacity_confidence, 3),
        "capacity_samples_count": snapshot.capacity_samples_count,
        "capacity_converged": snapshot.capacity_converged,
        "sulfation_score": _opt_round(snapshot.sulfation_score, 3),
        "sulfation_score_confidence": snapshot.sulfation_confidence,
        "days_since_deep": _opt_round(snapshot.days_since_deep, 1),
        "ir_trend_rate": _opt_round(snapshot.ir_trend_rate, 6),
        "recovery_delta": _opt_round(snapshot.recovery_delta, 3),
        "cycle_roi": _opt_round(snapshot.cycle_roi, 3),
        "cycle_budget_remaining": snapshot.cycle_budget_remaining,
        "scheduling_reason": snapshot.scheduling_reason,
        "next_test_timestamp": snapshot.next_test_timestamp,
        "last_discharge_timestamp": snapshot.last_discharge_timestamp,
        "consecutive_errors": snapshot.consecutive_errors,
    }
    health_path = HEALTH_ENDPOINT_PATH

    try:
        if health_path.is_symlink():
            raise OSError(f"{health_path} is a symlink, refusing to write")
        atomic_write_json(health_path, health_data, mode=0o644)
    except OSError as e:
        logger.error(
            "Failed to write health endpoint: %s", e,
            extra={'event_type': 'health_endpoint_write_failed'}
        )
    except (TypeError, ValueError) as e:
        logger.error(
            "Health endpoint serialization bug: %s", e, exc_info=True,
            extra={'event_type': 'health_endpoint_serialization_bug'}
        )
