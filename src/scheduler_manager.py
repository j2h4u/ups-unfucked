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

from src.battery_math.scheduler import SchedulerDecision, evaluate_test_scheduling
from src.model import BatteryModel
from src.monitor_config import CurrentMetrics, SchedulingConfig, safe_save

logger = logging.getLogger("ups-battery-monitor")


def validate_preconditions_before_upscmd(
    ups_status: str,
    soc: float,
    recent_power_glitches: int,
) -> tuple[bool, str]:
    """Validate preconditions before dispatching test command.

    Guard clauses (must all pass):
    - UPS is online: 'OL' in ups_status and 'OB' not in ups_status and 'CAL' not in ups_status
    - SoC ≥95%: soc >= 0.95
    - Grid stable: recent_power_glitches ≤ 2 (not yet implemented — caller passes 0)

    Overlapping dispatches are prevented by the scheduler's rate-limit gate
    (MIN_DAYS_BETWEEN_TESTS, keyed off last_upscmd_timestamp written on every
    attempt) — far more robust than a persisted in-flight flag, which has no
    completion signal to clear it and would block all future tests forever.

    Args:
        ups_status: UPS status string (e.g., "OL", "OB DISCHRG", "CAL")
        soc: State of charge [0.0, 1.0]
        recent_power_glitches: Count of grid state changes in last 4h

    Returns:
        tuple[bool, str]: (can_proceed, reason_if_blocked)
    """
    if "OL" not in ups_status or "OB" in ups_status or "CAL" in ups_status:
        return False, "UPS_not_online_cannot_test_during_discharge"

    if soc < 0.95:
        return False, "soc_below_threshold"

    if recent_power_glitches > 2:
        return False, "grid_unstable"

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

    preconditions_ok, block_reason = validate_preconditions_before_upscmd(
        ups_status=ups_status,
        soc=soc,
        recent_power_glitches=recent_power_glitches,
    )

    if not preconditions_ok:
        logger.info(
            f"Test dispatch precondition blocked: {block_reason}",
            extra={
                "event_type": "test_precondition_blocked",
                "reason": block_reason,
            },
        )
        return False

    command = f"test.battery.start.{decision.test_type}"
    upscmd_timestamp = datetime.now(timezone.utc).isoformat()

    try:
        success, result_msg = nut_client.send_instcmd(command)
    except (socket.error, OSError, ValueError) as e:
        battery_model.update_upscmd_result(
            upscmd_timestamp=upscmd_timestamp,
            upscmd_type=command,
            upscmd_status=f"ERR_SOCKET: {e}",
        )
        safe_save(battery_model)
        logger.error(f"Test dispatch socket error: {e}", exc_info=True)
        return False

    if success:
        upscmd_status = "OK"
    else:
        upscmd_status = result_msg or "ERR_UNKNOWN"

    battery_model.update_upscmd_result(
        upscmd_timestamp=upscmd_timestamp,
        upscmd_type=command,
        upscmd_status=upscmd_status,
    )
    safe_save(battery_model)

    if success:
        logger.info(
            f"Test dispatched: {command}",
            extra={
                "event_type": "test_dispatched",
                "test_type": decision.test_type,
                "command": command,
                "reason_code": decision.reason_code,
            },
        )
        return True
    else:
        logger.error(
            f"Test dispatch failed: {result_msg or 'unknown error'}",
            extra={
                "event_type": "test_dispatch_failed",
                "command": command,
                "error": result_msg or "unknown",
            },
        )
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
                upscmd result persistence, and grid-stability blackout queries.
            nut_client: NUTClient instance for sending test commands.
            scheduling_config: SchedulingConfig with eval_hour_utc, cooldown, verbose flag.
            discharge_handler: DischargeHandler for cycle budget remaining.
        """
        self.battery_model = battery_model
        self.nut_client = nut_client
        self.scheduling_config = scheduling_config
        self.discharge_handler = discharge_handler
        self.scheduler_evaluated_today = False
        self._last_scheduling_reason: str = "observing"
        self._last_next_test_timestamp: Optional[str] = None

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
                        "event_type": "scheduler_inputs",
                        "soh_fraction": f"{scheduler_inputs['soh_fraction']:.1%}",
                        "days_since_last_test_success": f"{scheduler_inputs['days_since_last_test_success']:.1f}",
                        "days_since_last_attempt": f"{scheduler_inputs['days_since_last_attempt']:.1f}",
                        "cycle_budget": int(scheduler_inputs["cycle_budget"]),
                    },
                )

            last_blackout = scheduler_inputs["last_blackout"]
            decision = evaluate_test_scheduling(
                soh_fraction=scheduler_inputs["soh_fraction"],
                days_since_last_test_success=scheduler_inputs["days_since_last_test_success"],
                days_since_last_attempt=scheduler_inputs["days_since_last_attempt"],
                last_blackout_timestamp=last_blackout.get("timestamp") if last_blackout else None,
                cycle_budget_remaining=int(scheduler_inputs["cycle_budget"]),
                grid_stability_cooldown_hours=self.scheduling_config.grid_stability_cooldown_hours,
            )

            self._execute_scheduler_decision(decision, scheduler_inputs, now, current_metrics)
        except (
            KeyError,
            AttributeError,
            TypeError,
            ValueError,
            OSError,
            ConnectionError,
            TimeoutError,
        ) as e:
            logger.error(
                f"Scheduler evaluation failed: {e}",
                exc_info=True,
                extra={"event_type": "scheduler_error", "error_class": type(e).__name__},
            )

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

    def _calculate_days_since_last_attempt(self) -> float:
        """Calculate days since last upscmd attempt regardless of status, or inf if never.

        Reads ONLY last_upscmd_timestamp via the public getter — status-agnostic.
        Any dispatch (OK or ERR) sets last_upscmd_timestamp via update_upscmd_result,
        so this value is small after a failed dispatch and large (or inf) after no attempt.
        Feeds the rate-limit gate only; keying off any attempt prevents a failed dispatch
        from being retried on the next daily run.
        """
        last_ts = self.battery_model.get_last_upscmd_timestamp()
        if not last_ts:
            return float("inf")
        try:
            last_dt = datetime.fromisoformat(last_ts)
            return (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400.0
        except (ValueError, TypeError) as e:
            logger.debug(
                f"Invalid last_upscmd_timestamp '{last_ts}': {e}; treating as never attempted"
            )
            return float("inf")

    def _calculate_days_since_last_test_success(self) -> float:
        """Calculate days since last SUCCESSFUL upscmd (status='OK'), or inf if never succeeded.

        Reads last_upscmd_timestamp via get_last_upscmd_timestamp() AND
        last_upscmd_status via get_last_upscmd_status() — both through public getters,
        never reaching into battery_model.state[...] directly for upscmd status.

        Returns inf when:
        - last_upscmd_status is absent (never attempted)
        - last_upscmd_status != 'OK' (transient error: ERR_SOCKET, ERR_UNKNOWN, etc.)
        - last_upscmd_timestamp is absent or unparseable (corrupt state)

        This ensures a failed dispatch cannot defer the next annual diagnostic ~365 days:
        update_upscmd_result writes timestamp+status together on every attempt, so a
        failed dispatch (status='ERR_SOCKET: ...') yields days_since_last_test_success=inf
        while days_since_last_attempt is small — the two-input split holds simultaneously.

        Feeds the annual cadence gate only.
        """
        status = self.battery_model.get_last_upscmd_status()
        if status != "OK":
            # Never succeeded, or last dispatch failed — cadence clock not reset
            return float("inf")
        last_ts = self.battery_model.get_last_upscmd_timestamp()
        if not last_ts:
            return float("inf")
        try:
            last_dt = datetime.fromisoformat(last_ts)
            return (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400.0
        except (ValueError, TypeError) as e:
            logger.debug(
                f"Invalid last_upscmd_timestamp '{last_ts}': {e}; treating as never succeeded"
            )
            return float("inf")

    def _get_last_natural_blackout(self) -> Optional[dict]:
        """Return most recent natural blackout event (DoD, timestamp).

        Reads discharge_events filtered by event_reason=="natural".
        """
        events = self.battery_model.state.get("discharge_events", [])
        for event in reversed(events):  # Most recent first
            if event.get("event_reason") == "natural":
                return {
                    "timestamp": event.get("timestamp"),
                    "depth": event.get("depth_of_discharge", 0.0),
                }
        return None

    def _gather_scheduler_inputs(self) -> dict:
        """Collect all inputs needed for scheduler evaluation.

        Returns dict with keys: soh_fraction, days_since_last_test_success,
        days_since_last_attempt, last_blackout, cycle_budget.

        Two separate timing inputs — never collapsed into one value:
        - days_since_last_test_success: age of last OK diagnostic (inf if never OK)
          → feeds cadence gate
        - days_since_last_attempt: age of last dispatch regardless of status (inf if never)
          → feeds rate-limit gate
        """
        return {
            "soh_fraction": self.battery_model.get_soh(),
            "days_since_last_test_success": self._calculate_days_since_last_test_success(),
            "days_since_last_attempt": self._calculate_days_since_last_attempt(),
            "last_blackout": self._get_last_natural_blackout(),
            "cycle_budget": self.discharge_handler.last_cycle_budget_remaining or 100,
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
        logger.info(
            f"Scheduler decision: {decision.action}",
            extra={
                "event_type": "scheduler_decision",
                "action": decision.action,
                "reason_code": decision.reason_code,
                "reason_detail": decision.reason_detail,
                "soh_fraction": f"{scheduler_inputs['soh_fraction']:.1%}",
            },
        )

        # Scheduling output is health.json-only (no model.json mirror) — nothing to
        # reconcile. The in-memory last_* properties below are what the health snapshot
        # reads each poll; model.json carries only learned state (upscmd results, SoH,
        # capacity estimates, LUT). There is no model.json scheduling copy to diverge.
        self.last_scheduling_reason = decision.reason_code
        self.last_next_test_timestamp = decision.next_eligible_timestamp

        if decision.action == "propose_test":
            dispatched = dispatch_test_with_audit(
                nut_client=self.nut_client,
                battery_model=self.battery_model,
                decision=decision,
                current_metrics=current_metrics,
            )
            if not dispatched:
                logger.warning(
                    "Test proposed but dispatch failed",
                    extra={
                        "event_type": "test_dispatch_not_sent",
                        "reason_code": decision.reason_code,
                    },
                )
        else:
            logger.info(
                f"Test {decision.action}: {decision.reason_code} ({decision.reason_detail})"
            )

        self.battery_model.save()
