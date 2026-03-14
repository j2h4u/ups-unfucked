import time
import signal
import sys
import math
import logging
import argparse
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from datetime import datetime
from typing import Optional
from systemd.journal import JournalHandler
try:
    from systemd.daemon import notify as sd_notify
except ImportError:
    sd_notify = lambda status: None  # No-op when running outside systemd

from src.nut_client import NUTClient
from src.ema_filter import EMAFilter, ir_compensate
from src.model import BatteryModel
from src.soc_predictor import soc_from_voltage, charge_percentage
from src.runtime_calculator import runtime_minutes, peukert_runtime_hours
from src.event_classifier import EventClassifier, EventType
from src.virtual_ups import write_virtual_ups_dev, compute_ups_status_override
from src import soh_calculator, replacement_predictor, alerter
from src.soh_calculator import interpolate_cliff_region

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

# User-configurable (from config.toml)
_CONFIGURABLE_DEFAULTS = {
    'ups_name': 'cyberpower',
    'shutdown_minutes': 5,
    'soh_alert': 0.80,
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
        reference_load_percent=REFERENCE_LOAD_PERCENT,
        ema_window_sec=EMA_WINDOW,
    )


# Default config instance (used by non-daemon code like scripts/battery-health.py)
# Daemon code receives config via __init__ parameter
_default_config = _load_config()

# Module-level backward-compat exports for scripts that import from monitor
UPS_NAME = _default_config.ups_name
SHUTDOWN_THRESHOLD_MINUTES = _default_config.shutdown_minutes
SOH_THRESHOLD = _default_config.soh_alert_threshold

MODEL_DIR = CONFIG_DIR
MODEL_PATH = MODEL_DIR / 'model.json'


