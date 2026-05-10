"""UPS Battery Monitor daemon — pipeline orchestrator.

Polls NUT upsd, applies EMA smoothing, classifies events, tracks discharge/sag,
computes metrics, and exports to virtual UPS + health endpoint.

Config/dataclasses extracted to monitor_config.py,
discharge lifecycle extracted to discharge_handler.py.
"""

import argparse
import signal
import socket
import sys
import time
from datetime import datetime, timezone
from typing import Optional

try:
    from systemd.daemon import notify as sd_notify  # pyright: ignore[reportMissingImports]
except ImportError:
    # python-systemd not installed (dev/test environment).
    # Only affects watchdog heartbeats and READY=1 signal — daemon logic is unaffected.

    def sd_notify(status):
        pass


from src.battery_math import ScalarRLS
from src.capacity_estimator import CapacityEstimator
from src.discharge_collector import DischargeCollector
from src.discharge_handler import DischargeHandler
from src.ema_filter import EMAFilter, ir_compensate
from src.event_classifier import EventClassifier, EventType
from src.model import BatteryModel
from src.monitor_config import (
    DAEMON_VERSION,
    ERROR_LOG_BURST,
    Config,
    CurrentMetrics,
    SchedulingConfig,
    load_config,
    logger,
)
from src.nut_client import NUTClient
from src.runtime_calculator import runtime_minutes
from src.sag_tracker import SagTracker
from src.scheduler_manager import SchedulerManager
from src.soc_predictor import charge_percentage, soc_from_voltage
from src.virtual_ups import compute_ups_status_override
from src.virtual_ups_exporter import VirtualUpsExporter


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

        config.model_dir.mkdir(parents=True, exist_ok=True)

        self.nut_client = NUTClient(
            host=config.nut_host,
            port=config.nut_port,
            timeout=config.nut_timeout,
            ups_name=config.ups_name,
        )

        self.ema_filter = EMAFilter(
            window_sec=config.ema_window_sec, poll_interval_sec=config.polling_interval
        )

        self._init_battery_model_and_estimators(config)

        self.current_metrics = CurrentMetrics()
        self._last_logged_soc = None
        self._last_logged_time_rem = None

        self.soh_threshold = config.soh_alert_threshold
        self.runtime_threshold_minutes = config.runtime_threshold_minutes

        self.reference_load_percent = config.reference_load_percent

        # SagTracker, SchedulerManager, and DischargeCollector constructed in _init_battery_model_and_estimators()

        self.has_logged_baseline_lock = False

        # Runtime fields (initialized here, populated in run())
        self.poll_count = 0
        self._stabilization_logged = False
        self._startup_logged = False
        self._consecutive_errors = 0
        self._startup_time: Optional[float] = None

        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        logger.info(
            f"Daemon initialized: shutdown_threshold={self.shutdown_threshold_minutes}min, "
            f"poll={config.polling_interval}s, model={self.battery_model.model_path}, nut={config.nut_host}:{config.nut_port}",
            extra={
                "event_type": "daemon_init",
                "shutdown_threshold_minutes": self.shutdown_threshold_minutes,
                "poll_interval_sec": config.polling_interval,
                "model_path": str(self.battery_model.model_path),
                "nut_host": config.nut_host,
                "nut_port": config.nut_port,
            },
        )

        # Fail fast on misconfigured NUT rather than silently looping
        self._check_nut_connectivity()
        self._probe_temperature_sensor()

    def _init_battery_model_and_estimators(self, config: Config):
        """Initialize battery model, capacity estimator, RLS filters, and discharge handler."""
        model_path = config.model_dir / "model.json"
        self.battery_model = BatteryModel(model_path)
        self.battery_model.state["full_capacity_ah_ref"] = config.capacity_ah
        self._validate_and_repair_model()

        if self.battery_model.get_battery_install_date() is None:
            self.battery_model.set_battery_install_date(datetime.now().strftime("%Y-%m-%d"))
        if not model_path.exists():
            self.battery_model.save()  # Write defaults so tools (battery-health.py, MOTD) can read
        self.event_classifier = EventClassifier()

        self.capacity_estimator = CapacityEstimator(
            peukert_exponent=self.battery_model.get_peukert_exponent(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            nominal_power_watts=self.battery_model.get_nominal_power_watts(),
            capacity_ah=self.battery_model.get_capacity_ah(),
        )

        # Replay historical estimates so has_converged()/get_confidence() survive restarts
        for estimate in self.battery_model.get_capacity_estimates():
            self.capacity_estimator.add_measurement(
                ah=estimate["ah_estimate"],
                timestamp=estimate["timestamp"],
                metadata=estimate["metadata"],
            )

        self.ir_reference_load_percent = self.battery_model.get_ir_reference_load()

        self.sag_tracker = SagTracker(
            battery_model=self.battery_model,
            rls_ir_k=ScalarRLS.from_dict(
                self.battery_model.get_rls_state("ir_k"), forgetting_factor=0.97
            ),
            ir_k=self.battery_model.get_ir_k(),
        )

        self.rls_peukert = ScalarRLS.from_dict(
            self.battery_model.get_rls_state("peukert"), forgetting_factor=0.97
        )

        self.discharge_handler = DischargeHandler(
            battery_model=self.battery_model,
            config=config,
            capacity_estimator=self.capacity_estimator,
            rls_peukert=self.rls_peukert,
            reference_load_percent=config.reference_load_percent,
            soh_threshold=config.soh_alert_threshold,
        )
        self.discharge_collector = DischargeCollector(
            battery_model=self.battery_model,
            config=config,
            discharge_handler=self.discharge_handler,
            ema_filter=self.ema_filter,
        )

        self.scheduler_manager = SchedulerManager(
            battery_model=self.battery_model,
            nut_client=self.nut_client,
            scheduling_config=config.scheduling or SchedulingConfig(),
            discharge_handler=self.discharge_handler,
        )

        self.exporter = VirtualUpsExporter(
            battery_model=self.battery_model,
            event_classifier=self.event_classifier,
            discharge_handler=self.discharge_handler,
            scheduler_manager=self.scheduler_manager,
        )

        self.battery_model.state["new_battery_detected"] = False
        self.battery_model.save()

    def _validate_and_repair_model(self):
        """Validate battery model; repair out-of-range values to safe defaults."""
        lut = self.battery_model.get_lut()
        if len(lut) < 2:
            logger.warning(
                f"Model LUT has only {len(lut)} point(s); predictions will be inaccurate until calibration"
            )

        if self.battery_model.get_anchor_voltage() is None:
            logger.warning(
                "Model LUT missing anchor entry (soc=0.0, source='anchor'); SoH calculation may fail"
            )

        soh = self.battery_model.get_soh()
        if not (0.0 < soh <= 1.0):
            logger.warning(f"Model SoH={soh} out of valid range (0, 1]; resetting to 1.0")
            self.battery_model.set_soh(1.0)

        capacity = self.battery_model.get_capacity_ah()
        if capacity <= 0:
            raise ValueError(f"Model capacity_ah={capacity} invalid; cannot compute runtime")

    def _check_nut_connectivity(self):
        """Verify NUT upsd is reachable before entering main loop."""
        try:
            _ = self.nut_client.get_ups_vars()
            logger.info(
                "NUT upsd reachable, polling started", extra={"event_type": "nut_reachable"}
            )
        except (socket.error, OSError, ConnectionError, TimeoutError, ValueError):
            logger.warning(
                f"NUT upsd unreachable at {self.config.nut_host}:{self.config.nut_port}, "
                f"will retry every {self.config.polling_interval}s",
                exc_info=True,
                extra={"event_type": "nut_unreachable"},
            )

    def _probe_temperature_sensor(self):
        """Check NUT for temperature variable at startup (one-time probe).

        Checks for standard NUT temperature variable names. If found, logs
        the sensor value. If absent, logs that thermal compensation is skipped
        (35°C assumed constant per v3.0 design).

        Does not raise — temperature is informational, not safety-critical.
        """
        _TEMPERATURE_VARS = ("ups.temperature", "battery.temperature", "ambient.temperature")
        try:
            ups_vars = self.nut_client.get_ups_vars()
        except (socket.error, OSError, ConnectionError, TimeoutError, ValueError):
            # NUT unreachable — _check_nut_connectivity already logged this
            return

        for var_name in _TEMPERATURE_VARS:
            if var_name in ups_vars:
                logger.info(
                    "Temperature sensor found: %s=%s°C",
                    var_name,
                    ups_vars[var_name],
                    extra={
                        "event_type": "temperature_sensor_found",
                        "temperature_var": var_name,
                        "temperature_celsius": ups_vars[var_name],
                    },
                )
                return

        logger.info(
            "Temperature sensor unavailable, skipping thermal compensation",
            extra={"event_type": "temperature_sensor_unavailable"},
        )

    def _handle_event_transition(self):
        """Execute actions based on event transitions.

        Mutates: current_metrics.shutdown_imminent, current_metrics.ups_status_override.
        On OB→OL transition: triggers _update_battery_health (SoH, LUT, capacity).
        """
        event_type = self.current_metrics.event_type
        previous_event_type = self.current_metrics.previous_event_type

        if event_type == EventType.BLACKOUT_REAL:
            time_rem = self.current_metrics.time_rem_minutes
            if time_rem is not None and time_rem < self.shutdown_threshold_minutes:
                logger.warning(
                    f"Real blackout: time_rem={time_rem:.1f}min < threshold {self.shutdown_threshold_minutes}min; "
                    f"prepare LB flag",
                    extra={
                        "event_type": "shutdown_imminent",
                        "time_rem_minutes": f"{time_rem:.1f}",
                        "threshold_minutes": self.shutdown_threshold_minutes,
                    },
                )
                self.current_metrics.shutdown_imminent = True
            else:
                self.current_metrics.shutdown_imminent = False

        if event_type == EventType.BLACKOUT_TEST:
            logger.info(
                "Battery test detected; collecting calibration data, no shutdown",
                extra={"event_type": "battery_test_detected"},
            )
            self.current_metrics.shutdown_imminent = False

        if event_type is not None:
            self.current_metrics.ups_status_override = compute_ups_status_override(
                event_type,
                self.current_metrics.time_rem_minutes or 0,
                self.shutdown_threshold_minutes,
            )

        if (
            self.current_metrics.transition_occurred
            and event_type == EventType.ONLINE
            and previous_event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)
        ):
            logger.info(
                "Power restored; updating LUT with measured discharge points",
                extra={"event_type": "power_restored"},
            )
            self._update_battery_health()

    def _update_battery_health(self):
        """Delegate to DischargeHandler; resets discharge buffer after processing."""
        self.discharge_handler.update_battery_health(self.discharge_collector.buffer)
        self.discharge_collector.reset_buffer()

    def _handle_discharge_complete(self, discharge_data: dict) -> None:
        """Delegate to DischargeHandler."""
        self.discharge_handler.handle_discharge_complete(discharge_data)

    def _auto_calibrate_peukert(self, current_soh: float):
        """Delegate to DischargeHandler."""
        self.discharge_handler._auto_calibrate_peukert(current_soh, self.discharge_collector.buffer)

    def _log_discharge_prediction(self):
        """Delegate to DischargeHandler."""
        self.discharge_handler._log_discharge_prediction(
            self.discharge_collector.buffer, self.current_metrics.soc
        )

    # --- Battery baseline reset ---

    def _reset_battery_baseline(self):
        """Reset capacity estimation and SoH history baseline on battery replacement."""

        old_capacity = self.battery_model.state.get("capacity_ah_measured")
        new_capacity = self.battery_model.get_capacity_ah()

        self.battery_model.state["capacity_estimates"] = []
        self.battery_model.state["capacity_ah_measured"] = None

        today = datetime.now().strftime("%Y-%m-%d")
        self.battery_model.state["soh"] = 1.0
        self.battery_model.add_soh_history_entry(
            date=today,
            soh=1.0,
            capacity_ah_ref=new_capacity,  # 7.2Ah (rated, fresh baseline)
        )

        self.battery_model.state["cycle_count"] = 0

        self.battery_model.reset_rls_state()
        self.sag_tracker.reset_rls(theta=0.015, P=1.0)
        self.rls_peukert = ScalarRLS(theta=1.2, P=1.0)
        self.discharge_handler.rls_peukert = self.rls_peukert

        msg = (
            f"baseline_reset: capacity baseline reset from {old_capacity:.2f}Ah to {new_capacity:.2f}Ah"
            if old_capacity is not None
            else f"baseline_reset: capacity baseline initialized to {new_capacity:.2f}Ah (first reset)"
        )
        extra = {
            "event_type": "baseline_reset",
            "capacity_ah_new": f"{new_capacity:.2f}",
        }
        if old_capacity is not None:
            extra["capacity_ah_old"] = f"{old_capacity:.2f}"
        logger.info(msg, extra=extra)

        self.battery_model.save()

    def _signal_handler(self, signum, frame):
        """Handle SIGTERM/SIGINT: persist model, then stop polling loop."""
        logger.info(
            f"Received signal {signum}; shutting down",
            extra={"event_type": "shutdown", "signal": signum},
        )
        try:
            self.battery_model.save()
            logger.info("Model saved before shutdown", extra={"event_type": "shutdown_save"})
        except (OSError, TypeError, ValueError) as e:
            logger.error(
                f"Failed to save model on shutdown: {e}",
                exc_info=True,
                extra={"event_type": "shutdown_save_failed"},
            )
        self.running = False

    # --- Pipeline stages ---

    def _update_ema(self, ups_data):
        """Feed voltage/load into EMA filter, log stabilization event."""
        voltage = ups_data.get("battery.voltage")
        load = ups_data.get("ups.load")
        if voltage is None or load is None:
            return None, None

        # Voltage bounds check (8.0-15.0V) and load bounds check (0-100%)
        if not (8.0 <= voltage <= 15.0):
            logger.warning(
                f"Voltage {voltage:.2f}V out of bounds [8.0-15.0V]; skipping sample",
                extra={
                    "event_type": "sensor_out_of_bounds",
                    "field": "voltage",
                    "value": f"{voltage:.2f}",
                },
            )
            return None, None
        if not (0 <= load <= 100):
            logger.warning(
                f"Load {load:.1f}% out of bounds [0-100%]; skipping sample",
                extra={
                    "event_type": "sensor_out_of_bounds",
                    "field": "load",
                    "value": f"{load:.1f}",
                },
            )
            return None, None

        self.ema_filter.add_sample(voltage, load)
        self.poll_count += 1
        if self.ema_filter.stabilized and not self._stabilization_logged:
            logger.info(
                f"EMA buffer stabilized after {self.poll_count} samples, IR compensation active",
                extra={"event_type": "ema_stabilized", "poll_count": self.poll_count},
            )
            self._stabilization_logged = True
        return voltage, load

    def _classify_event(self, ups_data):
        """Classify UPS event and log transitions."""
        ups_status = ups_data.get("ups.status")
        input_voltage = ups_data.get("input.voltage")
        if ups_status is None or input_voltage is None:
            logger.debug(
                f"Missing NUT fields: ups.status={ups_status}, input.voltage={input_voltage}"
            )
            return
        event_type = self.event_classifier.classify(ups_status, input_voltage)
        self.current_metrics.event_type = event_type
        self.current_metrics.transition_occurred = self.event_classifier.transition_occurred

    def _log_soc_change(self, soc, soc_prev):
        """Log SoC when it changes by more than 5% or on first reading."""
        if soc_prev is not None and abs(soc - soc_prev) <= 0.05:
            return
        if soc_prev is not None:
            logger.info(
                f"SoC updated: {soc_prev * 100:.0f}% \u2192 {soc * 100:.0f}%",
                extra={
                    "event_type": "soc_change",
                    "soc_old": f"{soc_prev * 100:.0f}",
                    "soc_new": f"{soc * 100:.0f}",
                },
            )
        else:
            logger.info(
                f"SoC initial: {soc * 100:.0f}%",
                extra={"event_type": "soc_initial", "soc": f"{soc * 100:.0f}"},
            )
        self._last_logged_soc = soc

    def _compute_metrics(self):
        """Calculate SoC, charge%, and runtime from EMA values. Returns (battery_charge, time_rem)."""
        v_ema = self.ema_filter.voltage
        l_ema = self.ema_filter.load
        if not self.ema_filter.stabilized:
            return None, None
        assert l_ema is not None

        v_norm = ir_compensate(v_ema, l_ema, self.ir_reference_load_percent, self.sag_tracker.ir_k)
        if v_norm is None:
            return None, None
        self._last_v_norm = v_norm

        soc = soc_from_voltage(v_norm, self.battery_model.get_lut())
        battery_charge = charge_percentage(soc)
        time_rem = runtime_minutes(
            soc,
            l_ema,
            self.battery_model.get_capacity_ah(),
            self.battery_model.get_soh(),
            peukert_exponent=self.battery_model.get_peukert_exponent(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            nominal_power_watts=self.battery_model.get_nominal_power_watts(),
        )

        self.current_metrics.soc = soc
        self.current_metrics.battery_charge = battery_charge
        self.current_metrics.time_rem_minutes = time_rem
        self.current_metrics.timestamp = datetime.now(timezone.utc)

        self._log_soc_change(soc, self._last_logged_soc)
        if self._last_logged_time_rem is None or abs(time_rem - self._last_logged_time_rem) > 1.0:
            logger.debug(
                f"Remaining runtime: {time_rem:.1f} minutes",
                extra={"event_type": "runtime_change", "time_rem_minutes": f"{time_rem:.1f}"},
            )
            self._last_logged_time_rem = time_rem

        return battery_charge, time_rem

    def _log_status(self, battery_charge, time_rem, poll_latency_ms=None):
        """Log periodic status line with all key metrics."""
        v_ema = self.ema_filter.voltage
        l_ema = self.ema_filter.load
        v_norm = getattr(self, "_last_v_norm", None)

        v_norm_str = f"{v_norm:.2f}V" if v_norm is not None else "N/A"
        charge_str = f"{battery_charge}%" if battery_charge is not None else "N/A"
        time_rem_str = f"{time_rem:.1f}min" if time_rem is not None else "N/A"
        event_type = self.current_metrics.event_type
        event_str = event_type.name if event_type else "N/A"
        latency_str = f"{poll_latency_ms:.0f}ms" if poll_latency_ms is not None else "N/A"
        logger.debug(
            f"Poll {self.poll_count}: V_ema={v_ema:.2f}V, L_ema={l_ema:.1f}%, "
            f"V_norm={v_norm_str}, charge={charge_str}, time_rem={time_rem_str}, "
            f"event={event_str}, stabilized={self.ema_filter.stabilized}, "
            f"nut_latency={latency_str}, discharge_buf={len(self.discharge_collector.buffer.voltages)}",
            extra={
                "event_type": "poll_status",
                "poll_count": str(self.poll_count),
                "v_ema": f"{v_ema:.2f}" if v_ema is not None else "N/A",
                "v_norm": f"{v_norm:.2f}" if v_norm is not None else "N/A",
                "load_pct": f"{l_ema:.1f}" if l_ema is not None else "N/A",
                "charge_pct": charge_str,
                "time_rem": time_rem_str,
                "soh": f"{self.battery_model.get_soh():.4f}",
                "event": event_str,
                "nut_latency_ms": f"{poll_latency_ms:.0f}"
                if poll_latency_ms is not None
                else "N/A",
            },
        )

    # --- Main loop ---

    def _poll_once(self) -> None:
        """Execute a single poll cycle: fetch UPS data, update metrics, write outputs."""
        timestamp = time.time()
        ups_data = self.nut_client.get_ups_vars()
        poll_latency_ms = (time.time() - timestamp) * 1000

        if not self._startup_logged:
            assert self._startup_time is not None
            startup_delta_ms = (time.monotonic() - self._startup_time) * 1000
            logger.info(
                f"First successful poll completed: startup took {startup_delta_ms:.0f}ms",
                extra={"event_type": "startup_complete", "startup_ms": f"{startup_delta_ms:.0f}"},
            )
            self._startup_logged = True
        voltage, load = self._update_ema(ups_data)
        if voltage is None:
            logger.warning(
                f"Poll {self.poll_count}: Missing voltage or load data",
                extra={"event_type": "missing_poll_data", "poll_count": self.poll_count},
            )
            time.sleep(self.config.polling_interval)
            return

        self._consecutive_errors = 0  # Reset after validated poll data

        self._classify_event(ups_data)
        self.sag_tracker.track(
            voltage,
            event_type=self.current_metrics.event_type,
            transition_occurred=self.event_classifier.transition_occurred,
            current_load=self.ema_filter.load,
        )
        cooldown_expired = self.discharge_collector.track(
            voltage, timestamp, self.current_metrics.event_type, self.current_metrics
        )
        if cooldown_expired:
            self._update_battery_health()

        event_type = self.current_metrics.event_type
        is_discharging = event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)

        # Event transition handling runs EVERY poll (not gated)
        self._handle_event_transition()
        # Default to ONLINE when classifier returned None (no status data)
        self.current_metrics.previous_event_type = (
            self.current_metrics.event_type or EventType.ONLINE
        )

        reporting_interval_polls = self.config.reporting_interval // self.config.polling_interval
        if is_discharging or self.poll_count % reporting_interval_polls == 0:
            logger.debug(
                f"Metrics gate: is_discharging={is_discharging}, poll_count={self.poll_count}"
            )
            battery_charge, time_rem = self._compute_metrics()
            self._log_status(battery_charge, time_rem, poll_latency_ms)
            self.exporter.write_virtual_ups(
                ups_data, battery_charge, time_rem, self.current_metrics
            )

        self.exporter.write_health_snapshot(
            poll_latency_ms, self.current_metrics, self._consecutive_errors
        )
        self.scheduler_manager.run_daily(datetime.now(timezone.utc), self.current_metrics)

        # Report healthy to systemd AFTER critical writes succeed
        sd_notify("WATCHDOG=1")
        time.sleep(1 if self.sag_tracker.is_measuring else self.config.polling_interval)

    def run(self):
        """
        Main polling loop.

        Polls UPS every POLL_INTERVAL seconds, processes data through the
        pipeline: EMA → event classification → sag/discharge tracking →
        metrics → virtual UPS output. Runs until SIGTERM/SIGINT.
        """
        sd_notify("READY=1")
        logger.info("ups-battery-monitor %s starting", DAEMON_VERSION)
        self.poll_count = 0
        self._stabilization_logged = False
        self._startup_logged = False
        self._consecutive_errors = 0
        self._startup_time = time.monotonic()

        while self.running:
            try:
                self._poll_once()
            except KeyboardInterrupt:
                logger.info("Interrupted by user")
                break
            except (socket.error, OSError, ConnectionError, TimeoutError) as e:
                self._consecutive_errors += 1
                self.sag_tracker.reset_idle()
                error_type = type(e).__name__
                error_type_changed = error_type != getattr(self, "_last_error_type", None)
                self._last_error_type = error_type
                reporting_interval_polls = (
                    self.config.reporting_interval // self.config.polling_interval
                )
                should_log = (
                    self._consecutive_errors <= ERROR_LOG_BURST
                    or error_type_changed
                    or self._consecutive_errors % reporting_interval_polls == 0
                )
                if should_log:
                    logger.error(
                        f"Transient error in polling loop ({self._consecutive_errors} consecutive): {e}",
                        exc_info=(
                            self._consecutive_errors <= ERROR_LOG_BURST or error_type_changed
                        ),
                        extra={
                            "event_type": "poll_error",
                            "consecutive_errors": self._consecutive_errors,
                            "error_class": error_type,
                        },
                    )
                time.sleep(self.config.polling_interval)
            except Exception as e:
                # Non-transient error (AttributeError, TypeError, KeyError, etc.)
                # indicates a bug — fail fast rather than silently retrying forever
                logger.critical(
                    f"Bug in polling loop: {e}",
                    exc_info=True,
                    extra={
                        "event_type": "poll_bug",
                        "error_class": type(e).__name__,
                    },
                )
                raise

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
        description="UPS Battery Monitor Daemon", prog="ups-battery-monitor"
    )
    parser.add_argument(
        "--new-battery",
        action="store_true",
        help="Signal that a new battery has been installed; daemon will use this for next discharge measurement",
    )
    return parser.parse_args(args)


def main():
    """Entry point for daemon."""
    args = parse_args()

    try:
        config = load_config()
        daemon = MonitorDaemon(config)
        if args.new_battery:
            daemon._reset_battery_baseline()
        daemon.run()
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
