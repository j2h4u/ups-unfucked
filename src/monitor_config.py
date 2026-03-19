"""UPS Battery Monitor — configuration, dataclasses, constants, and helpers.

Extracted from monitor.py (F58) to separate configuration/infrastructure
from daemon orchestration logic.
"""

import time
import logging
import sys
import tomllib
import importlib.metadata
from dataclasses import dataclass, field, fields
from enum import Enum
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from src.model import BatteryModel, atomic_write_json
from src.event_classifier import EventType


# === CONFIGURATION ===
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
    polling_interval: int
    reporting_interval: int
    nut_host: str
    nut_port: int
    nut_timeout: float
    shutdown_minutes: int
    soh_alert_threshold: float
    model_dir: Path
    runtime_threshold_minutes: int
    reference_load_percent: float
    ema_window_sec: int
    capacity_ah: float
    scheduling: Optional[SchedulingConfig] = None


def load_config() -> Config:
    """Load user config from TOML, falling back to defaults for missing keys.

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

    user_config = {k: cfg_dict.get(k, v) for k, v in _CONFIGURABLE_DEFAULTS.items()}

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
        ups_name=user_config['ups_name'],
        polling_interval=POLL_INTERVAL,
        reporting_interval=REPORTING_INTERVAL_POLLS * POLL_INTERVAL,
        nut_host=NUT_HOST,
        nut_port=NUT_PORT,
        nut_timeout=NUT_TIMEOUT,
        shutdown_minutes=user_config['shutdown_minutes'],
        soh_alert_threshold=user_config['soh_alert'],
        model_dir=CONFIG_DIR,
        runtime_threshold_minutes=RUNTIME_THRESHOLD_MINUTES,
        capacity_ah=user_config['capacity_ah'],
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
    scheduling_raw = config_dict.get('scheduling', {})
    known_fields = {f.name for f in fields(SchedulingConfig)}
    unknown_keys = set(scheduling_raw) - known_fields
    if unknown_keys:
        logger.warning("Unknown scheduling config keys ignored: %s", ', '.join(sorted(unknown_keys)))
    scheduling_params = {k: v for k, v in scheduling_raw.items() if k in known_fields}
    sched_config = SchedulingConfig(**scheduling_params)
    errors = sched_config.validate()
    if errors:
        raise ValueError(f"Invalid scheduling configuration: {'; '.join(errors)}")
    return sched_config


@dataclass
class CurrentMetrics:
    """Current UPS battery state snapshot, updated every poll.

    Fields correspond to the 9-key dict in monitor.py.__init__.
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
logger.handlers.clear()

try:
    from systemd.journal import JournalHandler
    handler = JournalHandler(identifier='ups-battery-monitor')
    handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
    logger.addHandler(handler)
except (ImportError, OSError, ValueError) as e:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
    logger.addHandler(handler)
    logger.info(f"JournalHandler unavailable, using stderr: {e}")


def safe_save(model: BatteryModel) -> None:
    """Save model to disk, log errors gracefully if disk full.

    PERSISTENCE MODEL: Memory is source of truth. model.json is written only on
    real events (discharge complete, battery replacement, capacity convergence,
    graceful shutdown) — NOT on every poll or sag measurement. Between events,
    the file can be safely edited externally; daemon picks up changes on restart.

    If you need to edit model.json while daemon is running:
        systemctl stop ups-battery-monitor
        # edit model.json
        systemctl start ups-battery-monitor

    Args:
        model: BatteryModel instance to persist

    Side effects:
        - Logs to logger at WARNING level if save fails
        - Does NOT raise exception; allows daemon to continue

    Raises:
        None; errors are logged only
    """
    try:
        model.save()
    except Exception as e:
        logger.warning(f"Failed to persist model (disk full?): {e}")


@dataclass
class HealthSnapshot:
    """Aggregated health state for external monitoring tools.

    Groups the 16+ metrics written to health.json every poll.
    Construct in monitor.py, pass to write_health_endpoint().
    """
    soc_percent: float = 0.0
    is_online: bool = False
    poll_latency_ms: Optional[float] = None
    capacity_ah_measured: Optional[float] = None
    capacity_ah_rated: float = 7.2
    capacity_confidence: float = 0.0
    capacity_samples_count: int = 0
    capacity_converged: bool = False
    sulfation_score: Optional[float] = None
    sulfation_confidence: str = 'high'
    days_since_deep: Optional[float] = None
    ir_trend_rate: Optional[float] = None
    recovery_delta: Optional[float] = None
    cycle_roi: Optional[float] = None
    cycle_budget_remaining: Optional[int] = None
    scheduling_reason: str = 'observing'
    next_test_timestamp: Optional[str] = None
    last_discharge_timestamp: Optional[str] = None
    natural_blackout_credit: Optional[float] = None


def write_health_endpoint(snapshot: HealthSnapshot) -> None:
    """Write daemon health state to file for external monitoring tools.

    Updates every poll (10s) with current daemon metrics. Tools like Grafana,
    check_mk, and custom scripts can read this file to track:
    - Daemon liveness (last_poll < 30s = healthy)
    - Current battery state (soc_percent, online status)
    - Poll latency for performance monitoring
    - Daemon version tracking
    - Capacity metrics (measured Ah, confidence, convergence status)

    Uses atomic_write_json for crash-safe writes.
    """
    try:
        daemon_version = importlib.metadata.version('ups-unfucked')
    except importlib.metadata.PackageNotFoundError:
        daemon_version = "unknown"

    s = snapshot
    health_data = {
        "last_poll": datetime.now(timezone.utc).isoformat(),
        "last_poll_unix": int(time.time()),
        "current_soc_percent": round(s.soc_percent, 1),
        "online": s.is_online,
        "daemon_version": daemon_version,
        "poll_latency_ms": round(s.poll_latency_ms, 1) if s.poll_latency_ms is not None else None,
        # Capacity metrics
        "capacity_ah_measured": round(s.capacity_ah_measured, 2) if s.capacity_ah_measured else None,
        "capacity_ah_rated": round(s.capacity_ah_rated, 2),
        "capacity_confidence": round(s.capacity_confidence, 3),
        "capacity_samples_count": s.capacity_samples_count,
        "capacity_converged": s.capacity_converged,
        # Sulfation metrics
        "sulfation_score": round(s.sulfation_score, 3) if s.sulfation_score is not None else None,
        "sulfation_score_confidence": s.sulfation_confidence,
        "days_since_deep": round(s.days_since_deep, 1) if s.days_since_deep is not None else None,
        "ir_trend_rate": round(s.ir_trend_rate, 6) if s.ir_trend_rate is not None else None,
        "recovery_delta": round(s.recovery_delta, 3) if s.recovery_delta is not None else None,
        # ROI metrics
        "cycle_roi": round(s.cycle_roi, 3) if s.cycle_roi is not None else None,
        "cycle_budget_remaining": s.cycle_budget_remaining,
        "scheduling_reason": s.scheduling_reason,
        "next_test_timestamp": s.next_test_timestamp,
        # Discharge metrics
        "last_discharge_timestamp": s.last_discharge_timestamp,
        "natural_blackout_credit": round(s.natural_blackout_credit, 3) if s.natural_blackout_credit is not None else None,
    }
    health_path = HEALTH_ENDPOINT_PATH

    try:
        # Guard against symlink attack
        if health_path.is_symlink():
            raise OSError(f"{health_path} is a symlink, refusing to write")

        atomic_write_json(health_path, health_data)

    except Exception as e:
        logger.warning(f"Failed to write health endpoint: {e}")
