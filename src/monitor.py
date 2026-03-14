import time
import signal
import sys
import math
import logging
import argparse
import tomllib
from pathlib import Path
from datetime import datetime
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

# User-configurable (from config.toml)
_CONFIGURABLE_DEFAULTS = {
    'ups_name': 'cyberpower',
    'shutdown_minutes': 5,
    'soh_alert': 0.80,
}


def _load_config():
    """Load user config from TOML, falling back to defaults for missing keys."""
    cfg = {}
    for path in [CONFIG_DIR / 'config.toml', REPO_ROOT / 'config.toml']:
        if path.is_file():
            with open(path, 'rb') as f:
                cfg = tomllib.load(f)
            break
    return {k: cfg.get(k, v) for k, v in _CONFIGURABLE_DEFAULTS.items()}


_cfg = _load_config()

UPS_NAME = _cfg['ups_name']
SHUTDOWN_THRESHOLD_MINUTES = _cfg['shutdown_minutes']
SOH_THRESHOLD = _cfg['soh_alert']

MODEL_DIR = CONFIG_DIR
MODEL_PATH = MODEL_DIR / 'model.json'

from enum import Enum


class SagState(Enum):
    """Voltage sag measurement state machine: IDLE → ARMED → MEASURING → COMPLETE."""
    IDLE = "idle"            # Waiting for OL→OB transition
    MEASURING = "measuring"  # Collecting samples (fast poll active)
    COMPLETE = "complete"    # Sag recorded, back to normal polling


