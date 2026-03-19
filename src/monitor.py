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
    Config, CurrentMetrics, DischargeBuffer, HealthSnapshot, SagState,
    CONFIG_DIR, REPO_ROOT, POLL_INTERVAL, NUT_HOST, NUT_PORT, NUT_TIMEOUT,
    RUNTIME_THRESHOLD_MINUTES, REFERENCE_LOAD_PERCENT, REPORTING_INTERVAL_POLLS,
    HEALTH_ENDPOINT_PATH, SAG_SAMPLES_REQUIRED, DISCHARGE_BUFFER_MAX_SAMPLES,
    ERROR_LOG_BURST, load_config, safe_save, write_health_endpoint, logger,
    SchedulingConfig, get_scheduling_config,
)
from src.discharge_handler import DischargeHandler
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
    # Check: UPS online
    if 'OL' not in ups_status or 'OB' in ups_status or 'CAL' in ups_status:
        return False, "UPS_not_online_cannot_test_during_discharge"

    # Check: SoC ≥95%
    if soc < 0.95:
        soc_percent = int(soc * 100)
        return False, f"SoC_below_95_percent_{soc_percent}%"

    # Check: Grid stable
    if recent_power_glitches > 2:
        return False, f"grid_unstable_{recent_power_glitches}_transitions_in_4h"

    # Check: No test running
    if test_already_running:
        return False, "test_already_running"

    # All checks passed
    return True, ""