def _safe_save(model: BatteryModel) -> None:
    """Save model to disk, log errors gracefully if disk full.

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

# Setup alerter logger (Phase 4) for health monitoring
ups_logger = alerter.setup_ups_logger("ups-battery-monitor")


class MonitorDaemon:
    """
    Main daemon for UPS battery monitoring.

    Polls NUT upsd, applies EMA smoothing, tracks battery state.
    """

    def __init__(self, config: Config):
        """Initialize daemon with provided configuration.

        Args:
            config: Config dataclass instance with all daemon parameters.
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
        self._validate_model()

        # Set battery install date on first ever startup
        if self.battery_model.get_battery_install_date() is None:
            self.battery_model.set_battery_install_date(datetime.now().strftime('%Y-%m-%d'))
        if not model_path.exists():
            self.battery_model.save()  # Write defaults so tools (battery-health.py, MOTD) can read
        self.event_classifier = EventClassifier()

        # Load physics params from model
        self.ir_k = self.battery_model.get_ir_k()
        self.ir_l_base = self.battery_model.get_ir_reference_load()

        # Metrics tracking for current battery state
        self.current_metrics = CurrentMetrics()
        self._last_logged_soc = None
        self._last_logged_time_rem = None

        # Phase 4: Discharge buffer and health monitoring thresholds
        self.discharge_buffer = {
            'voltages': [],      # Voltage samples during OB state
            'times': [],         # Timestamps relative to discharge start
            'collecting': False  # True while actively collecting (set before data arrives)
        }
        self._discharge_start_time = None  # Timestamp when OL→OB occurred (for cumulative on-battery tracking)
        self.soh_threshold = config.soh_alert_threshold
        self.runtime_threshold_minutes = config.runtime_threshold_minutes
        self.reference_load_percent = config.reference_load_percent

        # Voltage sag measurement for internal resistance tracking
        self.sag_state = SagState.IDLE
        self.v_before_sag = None
        self.sag_buffer = []

        # Phase 6: Track calibration writes
        self.calibration_last_written_index = 0

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
            self.battery_model.data['soh'] = 1.0

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
            # Phase 4: Calculate SoH and check health thresholds
            self._update_battery_health()

            # Refine cliff region (10.5-11.0V) if we have measured data there.
            # No-op if <2 measured points in cliff range — safe to call on every discharge.
            updated_lut = interpolate_cliff_region(self.battery_model.data['lut'])
            if updated_lut != self.battery_model.data['lut']:
                self.battery_model.update_lut_from_calibration(updated_lut)
                logger.info("LUT cliff region updated from measured discharge data")

            # Update model.lut with source="measured" (Phase 6 implementation detail)
            # Recalculate SoH (Phase 6 implementation detail)
            _safe_save(self.battery_model)

    def _update_battery_health(self):
        """
        Called when discharge event completes (OB→OL transition).

        Workflow:
        1. Extract discharge voltage/time series from buffer
        2. Calculate SoH using area-under-curve
        3. Append to soh_history in model.json
        4. Predict replacement date via linear regression
        5. Alert if SoH or runtime thresholds breached
        6. Clear discharge buffer
        """
        # Check discharge buffer has data
        if len(self.discharge_buffer['voltages']) < 2:
            return  # No discharge detected; skip SoH update

        # Phase 4: Calculate SoH from discharge data
        soh_new = soh_calculator.calculate_soh_from_discharge(
            discharge_voltage_series=self.discharge_buffer['voltages'],
            discharge_time_series=self.discharge_buffer['times'],
            reference_soh=self.battery_model.get_soh(),
            anchor_voltage=10.5,
            capacity_ah=self.battery_model.get_capacity_ah(),
            load_percent=self.reference_load_percent,
            nominal_power_watts=self.battery_model.get_nominal_power_watts(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            peukert_exponent=self.battery_model.get_peukert_exponent()
        )

        # Add to history
        today = datetime.now().strftime('%Y-%m-%d')
        self.battery_model.add_soh_history_entry(today, soh_new)
        _safe_save(self.battery_model)

        logger.info(f"SoH calculated: {soh_new:.2%}")

        # Predict replacement date
        result = replacement_predictor.linear_regression_soh(
            soh_history=self.battery_model.get_soh_history(),
            threshold_soh=self.soh_threshold
        )

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
                ups_logger,
                soh_new,
                self.soh_threshold,
                days_to_replacement
            )

        # Alert if runtime at 100% is low
        time_rem_at_100pct = runtime_minutes(
            soc=1.0, load_percent=self.reference_load_percent,
            capacity_ah=self.battery_model.get_capacity_ah(),
            soh=soh_new,
            peukert_exponent=self.battery_model.get_peukert_exponent(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            nominal_power_watts=self.battery_model.get_nominal_power_watts()
        )
        if time_rem_at_100pct < self.runtime_threshold_minutes:
            alerter.alert_runtime_below_threshold(
                ups_logger,
                time_rem_at_100pct,
                self.runtime_threshold_minutes
            )

        # Auto-calibrate Peukert exponent if prediction error > 10%
        self._auto_calibrate_peukert(soh_new)

        # Clear discharge buffer
        self.discharge_buffer = {'voltages': [], 'times': [], 'collecting': False}

    def _auto_calibrate_peukert(self, current_soh: float):
        """
        Auto-calibrate Peukert exponent from actual discharge duration.

        Compares predicted runtime (at current exponent) with actual discharge,
        and adjusts exponent if error exceeds 10%.
        """
        voltages = self.discharge_buffer['voltages']
        times = self.discharge_buffer['times']
        if len(times) < 2:
            logger.debug("Peukert calibration skipped: <2 discharge samples")
            return

        actual_duration_sec = times[-1] - times[0]
        if actual_duration_sec < 60:
            logger.debug(f"Peukert calibration skipped: discharge too short ({actual_duration_sec:.0f}s < 60s)")
            return

        actual_min = actual_duration_sec / 60.0

        # Estimate average load from EMA (best available)
        avg_load = self.ema_buffer.load
        if avg_load is None or avg_load <= 0:
            logger.debug(f"Peukert calibration skipped: invalid load ({avg_load})")
            return

        capacity_ah = self.battery_model.get_capacity_ah()
        current_exp = self.battery_model.get_peukert_exponent()
        nominal_voltage = self.battery_model.get_nominal_voltage()
        nominal_power_watts = self.battery_model.get_nominal_power_watts()

        T_full_hours = peukert_runtime_hours(
            avg_load, capacity_ah, current_exp, nominal_voltage, nominal_power_watts
        )
        if T_full_hours <= 0:
            logger.debug(f"Peukert calibration skipped: T_full_hours={T_full_hours}")
            return

        predicted = T_full_hours * current_soh * 60  # minutes
        error = abs(actual_min - predicted) / predicted
        if error > 0.10:
            I_rated = capacity_ah / 20.0
            I_actual = avg_load / 100.0 * nominal_power_watts / nominal_voltage
            ratio = I_rated / I_actual

            if ratio <= 0 or ratio == 1.0:
                logger.debug(f"Peukert calibration skipped: degenerate current ratio ({ratio:.3f})")
                return

            denom = math.log(ratio)
            if abs(denom) < 1e-10:
                logger.debug("Peukert calibration skipped: log(ratio) ≈ 0")
                return

            new_exp = math.log(actual_min / 60.0 / (20.0 * current_soh)) / denom
            new_exp = max(1.0, min(1.4, new_exp))

            logger.info(f"Peukert exponent calibrated: {current_exp:.3f} → {new_exp:.3f} "
                        f"(predicted={predicted:.1f}min, actual={actual_min:.1f}min)")
            self.battery_model.set_peukert_exponent(new_exp)
            _safe_save(self.battery_model)

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
        _safe_save(self.battery_model)
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
        """Accumulate discharge samples and write calibration points."""
        event_type = self.current_metrics.event_type
        if event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST):
            if not self.discharge_buffer['collecting']:
                self.discharge_buffer['collecting'] = True
                self.discharge_buffer['voltages'] = []
                self.discharge_buffer['times'] = []
                self._discharge_start_time = timestamp
                self.battery_model.increment_cycle_count()
                logger.info(f"Starting discharge buffer collection ({event_type.name}), "
                            f"cycle #{self.battery_model.get_cycle_count()}")
            if voltage is not None:
                if len(self.discharge_buffer['voltages']) >= DISCHARGE_BUFFER_MAX_SAMPLES:
                    logger.warning(f"Discharge buffer capped at {DISCHARGE_BUFFER_MAX_SAMPLES} samples")
                else:
                    self.discharge_buffer['voltages'].append(voltage)
                    self.discharge_buffer['times'].append(timestamp)
                self._write_calibration_points(event_type)
        else:
            if self.discharge_buffer['collecting']:
                # Track cumulative on-battery time
                if self._discharge_start_time is not None:
                    on_battery_sec = timestamp - self._discharge_start_time
                    self.battery_model.add_on_battery_time(on_battery_sec)
                    self._discharge_start_time = None
                self.discharge_buffer['collecting'] = False
                self.calibration_last_written_index = 0

    def _write_calibration_points(self, event_type):
        """Flush accumulated discharge points to LUT every 6 polls during any blackout."""
        reporting_interval_polls = self.config.reporting_interval // self.config.polling_interval
        if len(self.discharge_buffer['voltages']) - self.calibration_last_written_index < reporting_interval_polls:
            return
        for i in range(self.calibration_last_written_index, len(self.discharge_buffer['voltages'])):
            try:
                v = self.discharge_buffer['voltages'][i]
                t = self.discharge_buffer['times'][i]
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
                raise

    def _compute_metrics(self):
        """Calculate SoC, charge%, and runtime from EMA values. Returns (battery_charge, time_rem)."""
        v_ema = self.ema_buffer.voltage
        l_ema = self.ema_buffer.load
        if not self.ema_buffer.stabilized:
            return None, None

        v_norm = ir_compensate(v_ema, l_ema, self.ir_l_base, self.ir_k)
        if v_norm is None:
            return None, None

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

        self.current_metrics.update(
            soc=soc, battery_charge=battery_charge,
            time_rem_minutes=time_rem, timestamp=time.time()
        )

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
        v_norm = ir_compensate(v_ema, l_ema, self.ir_l_base, self.ir_k) if self.ema_buffer.stabilized else None

        v_norm_str = f"{v_norm:.2f}V" if v_norm is not None else "N/A"
        charge_str = f"{battery_charge}%" if battery_charge is not None else "N/A"
        time_rem_str = f"{time_rem:.1f}min" if time_rem is not None else "N/A"
        event_type = self.current_metrics.event_type
        event_str = event_type.name if event_type else "N/A"
        discharge_depth = len(self.discharge_buffer['voltages'])
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
            ups_status_override = self.current_metrics.ups_status_override or "OL"
            # Enterprise-equivalent metrics computed from discharge history
            soh = self.battery_model.get_soh()
            install_date = self.battery_model.get_battery_install_date() or ""
            cycle_count = self.battery_model.get_cycle_count()
            cumulative_sec = self.battery_model.get_cumulative_on_battery_sec()

            virtual_metrics = {
                "battery.runtime": int(time_rem * 60) if time_rem is not None else 0,
                "battery.charge": int(battery_charge) if battery_charge is not None else 0,
                "ups.status": ups_status_override,
                # Enterprise-equivalent fields
                "battery.health": f"{soh:.0%}",            # State of Health (like APC upsAdvBatteryHealthStatus)
                "battery.date": install_date,               # Battery install date (like APC battery.date)
                "battery.cycle.count": cycle_count,         # OL→OB transfers (like Eaton)
                "battery.cumulative.runtime": int(cumulative_sec),  # Total seconds on battery (like Eaton)
                **{k: v for k, v in ups_data.items()
                   if k not in ["battery.runtime", "battery.charge", "ups.status"]}
            }
            write_virtual_ups_dev(virtual_metrics)
        except Exception as e:
            logger.error(f"Failed to write virtual UPS metrics: {e}")

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

        while self.running:
            try:
                timestamp = time.time()
                ups_data = self.nut_client.get_ups_vars()
                poll_latency_ms = (time.time() - timestamp) * 1000

                self._consecutive_errors = 0  # Reset on successful NUT poll
                voltage, load = self._update_ema(ups_data)
                if voltage is None:
                    logger.warning(f"Poll {self.poll_count}: Missing voltage or load data")
                    time.sleep(self.config.polling_interval)
                    continue

                self._classify_event(ups_data)
                self._track_voltage_sag(voltage)
                self._track_discharge(voltage, timestamp)

                # Extract event type after classification to determine polling frequency
                event_type = self.current_metrics.event_type
                is_discharging = event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)

                # State-dependent gate: every poll during OB, every 6 polls during OL
                reporting_interval_polls = self.config.reporting_interval // self.config.polling_interval
                if is_discharging or self.poll_count % reporting_interval_polls == 0:
                    logger.debug(f"Metrics gate: is_discharging={is_discharging}, poll_count={self.poll_count}")
                    battery_charge, time_rem = self._compute_metrics()
                    self._handle_event_transition()
                    self.current_metrics.previous_event_type = self.current_metrics.event_type or EventType.ONLINE
                    self._log_status(battery_charge, time_rem, poll_latency_ms)
                    self._write_virtual_ups(ups_data, battery_charge, time_rem)

                sd_notify('WATCHDOG=1')
                time.sleep(1 if self.sag_state == SagState.MEASURING else self.config.polling_interval)

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


def main():
    """Entry point for daemon."""
    parser = argparse.ArgumentParser(
        description="UPS Battery Monitor Daemon",
        prog="ups-battery-monitor"
    )
    parser.parse_args()

    try:
        config = _load_config()
        daemon = MonitorDaemon(config)
        daemon.run()
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
