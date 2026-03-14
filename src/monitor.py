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

# === INLINE LOGGING SETUP (H3 fix: removed src/logger.py) ===
logger = logging.getLogger('ups-battery-monitor')
logger.setLevel(logging.INFO)
logger.handlers.clear()

try:
    handler = JournalHandler()
    handler.setFormatter(logging.Formatter('[ups-battery-monitor] %(levelname)s: %(message)s'))
    logger.addHandler(handler)
except Exception as e:
    # Fallback to stderr
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter('[ups-battery-monitor] %(levelname)s: %(message)s'))
    logger.addHandler(handler)

# Setup alerter logger (Phase 4) for health monitoring
ups_logger = alerter.setup_ups_logger("ups-battery-monitor")


class MonitorDaemon:
    """
    Main daemon for UPS battery monitoring.

    Polls NUT upsd, applies EMA smoothing, tracks battery state.
    """

    def __init__(self, calibration_mode=False):
        """
        Initialize daemon with inline configuration.

        Args:
            calibration_mode: If True, uses 1-minute shutdown threshold for battery discharge testing
        """
        self.running = True
        self.calibration_mode = calibration_mode

        # Set shutdown threshold based on mode
        self.shutdown_threshold_minutes = 1 if calibration_mode else SHUTDOWN_THRESHOLD_MINUTES

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
        self.last_soc = None
        self.last_time_rem = None

        # Phase 4: Discharge buffer and health monitoring thresholds
        # Max ~3 hours of discharge at 10s poll. Prevents unbounded growth if UPS stuck in OB.
        self.DISCHARGE_BUFFER_MAX_SAMPLES = 1000
        self.discharge_buffer = {
            'voltages': [],      # Voltage samples during OB state
            'times': [],         # Timestamps relative to discharge start
            'collecting': False  # True while actively collecting (set before data arrives)
        }
        self.soh_threshold = SOH_THRESHOLD
        self.runtime_threshold_minutes = RUNTIME_THRESHOLD_MINUTES
        self.reference_load_percent = REFERENCE_LOAD_PERCENT

        # Voltage sag measurement for internal resistance tracking
        self.v_before_sag = None
        self.sag_buffer = []
        self.sag_collected = False
        self.fast_poll_active = False

        # Phase 6: Track calibration writes
        self.calibration_last_written_index = 0

        # Signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        logger.info(f"Daemon initialized: calibration_mode={self.calibration_mode}, shutdown_threshold={self.shutdown_threshold_minutes}min, poll={POLL_INTERVAL}s, model={MODEL_PATH}, nut={NUT_HOST}:{NUT_PORT}")

        # H1 fix: Check NUT connectivity at startup
        self._check_nut_connectivity()

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

            # Phase 6 Wave 1: Calibration mode - interpolate cliff region on OB→OL in BLACKOUT_TEST
            if self.calibration_mode and previous_event_type == EventType.BLACKOUT_TEST:
                from src.soh_calculator import interpolate_cliff_region
                updated_lut = interpolate_cliff_region(self.battery_model.data['lut'])
                self.battery_model.update_lut_from_calibration(updated_lut)
                logger.warning("Calibration complete; remove --calibration-mode for normal operation")

            # Update model.lut with source="measured" (Phase 6 implementation detail)
            # Recalculate SoH (Phase 6 implementation detail)
            self.battery_model.save()  # Persist updated model to disk

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
        self.battery_model.save()

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
            self.battery_model.save()

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
        self.battery_model.save()
        logger.info(f"Voltage sag: {self.v_before_sag:.2f}V → {v_sag:.2f}V, "
                    f"R_internal={r_ohm*1000:.1f}mΩ at {load:.1f}% load")

    def _signal_handler(self, signum, frame):
        """Handle SIGTERM/SIGINT gracefully."""
        logger.info(f"Received signal {signum}; shutting down")
        self.running = False

    def run(self):
        """
        Main polling loop.

        Polls UPS every POLL_INTERVAL seconds, updates EMA, logs metrics.
        Runs until SIGTERM or SIGINT received.
        """
        logger.info("Starting main polling loop")
        poll_count = 0
        was_stabilized = False

        while self.running:
            try:
                # Poll UPS
                timestamp = time.time()
                ups_data = self.nut_client.get_ups_vars()

                # Extract voltage and load
                voltage = ups_data.get('battery.voltage')
                load = ups_data.get('ups.load')

                if voltage is not None and load is not None:
                    # Add to EMA buffer
                    self.ema_buffer.add_sample(voltage, load)
                    poll_count += 1

                    # L1 fix: Log when EMA stabilizes
                    if self.ema_buffer.stabilized and not was_stabilized:
                        logger.info(f"EMA buffer stabilized after {poll_count} samples, IR compensation active")
                        was_stabilized = True

                    # Classify event based on UPS status and input voltage
                    ups_status = ups_data.get('ups.status')
                    input_voltage = ups_data.get('input.voltage')
                    if ups_status is not None and input_voltage is not None:
                        event_type = self.event_classifier.classify(ups_status, input_voltage)
                        self.current_metrics["event_type"] = event_type
                        self.current_metrics["transition_occurred"] = self.event_classifier.transition_occurred

                        # Log transitions
                        if self.event_classifier.transition_occurred:
                            logger.info(f"Event transition: → {event_type.name}")

                        # Voltage sag: start fast poll on ONLINE → OB transition
                        if self.event_classifier.transition_occurred and event_type not in (EventType.ONLINE,):
                            self.v_before_sag = self.ema_buffer.voltage
                            self.sag_buffer = []
                            self.sag_collected = False
                            self.fast_poll_active = True

                        # Voltage sag: stop fast poll on OB → ONLINE
                        if self.event_classifier.transition_occurred and event_type == EventType.ONLINE:
                            self.fast_poll_active = False

                        # Collect sag samples during fast poll
                        if self.fast_poll_active and not self.sag_collected:
                            self.sag_buffer.append(voltage)
                            if len(self.sag_buffer) >= 5:
                                recent = sorted(self.sag_buffer[-3:])
                                v_sag = recent[1]  # median of 3
                                self._record_voltage_sag(v_sag, event_type)
                                self.sag_collected = True
                                self.fast_poll_active = False

                        # Phase 6: Collect discharge data during BLACKOUT_REAL or BLACKOUT_TEST
                        if event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST):
                            if not self.discharge_buffer['collecting']:
                                self.discharge_buffer['collecting'] = True
                                self.discharge_buffer['voltages'] = []
                                self.discharge_buffer['times'] = []
                                logger.info(f"Starting discharge buffer collection ({event_type.name})")

                            # Append voltage and timestamp to buffer (raw voltage, not normalized)
                            if voltage is not None:
                                if len(self.discharge_buffer['voltages']) >= self.DISCHARGE_BUFFER_MAX_SAMPLES:
                                    logger.warning(f"Discharge buffer capped at {self.DISCHARGE_BUFFER_MAX_SAMPLES} samples")
                                else:
                                    self.discharge_buffer['voltages'].append(voltage)
                                    self.discharge_buffer['times'].append(timestamp)

                                # Phase 6: Write calibration points every 6 polls (~60 seconds) if calibration mode
                                if self.calibration_mode and event_type == EventType.BLACKOUT_TEST:
                                    if len(self.discharge_buffer['voltages']) - self.calibration_last_written_index >= 6:
                                        # Write accumulated points to model, advancing index per successful write
                                        for i in range(self.calibration_last_written_index, len(self.discharge_buffer['voltages'])):
                                            try:
                                                v = self.discharge_buffer['voltages'][i]
                                                t = self.discharge_buffer['times'][i]
                                                soc_est = soc_from_voltage(v, self.battery_model.get_lut())
                                                self.battery_model.calibration_write(v, soc_est, t)
                                                self.calibration_last_written_index = i + 1
                                            except Exception as e:
                                                logger.error(f"Calibration write failed at index {i}: {e}")
                                                break  # Stop batch on first error, resume from this index next time
                        else:
                            # Not in blackout; clear buffer on next event transition
                            if self.discharge_buffer['collecting']:
                                self.discharge_buffer['collecting'] = False
                                self.calibration_last_written_index = 0

                    # Log every 6 polls (60 seconds at 10-sec interval)
                    if poll_count % 6 == 0:
                        v_ema = self.ema_buffer.voltage
                        l_ema = self.ema_buffer.load
                        stabilized = self.ema_buffer.stabilized

                        # Apply IR compensation if stabilized
                        if stabilized:
                            v_norm = ir_compensate(
                                v_ema, l_ema,
                                self.ir_l_base,
                                self.ir_k
                            )
                        else:
                            v_norm = None

                        # Calculate SoC and battery charge from normalized voltage
                        if stabilized and v_norm is not None:
                            soc = soc_from_voltage(v_norm, self.battery_model.get_lut())
                            battery_charge = charge_percentage(soc)

                            # Calculate remaining runtime using SoC and load
                            capacity_ah = self.battery_model.get_capacity_ah()
                            soh = self.battery_model.get_soh()
                            time_rem = runtime_minutes(
                                soc, l_ema, capacity_ah, soh,
                                peukert_exponent=self.battery_model.get_peukert_exponent(),
                                nominal_voltage=self.battery_model.get_nominal_voltage(),
                                nominal_power_watts=self.battery_model.get_nominal_power_watts()
                            )

                            # Store metrics
                            self.current_metrics["soc"] = soc
                            self.current_metrics["battery_charge"] = battery_charge
                            self.current_metrics["time_rem_minutes"] = time_rem
                            self.current_metrics["timestamp"] = timestamp

                            # Log significant SoC changes (>5%)
                            if self.last_soc is None or abs(soc - self.last_soc) > 0.05:
                                if self.last_soc is not None:
                                    logger.info(f"SoC updated: {self.last_soc*100:.0f}% → {soc*100:.0f}%")
                                else:
                                    logger.info(f"SoC initial: {soc*100:.0f}%")
                                self.last_soc = soc

                            # Log runtime changes
                            if self.last_time_rem is None or abs(time_rem - self.last_time_rem) > 1.0:
                                logger.info(f"Remaining runtime: {time_rem:.1f} minutes")
                                self.last_time_rem = time_rem
                        else:
                            battery_charge = None
                            time_rem = None

                        # Handle event-driven logic
                        self._handle_event_transition()

                        # Update previous event type for transition tracking
                        self.current_metrics["previous_event_type"] = self.current_metrics.get(
                            "event_type", EventType.ONLINE
                        )

                        v_norm_str = f"{v_norm:.2f}V" if v_norm is not None else "N/A"
                        charge_str = f"{battery_charge}%" if battery_charge is not None else "N/A"
                        time_rem_str = f"{time_rem:.1f}min" if time_rem is not None else "N/A"
                        event_str = self.current_metrics.get("event_type", "UNKNOWN").name if self.current_metrics.get("event_type") else "N/A"
                        logger.info(
                            f"Poll {poll_count}: V_ema={v_ema:.2f}V, "
                            f"L_ema={l_ema:.1f}%, "
                            f"V_norm={v_norm_str}, "
                            f"charge={charge_str}, "
                            f"time_rem={time_rem_str}, "
                            f"event={event_str}, "
                            f"stabilized={stabilized}"
                        )

                        # Write virtual UPS metrics to tmpfs for NUT dummy-ups to read
                        try:
                            # Use cached status override from _handle_event_transition()
                            ups_status_override = self.current_metrics.get("ups_status_override", "OL")

                            # Build virtual metrics dict with overrides + passthrough fields
                            virtual_metrics = {
                                "battery.runtime": int(time_rem * 60) if time_rem is not None else 0,  # seconds
                                "battery.charge": int(battery_charge) if battery_charge is not None else 0,  # %
                                "ups.status": ups_status_override,
                                **{k: v for k, v in ups_data.items()
                                   if k not in ["battery.runtime", "battery.charge", "ups.status"]}
                            }

                            # Write to tmpfs
                            write_virtual_ups_dev(virtual_metrics)
                        except Exception as e:
                            logger.error(f"Failed to write virtual UPS metrics: {e}")
                else:
                    logger.warning(f"Poll {poll_count}: Missing voltage or load data")

                # Sleep until next poll (1s during sag measurement, normal otherwise)
                time.sleep(1 if self.fast_poll_active else POLL_INTERVAL)

            except KeyboardInterrupt:
                logger.info("Interrupted by user")
                break
            except Exception as e:
                logger.error(f"Error in polling loop: {e}", exc_info=True)
                time.sleep(POLL_INTERVAL)

        logger.info("Polling loop ended; daemon shutting down")


def main():
    """Entry point for daemon."""
    parser = argparse.ArgumentParser(
        description="UPS Battery Monitor Daemon",
        prog="ups-battery-monitor"
    )
    parser.add_argument(
        '--calibration-mode',
        action='store_true',
        default=False,
        help="Enable calibration mode: shorter shutdown threshold (1 min) for battery discharge testing"
    )
    args = parser.parse_args()

    try:
        daemon = MonitorDaemon(calibration_mode=args.calibration_mode)
        daemon.run()
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