def dispatch_test_with_audit(
    nut_client,
    battery_model: BatteryModel,
    decision: SchedulerDecision,
    current_metrics,
) -> bool:
    """Dispatch test command with full precondition checks and journald logging.

    Flow:
    1. Validate preconditions (SoC, grid, no test running)
    2. If blocked → log reason to journald, return False
    3. If pass → call nut_client.send_instcmd(f'test.battery.start.{decision.test_type}')
    4. If success → update model.json with timestamp/type/status
    5. If failure → update model.json with error message
    6. Return True if dispatched, False if precondition blocked or send failed

    Args:
        nut_client: NUTClient instance for sending commands
        battery_model: BatteryModel for persistence
        decision: SchedulerDecision from evaluate_test_scheduling()
        current_metrics: CurrentMetrics with UPS status and SoC

    Returns:
        bool: True if test was dispatched, False if blocked or failed
    """
    # Extract current state for precondition checks
    ups_status = getattr(current_metrics, 'ups_status_override', None) or "OL"
    soc = getattr(current_metrics, 'soc', 1.0)
    recent_power_glitches = 0
    is_test_running = battery_model.data.get('test_running', False)

    # Validate preconditions
    preconditions_ok, block_reason = validate_preconditions_before_upscmd(
        ups_status=ups_status,
        soc=soc,
        recent_power_glitches=recent_power_glitches,
        test_already_running=is_test_running,
    )

    if not preconditions_ok:
        # Precondition blocked
        logger.info(f"Test dispatch precondition blocked: {block_reason}", extra={
            'event_type': 'test_precondition_blocked',
            'reason': block_reason,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })
        return False

    # All preconditions passed, attempt to dispatch
    command = f'test.battery.start.{decision.test_type}'
    upscmd_timestamp = datetime.now(timezone.utc).isoformat()

    try:
        success, response_msg = nut_client.send_instcmd(command)
    except (socket.error, OSError) as e:
        battery_model.update_upscmd_result(
            upscmd_timestamp=upscmd_timestamp,
            upscmd_type=command,
            upscmd_status=f'ERR_SOCKET: {e}',
        )
        battery_model.save()
        logger.error(f"Test dispatch socket error: {e}", exc_info=True)
        return False

    if success:
        # Success: update model with command result
        battery_model.update_upscmd_result(
            upscmd_timestamp=upscmd_timestamp,
            upscmd_type=command,
            upscmd_status='OK',
        )
        battery_model.data['test_running'] = True
        battery_model.save()

        logger.info(f"Test dispatched: {command}", extra={
            'event_type': 'test_dispatched',
            'test_type': decision.test_type,
            'command': command,
            'reason_code': decision.reason_code,
            'timestamp': upscmd_timestamp,
        })
        return True
    else:
        # Failure: update model with error
        battery_model.update_upscmd_result(
            upscmd_timestamp=upscmd_timestamp,
            upscmd_type=command,
            upscmd_status=response_msg or 'ERR_UNKNOWN',
        )
        battery_model.save()

        logger.error(f"Test dispatch failed: {response_msg or 'unknown error'}", extra={
            'event_type': 'test_dispatch_failed',
            'command': command,
            'error': response_msg or 'unknown',
            'timestamp': upscmd_timestamp,
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

        # Create model directory
        config.model_dir.mkdir(parents=True, exist_ok=True)

        # Initialize components
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

        # Initialize CapacityEstimator for capacity measurement (CAP-01, CAP-05)
        self.capacity_estimator = CapacityEstimator(
            peukert_exponent=self.battery_model.get_peukert_exponent(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            nominal_power_watts=self.battery_model.get_nominal_power_watts(),
            capacity_ah=self.battery_model.get_capacity_ah(),
        )

        # Load historical capacity estimates from model.json for convergence tracking
        # Ensures has_converged() and get_confidence() survive daemon restarts
        for estimate in self.battery_model.get_capacity_estimates():
            self.capacity_estimator.add_measurement(
                ah=estimate['ah_estimate'],
                timestamp=estimate['timestamp'],
                metadata=estimate['metadata']
            )

        # Load physics params from model
        self.ir_k = self.battery_model.get_ir_k()
        self.ir_reference_load_percent = self.battery_model.get_ir_reference_load()

        # RLS estimators for online parameter calibration
        self.rls_ir_k = ScalarRLS.from_dict(
            self.battery_model.get_rls_state('ir_k'), forgetting_factor=0.97)
        self.rls_peukert = ScalarRLS.from_dict(
            self.battery_model.get_rls_state('peukert'), forgetting_factor=0.97)

        # Discharge lifecycle handler (extracted from MonitorDaemon)
        self.discharge_handler = DischargeHandler(
            battery_model=self.battery_model,
            config=config,
            capacity_estimator=self.capacity_estimator,
            rls_peukert=self.rls_peukert,
            reference_load_percent=config.reference_load_percent,
            soh_threshold=config.soh_alert_threshold,
        )
        self._discharge_predicted_runtime = None  # Snapshot for prediction error logging

        # Clear auto-detection flag on startup
        self.battery_model.data['new_battery_detected'] = False
        self.battery_model.save()

        # Metrics tracking for current battery state
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

        # Scheduling configuration (always set by load_config; fallback for tests)
        self.scheduling_config = config.scheduling or SchedulingConfig()

        # Voltage sag measurement for internal resistance tracking
        self.sag_state = SagState.IDLE
        self.v_before_sag = None
        self.sag_buffer = []

        self.calibration_last_written_index = 0

        self.has_logged_baseline_lock = False

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
        """Verify NUT upsd is reachable before entering main loop."""
        try:
            _ = self.nut_client.get_ups_vars()
            logger.info("NUT upsd reachable, polling started")
        except Exception:
            logger.warning(
                f"NUT upsd unreachable at {self.config.nut_host}:{self.config.nut_port}, "
                f"will retry every {self.config.polling_interval}s",
                exc_info=True
            )

    def _handle_event_transition(self):
        """
        Execute actions based on event transitions.

        Implements EVT-02 (blackout), EVT-03 (test), EVT-04 (status arbiter),
        and EVT-05 (model update on discharge completion).
        """
        event_type = self.current_metrics.event_type
        previous_event_type = self.current_metrics.previous_event_type

        # EVT-02
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

        # EVT-03
        if event_type == EventType.BLACKOUT_TEST:
            logger.info("Battery test detected; collecting calibration data, no shutdown")
            self.current_metrics.shutdown_imminent = False

        # EVT-04
        self.current_metrics.ups_status_override = compute_ups_status_override(
            event_type,
            self.current_metrics.time_rem_minutes or 0,
            self.shutdown_threshold_minutes
        )

        # EVT-05
        if (self.current_metrics.transition_occurred and
            event_type == EventType.ONLINE and
            previous_event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)):
            logger.info("Power restored; updating LUT with measured discharge points")
            self._update_battery_health()

    # --- Delegation to DischargeHandler ---

    def _sync_handler_refs(self):
        """Sync mutable references to discharge handler.

        Tests may replace daemon.battery_model, daemon.capacity_estimator, or
        daemon.rls_peukert after init. This ensures the handler sees the same
        objects as the daemon.
        """
        dh = self.discharge_handler
        dh.battery_model = self.battery_model
        dh.capacity_estimator = self.capacity_estimator
        dh.rls_peukert = self.rls_peukert

    def _update_battery_health(self):
        """Delegate to DischargeHandler; clear buffer after."""
        self._sync_handler_refs()
        self.discharge_handler.update_battery_health(self.discharge_buffer)
        self.discharge_buffer = DischargeBuffer()

    def _handle_discharge_complete(self, discharge_data: dict) -> None:
        """Delegate to DischargeHandler."""
        self._sync_handler_refs()
        self.discharge_handler.handle_discharge_complete(discharge_data)

    def _auto_calibrate_peukert(self, current_soh: float):
        """Delegate to DischargeHandler."""
        self._sync_handler_refs()
        self.discharge_handler._auto_calibrate_peukert(current_soh, self.discharge_buffer)

    def _log_discharge_prediction(self):
        """Delegate to DischargeHandler."""
        self._sync_handler_refs()
        self.discharge_handler._log_discharge_prediction(
            self.discharge_buffer, self.current_metrics.soc or 0.0)

    # --- Battery baseline reset (stays here — touches rls_ir_k) ---

    def _reset_battery_baseline(self):
        """Reset capacity estimation and SoH history baseline on battery replacement."""

        old_capacity = self.battery_model.data.get('capacity_ah_measured')
        new_capacity = self.battery_model.get_capacity_ah()

        # Clear capacity estimates (will rebuild from next deep discharge)
        self.battery_model.data['capacity_estimates'] = []

        # Clear capacity_ah_measured (will be set when new measurements converge)
        self.battery_model.data['capacity_ah_measured'] = None

        # Add fresh SoH entry with new baseline
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
        # Sync discharge handler's reference
        self.discharge_handler.rls_peukert = self.rls_peukert

        # Structured journald logging for baseline reset
        msg = (f"baseline_reset: capacity baseline reset from {old_capacity:.2f}Ah to {new_capacity:.2f}Ah"
               if old_capacity is not None
               else f"baseline_reset: capacity baseline initialized to {new_capacity:.2f}Ah (first reset)")
        extra = {
            'event_type': 'baseline_reset',
            'capacity_ah_new': f'{new_capacity:.2f}',
            'timestamp': datetime.now(timezone.utc).isoformat(),
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
        except (ValueError, TypeError):
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

        Returns dict with keys: sulfation_score, cycle_roi, soh_percent,
        days_since_last_test, last_blackout, active_credit, cycle_budget.
        """
        return {
            'sulfation_score': self.discharge_handler.last_sulfation_score or 0.0,
            'cycle_roi': self.discharge_handler.last_cycle_roi or 0.0,
            'soh_percent': self.battery_model.get_soh(),
            'days_since_last_test': self._calculate_days_since_last_test(),
            'last_blackout': self._get_last_natural_blackout(),
            'active_credit': self.battery_model.get_blackout_credit(),
            'cycle_budget': self.discharge_handler.last_cycle_budget_remaining or 100,
        }

    def _execute_scheduler_decision(self, decision: SchedulerDecision, sched_inputs: dict, now: datetime) -> None:
        """Act on a scheduler decision: log, persist, and dispatch if proposed.

        Args:
            decision: SchedulerDecision from evaluate_test_scheduling()
            sched_inputs: Dict from _gather_scheduler_inputs() (for structured logging)
            now: Current UTC datetime
        """
        logger.info(f"Scheduler decision: {decision.action}", extra={
            'event_type': 'scheduler_decision',
            'action': decision.action,
            'reason_code': decision.reason_code,
            'sulfation_score': f"{sched_inputs['sulfation_score']:.3f}",
            'roi': f"{sched_inputs['cycle_roi']:.3f}",
            'soh_percent': f"{sched_inputs['soh_percent']:.1%}",
            'timestamp': now.isoformat(),
        })

        # Store for health endpoint (persists between daily scheduler runs)
        self.last_scheduling_reason = decision.reason_code
        self.last_next_test_timestamp = decision.next_eligible_timestamp

        # Update model.json with scheduled info
        self.battery_model.update_scheduling_state(
            scheduled_timestamp=decision.next_eligible_timestamp,
            reason=decision.reason_code,
            block_reason=decision.reason_code if decision.action == 'block_test' else None,
        )

        # If proposed, attempt dispatch
        if decision.action == 'propose_test':
            dispatch_test_with_audit(
                nut_client=self.nut_client,
                battery_model=self.battery_model,
                decision=decision,
                current_metrics=self.current_metrics,
            )
        else:
            logger.info(f"Test {decision.action}: {decision.reason_code}")

        self.battery_model.save()

    def _run_daily_scheduler(self, now: datetime) -> None:
        """Evaluate test scheduling once daily at the configured UTC hour.

        Orchestrates: gather inputs → evaluate → execute decision.
        Resets the evaluated flag after the scheduling window passes.
        """
        current_hour = now.hour
        scheduler_hour = self.scheduling_config.scheduler_eval_hour_utc

        # Reset flag once we leave the scheduling hour
        if current_hour != scheduler_hour:
            self.scheduler_evaluated_today = False
            return

        if self.scheduler_evaluated_today or now.minute >= 10:
            return

        self.scheduler_evaluated_today = True

        try:
            sched_inputs = self._gather_scheduler_inputs()

            if self.scheduling_config.verbose_scheduling:
                logger.debug(
                    "Scheduler inputs",
                    extra={
                        'event_type': 'scheduler_inputs',
                        'sulfation_score': f"{sched_inputs['sulfation_score']:.3f}",
                        'cycle_roi': f"{sched_inputs['cycle_roi']:.3f}",
                        'soh_percent': f"{sched_inputs['soh_percent']:.1%}",
                        'days_since_last_test': f"{sched_inputs['days_since_last_test']:.1f}",
                        'cycle_budget': int(sched_inputs['cycle_budget']),
                    }
                )

            last_blackout = sched_inputs['last_blackout']
            decision = evaluate_test_scheduling(
                sulfation_score=sched_inputs['sulfation_score'],
                cycle_roi=sched_inputs['cycle_roi'],
                soh_percent=sched_inputs['soh_percent'],
                days_since_last_test=sched_inputs['days_since_last_test'],
                last_blackout_timestamp=last_blackout.get('timestamp') if last_blackout else None,
                active_blackout_credit=sched_inputs['active_credit'],
                cycle_budget_remaining=int(sched_inputs['cycle_budget']),
                grid_stability_cooldown_hours=self.scheduling_config.grid_stability_cooldown_hours,
            )

            self._execute_scheduler_decision(decision, sched_inputs, now)
        except Exception as e:
            logger.error(f"Scheduler evaluation failed: {e}", exc_info=True)

    # --- Voltage sag tracking ---

    def _record_voltage_sag(self, v_sag, event_type):
        """Record voltage sag measurement and compute internal resistance."""
        if self.v_before_sag is None or self.ema_filter.load is None:
            return
        load = self.ema_filter.load
        nominal_voltage = self.battery_model.get_nominal_voltage()
        nominal_power_watts = self.battery_model.get_nominal_power_watts()
        I_actual = load / 100.0 * nominal_power_watts / nominal_voltage
        if I_actual <= 0:
            return
        delta_v = self.v_before_sag - v_sag
        r_ohm = delta_v / I_actual
        today = datetime.now().strftime('%Y-%m-%d')
        self.battery_model.add_r_internal_entry(today, r_ohm, self.v_before_sag, v_sag, load, event_type.name)

        # RLS auto-calibration of ir_k from measured sag data
        if nominal_voltage > 0:
            ir_k_measured = r_ohm * nominal_power_watts / (nominal_voltage * 100.0)
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
                    'event_type': 'ir_k_calibration',
                    'ir_k': f'{new_ir_k:.4f}',
                    'ir_k_measured': f'{ir_k_measured:.4f}',
                    'rls_p': f'{new_P:.4f}',
                    'rls_confidence': f'{self.rls_ir_k.confidence:.3f}',
                    'sample_count': str(self.rls_ir_k.sample_count),
                })

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

    # --- Pipeline stages ---

    def _update_ema(self, ups_data):
        """Feed voltage/load into EMA filter, log stabilization event."""
        voltage = ups_data.get('battery.voltage')
        load = ups_data.get('ups.load')
        if voltage is None or load is None:
            return None, None

        # Voltage bounds check (8.0-15.0V) and load bounds check (0-100%)
        if not (8.0 <= voltage <= 15.0):
            logger.warning(f"Voltage {voltage:.2f}V out of bounds [8.0-15.0V]; skipping sample")
            return None, None
        if not (0 <= load <= 100):
            logger.warning(f"Load {load:.1f}% out of bounds [0-100%]; skipping sample")
            return None, None

        self.ema_filter.add_sample(voltage, load)
        self.poll_count += 1
        if self.ema_filter.stabilized and not self._stabilization_logged:
            logger.info(f"EMA buffer stabilized after {self.poll_count} samples, IR compensation active")
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

    def _track_voltage_sag(self, voltage):
        """Measure voltage sag on OL→OB transition to estimate internal resistance.

        State machine: IDLE → MEASURING → COMPLETE → IDLE.
        MEASURING enables fast polling (1s instead of 10s) for precise sag capture.
        """
        event_type = self.current_metrics.event_type

        # OL→OB: start measuring
        if self.event_classifier.transition_occurred and event_type not in (EventType.ONLINE,):
            self.v_before_sag = self.ema_filter.voltage
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
                    f"cycle #{self.battery_model.get_cycle_count()}")

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
                self._start_discharge_collection(timestamp)
            if voltage is not None:
                if len(self.discharge_buffer.voltages) >= DISCHARGE_BUFFER_MAX_SAMPLES:
                    logger.warning(f"Discharge buffer capped at {DISCHARGE_BUFFER_MAX_SAMPLES} samples")
                else:
                    self.discharge_buffer.voltages.append(voltage)
                    self.discharge_buffer.times.append(timestamp)
                    load = self.ema_filter.load if self.ema_filter.load is not None else 0.0
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
                logger.error(f"Calibration write failed at index {i}: {e}", exc_info=True)
                self.calibration_last_written_index = i + 1
                continue

        # Batch flush: persist all accumulated points once per REPORTING_INTERVAL
        points_written = self.calibration_last_written_index
        if points_written > 0:
            try:
                self.battery_model.calibration_batch_flush()
                logger.info(f"Batch flushed {points_written} calibration points to disk")
            except Exception as e:
                logger.error(f"Calibration batch flush failed: {e}", exc_info=True)

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

        v_norm = ir_compensate(v_ema, l_ema, self.ir_reference_load_percent, self.ir_k)
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

        # Log significant changes
        self._log_soc_change(soc, self._last_logged_soc)
        if self._last_logged_time_rem is None or abs(time_rem - self._last_logged_time_rem) > 1.0:
            logger.info(
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
                'load_pct': f'{l_ema:.1f}' if l_ema is not None else 'N/A',
                'charge_pct': charge_str,
                'time_rem': time_rem_str,
                'event': event_str,
                'nut_latency_ms': f'{poll_latency_ms:.0f}' if poll_latency_ms is not None else 'N/A',
            }
        )

    def _write_virtual_ups(self, ups_data, battery_charge, time_rem):
        """Write computed metrics to tmpfs for NUT dummy-ups driver."""
        try:
            ups_status_override = self.current_metrics.ups_status_override or ups_data.get("ups.status", "OL")
            # If classifier returned unknown (kept previous state without transition),
            # and original status contains "OB", pass through original to be safe
            raw_status = ups_data.get("ups.status", "")
            if (not self.event_classifier.transition_occurred
                    and "OB" in raw_status.split()
                    and self.event_classifier.state == EventType.ONLINE):
                ups_status_override = raw_status
            # Enterprise-equivalent metrics computed from discharge history
            soh = self.battery_model.get_soh()
            install_date = self.battery_model.get_battery_install_date() or ""
            cycle_count = self.battery_model.get_cycle_count()
            cumulative_sec = self.battery_model.get_cumulative_on_battery_sec()
            replacement_due = self.battery_model.get_replacement_due() or ""
            # R_internal: median of non-zero measurements, require ≥3 for noise rejection.
            r_internal_history = self.battery_model.get_r_internal_history()
            valid_r_measurements = [e["r_ohm"] for e in r_internal_history if e["r_ohm"] > 0]
            r_internal_mohm = round(sorted(valid_r_measurements)[len(valid_r_measurements) // 2] * 1000, 1) if len(valid_r_measurements) >= 3 else 0

            virtual_metrics = {
                "battery.runtime": int(time_rem * 60) if time_rem is not None else int(float(ups_data.get("battery.runtime", 0))),
                "battery.charge": int(battery_charge) if battery_charge is not None else int(float(ups_data.get("battery.charge", 0))),
                "ups.status": ups_status_override,
                # Enterprise-equivalent fields
                "battery.health": round(soh * 100),
                "battery.date": install_date,
                "battery.cycle.count": cycle_count,
                "battery.cumulative.runtime": int(cumulative_sec),
                "battery.replacement.due": replacement_due,
                "battery.internal_resistance": r_internal_mohm,
                **{k: v for k, v in ups_data.items()
                   if k not in ["battery.runtime", "battery.charge", "ups.status"]}
            }
            write_virtual_ups_dev(virtual_metrics)
        except Exception as e:
            logger.error(f"Failed to write virtual UPS metrics: {e}", exc_info=True)

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

        self._consecutive_errors = 0  # Reset on successful NUT poll

        # Log startup timing on first successful poll
        if not self._startup_logged:
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

        # Event transition handling runs EVERY poll (not gated)
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
        self._write_health_snapshot(poll_latency_ms)

        # Daily scheduler evaluation
        self._run_daily_scheduler(datetime.now(timezone.utc))

        # Report healthy to systemd AFTER critical writes succeed
        sd_notify('WATCHDOG=1')
        time.sleep(1 if self.sag_state == SagState.MEASURING else self.config.polling_interval)

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
        self._startup_time = time.monotonic()  # Startup timing marker

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
