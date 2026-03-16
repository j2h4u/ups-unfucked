import json
import time
import signal
import sys
import math
import logging
import argparse
import tomllib
import os
import tempfile
import importlib.metadata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from systemd.journal import JournalHandler
try:
    from systemd.daemon import notify as sd_notify
except ImportError:
    sd_notify = lambda status: None  # No-op when running outside systemd

from src.nut_client import NUTClient
from src.ema_filter import EMAFilter, ir_compensate
from src.model import BatteryModel
from src.capacity_estimator import CapacityEstimator
from src.soc_predictor import soc_from_voltage, charge_percentage
from src.runtime_calculator import runtime_minutes, peukert_runtime_hours
from src.event_classifier import EventClassifier, EventType
from src.virtual_ups import write_virtual_ups_dev, compute_ups_status_override
from src import soh_calculator, replacement_predictor, alerter
from src.battery_math import calibrate_peukert, ScalarRLS
from src.battery_math.soh import interpolate_cliff_region

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


def _load_config() -> Config:
    """Load user config from TOML, falling back to defaults for missing keys.

    Returns: Config dataclass instance with all fields populated.
    """
    cfg_dict = {}
    for path in [CONFIG_DIR / 'config.toml', REPO_ROOT / 'config.toml']:
        if path.is_file():
            with open(path, 'rb') as f:
                cfg_dict = tomllib.load(f)
            break

    user_config = {k: cfg_dict.get(k, v) for k, v in _CONFIGURABLE_DEFAULTS.items()}

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


def _safe_save(model: BatteryModel) -> None:
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


# Internal constants (not user-configurable)
SAG_SAMPLES_REQUIRED = 5             # Collect 5 voltage samples, take median of last 3 for noise rejection
DISCHARGE_BUFFER_MAX_SAMPLES = 1000  # Cap at ~3 hours (1000 * 10s ≈ 2.8h), prevents unbounded memory growth
ERROR_LOG_BURST = 10                 # Full traceback for first N errors, then summary every REPORTING_INTERVAL_POLLS

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



def _write_health_endpoint(soc_percent: float, is_online: bool, poll_latency_ms: Optional[float] = None,
                           capacity_ah_measured: Optional[float] = None,
                           capacity_ah_rated: float = 7.2,
                           capacity_confidence: float = 0.0,
                           capacity_samples_count: int = 0,
                           capacity_converged: bool = False) -> None:
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

    Args:
        soc_percent: Current state of charge as percentage (0-100)
        is_online: True if UPS in OL state, False if OB state
        poll_latency_ms: NUT poll latency in milliseconds (optional)
        capacity_ah_measured: Measured capacity in Ah from CapacityEstimator (None if not yet estimated)
        capacity_ah_rated: Rated capacity in Ah (default 7.2)
        capacity_confidence: Convergence score 0-1 (displayed as 0-100%)
        capacity_samples_count: Number of capacity measurements collected
        capacity_converged: True if convergence threshold met (count >= 3 AND CoV < 0.10)
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


