"""UPS Battery Monitor daemon — pipeline orchestrator.

Polls NUT upsd, applies EMA smoothing, classifies events, tracks discharge/sag,
computes metrics, and exports to virtual UPS + health endpoint.

Config/dataclasses extracted to monitor_config.py,
discharge lifecycle extracted to discharge_handler.py.
"""

import time
import signal
import socket
import sys
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
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
from src.battery_math import calibrate_peukert, ScalarRLS

from src.monitor_config import (
    Config, CurrentMetrics, DischargeBuffer, HealthSnapshot,
    CONFIG_DIR, REPO_ROOT, POLL_INTERVAL, NUT_HOST, NUT_PORT, NUT_TIMEOUT,
    RUNTIME_THRESHOLD_MINUTES, REFERENCE_LOAD_PERCENT, REPORTING_INTERVAL_POLLS,
    HEALTH_ENDPOINT_PATH, DISCHARGE_BUFFER_MAX_SAMPLES,
    ERROR_LOG_BURST, load_config, safe_save, write_health_endpoint, logger,
    SchedulingConfig, get_scheduling_config,
)
from src.discharge_handler import DischargeHandler
from src.sag_tracker import SagTracker
from src.battery_math.scheduler import evaluate_test_scheduling, SchedulerDecision
from src.battery_math.sulfation import compute_sulfation_score
from src.battery_math.cycle_roi import compute_cycle_roi


def validate_preconditions_before_upscmd(
    ups_status: str,
    soc: float,
    recent_power_glitches: int,
    test_already_running: bool,
) -> tuple[bool, str]:
    """Validate preconditions before dispatching test command.

    Guard clauses (must all pass):
    - UPS is online: 'OL' in ups_status and 'OB' not in ups_status and 'CAL' not in ups_status
    - SoC ≥95%: soc >= 0.95
    - Grid stable: recent_power_glitches ≤ 2 (not yet implemented — caller passes 0)
    - No test running: test_already_running == False

    Args:
        ups_status: UPS status string (e.g., "OL", "OB DISCHRG", "CAL")
        soc: State of charge [0.0, 1.0]
        recent_power_glitches: Count of grid state changes in last 4h
        test_already_running: Whether a test is currently running

    Returns:
        tuple[bool, str]: (can_proceed, reason_if_blocked)
    """
    if 'OL' not in ups_status or 'OB' in ups_status or 'CAL' in ups_status:
        return False, "UPS_not_online_cannot_test_during_discharge"

    if soc < 0.95:
        return False, "soc_below_threshold"

    if recent_power_glitches > 2:
        return False, "grid_unstable"

    if test_already_running:
        return False, "test_already_running"

    return True, ""