# Internal constants (not user-configurable)
REPORTING_INTERVAL_POLLS = 6          # Log metrics every N polls (6 * 10s = 60s)
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

    def __init__(self):
        """Initialize daemon with TOML configuration."""
        self.running = True
        self.shutdown_threshold_minutes = SHUTDOWN_THRESHOLD_MINUTES

        # Create model directory
        MODEL_DIR.mkdir(parents=True, exist_ok=True)

        # Initialize components
        self.nut_client = NUTClient(
            host=NUT_HOST,
            port=NUT_PORT,
            timeout=NUT_TIMEOUT,
            ups_name=UPS_NAME
        )

        self.ema_buffer = EMAFilter(
            window_sec=EMA_WINDOW,
            poll_interval_sec=POLL_INTERVAL
        )

        self.battery_model = BatteryModel(MODEL_PATH)
        self._validate_model()

        # Set battery install date on first ever startup
        if self.battery_model.get_battery_install_date() is None:
            self.battery_model.set_battery_install_date(datetime.now().strftime('%Y-%m-%d'))
        if not MODEL_PATH.exists():
            self.battery_model.save()  # Write defaults so tools (battery-health.py, MOTD) can read
        self.event_classifier = EventClassifier()

        # Load physics params from model
        self.ir_k = self.battery_model.get_ir_k()
        self.ir_l_base = self.battery_model.get_ir_reference_load()

        # Metrics tracking for current battery state
        self.current_metrics = {
            "soc": None,
            "battery_charge": None,
            "time_rem_minutes": None,
            "event_type": None,
            "transition_occurred": False,
            "shutdown_imminent": False,
            "ups_status_override": None,
            "previous_event_type": EventType.ONLINE,
            "timestamp": None,
        }
        self._last_logged_soc = None
        self._last_logged_time_rem = None

        # Phase 4: Discharge buffer and health monitoring thresholds
        self.discharge_buffer = {
            'voltages': [],      # Voltage samples during OB state
            'times': [],         # Timestamps relative to discharge start
            'collecting': False  # True while actively collecting (set before data arrives)
        }
        self._discharge_start_time = None  # Timestamp when OL→OB occurred (for cumulative on-battery tracking)
        self.soh_threshold = SOH_THRESHOLD
        self.runtime_threshold_minutes = RUNTIME_THRESHOLD_MINUTES
        self.reference_load_percent = REFERENCE_LOAD_PERCENT

        # Voltage sag measurement for internal resistance tracking
        self.sag_state = SagState.IDLE
        self.v_before_sag = None
        self.sag_buffer = []

        # Phase 6: Track calibration writes
        self.calibration_last_written_index = 0

        # Signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        logger.info(f"Daemon initialized: shutdown_threshold={self.shutdown_threshold_minutes}min, poll={POLL_INTERVAL}s, model={MODEL_PATH}, nut={NUT_HOST}:{NUT_PORT}")

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
            logger.warning(f"NUT upsd unreachable at startup, will retry every {POLL_INTERVAL}s")

    def _handle_event_transition(self):
        """
        Execute actions based on event transitions.

        Implements EVT-02 (blackout), EVT-03 (test), EVT-04 (status arbiter),
        and EVT-05 (model update on discharge completion).
        """
        event_type = self.current_metrics["event_type"]
        previous_event_type = self.current_metrics["previous_event_type"]

        # EVT-02: Real blackout - prepare shutdown signal
        if event_type == EventType.BLACKOUT_REAL:
            time_rem = self.current_metrics.get("time_rem_minutes")
            if time_rem is not None and time_rem < self.shutdown_threshold_minutes:
                logger.warning(
                    f"Real blackout: time_rem={time_rem:.1f}min < threshold {self.shutdown_threshold_minutes}min; "
                    f"prepare LB flag"
                )
                self.current_metrics["shutdown_imminent"] = True
            else:
                self.current_metrics["shutdown_imminent"] = False

        # EVT-03: Battery test - suppress shutdown
        if event_type == EventType.BLACKOUT_TEST:
            logger.info("Battery test detected; collecting calibration data, no shutdown")
            self.current_metrics["shutdown_imminent"] = False

        # EVT-04: Status arbitration via compute_ups_status_override()
        self.current_metrics["ups_status_override"] = compute_ups_status_override(
            event_type,
            self.current_metrics.get("time_rem_minutes", 0) or 0,
            self.shutdown_threshold_minutes
        )

        # EVT-05: Model update on OB→OL transition
        if (self.current_metrics.get("transition_occurred") and
            event_type == EventType.ONLINE and
            previous_event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)):
            logger.info("Power restored; updating LUT with measured discharge points")
            # Phase 4: Calculate SoH and check health thresholds
            self._update_battery_health()

            # Refine cliff region (10.5-11.0V) if we have measured data there.
            # No-op if <2 measured points in cliff range — safe to call on every discharge.
            from src.soh_calculator import interpolate_cliff_region
            updated_lut = interpolate_cliff_region(self.battery_model.data['lut'])
            if updated_lut != self.battery_model.data['lut']:
                self.battery_model.update_lut_from_calibration(updated_lut)
                logger.info("LUT cliff region updated from measured discharge data")

            # Update model.lut with source="measured" (Phase 6 implementation detail)
            # Recalculate SoH (Phase 6 implementation detail)
            try:
                self.battery_model.save()
            except OSError as e:
                logger.error(f"Failed to persist model (disk full?): {e}")

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
        try:
            self.battery_model.save()
        except OSError as e:
            logger.error(f"Failed to persist model (disk full?): {e}")

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
            try:
                self.battery_model.save()
            except OSError as e:
                logger.error(f"Failed to persist model (disk full?): {e}")

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
        try:
            self.battery_model.save()
        except OSError as e:
            logger.error(f"Failed to persist model (disk full?): {e}")
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
        self.current_metrics["event_type"] = event_type
        self.current_metrics["transition_occurred"] = self.event_classifier.transition_occurred
        if self.event_classifier.transition_occurred:
            logger.info(f"Event transition: → {event_type.name}")

    def _track_voltage_sag(self, voltage):
        """Measure voltage sag on OL→OB transition to estimate internal resistance.

        State machine: IDLE → MEASURING → COMPLETE → IDLE.
        MEASURING enables fast polling (1s instead of 10s) for precise sag capture.
        """
        event_type = self.current_metrics.get("event_type")

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
        event_type = self.current_metrics.get("event_type")
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
        if len(self.discharge_buffer['voltages']) - self.calibration_last_written_index < REPORTING_INTERVAL_POLLS:
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
        event_type = self.current_metrics.get("event_type")
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
            ups_status_override = self.current_metrics.get("ups_status_override", "OL")
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
                    time.sleep(POLL_INTERVAL)
                    continue

                self._classify_event(ups_data)
                self._track_voltage_sag(voltage)
                self._track_discharge(voltage, timestamp)

                # Every 6 polls (~60s): compute metrics, handle events, write virtual UPS
                if self.poll_count % REPORTING_INTERVAL_POLLS == 0:
                    battery_charge, time_rem = self._compute_metrics()
                    self._handle_event_transition()
                    self.current_metrics["previous_event_type"] = self.current_metrics.get(
                        "event_type", EventType.ONLINE
                    )
                    self._log_status(battery_charge, time_rem, poll_latency_ms)
                    self._write_virtual_ups(ups_data, battery_charge, time_rem)

                sd_notify('WATCHDOG=1')
                time.sleep(1 if self.sag_state == SagState.MEASURING else POLL_INTERVAL)

            except KeyboardInterrupt:
                logger.info("Interrupted by user")
                break
            except Exception as e:
                self._consecutive_errors += 1
                # Reset sag state so we don't get stuck in 1s sleep on persistent errors
                self.sag_state = SagState.IDLE
                # Rate-limit: full traceback for first 10, then summary every 6th (~60s)
                if self._consecutive_errors <= ERROR_LOG_BURST or self._consecutive_errors % REPORTING_INTERVAL_POLLS == 0:
                    logger.error(f"Error in polling loop ({self._consecutive_errors} consecutive): {e}",
                                 exc_info=(self._consecutive_errors <= ERROR_LOG_BURST))
                time.sleep(POLL_INTERVAL)

        logger.info("Polling loop ended; daemon shutting down")


def main():
    """Entry point for daemon."""
    parser = argparse.ArgumentParser(
        description="UPS Battery Monitor Daemon",
        prog="ups-battery-monitor"
    )
    parser.parse_args()

    try:
        daemon = MonitorDaemon()
        daemon.run()
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
