"""UPS Battery Monitor — configuration, dataclasses, constants, and helpers.

Extracted from monitor.py (F58) to separate configuration/infrastructure
from daemon orchestration logic.
"""

import json
import os
import time
import logging
import sys
import tomllib
import tempfile
import importlib.metadata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from systemd.journal import JournalHandler

from src.model import BatteryModel
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
REPORTING_INTERVAL_POLLS = 6  # Log metrics every N polls (6 * 10s = 60s)
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
DISCHARGE_BUFFER_MAX_SAMPLES = 1000  # Cap at ~3 hours (1000 * 10s ≈ 2.8h), prevents unbounded memory growth
ERROR_LOG_BURST = 10                 # Full traceback for first N errors, then summary every REPORTING_INTERVAL_POLLS


class SchedulingConfig:
    """Phase 17 scheduling parameters — all optional with sensible defaults."""

    def __init__(self,
                 grid_stability_cooldown_hours: float = 4.0,
                 soh_floor_threshold: float = 0.60,
                 min_days_between_tests: float = 7.0,
                 roi_threshold: float = 0.2,
                 blackout_credit_window_days: float = 7.0,
                 critical_cycle_budget_threshold: int = 5,
                 deep_test_sulfation_threshold: float = 0.65,
                 quick_test_sulfation_threshold: float = 0.40,
                 scheduler_eval_hour_utc: int = 8,
                 verbose_scheduling: bool = False):
        self.grid_stability_cooldown_hours = grid_stability_cooldown_hours
        self.soh_floor_threshold = soh_floor_threshold
        self.min_days_between_tests = min_days_between_tests
        self.roi_threshold = roi_threshold
        self.blackout_credit_window_days = blackout_credit_window_days
        self.critical_cycle_budget_threshold = critical_cycle_budget_threshold
        self.deep_test_sulfation_threshold = deep_test_sulfation_threshold
        self.quick_test_sulfation_threshold = quick_test_sulfation_threshold
        self.scheduler_eval_hour_utc = scheduler_eval_hour_utc
        self.verbose_scheduling = verbose_scheduling

    def validate(self) -> list[str]:
        """Return list of validation errors, empty if valid."""
        errors = []

        # grid_stability_cooldown_hours: 0 is valid (disables gate), else ≥0
        if self.grid_stability_cooldown_hours < 0:
            errors.append("grid_stability_cooldown_hours must be ≥0 (0 disables gate)")

        # soh_floor_threshold: 0.0–1.0
        if not (0.0 <= self.soh_floor_threshold <= 1.0):
            errors.append("soh_floor_threshold must be in [0.0, 1.0]")

        # min_days_between_tests: ≥1.0
        if self.min_days_between_tests < 1.0:
            errors.append("min_days_between_tests must be ≥1.0")

        # roi_threshold: 0.0–1.0
        if not (0.0 <= self.roi_threshold <= 1.0):
            errors.append("roi_threshold must be in [0.0, 1.0]")

        # blackout_credit_window_days: 1.0–30.0
        if not (1.0 <= self.blackout_credit_window_days <= 30.0):
            errors.append("blackout_credit_window_days must be in [1.0, 30.0]")

        # critical_cycle_budget_threshold: ≥1
        if self.critical_cycle_budget_threshold < 1:
            errors.append("critical_cycle_budget_threshold must be ≥1")

        # deep_test_sulfation_threshold: 0.0–1.0
        if not (0.0 <= self.deep_test_sulfation_threshold <= 1.0):
            errors.append("deep_test_sulfation_threshold must be in [0.0, 1.0]")

        # quick_test_sulfation_threshold: 0.0–1.0
        if not (0.0 <= self.quick_test_sulfation_threshold <= 1.0):
            errors.append("quick_test_sulfation_threshold must be in [0.0, 1.0]")

        # quick < deep (logical ordering)
        if self.quick_test_sulfation_threshold > self.deep_test_sulfation_threshold:
            errors.append("quick_test_sulfation_threshold must be ≤ deep_test_sulfation_threshold")

        # scheduler_eval_hour_utc: 0–23
        if not (0 <= self.scheduler_eval_hour_utc <= 23):
            errors.append("scheduler_eval_hour_utc must be in [0, 23]")

        return errors


