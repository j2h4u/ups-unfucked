"""Scheduler manager — daily test scheduling, precondition validation, dispatch.

Extracted from MonitorDaemon to reduce its responsibility surface (ARCH-04).
SchedulerManager owns the daily scheduler evaluation, precondition checking,
and test dispatch — all stateful collaborator behavior that does not belong
inline in the daemon orchestration loop.
"""

import logging
import socket
from datetime import datetime, timezone
from typing import Optional

from src.model import BatteryModel
from src.battery_math.scheduler import SchedulerDecision, evaluate_test_scheduling
from src.monitor_config import CurrentMetrics, SchedulingConfig, safe_save

logger = logging.getLogger('ups-battery-monitor')


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
    test_already_running = battery_model.state.get('test_running', False)

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
        battery_model.state['test_running'] = True
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


class SchedulerManager:
    """Daily test scheduler — evaluation, precondition checks, and dispatch.

    Owns all scheduler state (evaluated_today flag, last decision reason,
    next test timestamp) and orchestrates the daily evaluation pipeline:
    gather inputs → evaluate → execute decision.

    Usage:
        scheduler = SchedulerManager(battery_model, nut_client, scheduling_config, discharge_handler)
        # Each poll:
        scheduler.run_daily(datetime.now(timezone.utc), current_metrics)
        # In health snapshot:
        reason = scheduler.last_scheduling_reason
        ts = scheduler.last_next_test_timestamp
    """

    def __init__(
        self,
        battery_model: BatteryModel,
        nut_client,
        scheduling_config: SchedulingConfig,
        discharge_handler,
    ):
        """Initialize SchedulerManager.

        Args:
            battery_model: Persistent battery model — used for scheduling state,
                upscmd result persistence, and blackout credit queries.
            nut_client: NUTClient instance for sending test commands.
            scheduling_config: SchedulingConfig with eval_hour_utc, cooldown, verbose flag.
            discharge_handler: DischargeHandler for sulfation score, cycle ROI, cycle budget.
        """
        self.battery_model = battery_model
        self.nut_client = nut_client
        self.scheduling_config = scheduling_config
        self.discharge_handler = discharge_handler
        self.scheduler_evaluated_today = False
        self.last_scheduling_reason: str = 'observing'
        self.last_next_test_timestamp: Optional[str] = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def last_scheduling_reason(self) -> str:
        """Most recent scheduling decision reason code."""
        return self._last_scheduling_reason

    @last_scheduling_reason.setter
    def last_scheduling_reason(self, value: str) -> None:
        self._last_scheduling_reason = value

    @property
    def last_next_test_timestamp(self) -> Optional[str]:
        """ISO8601 timestamp of next eligible test, or None."""
        return self._last_next_test_timestamp

    @last_next_test_timestamp.setter
    def last_next_test_timestamp(self, value: Optional[str]) -> None:
        self._last_next_test_timestamp = value

    def run_daily(self, now: datetime, current_metrics: CurrentMetrics) -> None:
        """Evaluate test scheduling once daily at the configured UTC hour.

        Orchestrates: gather inputs → evaluate → execute decision.

        Args:
            now: Current UTC datetime.
            current_metrics: CurrentMetrics for precondition checks in dispatch.
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

            self._execute_scheduler_decision(decision, scheduler_inputs, now, current_metrics)
        except (KeyError, AttributeError, TypeError, ValueError, OSError, ConnectionError, TimeoutError) as e:
            logger.error(f"Scheduler evaluation failed: {e}", exc_info=True,
                         extra={'event_type': 'scheduler_error', 'error_class': type(e).__name__})

    # ------------------------------------------------------------------
    # Private methods
    # ------------------------------------------------------------------

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

    def _get_last_natural_blackout(self) -> Optional[dict]:
        """Return most recent natural blackout event (DoD, timestamp)."""
        events = self.battery_model.state.get('discharge_events', [])
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

    def _execute_scheduler_decision(
        self,
        decision: SchedulerDecision,
        scheduler_inputs: dict,
        now: datetime,
        current_metrics: CurrentMetrics,
    ) -> None:
        """Act on a scheduler decision: log, persist, and dispatch if proposed.

        Args:
            decision: SchedulerDecision from evaluate_test_scheduling()
            scheduler_inputs: Dict from _gather_scheduler_inputs() (for structured logging)
            now: Current UTC datetime
            current_metrics: CurrentMetrics passed through to dispatch_test_with_audit
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
                current_metrics=current_metrics,
            )
            if not dispatched:
                logger.warning("Test proposed but dispatch failed",
                               extra={'event_type': 'test_dispatch_not_sent',
                                      'reason_code': decision.reason_code})
        else:
            logger.info(f"Test {decision.action}: {decision.reason_code} ({decision.reason_detail})")

        self.battery_model.save()
