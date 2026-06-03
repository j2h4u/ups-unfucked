"""Pure scheduler decision engine for diagnostic battery capacity/SoH verification.

No I/O, no daemon coupling. Fully testable offline.
Logging limited to safety-gate corruption warnings.

The scheduler evaluates test candidacy daily based on a persistent time cadence
(IEEE-1188 annual capacity verification) and safety constraints:
- SoH floor, rate-limiting, grid stability, cycle budget
- Proposes a diagnostic `quick` test when the annual cadence is overdue

Output is a SchedulerDecision with action (propose/defer/block) and reason code
for audit trail and decision debugging.

Guard clause order (enforcement):
    1. SoH floor gate      — blocks testing when battery too degraded
    2. Rate-limit gate     — keyed off days_since_last_attempt (ANY attempt, OK or ERR)
    3. Grid stability gate — defers after recent blackout (configurable cooldown)
    4. Cycle budget gate   — blocks when cycles critically low
    5. Cadence gate        — keyed off days_since_last_test_success (OK only);
                             proposes when >= DIAGNOSTIC_TEST_INTERVAL_DAYS

Two-input timing split (resolves cycle-2 HIGH):
    - days_since_last_attempt  → rate-limit gate only (any dispatch, OK or ERR)
    - days_since_last_test_success → cadence gate only (last OK diagnostic)
    These are computed separately in SchedulerManager and MUST NOT be collapsed.
    A failed dispatch writes last_upscmd_timestamp but not "OK" status; this means
    days_since_last_attempt is small (rate-limited) while days_since_last_test_success
    stays inf — so a transient error cannot defer the next annual diagnostic ~365 days.

All timestamps use ISO8601 format in UTC.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

logger = logging.getLogger("ups-battery-monitor")

# Algorithmic constants — internal to scheduler, not user-configurable.
SOH_FLOOR = 0.60  # Below 60% SoH, testing accelerates degradation (IEEE-450 guidance)
MIN_DAYS_BETWEEN_TESTS = 7.0  # Minimum days between any dispatch attempts (rate-limit)
DIAGNOSTIC_TEST_INTERVAL_DAYS = 365.0  # IEEE-1188 annual capacity/diagnostic cadence
CRITICAL_CYCLE_BUDGET = 5  # At ≤5 cycles remaining, every cycle counts — hard block


@dataclass(frozen=True)
class SchedulerDecision:
    """Immutable scheduling decision with full audit trail.

    Attributes:
        action: Decision outcome (propose_test, defer_test, or block_test)
        test_type: Type of test (deep or quick) if action='propose_test', else None.
            Engine only autonomously proposes 'quick' per SCH-03; 'deep' is reserved
            for Literal completeness but is not emitted autonomously.
        reason_code: Stable category string for metric labels (e.g., 'soh_floor', 'rate_limit').
            Must NOT contain variable numeric data — use reason_detail for that.
        reason_detail: Human-readable detail with numeric context (e.g., 'soh=55%, floor=60%').
            Safe for logs and model.json but NOT for metric labels.
        next_eligible_timestamp: ISO8601 timestamp when test becomes eligible (for defer/block)
    """

    action: Literal["propose_test", "defer_test", "block_test"]
    test_type: Optional[Literal["deep", "quick"]] = None
    reason_code: str = ""
    reason_detail: str = ""
    next_eligible_timestamp: Optional[str] = None


def _parse_iso_or_warn(raw: str, field: str) -> Optional[datetime]:
    """Parse an ISO8601 timestamp; on corruption log a structured warning and return None.

    Lets a safety gate skip itself on a malformed model.json value instead of crashing.
    """
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        logger.warning(
            "Corrupted %s timestamp %r — scheduling gate skipped",
            field,
            raw,
            extra={"event_type": "corrupted_scheduling_timestamp", "field": field},
        )
        return None


def evaluate_test_scheduling(
    *,
    soh_fraction: float,
    days_since_last_test_success: float,
    days_since_last_attempt: float,
    last_blackout_timestamp: Optional[str],
    cycle_budget_remaining: int,
    grid_stability_cooldown_hours: float = 4.0,
) -> SchedulerDecision:
    """Pure diagnostic scheduler: evaluate test candidacy with safety gates.

    Args:
        soh_fraction: State of health as decimal [0.0, 1.0]
        days_since_last_test_success: Days since last OK diagnostic (inf if never succeeded).
            Drives the annual cadence gate (gate 5) only.
        days_since_last_attempt: Days since last dispatch attempt regardless of OK/ERR status
            (inf if never attempted). Drives the rate-limit gate (gate 2) only.
            A failed dispatch still writes last_upscmd_timestamp, so a failed dispatch 1d ago
            yields days_since_last_attempt=1 (rate-limited) AND days_since_last_test_success=inf
            (cadence not deferred ~365d). Both requirements hold simultaneously.
        last_blackout_timestamp: ISO8601 timestamp of most recent natural blackout, or None
        cycle_budget_remaining: Cycles left before SoH=65% (mandatory replacement)
        grid_stability_cooldown_hours: Hours to wait after blackout (default 4.0, 0 = disabled)

    Returns:
        SchedulerDecision with action, test_type, reason_code, next_eligible_timestamp

    Guard clause order (enforcement):
        1. SoH floor gate      (block_test/soh_floor)
        2. Rate-limit gate     (defer_test/rate_limit) — keyed off days_since_last_attempt
        3. Grid stability gate (defer_test/grid_unstable)
        4. Cycle budget gate   (block_test/critical_cycle_budget)
        5. Cadence gate        — propose_test/diagnostic_cadence if overdue,
                                 else defer_test/within_cadence
    """
    now = datetime.now(timezone.utc)

    def _decision(
        action: Literal["propose_test", "defer_test", "block_test"],
        reason_code: str,
        reason_detail: str,
        *,
        eligible_in: Optional[timedelta] = None,
        eligible_at: Optional[str] = None,
        test_type: Optional[Literal["deep", "quick"]] = None,
    ) -> SchedulerDecision:
        """Build a decision, resolving next-eligible from a delta-from-now or a literal timestamp.

        Exactly one of eligible_in / eligible_at is set for defer/block gates; propose
        gates pass neither (next_eligible_timestamp stays None).
        """
        if eligible_at is not None:
            next_eligible = eligible_at
        elif eligible_in is not None:
            next_eligible = (now + eligible_in).isoformat()
        else:
            next_eligible = None
        return SchedulerDecision(
            action=action,
            test_type=test_type,
            reason_code=reason_code,
            reason_detail=reason_detail,
            next_eligible_timestamp=next_eligible,
        )

    # Gate 1: SoH floor — test would accelerate degradation on a weak battery
    if soh_fraction < SOH_FLOOR:
        floor_percent = int(soh_fraction * 100)
        return _decision(
            "block_test",
            "soh_floor",
            f"soh={floor_percent}%, floor={int(SOH_FLOOR * 100)}%",
            eligible_in=timedelta(days=30),
        )

    # Gate 2: Rate-limit — keyed off days_since_last_attempt (ANY dispatch, OK or ERR).
    # A failed dispatch still writes last_upscmd_timestamp via update_upscmd_result,
    # so this gate fires even when the attempt failed — preventing a retry the next daily run.
    if days_since_last_attempt < MIN_DAYS_BETWEEN_TESTS:
        days_remaining = MIN_DAYS_BETWEEN_TESTS - days_since_last_attempt
        return _decision(
            "defer_test",
            "rate_limit",
            f"{days_remaining:.1f}d remaining",
            eligible_in=timedelta(days=days_remaining),
        )

    # Gate 3: Grid stability — defer after a recent blackout (configurable cooldown)
    if grid_stability_cooldown_hours > 0 and last_blackout_timestamp:
        last_blackout_dt = _parse_iso_or_warn(last_blackout_timestamp, "last_blackout_timestamp")
        if last_blackout_dt:
            time_since_blackout = (now - last_blackout_dt).total_seconds() / 3600.0  # hours
            if time_since_blackout < grid_stability_cooldown_hours:
                hours_remaining = grid_stability_cooldown_hours - time_since_blackout
                return _decision(
                    "defer_test",
                    "grid_unstable",
                    f"blackout {time_since_blackout:.1f}h ago",
                    eligible_in=timedelta(hours=hours_remaining),
                )

    # Gate 4: Cycle budget — hard block when too few cycles remain
    if cycle_budget_remaining < CRITICAL_CYCLE_BUDGET:
        return _decision(
            "block_test",
            "critical_cycle_budget",
            f"{cycle_budget_remaining} cycles remaining",
            eligible_in=timedelta(days=60),
        )

    # Gate 5: Diagnostic cadence — keyed off days_since_last_test_success (OK only).
    # A failed dispatch leaves days_since_last_test_success=inf (because last_upscmd_status
    # is not "OK"), so a transient error cannot defer the next annual diagnostic ~365 days.
    # First test (never succeeded, days_since_last_test_success=inf) satisfies >= 365 → proposes.
    if days_since_last_test_success >= DIAGNOSTIC_TEST_INTERVAL_DAYS:
        return _decision(
            "propose_test",
            "diagnostic_cadence",
            f"{days_since_last_test_success:.0f}d since last successful test",
            test_type="quick",
        )
    else:
        days_remaining = DIAGNOSTIC_TEST_INTERVAL_DAYS - days_since_last_test_success
        return _decision(
            "defer_test",
            "within_cadence",
            f"{days_remaining:.0f}d until next diagnostic",
            eligible_in=timedelta(days=days_remaining),
        )