class MonitorDaemon:
    """
    Main daemon for UPS battery monitoring.

    Polls NUT upsd, applies EMA smoothing, tracks battery state.
    """

    def __init__(self, config: Config, new_battery_flag: bool = False):
        """Initialize daemon with provided configuration.

        Args:
            config: Config dataclass instance with all daemon parameters.
            new_battery_flag: Boolean flag from CLI --new-battery; indicates battery swap.
                             When True, Phase 13 detection logic will check on next discharge.
        """
        self.running = True
        self.config = config
        self.shutdown_threshold_minutes = config.shutdown_minutes

        # Create model directory
        config.model_dir.mkdir(parents=True, exist_ok=True)

        # Initialize components
        self.nut_client = NUTClient(
            host=config.nut_host,
            port=config.nut_port,
            timeout=config.nut_timeout,
            ups_name=config.ups_name
        )

        self.ema_buffer = EMAFilter(
            window_sec=config.ema_window_sec,
            poll_interval_sec=config.polling_interval
        )

        model_path = config.model_dir / 'model.json'
        self.battery_model = BatteryModel(model_path)
        self.battery_model.data['full_capacity_ah_ref'] = config.capacity_ah
        self._validate_model()

        # Set battery install date on first ever startup
        if self.battery_model.get_battery_install_date() is None:
            self.battery_model.set_battery_install_date(datetime.now().strftime('%Y-%m-%d'))
        if not model_path.exists():
            self.battery_model.save()  # Write defaults so tools (battery-health.py, MOTD) can read
        self.event_classifier = EventClassifier()

        # Initialize CapacityEstimator for Phase 12 capacity measurement (CAP-01, CAP-05)
        self.capacity_estimator = CapacityEstimator(
            peukert_exponent=self.battery_model.get_peukert_exponent(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            nominal_power_watts=self.battery_model.get_nominal_power_watts()
        )

        # Load historical capacity estimates from model.json for convergence tracking
        # Ensures has_converged() and get_confidence() survive daemon restarts
        for estimate in self.battery_model.get_capacity_estimates():
            self.capacity_estimator.add_measurement(
                ah=estimate['ah_estimate'],
                timestamp=estimate['timestamp'],
                metadata=estimate['metadata']
            )

        # Store new_battery_requested flag from CLI for Phase 13 detection logic
        self.battery_model.data['new_battery_requested'] = new_battery_flag
        if new_battery_flag:
            # Log that flag was set (informational only)
            logger.info("New battery flag set via --new-battery CLI; Phase 13 detection will check on next discharge")

        # Phase 13: If user signals --new-battery, reset baseline
        if new_battery_flag or self.battery_model.data.get('new_battery_requested', False):
            self._reset_battery_baseline()

        # Clear auto-detection flag after user confirmed via --new-battery
        self.battery_model.data['new_battery_detected'] = False
        self.battery_model.save()

        # Load physics params from model
        self.ir_k = self.battery_model.get_ir_k()
        self.ir_l_base = self.battery_model.get_ir_reference_load()

        # RLS estimators for online parameter calibration
        self.rls_ir_k = ScalarRLS.from_dict(
            self.battery_model.get_rls_state('ir_k'), forgetting_factor=0.97)
        self.rls_peukert = ScalarRLS.from_dict(
            self.battery_model.get_rls_state('peukert'), forgetting_factor=0.97)
        self._discharge_predicted_runtime = None  # Snapshot for prediction error logging

        # Metrics tracking for current battery state
        self.current_metrics = CurrentMetrics()
        self._last_logged_soc = None
        self._last_logged_time_rem = None

        self.discharge_buffer = DischargeBuffer()
        self._discharge_start_time = None  # Timestamp when OL→OB occurred (for cumulative on-battery tracking)
        self.discharge_buffer_clear_countdown = None  # Cooldown timer (60s) before clearing buffer after OL
        self.soh_threshold = config.soh_alert_threshold
        self.runtime_threshold_minutes = config.runtime_threshold_minutes
        self.reference_load_percent = config.reference_load_percent

        # Voltage sag measurement for internal resistance tracking
        self.sag_state = SagState.IDLE
        self.v_before_sag = None
        self.sag_buffer = []

        self.calibration_last_written_index = 0

        # Phase 14: Track if baseline_lock event has been logged (prevent duplicates)
        self.capacity_locked_previously = False

        # Signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        logger.info(f"Daemon initialized: shutdown_threshold={self.shutdown_threshold_minutes}min, poll={config.polling_interval}s, model={model_path}, nut={config.nut_host}:{config.nut_port}")

        # H1 fix: Check NUT connectivity at startup
        self._check_nut_connectivity()

    def _validate_model(self):
        """Validate battery model has minimum viable data for SoC/runtime predictions."""
        lut = self.battery_model.get_lut()
        if len(lut) < 2:
            logger.warning(f"Model LUT has only {len(lut)} point(s); predictions will be inaccurate until calibration")

        anchor = self.battery_model.data.get('anchor_voltage')
        if anchor is None:
            logger.warning("Model missing anchor_voltage; SoH calculation may fail")

        soh = self.battery_model.get_soh()
        if not (0.0 < soh <= 1.0):
            logger.warning(f"Model SoH={soh} out of valid range (0, 1]; resetting to 1.0")
            self.battery_model.set_soh(1.0)

        capacity = self.battery_model.get_capacity_ah()
        if capacity <= 0:
            raise ValueError(f"Model capacity_ah={capacity} invalid; cannot compute runtime")

    def _check_nut_connectivity(self):
        """
        Verify NUT upsd is reachable before entering main loop.
        Only 4 lines as specified.
        """
        try:
            _ = self.nut_client.get_ups_vars()
            logger.info("NUT upsd reachable, polling started")
        except Exception:
            logger.warning(f"NUT upsd unreachable at startup, will retry every {self.config.polling_interval}s")

    def _handle_event_transition(self):
        """
        Execute actions based on event transitions.

        Implements EVT-02 (blackout), EVT-03 (test), EVT-04 (status arbiter),
        and EVT-05 (model update on discharge completion).
        """
        event_type = self.current_metrics.event_type
        previous_event_type = self.current_metrics.previous_event_type

        # EVT-02: Real blackout - prepare shutdown signal
        if event_type == EventType.BLACKOUT_REAL:
            time_rem = self.current_metrics.time_rem_minutes
            if time_rem is not None and time_rem < self.shutdown_threshold_minutes:
                logger.warning(
                    f"Real blackout: time_rem={time_rem:.1f}min < threshold {self.shutdown_threshold_minutes}min; "
                    f"prepare LB flag"
                )
                self.current_metrics.shutdown_imminent = True
            else:
                self.current_metrics.shutdown_imminent = False

        # EVT-03: Battery test - suppress shutdown
        if event_type == EventType.BLACKOUT_TEST:
            logger.info("Battery test detected; collecting calibration data, no shutdown")
            self.current_metrics.shutdown_imminent = False

        # EVT-04: Status arbitration via compute_ups_status_override()
        self.current_metrics.ups_status_override = compute_ups_status_override(
            event_type,
            self.current_metrics.time_rem_minutes or 0,
            self.shutdown_threshold_minutes
        )

        # EVT-05: Model update on OB→OL transition
        if (self.current_metrics.transition_occurred and
            event_type == EventType.ONLINE and
            previous_event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)):
            logger.info("Power restored; updating LUT with measured discharge points")
            self._update_battery_health()

            # Refine cliff region (10.5-11.0V) if we have measured data there.
            # No-op if <2 measured points in cliff range — safe to call on every discharge.
            updated_lut = interpolate_cliff_region(self.battery_model.get_lut())
            if updated_lut != self.battery_model.get_lut():
                self.battery_model.update_lut_from_calibration(updated_lut)
                logger.info("LUT cliff region updated from measured discharge data")

            # No _safe_save here: _update_battery_health() and update_lut_from_calibration()
            # already persist. Removing redundant write reduces SSD wear.

    def _update_battery_health(self):
        """
        Called when discharge event completes (OB→OL transition).

        Workflow:
        1. Extract discharge voltage/time series from buffer
        2. Calculate SoH using area-under-curve
        3. Append to soh_history in model.json
        4. Predict replacement date via linear regression
        5. Alert if SoH or runtime thresholds breached
        6. Single save at end after all mutations
        7. Clear discharge buffer
        """
        # Check discharge buffer has data
        if len(self.discharge_buffer.voltages) < 2:
            return  # No discharge detected; skip SoH update

        # Skip SoH/Peukert update for micro-discharges (<5 min).
        # Short discharges have terrible signal-to-noise: 105s discharge
        # caused SoH to drop from 99.7% to 88.6% (incident 2026-03-16).
        # Cycle count and on-battery time are still tracked (in _track_discharge).
        discharge_duration = self.discharge_buffer.times[-1] - self.discharge_buffer.times[0]
        if discharge_duration < 300:
            logger.info(f"Discharge too short for model update ({discharge_duration:.0f}s < 300s); "
                        f"skipping SoH/Peukert calibration")
            self.discharge_buffer = DischargeBuffer()
            return

        # Calculate average load from accumulated samples
        avg_load = (sum(self.discharge_buffer.loads) / len(self.discharge_buffer.loads)
                   if self.discharge_buffer.loads else self.reference_load_percent)

        # Phase 13: Call orchestrator which selects measured vs. rated capacity
        soh_result = soh_calculator.calculate_soh_from_discharge(
            discharge_voltage_series=self.discharge_buffer.voltages,
            discharge_time_series=self.discharge_buffer.times,
            reference_soh=self.battery_model.get_soh(),
            battery_model=self.battery_model,
            load_percent=avg_load,
            nominal_power_watts=self.battery_model.get_nominal_power_watts(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            peukert_exponent=self.battery_model.get_peukert_exponent()
        )

        if soh_result is None:
            logger.info("SoH update returned None; skipping history entry")
            self.discharge_buffer = DischargeBuffer()
            return

        soh_new, capacity_ah_used = soh_result

        # Add to history with capacity baseline tag
        today = datetime.now().strftime('%Y-%m-%d')
        self.battery_model.add_soh_history_entry(today, soh_new, capacity_ah_ref=capacity_ah_used)

        logger.info(f"SoH calculated: {soh_new:.2%}")

        # Predict replacement date — only if capacity has converged.
        # Why capacity convergence and not a time-span threshold (e.g., "90 days"):
        # - Time span is a magic number that doesn't reflect actual data quality.
        # - Capacity convergence (≥3 deep discharges, CoV < 10%) is a data-driven gate:
        #   the system itself says "I know this battery well enough to make predictions."
        # - Without converged capacity, SoH is computed against rated (not real) Ah,
        #   so regression on those SoH values would extrapolate from inaccurate inputs.
        convergence = self.battery_model.get_convergence_status()
        if convergence.get('converged', False):
            result = replacement_predictor.linear_regression_soh(
                soh_history=self.battery_model.get_soh_history(),
                threshold_soh=self.soh_threshold
            )
        else:
            result = None

        # Persist replacement date for MOTD and telemetry
        if result:
            _, _, _, replacement_date = result
            self.battery_model.set_replacement_due(replacement_date)
        else:
            self.battery_model.set_replacement_due(None)

        # Alert if SoH below threshold
        if soh_new < self.soh_threshold:
            days_to_replacement = None
            if result:
                slope, intercept, r2, replacement_date = result
                if replacement_date and replacement_date != 'overdue':
                    try:
                        repl_dt = datetime.strptime(replacement_date, '%Y-%m-%d')
                        days_to_replacement = (repl_dt - datetime.now()).days
                    except ValueError:
                        pass

            alerter.alert_soh_below_threshold(
                logger,
                soh_new,
                self.soh_threshold,
                days_to_replacement
            )

        # Alert if runtime at 100% is low
        time_rem_at_100pct = runtime_minutes(
            soc=1.0, load_percent=avg_load,
            capacity_ah=self.battery_model.get_capacity_ah(),
            soh=soh_new,
            peukert_exponent=self.battery_model.get_peukert_exponent(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            nominal_power_watts=self.battery_model.get_nominal_power_watts()
        )
        if time_rem_at_100pct < self.runtime_threshold_minutes:
            alerter.alert_runtime_below_threshold(
                logger,
                time_rem_at_100pct,
                self.runtime_threshold_minutes
            )

        # Auto-calibrate Peukert exponent if prediction error > 10%
        self._auto_calibrate_peukert(soh_new)

        # Log prediction error before clearing buffer
        self._log_discharge_prediction()

        # Single save at end after all mutations
        _safe_save(self.battery_model)

        # Clear discharge buffer
        self.discharge_buffer = DischargeBuffer()

    def _log_discharge_prediction(self):
        """Log prediction vs actual runtime for model accuracy tracking.

        Gate: predicted runtime must exist AND discharge >= 300s.
        Logs raw data only (predicted, actual, load, start SoC) — no error % in daemon.
        """
        if self._discharge_predicted_runtime is None:
            return

        times = self.discharge_buffer.times
        if len(times) < 2:
            self._discharge_predicted_runtime = None
            return

        discharge_duration_sec = times[-1] - times[0]
        if discharge_duration_sec < 300:
            self._discharge_predicted_runtime = None
            return

        actual_minutes = discharge_duration_sec / 60.0
        avg_load = (sum(self.discharge_buffer.loads) / len(self.discharge_buffer.loads)
                    if self.discharge_buffer.loads else 0.0)
        start_soc = self.current_metrics.soc or 0.0

        logger.info(
            f"Discharge prediction: predicted={self._discharge_predicted_runtime:.1f}min, "
            f"actual={actual_minutes:.1f}min, load_avg={avg_load:.1f}%",
            extra={
                'EVENT_TYPE': 'discharge_prediction',
                'PREDICTED_MINUTES': f'{self._discharge_predicted_runtime:.1f}',
                'ACTUAL_MINUTES': f'{actual_minutes:.1f}',
                'AVG_LOAD_PERCENT': f'{avg_load:.1f}',
                'START_SOC': f'{start_soc:.3f}',
                'TIMESTAMP': datetime.now(timezone.utc).isoformat(),
            })

        self._discharge_predicted_runtime = None

    def _reset_battery_baseline(self):
        """Reset capacity estimation and SoH history baseline on battery replacement."""

        old_capacity = self.battery_model.data.get('capacity_ah_measured')
        old_soh = self.battery_model.data.get('soh', 1.0)
        new_capacity = self.battery_model.get_capacity_ah()  # 7.2Ah (rated)

        # Clear capacity estimates (will rebuild from next deep discharge)
        self.battery_model.data['capacity_estimates'] = []

        # Clear capacity_ah_measured (will be set when new measurements converge)
        self.battery_model.data['capacity_ah_measured'] = None

        # Add fresh SoH entry with new baseline (old entries stay in history for record, but excluded by filtering)
        today = datetime.now().strftime('%Y-%m-%d')
        self.battery_model.data['soh'] = 1.0  # New battery assumed 100% SoH
        self.battery_model.add_soh_history_entry(
            date=today,
            soh=1.0,
            capacity_ah_ref=new_capacity  # 7.2Ah (rated, fresh baseline)
        )

        # Reset cycle counter to indicate new battery era
        self.battery_model.data['cycle_count'] = 0

        # Reset RLS estimators to defaults (new battery = fresh calibration)
        self.battery_model.reset_rls_state()
        self.rls_ir_k = ScalarRLS(theta=0.015, P=1.0)
        self.rls_peukert = ScalarRLS(theta=1.2, P=1.0)

        # Phase 14: Event 3 - baseline_reset: Structured journald logging
        if old_capacity is not None:
            logger.info(
                f"baseline_reset: capacity baseline reset from {old_capacity:.2f}Ah to {new_capacity:.2f}Ah",
                extra={
                    'EVENT_TYPE': 'baseline_reset',
                    'CAPACITY_AH_OLD': f'{old_capacity:.2f}',
                    'CAPACITY_AH_NEW': f'{new_capacity:.2f}',
                    'TIMESTAMP': datetime.now(timezone.utc).isoformat(),
                }
            )
        else:
            logger.info(
                f"baseline_reset: capacity baseline initialized to {new_capacity:.2f}Ah (first reset)",
                extra={
                    'EVENT_TYPE': 'baseline_reset',
                    'CAPACITY_AH_NEW': f'{new_capacity:.2f}',
                    'TIMESTAMP': datetime.now(timezone.utc).isoformat(),
                }
            )

        self.battery_model.save()

    def _auto_calibrate_peukert(self, current_soh: float):
        """Orchestrator: applies guard clauses, calls kernel, handles result.

        Auto-calibrate Peukert exponent from actual discharge duration.
        Guard clauses (sample count, duration, load validity) stay here in orchestrator.
        Pure math is delegated to kernel function.
        """
        # Guard clause 1: Minimum discharge samples
        times = self.discharge_buffer.times
        if len(times) < 2:
            logger.debug("Peukert calibration skipped: <2 discharge samples")
            return

        # Guard clause 2: Discharge duration threshold
        actual_duration_sec = times[-1] - times[0]
        if actual_duration_sec < 60:
            logger.debug(f"Peukert calibration skipped: discharge too short ({actual_duration_sec:.0f}s < 60s)")
            return

        # Guard clause 3: Valid average load (use raw load from discharge buffer)
        avg_load = (sum(self.discharge_buffer.loads) / len(self.discharge_buffer.loads)
                   if self.discharge_buffer.loads else self.reference_load_percent)
        if avg_load is None or avg_load <= 0 or avg_load > 100:
            logger.debug(f"Peukert calibration skipped: invalid load ({avg_load})")
            return

        # Data validated; call pure kernel function
        # Use RATED capacity (self.config.capacity_ah), not measured (VAL-02)
        new_exponent = calibrate_peukert(
            actual_duration_sec=actual_duration_sec,
            avg_load_percent=avg_load,
            current_soh=current_soh,
            capacity_ah=self.config.capacity_ah,
            current_exponent=self.battery_model.get_peukert_exponent(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            nominal_power_watts=self.battery_model.get_nominal_power_watts()
        )

        # Handle kernel result: RLS smoothing instead of direct set
        if new_exponent is not None:
            old_exponent = self.battery_model.get_peukert_exponent()
            smoothed, new_P = self.rls_peukert.update(new_exponent)
            smoothed = max(1.0, min(1.4, smoothed))  # physical bounds
            self.battery_model.set_peukert_exponent(smoothed)
            self.battery_model.set_rls_state(
                'peukert', smoothed, new_P, self.rls_peukert.sample_count)
            logger.info(
                f"Peukert calibrated: {old_exponent:.3f} → {smoothed:.3f} "
                f"(single-point={new_exponent:.3f}), "
                f"confidence={self.rls_peukert.confidence:.0%}",
                extra={
                    'EVENT_TYPE': 'peukert_calibration',
                    'PEUKERT_OLD': f'{old_exponent:.3f}',
                    'PEUKERT_NEW': f'{smoothed:.3f}',
                    'PEUKERT_RAW': f'{new_exponent:.3f}',
                    'RLS_P': f'{new_P:.4f}',
                    'RLS_CONFIDENCE': f'{self.rls_peukert.confidence:.3f}',
                    'SAMPLE_COUNT': str(self.rls_peukert.sample_count),
                })
        else:
            logger.error("Peukert calibration returned None (unexpected — math undefined?)")

    def _handle_discharge_complete(self, discharge_data: dict) -> None:
        """
        Handle discharge completion event: measure capacity via CapacityEstimator.

        Called when OB→OL transition detected. Extracts discharge_buffer data (V, t, I series),
        calls CapacityEstimator.estimate() to measure capacity, and if successful, stores
        result in model.json via add_capacity_estimate(). Implements CAP-01 and CAP-05.

        Args:
            discharge_data: Dict with keys:
                - voltage_series: List[float] voltage readings (V)
                - time_series: List[float] unix timestamps (sec)
                - current_series: List[float] load percent (%)
                - timestamp: str ISO8601 timestamp

        Flow:
            1. Extract discharge_buffer data
            2. Call CapacityEstimator.estimate(V, t, I, lut)
            3. If None (quality filter): log rejection, return
            4. If tuple (success): call model.add_capacity_estimate()
            5. Check has_converged(): if True, set model['capacity_converged'] flag
            6. Log measurement with confidence

        NOTE: Phase 12 does NOT modify full_capacity_ah_ref. Measured capacity lives
        only in capacity_estimates[] array. Replacement of rated→measured is Phase 13 scope.
        """
        voltage_series = discharge_data.get('voltage_series', [])
        time_series = discharge_data.get('time_series', [])
        current_series = discharge_data.get('current_series', [])
        timestamp = discharge_data.get('timestamp', datetime.now().isoformat())

        # Guard: need at least 2 samples
        if len(voltage_series) < 2 or len(time_series) < 2 or len(current_series) < 2:
            logger.debug(f"Discharge data incomplete for capacity estimation: "
                        f"{len(voltage_series)} V, {len(time_series)} t, {len(current_series)} I")
            return

        # Call CapacityEstimator
        result = self.capacity_estimator.estimate(
            voltage_series=voltage_series,
            time_series=time_series,
            current_series=current_series,
            lut=self.battery_model.data.get('lut', [])
        )

        # Quality filter rejection (VAL-01: micro/shallow discharges rejected)
        if result is None:
            logger.debug("Discharge rejected by CapacityEstimator quality filter")
            return

        # Success: unpack estimate
        ah_estimate, confidence, metadata = result

        # Store in model
        self.battery_model.add_capacity_estimate(
            ah_estimate=ah_estimate,
            confidence=confidence,
            metadata=metadata,
            timestamp=timestamp
        )

        # Phase 14: Event 1 - capacity_measurement: Structured journald logging
        # Get convergence status to compute CoV and sample count
        convergence_status = self.battery_model.get_convergence_status()
        sample_count = convergence_status['sample_count']

        # Compute CoV for human-readable message
        estimates = self.battery_model.data.get('capacity_estimates', [])
        ah_values = [e['ah_estimate'] for e in estimates]
        if len(ah_values) >= 2:
            mean_ah = sum(ah_values) / len(ah_values)
            std_ah = (sum((x - mean_ah) ** 2 for x in ah_values) / len(ah_values)) ** 0.5
            cov = std_ah / mean_ah if mean_ah > 0 else 0.0
        else:
            std_ah = 0.0
            cov = 0.0

        confidence_pct = int(confidence * 100) if confidence else 0

        # Extract metadata fields with safe defaults
        delta_soc_percent = metadata.get('delta_soc_percent', 0.0)
        duration_sec = metadata.get('duration_sec', 0)
        load_avg_percent = metadata.get('load_avg_percent', 0.0)

        logger.info(
            f"capacity_measurement: {ah_estimate:.2f}Ah (±{std_ah:.2f}), CoV={cov:.3f} "
            f"({sample_count} samples, {confidence_pct}% confidence)",
            extra={
                'EVENT_TYPE': 'capacity_measurement',
                'CAPACITY_AH': f'{ah_estimate:.2f}',
                'CONFIDENCE_PERCENT': str(confidence_pct),
                'SAMPLE_COUNT': str(sample_count),
                'DELTA_SOC_PERCENT': f'{delta_soc_percent:.1f}',
                'DURATION_SEC': str(int(duration_sec)),
                'LOAD_AVG_PERCENT': f'{load_avg_percent:.1f}',
            }
        )

        # Phase 14: Event 2 - baseline_lock: When convergence detected
        # Check convergence: count >= 3 AND CoV < 0.10 (expert-approved)
        if self.capacity_estimator.has_converged():
            self.battery_model.data['capacity_converged'] = True

            # Log baseline_lock event only once per convergence (use flag to deduplicate)
            if not self.capacity_locked_previously:
                logger.info(
                    f"baseline_lock: capacity converged at {convergence_status['latest_ah']:.2f}Ah after {convergence_status['sample_count']} deep discharges",
                    extra={
                        'EVENT_TYPE': 'baseline_lock',
                        'CAPACITY_AH': f'{convergence_status["latest_ah"]:.2f}',
                        'SAMPLE_COUNT': str(convergence_status['sample_count']),
                        'TIMESTAMP': datetime.now(timezone.utc).isoformat(),
                    }
                )
                self.capacity_locked_previously = True

            _safe_save(self.battery_model)

        # Phase 13: NEW BATTERY DETECTION (post-discharge)
        # Compare fresh capacity measurement to stored baseline
        convergence = self.battery_model.get_convergence_status()

        if convergence.get('converged', False):
            # Capacity estimation has stabilized; we have a reliable baseline
            current_measured = convergence.get('latest_ah')
            stored_baseline = self.battery_model.data.get('capacity_ah_measured', None)

            if stored_baseline is not None:
                # Compare current measurement to last stored baseline
                delta_ah = abs(current_measured - stored_baseline)
                delta_percent = (delta_ah / stored_baseline) * 100

                if delta_percent > 10.0:  # >10% threshold
                    logger.warning(
                        f"New battery detection: measured capacity {current_measured:.2f}Ah "
                        f"differs from baseline {stored_baseline:.2f}Ah ({delta_percent:.1f}% > 10% threshold)"
                    )

                    # Set flag for MOTD and user to acknowledge
                    self.battery_model.data['new_battery_detected'] = True
                    self.battery_model.data['new_battery_detected_timestamp'] = datetime.now().isoformat()
                    self.battery_model.save()

                    logger.info(
                        "New battery flag set; MOTD will show alert next shell session. "
                        "User can confirm with: ups-battery-monitor --new-battery"
                    )
            else:
                # First time convergence; store as baseline for future comparisons
                self.battery_model.data['capacity_ah_measured'] = current_measured
                self.battery_model.save()
                logger.info(f"Capacity baseline stored: {current_measured:.2f}Ah (first convergence)")

    def _record_voltage_sag(self, v_sag, event_type):
        """Record voltage sag measurement and compute internal resistance."""
        if self.v_before_sag is None or self.ema_buffer.load is None:
            return
        load = self.ema_buffer.load
        I_actual = load / 100.0 * self.battery_model.get_nominal_power_watts() / self.battery_model.get_nominal_voltage()
        if I_actual <= 0:
            return
        delta_v = self.v_before_sag - v_sag
        r_ohm = delta_v / I_actual
        today = datetime.now().strftime('%Y-%m-%d')
        self.battery_model.add_r_internal_entry(today, r_ohm, self.v_before_sag, v_sag, load, event_type.name)

        # RLS auto-calibration of ir_k from measured sag data
        nominal_V = self.battery_model.get_nominal_voltage()
        nominal_W = self.battery_model.get_nominal_power_watts()
        if nominal_V > 0:
            ir_k_measured = r_ohm * nominal_W / (nominal_V * 100.0)
            new_ir_k, new_P = self.rls_ir_k.update(ir_k_measured)
            new_ir_k = max(0.005, min(0.025, new_ir_k))  # physical bounds
            self.ir_k = new_ir_k
            self.battery_model.set_ir_k(new_ir_k)
            self.battery_model.set_rls_state(
                'ir_k', new_ir_k, new_P, self.rls_ir_k.sample_count)
            logger.info(
                f"ir_k calibrated: {new_ir_k:.4f} (P={new_P:.4f}, "
                f"confidence={self.rls_ir_k.confidence:.0%}, "
                f"measured={ir_k_measured:.4f})",
                extra={
                    'EVENT_TYPE': 'ir_k_calibration',
                    'IR_K': f'{new_ir_k:.4f}',
                    'IR_K_MEASURED': f'{ir_k_measured:.4f}',
                    'RLS_P': f'{new_P:.4f}',
                    'RLS_CONFIDENCE': f'{self.rls_ir_k.confidence:.3f}',
                    'SAMPLE_COUNT': str(self.rls_ir_k.sample_count),
                })

        # No _safe_save here: sag/IR data is ephemeral — persisted on next discharge
        # complete or graceful shutdown. Avoids unnecessary SSD write on every OL→OB.
        logger.info(f"Voltage sag: {self.v_before_sag:.2f}V → {v_sag:.2f}V, "
                    f"R_internal={r_ohm*1000:.1f}mΩ at {load:.1f}% load")

    def _signal_handler(self, signum, frame):
        """Handle SIGTERM/SIGINT: persist model, then stop polling loop."""
        logger.info(f"Received signal {signum}; shutting down")
        try:
            self.battery_model.save()
            logger.info("Model saved before shutdown")
        except Exception as e:
            logger.error(f"Failed to save model on shutdown: {e}")
        self.running = False

    def _update_ema(self, ups_data):
        """Feed voltage/load into EMA filter, log stabilization event."""
        voltage = ups_data.get('battery.voltage')
        load = ups_data.get('ups.load')
        if voltage is None or load is None:
            return None, None

        # F7: Voltage bounds check (8.0-15.0V) and load bounds check (0-100%)
        if not (8.0 <= voltage <= 15.0):
            logger.warning(f"Voltage {voltage:.2f}V out of bounds [8.0-15.0V]; skipping sample")
            return None, None
        if not (0 <= load <= 100):
            logger.warning(f"Load {load:.1f}% out of bounds [0-100%]; skipping sample")
            return None, None

        self.ema_buffer.add_sample(voltage, load)
        self.poll_count += 1
        if self.ema_buffer.stabilized and not self._was_stabilized:
            logger.info(f"EMA buffer stabilized after {self.poll_count} samples, IR compensation active")
            self._was_stabilized = True
        return voltage, load

    def _classify_event(self, ups_data):
        """Classify UPS event and log transitions."""
        ups_status = ups_data.get('ups.status')
        input_voltage = ups_data.get('input.voltage')
        if ups_status is None or input_voltage is None:
            return
        event_type = self.event_classifier.classify(ups_status, input_voltage)
        self.current_metrics.event_type = event_type
        self.current_metrics.transition_occurred = self.event_classifier.transition_occurred
        if self.event_classifier.transition_occurred:
            logger.info(f"Event transition: → {event_type.name}")

    def _track_voltage_sag(self, voltage):
        """Measure voltage sag on OL→OB transition to estimate internal resistance.

        State machine: IDLE → MEASURING → COMPLETE → IDLE.
        MEASURING enables fast polling (1s instead of 10s) for precise sag capture.
        """
        event_type = self.current_metrics.event_type

        # OL→OB: start measuring
        if self.event_classifier.transition_occurred and event_type not in (EventType.ONLINE,):
            self.v_before_sag = self.ema_buffer.voltage
            self.sag_buffer = []
            self.sag_state = SagState.MEASURING

        # OB→OL: cancel if still measuring (power restored before enough samples)
        if self.event_classifier.transition_occurred and event_type == EventType.ONLINE:
            if self.sag_state == SagState.MEASURING:
                self.sag_state = SagState.IDLE

        # Collect samples during MEASURING
        if self.sag_state == SagState.MEASURING:
            self.sag_buffer.append(voltage)
            if len(self.sag_buffer) >= SAG_SAMPLES_REQUIRED:  # 5 samples → median of last 3
                v_sag = sorted(self.sag_buffer[-3:])[1]
                self._record_voltage_sag(v_sag, event_type)
                self.sag_state = SagState.COMPLETE

    def _track_discharge(self, voltage, timestamp):
        """Accumulate discharge samples (voltage/time/load) and write calibration points.

        Implements discharge cooldown logic: OB→OL→OB within 60s is treated as a single
        discharge event, not two separate events. Only clear buffer after 60s confirmed OL.
        """
        event_type = self.current_metrics.event_type
        previous_event = self.current_metrics.previous_event_type

        # Handle cooldown state transitions
        if event_type not in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST):
            # We are now in OL (online) state
            if previous_event in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST):
                # OB→OL transition detected: start cooldown
                logger.info("Power loss detected; starting 60s discharge cooldown")
                self.discharge_buffer_clear_countdown = 60

        if event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST):
            # We are in OB (blackout) state
            if self.discharge_buffer_clear_countdown is not None:
                # Power restored during cooldown
                logger.info("Power restored during cooldown; treating as discharge continuation")
                self.discharge_buffer_clear_countdown = None  # Cancel cooldown, keep buffer

        # Count down cooldown timer on each poll (POLL_INTERVAL = config.polling_interval)
        if self.discharge_buffer_clear_countdown is not None:
            self.discharge_buffer_clear_countdown -= self.config.polling_interval
            if self.discharge_buffer_clear_countdown <= 0:
                logger.info("Cooldown expired (60s OL confirmed); clearing discharge buffer and calling _update_battery_health")
                self._update_battery_health()  # Triggers SoH update and buffer clear
                return  # Early exit; _update_battery_health already clears buffer

        # Standard discharge collection logic
        if event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST):
            if not self.discharge_buffer.collecting:
                self.discharge_buffer.collecting = True
                self.discharge_buffer.voltages = []
                self.discharge_buffer.times = []
                self.discharge_buffer.loads = []
                self._discharge_start_time = timestamp
                self.battery_model.increment_cycle_count()
                # Snapshot predicted runtime at OB start for prediction error logging
                if self.ema_buffer.stabilized and self.current_metrics.time_rem_minutes is not None:
                    self._discharge_predicted_runtime = self.current_metrics.time_rem_minutes
                else:
                    self._discharge_predicted_runtime = None
                logger.info(f"Starting discharge buffer collection ({event_type.name}), "
                            f"cycle #{self.battery_model.get_cycle_count()}")
            if voltage is not None:
                if len(self.discharge_buffer.voltages) >= DISCHARGE_BUFFER_MAX_SAMPLES:
                    logger.warning(f"Discharge buffer capped at {DISCHARGE_BUFFER_MAX_SAMPLES} samples")
                else:
                    self.discharge_buffer.voltages.append(voltage)
                    self.discharge_buffer.times.append(timestamp)
                    load = self.ema_buffer.load if self.ema_buffer.load is not None else 0.0
                    self.discharge_buffer.loads.append(load)
                self._write_calibration_points(event_type)
        else:
            if self.discharge_buffer.collecting:
                # Track cumulative on-battery time
                if self._discharge_start_time is not None:
                    on_battery_sec = timestamp - self._discharge_start_time
                    self.battery_model.add_on_battery_time(on_battery_sec)
                    self._discharge_start_time = None
                self.discharge_buffer.collecting = False
                self.calibration_last_written_index = 0

    def _write_calibration_points(self, event_type):
        """Flush accumulated discharge points to LUT every 6 polls during any blackout."""
        reporting_interval_polls = self.config.reporting_interval // self.config.polling_interval
        if len(self.discharge_buffer.voltages) - self.calibration_last_written_index < reporting_interval_polls:
            return
        for i in range(self.calibration_last_written_index, len(self.discharge_buffer.voltages)):
            try:
                v = self.discharge_buffer.voltages[i]
                t = self.discharge_buffer.times[i]
                soc_est = soc_from_voltage(v, self.battery_model.get_lut())
                self.battery_model.calibration_write(v, soc_est, t)
                self.calibration_last_written_index = i + 1
            except Exception as e:
                logger.error(f"Calibration write failed at index {i}: {e}")
                break

        # Batch flush: persist all accumulated points once per REPORTING_INTERVAL (60x SSD wear reduction)
        points_written = self.calibration_last_written_index
        if points_written > 0:
            try:
                self.battery_model.calibration_batch_flush()
                logger.info(f"Batch flushed {points_written} calibration points to disk")
            except Exception as e:
                logger.error(f"Calibration batch flush failed: {e}")

    def _compute_metrics(self):
        """Calculate SoC, charge%, and runtime from EMA values. Returns (battery_charge, time_rem)."""
        v_ema = self.ema_buffer.voltage
        l_ema = self.ema_buffer.load
        if not self.ema_buffer.stabilized:
            return None, None

        v_norm = ir_compensate(v_ema, l_ema, self.ir_l_base, self.ir_k)
        if v_norm is None:
            return None, None
        self._last_v_norm = v_norm

        soc = soc_from_voltage(v_norm, self.battery_model.get_lut())
        battery_charge = charge_percentage(soc)
        time_rem = runtime_minutes(
            soc, l_ema,
            self.battery_model.get_capacity_ah(),
            self.battery_model.get_soh(),
            peukert_exponent=self.battery_model.get_peukert_exponent(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            nominal_power_watts=self.battery_model.get_nominal_power_watts()
        )

        self.current_metrics.soc = soc
        self.current_metrics.battery_charge = battery_charge
        self.current_metrics.time_rem_minutes = time_rem
        self.current_metrics.timestamp = time.time()

        # Log significant changes
        if self._last_logged_soc is None or abs(soc - self._last_logged_soc) > 0.05:
            if self._last_logged_soc is not None:
                logger.info(f"SoC updated: {self._last_logged_soc*100:.0f}% → {soc*100:.0f}%")
            else:
                logger.info(f"SoC initial: {soc*100:.0f}%")
            self._last_logged_soc = soc
        if self._last_logged_time_rem is None or abs(time_rem - self._last_logged_time_rem) > 1.0:
            logger.info(f"Remaining runtime: {time_rem:.1f} minutes")
            self._last_logged_time_rem = time_rem

        return battery_charge, time_rem

    def _log_status(self, battery_charge, time_rem, poll_latency_ms=None):
        """Log periodic status line with all key metrics."""
        v_ema = self.ema_buffer.voltage
        l_ema = self.ema_buffer.load
        v_norm = getattr(self, '_last_v_norm', None)

        v_norm_str = f"{v_norm:.2f}V" if v_norm is not None else "N/A"
        charge_str = f"{battery_charge}%" if battery_charge is not None else "N/A"
        time_rem_str = f"{time_rem:.1f}min" if time_rem is not None else "N/A"
        event_type = self.current_metrics.event_type
        event_str = event_type.name if event_type else "N/A"
        discharge_depth = len(self.discharge_buffer.voltages)
        latency_str = f"{poll_latency_ms:.0f}ms" if poll_latency_ms is not None else "N/A"
        logger.info(
            f"Poll {self.poll_count}: V_ema={v_ema:.2f}V, L_ema={l_ema:.1f}%, "
            f"V_norm={v_norm_str}, charge={charge_str}, time_rem={time_rem_str}, "
            f"event={event_str}, stabilized={self.ema_buffer.stabilized}, "
            f"nut_latency={latency_str}, discharge_buf={discharge_depth}"
        )

    def _write_virtual_ups(self, ups_data, battery_charge, time_rem):
        """Write computed metrics to tmpfs for NUT dummy-ups driver."""
        try:
            ups_status_override = self.current_metrics.ups_status_override or ups_data.get("ups.status", "OL")
            # Enterprise-equivalent metrics computed from discharge history
            soh = self.battery_model.get_soh()
            install_date = self.battery_model.get_battery_install_date() or ""
            cycle_count = self.battery_model.get_cycle_count()
            cumulative_sec = self.battery_model.get_cumulative_on_battery_sec()
            replacement_due = self.battery_model.get_replacement_due() or ""
            # R_internal: export only after ≥3 non-zero measurements (noise rejection).
            # Early sag readings often show 0.0mΩ (no delta) or outliers from quick tests.
            # Before enough data: omit field entirely (nut_exporter exports missing fields as 0).
            r_internal_history = self.battery_model.get_r_internal_history()
            nonzero = [e["r_ohm"] for e in r_internal_history if e["r_ohm"] > 0]
            r_internal_mohm = round(sorted(nonzero)[len(nonzero) // 2] * 1000, 1) if len(nonzero) >= 3 else None

            virtual_metrics = {
                "battery.runtime": int(time_rem * 60) if time_rem is not None else int(float(ups_data.get("battery.runtime", 0))),
                "battery.charge": int(battery_charge) if battery_charge is not None else int(float(ups_data.get("battery.charge", 0))),
                "ups.status": ups_status_override,
                # Enterprise-equivalent fields
                "battery.health": round(soh * 100),          # State of Health as % (like APC upsAdvBatteryHealthStatus)
                "battery.date": install_date,               # Battery install date (like APC battery.date)
                "battery.cycle.count": cycle_count,         # OL→OB transfers (like Eaton)
                "battery.cumulative.runtime": int(cumulative_sec),  # Total seconds on battery (like Eaton)
                "battery.replacement.due": replacement_due,           # Predicted replacement due date (regression)
                **({"battery.internal_resistance": r_internal_mohm} if r_internal_mohm is not None else {}),
                **{k: v for k, v in ups_data.items()
                   if k not in ["battery.runtime", "battery.charge", "ups.status"]}
            }
            write_virtual_ups_dev(virtual_metrics)
        except Exception as e:
            logger.error(f"Failed to write virtual UPS metrics: {e}")

    def _poll_once(self) -> None:
        """
        Execute a single poll cycle: fetch UPS data, update metrics, write outputs.

        This method encapsulates the body of the try-block in run(), allowing
        clean separation of error handling and polling logic.
        """
        timestamp = time.time()
        ups_data = self.nut_client.get_ups_vars()
        poll_latency_ms = (time.time() - timestamp) * 1000

        self._consecutive_errors = 0  # Reset on successful NUT poll

        # F12: Log startup timing on first successful poll
        if not hasattr(self, '_startup_logged'):
            startup_delta_ms = (time.monotonic() - self._startup_time) * 1000
            logger.info(f"First successful poll completed: startup took {startup_delta_ms:.0f}ms")
            self._startup_logged = True
        voltage, load = self._update_ema(ups_data)
        if voltage is None:
            logger.warning(f"Poll {self.poll_count}: Missing voltage or load data")
            time.sleep(self.config.polling_interval)
            return

        self._classify_event(ups_data)
        self._track_voltage_sag(voltage)
        self._track_discharge(voltage, timestamp)

        # Extract event type after classification to determine polling frequency
        event_type = self.current_metrics.event_type
        is_discharging = event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)

        # F13 fix: Event transition handling runs EVERY poll (not gated)
        self._handle_event_transition()
        self.current_metrics.previous_event_type = self.current_metrics.event_type or EventType.ONLINE

        # State-dependent gate: every poll during OB, every 6 polls during OL
        reporting_interval_polls = self.config.reporting_interval // self.config.polling_interval
        if is_discharging or self.poll_count % reporting_interval_polls == 0:
            logger.debug(f"Metrics gate: is_discharging={is_discharging}, poll_count={self.poll_count}")
            battery_charge, time_rem = self._compute_metrics()
            self._log_status(battery_charge, time_rem, poll_latency_ms)
            self._write_virtual_ups(ups_data, battery_charge, time_rem)

        # Write health endpoint for external monitoring (every poll)
        # Extract capacity metrics from battery model for Phase 14 reporting
        convergence_status = self.battery_model.get_convergence_status()
        _write_health_endpoint(
            soc_percent=(self.current_metrics.soc or 0.0) * 100.0,
            is_online=(self.current_metrics.ups_status_override == "OL"),
            poll_latency_ms=poll_latency_ms,
            capacity_ah_measured=convergence_status.get('latest_ah'),
            capacity_ah_rated=convergence_status.get('rated_ah', 7.2),
            capacity_confidence=convergence_status.get('confidence_percent', 0.0) / 100.0,  # Convert % to 0–1 range
            capacity_samples_count=convergence_status.get('sample_count', 0),
            capacity_converged=convergence_status.get('converged', False)
        )

        # F11 fix: Report healthy to systemd AFTER critical writes succeed
        sd_notify('WATCHDOG=1')
        time.sleep(1 if self.sag_state == SagState.MEASURING else self.config.polling_interval)

    def run(self):
        """
        Main polling loop.

        Polls UPS every POLL_INTERVAL seconds, processes data through the
        pipeline: EMA → event classification → sag/discharge tracking →
        metrics → virtual UPS output. Runs until SIGTERM/SIGINT.

        Metrics write frequency is state-dependent: every poll during OB state,
        every 6 polls (~60s) during OL state.
        """
        sd_notify('READY=1')
        logger.info("Starting main polling loop")
        self.poll_count = 0
        self._was_stabilized = False
        self._consecutive_errors = 0
        self._startup_time = time.monotonic()  # F12: Startup timing marker

        while self.running:
            try:
                self._poll_once()
            except KeyboardInterrupt:
                logger.info("Interrupted by user")
                break
            except Exception as e:
                self._consecutive_errors += 1
                # Reset sag state so we don't get stuck in 1s sleep on persistent errors
                self.sag_state = SagState.IDLE
                # Rate-limit: full traceback for first 10, then summary every 6th (~60s)
                reporting_interval_polls = self.config.reporting_interval // self.config.polling_interval
                if self._consecutive_errors <= ERROR_LOG_BURST or self._consecutive_errors % reporting_interval_polls == 0:
                    logger.error(f"Error in polling loop ({self._consecutive_errors} consecutive): {e}",
                                 exc_info=(self._consecutive_errors <= ERROR_LOG_BURST))
                time.sleep(self.config.polling_interval)

        logger.info("Polling loop ended; daemon shutting down")


def parse_args(args=None):
    """Parse command-line arguments.

    Args:
        args: List of arguments to parse (defaults to sys.argv[1:] if None)
              Used by tests to inject specific argument sequences.

    Returns:
        Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        description="UPS Battery Monitor Daemon",
        prog="ups-battery-monitor"
    )
    parser.add_argument(
        '--new-battery',
        action='store_true',
        help='Signal that a new battery has been installed; daemon will use this for next discharge measurement'
    )
    return parser.parse_args(args)


def main():
    """Entry point for daemon."""
    args = parse_args()

    try:
        config = _load_config()
        daemon = MonitorDaemon(config, new_battery_flag=args.new_battery)
        daemon.run()
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