def dispatch_test_with_audit(
    nut_client,
    battery_model: BatteryModel,
    decision: SchedulerDecision,
    current_metrics: CurrentMetrics,
) -> bool:
    """Dispatch test command with full precondition checks and journald logging.

    Args:
        nut_client: NUTClient instance for sending commands
        battery_model: BatteryModel for persistence
        decision: SchedulerDecision from evaluate_test_scheduling()
        current_metrics: CurrentMetrics with UPS status and SoC

    Returns:
        bool: True if test was dispatched, False if blocked or failed
    """
    ups_status = current_metrics.ups_status_override or "OL"
    if current_metrics.ups_status_override is None:
        logger.debug("ups_status_override is None (before first poll); defaulting to OL")
    soc = current_metrics.soc if current_metrics.soc is not None else 1.0
    recent_power_glitches = 0
    test_already_running = battery_model.data.get('test_running', False)

    preconditions_ok, block_reason = validate_preconditions_before_upscmd(
        ups_status=ups_status,
        soc=soc,
        recent_power_glitches=recent_power_glitches,
        test_already_running=test_already_running,
    )

    if not preconditions_ok:
        logger.info(f"Test dispatch precondition blocked: {block_reason}", extra={
            'event_type': 'test_precondition_blocked',
            'reason': block_reason,
        })
        return False

    command = f'test.battery.start.{decision.test_type}'
    upscmd_timestamp = datetime.now(timezone.utc).isoformat()

    try:
        success, result_msg = nut_client.send_instcmd(command)
    except (socket.error, OSError, ValueError) as e:
        battery_model.update_upscmd_result(
            upscmd_timestamp=upscmd_timestamp,
            upscmd_type=command,
            upscmd_status=f'ERR_SOCKET: {e}',
        )
        safe_save(battery_model)
        logger.error(f"Test dispatch socket error: {e}", exc_info=True)
        return False

    if success:
        upscmd_status = 'OK'
        battery_model.data['test_running'] = True
    else:
        upscmd_status = result_msg or 'ERR_UNKNOWN'

    battery_model.update_upscmd_result(
        upscmd_timestamp=upscmd_timestamp,
        upscmd_type=command,
        upscmd_status=upscmd_status,
    )
    safe_save(battery_model)

    if success:
        logger.info(f"Test dispatched: {command}", extra={
            'event_type': 'test_dispatched',
            'test_type': decision.test_type,
            'command': command,
            'reason_code': decision.reason_code,
        })
        return True
    else:
        logger.error(f"Test dispatch failed: {result_msg or 'unknown error'}", extra={
            'event_type': 'test_dispatch_failed',
            'command': command,
            'error': result_msg or 'unknown',
        })
        return False


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
            ups_name=config.ups_name
        )

        self.ema_filter = EMAFilter(
            window_sec=config.ema_window_sec,
            poll_interval_sec=config.polling_interval
        )

        self._init_battery_model_and_estimators(config)

        self.current_metrics = CurrentMetrics()
        self._last_logged_soc = None
        self._last_logged_time_rem = None

        self.discharge_buffer = DischargeBuffer()
        self._discharge_start_time = None  # Timestamp when OL→OB occurred (for cumulative on-battery tracking)
        self.discharge_buffer_clear_countdown = None  # Cooldown timer (60s) before clearing buffer after OL
        self.soh_threshold = config.soh_alert_threshold
        self.runtime_threshold_minutes = config.runtime_threshold_minutes

        self.scheduler_evaluated_today = False  # Flag to run scheduler once daily
        self.last_scheduling_reason: str = 'observing'
        self.last_next_test_timestamp: str | None = None
        self.reference_load_percent = config.reference_load_percent

        self.scheduling_config = config.scheduling or SchedulingConfig()

        # SagTracker constructed in _init_battery_model_and_estimators()

        self.calibration_last_written_index = 0

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
                'event_type': 'daemon_init',
                'shutdown_threshold_minutes': self.shutdown_threshold_minutes,
                'poll_interval_sec': config.polling_interval,
                'model_path': str(self.battery_model.model_path),
                'nut_host': config.nut_host,
                'nut_port': config.nut_port,
            }
        )

        # Fail fast on misconfigured NUT rather than silently looping
        self._check_nut_connectivity()

    def _init_battery_model_and_estimators(self, config: Config):
        """Initialize battery model, capacity estimator, RLS filters, and discharge handler."""
        model_path = config.model_dir / 'model.json'
        self.battery_model = BatteryModel(model_path)
        self.battery_model.data['full_capacity_ah_ref'] = config.capacity_ah
        self._validate_and_repair_model()

        if self.battery_model.get_battery_install_date() is None:
            self.battery_model.set_battery_install_date(datetime.now().strftime('%Y-%m-%d'))
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
                ah=estimate['ah_estimate'],
                timestamp=estimate['timestamp'],
                metadata=estimate['metadata']
            )

        self.ir_reference_load_percent = self.battery_model.get_ir_reference_load()

        self.sag_tracker = SagTracker(
            battery_model=self.battery_model,
            rls_ir_k=ScalarRLS.from_dict(
                self.battery_model.get_rls_state('ir_k'), forgetting_factor=0.97),
            ir_k=self.battery_model.get_ir_k(),
        )

        self.rls_peukert = ScalarRLS.from_dict(
            self.battery_model.get_rls_state('peukert'), forgetting_factor=0.97)

        self.discharge_handler = DischargeHandler(
            battery_model=self.battery_model,
            config=config,
            capacity_estimator=self.capacity_estimator,
            rls_peukert=self.rls_peukert,
            reference_load_percent=config.reference_load_percent,
            soh_threshold=config.soh_alert_threshold,
        )
        self._discharge_predicted_runtime = None  # Snapshot for prediction error logging

        self.battery_model.data['new_battery_detected'] = False
        self.battery_model.save()

    def _validate_and_repair_model(self):
        """Validate battery model; repair out-of-range values to safe defaults."""
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
        """Verify NUT upsd is reachable before entering main loop."""
        try:
            _ = self.nut_client.get_ups_vars()
            logger.info("NUT upsd reachable, polling started", extra={'event_type': 'nut_reachable'})
        except (socket.error, OSError, ConnectionError, TimeoutError, ValueError):
            logger.warning(
                f"NUT upsd unreachable at {self.config.nut_host}:{self.config.nut_port}, "
                f"will retry every {self.config.polling_interval}s",
                exc_info=True,
                extra={'event_type': 'nut_unreachable'}
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
                    extra={'event_type': 'shutdown_imminent',
                           'time_rem_minutes': f'{time_rem:.1f}',
                           'threshold_minutes': self.shutdown_threshold_minutes}
                )
                self.current_metrics.shutdown_imminent = True
            else:
                self.current_metrics.shutdown_imminent = False

        if event_type == EventType.BLACKOUT_TEST:
            logger.info("Battery test detected; collecting calibration data, no shutdown",
                        extra={'event_type': 'battery_test_detected'})
            self.current_metrics.shutdown_imminent = False

        self.current_metrics.ups_status_override = compute_ups_status_override(
            event_type,
            self.current_metrics.time_rem_minutes or 0,
            self.shutdown_threshold_minutes
        )

        if (self.current_metrics.transition_occurred and
            event_type == EventType.ONLINE and
            previous_event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)):
            logger.info("Power restored; updating LUT with measured discharge points",
                        extra={'event_type': 'power_restored'})
            self._update_battery_health()

    def _update_battery_health(self):
        """Delegate to DischargeHandler; resets discharge_buffer to empty after processing."""
        self.discharge_handler.update_battery_health(self.discharge_buffer)
        self.discharge_buffer = DischargeBuffer()

    def _handle_discharge_complete(self, discharge_data: dict) -> None:
        """Delegate to DischargeHandler."""
        self.discharge_handler.handle_discharge_complete(discharge_data)

    def _auto_calibrate_peukert(self, current_soh: float):
        """Delegate to DischargeHandler."""
        self.discharge_handler._auto_calibrate_peukert(current_soh, self.discharge_buffer)

    def _log_discharge_prediction(self):
        """Delegate to DischargeHandler."""
        self.discharge_handler._log_discharge_prediction(
            self.discharge_buffer, self.current_metrics.soc)

    # --- Battery baseline reset ---

    def _reset_battery_baseline(self):
        """Reset capacity estimation and SoH history baseline on battery replacement."""

        old_capacity = self.battery_model.data.get('capacity_ah_measured')
        new_capacity = self.battery_model.get_capacity_ah()

        self.battery_model.data['capacity_estimates'] = []
        self.battery_model.data['capacity_ah_measured'] = None

        today = datetime.now().strftime('%Y-%m-%d')
        self.battery_model.data['soh'] = 1.0
        self.battery_model.add_soh_history_entry(
            date=today,
            soh=1.0,
            capacity_ah_ref=new_capacity  # 7.2Ah (rated, fresh baseline)
        )

        self.battery_model.data['cycle_count'] = 0

        self.battery_model.reset_rls_state()
        self.sag_tracker.reset_rls(theta=0.015, P=1.0)
        self.rls_peukert = ScalarRLS(theta=1.2, P=1.0)
        self.discharge_handler.rls_peukert = self.rls_peukert

        msg = (f"baseline_reset: capacity baseline reset from {old_capacity:.2f}Ah to {new_capacity:.2f}Ah"
               if old_capacity is not None
               else f"baseline_reset: capacity baseline initialized to {new_capacity:.2f}Ah (first reset)")
        extra = {
            'event_type': 'baseline_reset',
            'capacity_ah_new': f'{new_capacity:.2f}',
        }
        if old_capacity is not None:
            extra['capacity_ah_old'] = f'{old_capacity:.2f}'
        logger.info(msg, extra=extra)

        self.battery_model.save()

    # --- Scheduler helpers ---

    def _calculate_days_since_last_test(self) -> float:
        """Calculate days since last upscmd, or inf if never tested."""
        last_ts = self.battery_model.get_last_upscmd_timestamp()
        if not last_ts:
            return float('inf')
        try:
            last_dt = datetime.fromisoformat(last_ts)
            return (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400.0
        except (ValueError, TypeError) as e:
            logger.debug(f"Invalid last_upscmd_timestamp '{last_ts}': {e}; treating as never tested")
            return float('inf')

    def _get_last_natural_blackout(self) -> dict | None:
        """Return most recent natural blackout event (DoD, timestamp)."""
        events = self.battery_model.data.get('discharge_events', [])
        for event in reversed(events):  # Most recent first
            if event.get('event_reason') == 'natural':
                return {
                    'timestamp': event.get('timestamp'),
                    'depth': event.get('depth_of_discharge', 0.0),
                }
        return None

    def _gather_scheduler_inputs(self) -> dict:
        """Collect all inputs needed for scheduler evaluation.

        Returns dict with keys: sulfation_score, cycle_roi, soh_fraction,
        days_since_last_test, last_blackout, active_credit, cycle_budget.
        """
        return {
            'sulfation_score': self.discharge_handler.last_sulfation_score or 0.0,
            'cycle_roi': self.discharge_handler.last_cycle_roi or 0.0,
            'soh_fraction': self.battery_model.get_soh(),
            'days_since_last_test': self._calculate_days_since_last_test(),
            'last_blackout': self._get_last_natural_blackout(),
            'active_credit': self.battery_model.get_blackout_credit(),
            'cycle_budget': self.discharge_handler.last_cycle_budget_remaining or 100,
        }

    def _execute_scheduler_decision(self, decision: SchedulerDecision, scheduler_inputs: dict, now: datetime) -> None:
        """Act on a scheduler decision: log, persist, and dispatch if proposed.

        Args:
            decision: SchedulerDecision from evaluate_test_scheduling()
            scheduler_inputs: Dict from _gather_scheduler_inputs() (for structured logging)
            now: Current UTC datetime
        """
        logger.info(f"Scheduler decision: {decision.action}", extra={
            'event_type': 'scheduler_decision',
            'action': decision.action,
            'reason_code': decision.reason_code,
            'reason_detail': decision.reason_detail,
            'sulfation_score': f"{scheduler_inputs['sulfation_score']:.3f}",
            'roi': f"{scheduler_inputs['cycle_roi']:.3f}",
            'soh_fraction': f"{scheduler_inputs['soh_fraction']:.1%}",
        })

        self.last_scheduling_reason = decision.reason_code
        self.last_next_test_timestamp = decision.next_eligible_timestamp

        self.battery_model.update_scheduling_state(
            scheduled_timestamp=decision.next_eligible_timestamp,
            reason=decision.reason_code,
            block_reason=decision.reason_code if decision.action == 'block_test' else None,
        )

        if decision.action == 'propose_test':
            dispatched = dispatch_test_with_audit(
                nut_client=self.nut_client,
                battery_model=self.battery_model,
                decision=decision,
                current_metrics=self.current_metrics,
            )
            if not dispatched:
                logger.warning("Test proposed but dispatch failed",
                               extra={'event_type': 'test_dispatch_not_sent',
                                      'reason_code': decision.reason_code})
        else:
            logger.info(f"Test {decision.action}: {decision.reason_code} ({decision.reason_detail})")

        self.battery_model.save()

    def _should_run_scheduler(self, now: datetime) -> bool:
        """Check if scheduler should run this poll. Resets daily flag when hour passes."""
        current_hour = now.hour
        scheduler_hour = self.scheduling_config.scheduler_eval_hour_utc

        if current_hour != scheduler_hour:
            self.scheduler_evaluated_today = False
            return False

        if self.scheduler_evaluated_today or now.minute >= 10:
            return False

        return True

    def _run_daily_scheduler(self, now: datetime) -> None:
        """Evaluate test scheduling once daily at the configured UTC hour.

        Orchestrates: gather inputs → evaluate → execute decision.
        """
        if not self._should_run_scheduler(now):
            return

        self.scheduler_evaluated_today = True

        try:
            scheduler_inputs = self._gather_scheduler_inputs()

            if self.scheduling_config.verbose_scheduling:
                logger.debug(
                    "Scheduler inputs",
                    extra={
                        'event_type': 'scheduler_inputs',
                        'sulfation_score': f"{scheduler_inputs['sulfation_score']:.3f}",
                        'cycle_roi': f"{scheduler_inputs['cycle_roi']:.3f}",
                        'soh_fraction': f"{scheduler_inputs['soh_fraction']:.1%}",
                        'days_since_last_test': f"{scheduler_inputs['days_since_last_test']:.1f}",
                        'cycle_budget': int(scheduler_inputs['cycle_budget']),
                    }
                )

            last_blackout = scheduler_inputs['last_blackout']
            decision = evaluate_test_scheduling(
                sulfation_score=scheduler_inputs['sulfation_score'],
                cycle_roi=scheduler_inputs['cycle_roi'],
                soh_fraction=scheduler_inputs['soh_fraction'],
                days_since_last_test=scheduler_inputs['days_since_last_test'],
                last_blackout_timestamp=last_blackout.get('timestamp') if last_blackout else None,
                active_blackout_credit=scheduler_inputs['active_credit'],
                cycle_budget_remaining=int(scheduler_inputs['cycle_budget']),
                grid_stability_cooldown_hours=self.scheduling_config.grid_stability_cooldown_hours,
            )

            self._execute_scheduler_decision(decision, scheduler_inputs, now)
        except (KeyError, AttributeError, TypeError, ValueError, OSError, ConnectionError, TimeoutError) as e:
            logger.error(f"Scheduler evaluation failed: {e}", exc_info=True,
                         extra={'event_type': 'scheduler_error', 'error_class': type(e).__name__})

    def _signal_handler(self, signum, frame):
        """Handle SIGTERM/SIGINT: persist model, then stop polling loop."""
        logger.info(f"Received signal {signum}; shutting down",
                    extra={'event_type': 'shutdown', 'signal': signum})
        try:
            self.battery_model.save()
            logger.info("Model saved before shutdown", extra={'event_type': 'shutdown_save'})
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"Failed to save model on shutdown: {e}",
                         exc_info=True, extra={'event_type': 'shutdown_save_failed'})
        self.running = False

    # --- Pipeline stages ---

    def _update_ema(self, ups_data):
        """Feed voltage/load into EMA filter, log stabilization event."""
        voltage = ups_data.get('battery.voltage')
        load = ups_data.get('ups.load')
        if voltage is None or load is None:
            return None, None

        # Voltage bounds check (8.0-15.0V) and load bounds check (0-100%)
        if not (8.0 <= voltage <= 15.0):
            logger.warning(f"Voltage {voltage:.2f}V out of bounds [8.0-15.0V]; skipping sample",
                           extra={'event_type': 'sensor_out_of_bounds', 'field': 'voltage', 'value': f'{voltage:.2f}'})
            return None, None
        if not (0 <= load <= 100):
            logger.warning(f"Load {load:.1f}% out of bounds [0-100%]; skipping sample",
                           extra={'event_type': 'sensor_out_of_bounds', 'field': 'load', 'value': f'{load:.1f}'})
            return None, None

        self.ema_filter.add_sample(voltage, load)
        self.poll_count += 1
        if self.ema_filter.stabilized and not self._stabilization_logged:
            logger.info(f"EMA buffer stabilized after {self.poll_count} samples, IR compensation active",
                        extra={'event_type': 'ema_stabilized', 'poll_count': self.poll_count})
            self._stabilization_logged = True
        return voltage, load

    def _classify_event(self, ups_data):
        """Classify UPS event and log transitions."""
        ups_status = ups_data.get('ups.status')
        input_voltage = ups_data.get('input.voltage')
        if ups_status is None or input_voltage is None:
            logger.debug(f"Missing NUT fields: ups.status={ups_status}, input.voltage={input_voltage}")
            return
        event_type = self.event_classifier.classify(ups_status, input_voltage)
        self.current_metrics.event_type = event_type
        self.current_metrics.transition_occurred = self.event_classifier.transition_occurred


    def _start_discharge_collection(self, timestamp):
        """Initialize discharge buffer for a new OL→OB event.

        Clears buffers, increments cycle count, snapshots predicted runtime.
        """
        event_type = self.current_metrics.event_type
        if event_type is None:
            return
        if self.discharge_buffer.collecting:
            return

        self.discharge_buffer.collecting = True
        self.discharge_buffer.voltages = []
        self.discharge_buffer.times = []
        self.discharge_buffer.loads = []
        self._discharge_start_time = timestamp
        # cycle_count counts OL→OB transitions (including flicker),
        # matching enterprise "transfer count" metric (Eaton/APC). This is
        # NOT the same as discharge events (which require 300s+ duration).
        # Actual battery wear proxy = cumulative_on_battery_sec.
        self.battery_model.increment_cycle_count()
        # Snapshot predicted runtime at OB start for prediction error logging
        if self.ema_filter.stabilized and self.current_metrics.time_rem_minutes is not None:
            self.discharge_handler.discharge_predicted_runtime = self.current_metrics.time_rem_minutes
        else:
            self.discharge_handler.discharge_predicted_runtime = None
        logger.info(f"Starting discharge buffer collection ({event_type.name}), "
                    f"cycle #{self.battery_model.get_cycle_count()}",
                    extra={'event_type': 'discharge_start', 'discharge_type': event_type.name,
                           'cycle_count': self.battery_model.get_cycle_count()})

    def _handle_discharge_cooldown(self) -> bool:
        """Manage 60s cooldown timer after OB→OL transition.

        OB→OL→OB within 60s is treated as a single discharge event.
        Returns True if cooldown expired and buffer was processed (caller should return).
        """
        event_type = self.current_metrics.event_type
        previous_event_type = self.current_metrics.previous_event_type
        is_discharging = event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)

        if not is_discharging:
            if previous_event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST):
                logger.info("Power loss detected; starting 60s discharge cooldown",
                            extra={'event_type': 'discharge_cooldown_start'})
                self.discharge_buffer_clear_countdown = 60

        if is_discharging and self.discharge_buffer_clear_countdown is not None:
            logger.info("Power restored during cooldown; treating as discharge continuation",
                        extra={'event_type': 'discharge_cooldown_cancelled'})
            self.discharge_buffer_clear_countdown = None

        if self.discharge_buffer_clear_countdown is not None:
            self.discharge_buffer_clear_countdown -= self.config.polling_interval
            if self.discharge_buffer_clear_countdown <= 0:
                logger.info("Cooldown expired (60s OL confirmed); clearing discharge buffer and calling _update_battery_health",
                            extra={'event_type': 'discharge_cooldown_expired'})
                self._update_battery_health()
                return True

        return False

    def _track_discharge(self, voltage, timestamp):
        """Accumulate discharge samples (voltage/time/load) and write calibration points."""
        if self._handle_discharge_cooldown():
            return

        event_type = self.current_metrics.event_type
        if event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST):
            if not self.discharge_buffer.collecting:
                self._start_discharge_collection(timestamp)
            if voltage is not None:
                if len(self.discharge_buffer.voltages) >= DISCHARGE_BUFFER_MAX_SAMPLES:
                    logger.warning(f"Discharge buffer capped at {DISCHARGE_BUFFER_MAX_SAMPLES} samples",
                                   extra={'event_type': 'discharge_buffer_capped', 'max_samples': DISCHARGE_BUFFER_MAX_SAMPLES})
                else:
                    self.discharge_buffer.voltages.append(voltage)
                    self.discharge_buffer.times.append(timestamp)
                    load = self.ema_filter.load if self.ema_filter.load is not None else 0.0
                    self.discharge_buffer.loads.append(load)
                self._write_calibration_points(event_type)
        else:
            if self.discharge_buffer.collecting:
                self._finalize_discharge_collection(timestamp)

    def _finalize_discharge_collection(self, timestamp):
        """End discharge collection: record on-battery time and reset buffer state."""
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
            except (KeyError, ValueError, OSError) as e:
                logger.error(f"Calibration write failed at index {i}: {e}", exc_info=True)
                self.calibration_last_written_index = i + 1
                continue

        # Batch flush: persist all accumulated points once per REPORTING_INTERVAL
        points_written = self.calibration_last_written_index
        if points_written > 0:
            try:
                self.battery_model.calibration_batch_flush()
                logger.info(f"Batch flushed {points_written} calibration points to disk",
                            extra={'event_type': 'calibration_batch_flush', 'points_written': points_written})
            except OSError as e:
                logger.error(f"Calibration batch flush failed: {e}", exc_info=True,
                             extra={'event_type': 'calibration_flush_failed'})

    def _log_soc_change(self, soc, soc_prev):
        """Log SoC when it changes by more than 5% or on first reading."""
        if soc_prev is not None and abs(soc - soc_prev) <= 0.05:
            return
        if soc_prev is not None:
            logger.info(
                f"SoC updated: {soc_prev*100:.0f}% \u2192 {soc*100:.0f}%",
                extra={'event_type': 'soc_change', 'soc_old': f'{soc_prev*100:.0f}', 'soc_new': f'{soc*100:.0f}'}
            )
        else:
            logger.info(
                f"SoC initial: {soc*100:.0f}%",
                extra={'event_type': 'soc_initial', 'soc': f'{soc*100:.0f}'}
            )
        self._last_logged_soc = soc

    def _compute_metrics(self):
        """Calculate SoC, charge%, and runtime from EMA values. Returns (battery_charge, time_rem)."""
        v_ema = self.ema_filter.voltage
        l_ema = self.ema_filter.load
        if not self.ema_filter.stabilized:
            return None, None

        v_norm = ir_compensate(v_ema, l_ema, self.ir_reference_load_percent, self.sag_tracker.ir_k)
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
        self.current_metrics.timestamp = datetime.now(timezone.utc)

        self._log_soc_change(soc, self._last_logged_soc)
        if self._last_logged_time_rem is None or abs(time_rem - self._last_logged_time_rem) > 1.0:
            logger.debug(
                f"Remaining runtime: {time_rem:.1f} minutes",
                extra={'event_type': 'runtime_change', 'time_rem_minutes': f'{time_rem:.1f}'}
            )
            self._last_logged_time_rem = time_rem

        return battery_charge, time_rem

    def _log_status(self, battery_charge, time_rem, poll_latency_ms=None):
        """Log periodic status line with all key metrics."""
        v_ema = self.ema_filter.voltage
        l_ema = self.ema_filter.load
        v_norm = getattr(self, '_last_v_norm', None)

        v_norm_str = f"{v_norm:.2f}V" if v_norm is not None else "N/A"
        charge_str = f"{battery_charge}%" if battery_charge is not None else "N/A"
        time_rem_str = f"{time_rem:.1f}min" if time_rem is not None else "N/A"
        event_type = self.current_metrics.event_type
        event_str = event_type.name if event_type else "N/A"
        latency_str = f"{poll_latency_ms:.0f}ms" if poll_latency_ms is not None else "N/A"
        logger.info(
            f"Poll {self.poll_count}: V_ema={v_ema:.2f}V, L_ema={l_ema:.1f}%, "
            f"V_norm={v_norm_str}, charge={charge_str}, time_rem={time_rem_str}, "
            f"event={event_str}, stabilized={self.ema_filter.stabilized}, "
            f"nut_latency={latency_str}, discharge_buf={len(self.discharge_buffer.voltages)}",
            extra={
                'event_type': 'poll_status',
                'poll_count': str(self.poll_count),
                'v_ema': f'{v_ema:.2f}' if v_ema is not None else 'N/A',
                'v_norm': f'{v_norm:.2f}' if v_norm is not None else 'N/A',
                'load_pct': f'{l_ema:.1f}' if l_ema is not None else 'N/A',
                'charge_pct': charge_str,
                'time_rem': time_rem_str,
                'soh': f'{self.battery_model.get_soh():.4f}',
                'event': event_str,
                'nut_latency_ms': f'{poll_latency_ms:.0f}' if poll_latency_ms is not None else 'N/A',
            }
        )

    def _should_passthrough_ob_status(self, raw_status: str) -> bool:
        """True when classifier kept previous state but raw NUT status contains OB.

        Guards against the classifier swallowing an OB status as unknown — pass
        through the original so downstream consumers (upsmon, Grafana) see it.
        """
        return (not self.event_classifier.transition_occurred
                and "OB" in raw_status.split()
                and self.event_classifier.state == EventType.ONLINE)

    def _build_virtual_metrics(self, ups_data, battery_charge, time_rem):
        """Assemble enterprise-equivalent metrics dict for the virtual UPS device."""
        ups_status_override = self.current_metrics.ups_status_override or ups_data.get("ups.status", "OL")
        raw_status = ups_data.get("ups.status", "")
        if self._should_passthrough_ob_status(raw_status):
            ups_status_override = raw_status

        soh = self.battery_model.get_soh()
        install_date = self.battery_model.get_battery_install_date() or ""
        cycle_count = self.battery_model.get_cycle_count()
        cumulative_sec = self.battery_model.get_cumulative_on_battery_sec()
        replacement_due = self.battery_model.get_replacement_due() or ""
        r_internal_mohm = self._compute_median_r_internal_mohm()

        return {
            "battery.runtime": int(time_rem * 60) if time_rem is not None else int(float(ups_data.get("battery.runtime", 0))),
            "battery.charge": int(battery_charge) if battery_charge is not None else int(float(ups_data.get("battery.charge", 0))),
            "ups.status": ups_status_override,
            "battery.health": round(soh * 100),
            "battery.date": install_date,
            "battery.cycle.count": cycle_count,
            "battery.cumulative.runtime": int(cumulative_sec),
            "battery.replacement.due": replacement_due,
            "battery.internal_resistance": r_internal_mohm,
            **{k: v for k, v in ups_data.items()
               if k not in ["battery.runtime", "battery.charge", "ups.status"]}
        }

    def _compute_median_r_internal_mohm(self) -> float:
        """Median of non-zero R_internal measurements in mΩ; requires ≥3 for noise rejection."""
        r_internal_history = self.battery_model.get_r_internal_history()
        valid = [e["r_ohm"] for e in r_internal_history if e["r_ohm"] > 0]
        if len(valid) >= 3:
            sorted_r = sorted(valid)
            return round(sorted_r[len(sorted_r) // 2] * 1000, 1)
        return 0

    def _write_virtual_ups(self, ups_data, battery_charge, time_rem):
        """Write computed metrics to tmpfs for NUT dummy-ups driver."""
        try:
            virtual_metrics = self._build_virtual_metrics(ups_data, battery_charge, time_rem)
            write_virtual_ups_dev(virtual_metrics)
        except (OSError, IOError) as e:
            logger.error(f"Failed to write virtual UPS metrics: {e}", exc_info=True,
                         extra={'event_type': 'virtual_ups_write_failed'})

    # --- Main loop ---

    def _write_health_snapshot(self, poll_latency_ms):
        """Construct health snapshot from current state and write to endpoint."""
        convergence_status = self.battery_model.get_convergence_status()
        dh = self.discharge_handler
        snapshot = HealthSnapshot(
            soc_percent=(self.current_metrics.soc or 0.0) * 100.0,
            is_online=(self.current_metrics.ups_status_override == "OL"),
            poll_latency_ms=poll_latency_ms,
            capacity_ah_measured=convergence_status.get('latest_ah'),
            capacity_ah_rated=convergence_status.get('rated_ah', 7.2),
            capacity_confidence=convergence_status.get('confidence_percent', 0.0) / 100.0,
            capacity_samples_count=convergence_status.get('sample_count', 0),
            capacity_converged=convergence_status.get('converged', False),
            sulfation_score=dh.last_sulfation_score,
            sulfation_confidence=dh.last_sulfation_confidence,
            days_since_deep=dh.last_days_since_deep,
            ir_trend_rate=dh.last_ir_trend_rate,
            recovery_delta=dh.last_recovery_delta,
            cycle_roi=dh.last_cycle_roi,
            cycle_budget_remaining=dh.last_cycle_budget_remaining,
            scheduling_reason=self.last_scheduling_reason,
            next_test_timestamp=self.last_next_test_timestamp,
            last_discharge_timestamp=dh.last_discharge_timestamp,
            consecutive_errors=self._consecutive_errors,
        )
        write_health_endpoint(snapshot)

    def _poll_once(self) -> None:
        """Execute a single poll cycle: fetch UPS data, update metrics, write outputs."""
        timestamp = time.time()
        ups_data = self.nut_client.get_ups_vars()
        poll_latency_ms = (time.time() - timestamp) * 1000

        if not self._startup_logged:
            startup_delta_ms = (time.monotonic() - self._startup_time) * 1000
            logger.info(f"First successful poll completed: startup took {startup_delta_ms:.0f}ms",
                        extra={'event_type': 'startup_complete', 'startup_ms': f'{startup_delta_ms:.0f}'})
            self._startup_logged = True
        voltage, load = self._update_ema(ups_data)
        if voltage is None:
            logger.warning(f"Poll {self.poll_count}: Missing voltage or load data",
                          extra={'event_type': 'missing_poll_data', 'poll_count': self.poll_count})
            time.sleep(self.config.polling_interval)
            return

        self._consecutive_errors = 0  # Reset after validated poll data

        self._classify_event(ups_data)
        self.sag_tracker.track(
            voltage, event_type=self.current_metrics.event_type,
            transition_occurred=self.event_classifier.transition_occurred,
            current_load=self.ema_filter.load)
        self._track_discharge(voltage, timestamp)

        event_type = self.current_metrics.event_type
        is_discharging = event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)

        # Event transition handling runs EVERY poll (not gated)
        self._handle_event_transition()
        # Default to ONLINE when classifier returned None (no status data)
        self.current_metrics.previous_event_type = self.current_metrics.event_type or EventType.ONLINE

        reporting_interval_polls = self.config.reporting_interval // self.config.polling_interval
        if is_discharging or self.poll_count % reporting_interval_polls == 0:
            logger.debug(f"Metrics gate: is_discharging={is_discharging}, poll_count={self.poll_count}")
            battery_charge, time_rem = self._compute_metrics()
            self._log_status(battery_charge, time_rem, poll_latency_ms)
            self._write_virtual_ups(ups_data, battery_charge, time_rem)

        self._write_health_snapshot(poll_latency_ms)
        self._run_daily_scheduler(datetime.now(timezone.utc))

        # Report healthy to systemd AFTER critical writes succeed
        sd_notify('WATCHDOG=1')
        time.sleep(1 if self.sag_tracker.is_measuring else self.config.polling_interval)

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
                error_type_changed = error_type != getattr(self, '_last_error_type', None)
                self._last_error_type = error_type
                reporting_interval_polls = self.config.reporting_interval // self.config.polling_interval
                should_log = (self._consecutive_errors <= ERROR_LOG_BURST
                              or error_type_changed
                              or self._consecutive_errors % reporting_interval_polls == 0)
                if should_log:
                    logger.error(
                        f"Transient error in polling loop ({self._consecutive_errors} consecutive): {e}",
                        exc_info=(self._consecutive_errors <= ERROR_LOG_BURST or error_type_changed),
                        extra={
                            'event_type': 'poll_error',
                            'consecutive_errors': self._consecutive_errors,
                            'error_class': error_type,
                        }
                    )
                time.sleep(self.config.polling_interval)
            except Exception as e:
                # Non-transient error (AttributeError, TypeError, KeyError, etc.)
                # indicates a bug — fail fast rather than silently retrying forever
                logger.critical(
                    f"Bug in polling loop: {e}", exc_info=True,
                    extra={
                        'event_type': 'poll_bug',
                        'error_class': type(e).__name__,
                    }
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
        config = load_config()
        daemon = MonitorDaemon(config)
        if args.new_battery:
            daemon._reset_battery_baseline()
        daemon.run()
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