@dataclass(frozen=True)
class Config:
    """Immutable UPS daemon configuration.

    frozen=True prevents accidental mutation at runtime. All fields are read-only.
    Config instance is created at startup and passed to MonitorDaemon.__init__.
    """
    ups_name: str                          # From config.toml['ups_name'] or default 'cyberpower'
    polling_interval: int                  # 10 seconds
    reporting_interval: int                # 60 seconds (REPORTING_INTERVAL_POLLS * polling_interval)
    nut_host: str                          # 'localhost'
    nut_port: int                          # 3493
    nut_timeout: float                     # 2.0 seconds
    shutdown_minutes: int                  # From config.toml['shutdown_minutes'] or default 5
    soh_alert_threshold: float             # From config.toml['soh_alert'] or default 0.80
    model_dir: Path                        # ~/.config/ups-battery-monitor
    config_dir: Path                       # ~/.config/ups-battery-monitor
    runtime_threshold_minutes: int         # 20 minutes (hardcoded constant)
    reference_load_percent: float          # 20.0% (hardcoded constant)
    ema_window_sec: int                    # 120 seconds (hardcoded constant)
    capacity_ah: float                     # From config.toml['capacity_ah'] or default 7.2


def load_config() -> Config:
    """Load user config from TOML, falling back to defaults for missing keys.

    Returns: Config dataclass instance with all fields populated.
    Raises: ValueError if Phase 17 scheduling configuration is invalid.
    """
    cfg_dict = {}
    for path in [CONFIG_DIR / 'config.toml', REPO_ROOT / 'config.toml']:
        if path.is_file():
            with open(path, 'rb') as f:
                cfg_dict = tomllib.load(f)
            break

    user_config = {k: cfg_dict.get(k, v) for k, v in _CONFIGURABLE_DEFAULTS.items()}

    # Validate Phase 17 scheduling configuration if present
    if 'scheduling' in cfg_dict:
        scheduling_params = cfg_dict.get('scheduling', {})
        sched_config = SchedulingConfig(**scheduling_params)
        errors = sched_config.validate()
        if errors:
            raise ValueError(f"Invalid scheduling configuration: {'; '.join(errors)}")
        logger.info(
            "Phase 17 scheduling configuration loaded and validated",
            extra={
                'event_type': 'config_loaded',
                'grid_stability_cooldown_hours': sched_config.grid_stability_cooldown_hours,
                'soh_floor_threshold': sched_config.soh_floor_threshold,
                'min_days_between_tests': sched_config.min_days_between_tests,
                'roi_threshold': sched_config.roi_threshold,
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
        config_dir=CONFIG_DIR,
        runtime_threshold_minutes=RUNTIME_THRESHOLD_MINUTES,
        capacity_ah=user_config['capacity_ah'],
        reference_load_percent=REFERENCE_LOAD_PERCENT,
        ema_window_sec=EMA_WINDOW,
    )


def get_scheduling_config(config_dict: dict) -> SchedulingConfig:
    """Extract and validate Phase 17 scheduling configuration from TOML dict.

    Args:
        config_dict: Parsed TOML dictionary (from tomllib.load)

    Returns:
        SchedulingConfig with all parameters (defaults applied for missing keys)

    Raises:
        ValueError: If any parameter is invalid
    """
    scheduling_params = config_dict.get('scheduling', {})
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
    """Voltage sag measurement state machine: IDLE → ARMED → MEASURING → COMPLETE."""
    IDLE = "idle"            # Waiting for OL→OB transition
    MEASURING = "measuring"  # Collecting samples (fast poll active)
    COMPLETE = "complete"    # Sag recorded, back to normal polling


# Logging: JournalHandler when running under systemd, stderr fallback otherwise.
# SyslogIdentifier in service file provides the prefix — no need to repeat in formatter.
logger = logging.getLogger('ups-battery-monitor')
logger.setLevel(logging.INFO)
logger.handlers.clear()

try:
    handler = JournalHandler(identifier='ups-battery-monitor')
    handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
    logger.addHandler(handler)
except Exception:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
    logger.addHandler(handler)


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
        - Logs to logger at ERROR level if save fails
        - Does NOT raise exception; allows daemon to continue

    Raises:
        None; errors are logged only
    """
    try:
        model.save()
    except OSError as e:
        logger.error(f"Failed to persist model (disk full?): {e}")


def write_health_endpoint(
    soc_percent: float,
    is_online: bool,
    poll_latency_ms: Optional[float] = None,
    capacity_ah_measured: Optional[float] = None,
    capacity_ah_rated: float = 7.2,
    capacity_confidence: float = 0.0,
    capacity_samples_count: int = 0,
    capacity_converged: bool = False,
    # Phase 16 NEW parameters:
    sulfation_score: Optional[float] = None,
    sulfation_confidence: str = 'high',
    days_since_deep: Optional[float] = None,
    ir_trend_rate: Optional[float] = None,
    recovery_delta: Optional[float] = None,
    cycle_roi: Optional[float] = None,
    cycle_budget_remaining: Optional[int] = None,
    scheduling_reason: str = 'observing',
    next_test_timestamp: Optional[int] = None,
    last_discharge_timestamp: Optional[str] = None,
    natural_blackout_credit: Optional[float] = None,
) -> None:
    """Write daemon health state to file for external monitoring tools.

    Updates every poll (10s) with current daemon metrics. Tools like Grafana,
    check_mk, and custom scripts can read this file to track:
    - Daemon liveness (last_poll < 30s = healthy)
    - Current battery state (soc_percent, online status)
    - Poll latency for performance monitoring
    - Daemon version tracking
    - Phase 14: capacity metrics (measured Ah, confidence, convergence status)

    Uses atomic write pattern (tempfile + fdatasync + rename) to prevent
    partial writes on crash or power loss.
    """
    try:
        daemon_version = importlib.metadata.version('ups-unfucked')
    except importlib.metadata.PackageNotFoundError:
        daemon_version = "unknown"

    health_data = {
        "last_poll": datetime.now(timezone.utc).isoformat(),
        "last_poll_unix": int(time.time()),
        "current_soc_percent": round(soc_percent, 1),
        "online": is_online,
        "daemon_version": daemon_version,
        "poll_latency_ms": round(poll_latency_ms, 1) if poll_latency_ms is not None else None,
        # Phase 14: capacity metrics
        "capacity_ah_measured": round(capacity_ah_measured, 2) if capacity_ah_measured else None,
        "capacity_ah_rated": round(capacity_ah_rated, 2),
        "capacity_confidence": round(capacity_confidence, 3),
        "capacity_samples_count": capacity_samples_count,
        "capacity_converged": capacity_converged,
        # Phase 16 NEW: sulfation metrics
        "sulfation_score": round(sulfation_score, 3) if sulfation_score is not None else None,
        "sulfation_score_confidence": sulfation_confidence,
        "days_since_deep": round(days_since_deep, 1) if days_since_deep is not None else None,
        "ir_trend_rate": round(ir_trend_rate, 6) if ir_trend_rate is not None else None,
        "recovery_delta": round(recovery_delta, 3) if recovery_delta is not None else None,
        # Phase 16 NEW: ROI metrics
        "cycle_roi": round(cycle_roi, 3) if cycle_roi is not None else None,
        "cycle_budget_remaining": cycle_budget_remaining,
        "scheduling_reason": scheduling_reason,
        "next_test_timestamp": next_test_timestamp,
        # Phase 16 NEW: discharge metrics
        "last_discharge_timestamp": last_discharge_timestamp,
        "natural_blackout_credit": round(natural_blackout_credit, 3) if natural_blackout_credit is not None else None,
    }
    health_path = HEALTH_ENDPOINT_PATH
    tmp_path = None

    try:
        # Guard against symlink attack
        if health_path.is_symlink():
            raise OSError(f"{health_path} is a symlink, refusing to write")

        health_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write pattern: tempfile in same mount + fdatasync + rename
        with tempfile.NamedTemporaryFile(
            mode='w',
            dir=str(health_path.parent),
            delete=False,
            suffix='.tmp',
            prefix='ups-health-'
        ) as tmp:
            json.dump(health_data, tmp, indent=2)
            tmp.flush()
            os.fdatasync(tmp.fileno())
            os.fchmod(tmp.fileno(), 0o644)
            tmp_path = Path(tmp.name)

        # Atomic rename (POSIX guarantees)
        tmp_path.replace(health_path)
        logger.debug(f"Health endpoint written to {health_path}")

    except Exception as e:
        # Consolidated handler: clean up + log once + re-raise
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        logger.error(f"Failed to write health endpoint: {e}")
        raise
