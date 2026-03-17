"""Pure scheduler decision engine for test scheduling with safety gates.

No I/O, no logging, no daemon coupling. Fully testable offline.

The scheduler evaluates test candidacy daily based on:
- Sulfation score (0.0-1.0) indicating crystal growth
- Cycle ROI (desulfation benefit vs wear cost)
- Safety constraints (SoH floor, rate limiting, blackout credit, grid stability)

Output is a SchedulerDecision with action (propose/defer/block) and reason code
for audit trail and decision debugging.

All timestamps use ISO8601 format in UTC.
"""

from dataclasses import dataclass
from typing import Literal, Optional
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class SchedulerDecision:
    """Immutable scheduling decision with full audit trail.

    Attributes:
        action: Decision outcome (propose_test, defer_test, or block_test)
        test_type: Type of test (deep or quick) if action='propose_test', else None
        reason_code: Human-readable reason for decision (e.g., 'soh_floor_55%')
        next_eligible_timestamp: ISO8601 timestamp when test becomes eligible (for defer/block)
    """
    action: Literal['propose_test', 'defer_test', 'block_test']
    test_type: Optional[Literal['deep', 'quick']] = None
    reason_code: str = ""
    next_eligible_timestamp: Optional[str] = None


def evaluate_test_scheduling(
    sulfation_score: float,
    cycle_roi: float,
    soh_percent: float,
    days_since_last_test: float,
    last_blackout_timestamp: Optional[str],
    last_blackout_depth: float,
    active_blackout_credit: Optional[dict],
    cycle_budget_remaining: int,
    soh_floor_threshold: float = 0.60,
    min_days_between_tests: float = 7.0,
    roi_threshold: float = 0.2,
    grid_stability_cooldown_hours: float = 4.0,
) -> SchedulerDecision:
    """Pure scheduler decision engine: evaluate test candidacy with safety gates.

    Args:
        sulfation_score: Current sulfation level [0.0, 1.0] where 1.0 = critical
        cycle_roi: Return-on-investment for discharge [-1.0, 1.0]
        soh_percent: State of health as decimal [0.0, 1.0]
        days_since_last_test: Days elapsed since last upscmd (inf if never tested)
        last_blackout_timestamp: ISO8601 timestamp of most recent natural blackout, or None
        last_blackout_depth: Depth of discharge [0.0, 1.0] for that blackout
        active_blackout_credit: Dict with 'active', 'credit_expires' fields, or None
        cycle_budget_remaining: Cycles left before SoH=65% (mandatory replacement)
        soh_floor_threshold: Minimum acceptable SoH (default 60%)
        min_days_between_tests: Minimum days between tests (default 7.0 = 1/week)
        roi_threshold: Minimum ROI to consider test (default 0.2)
        grid_stability_cooldown_hours: Hours to wait after blackout (default 4.0, 0 = disabled)

    Returns:
        SchedulerDecision with action, test_type, reason_code, next_eligible_timestamp

    Guard clause order (enforcement):
        1. SoH floor gate (SCHED-05)
        2. Rate limiting gate (SCHED-01)
        3. Blackout credit gate (SCHED-03)
        4. Grid stability gate (SCHED-06, configurable)
        5. Cycle budget gate
        6. ROI threshold gate
        7. Sulfation threshold gate
    """
    now = datetime.now(timezone.utc)

    # GATE 1: SoH floor (hard block)
    if soh_percent < soh_floor_threshold:
        floor_percent = int(soh_percent * 100)
        next_eligible = (now + timedelta(days=30)).isoformat()  # Long deferral
        return SchedulerDecision(
            action='block_test',
            reason_code=f'soh_floor_{floor_percent}%',
            next_eligible_timestamp=next_eligible,
        )

    # GATE 2: Rate limiting (1 test per min_days_between_tests)
    if days_since_last_test < min_days_between_tests:
        days_remaining = min_days_between_tests - days_since_last_test
        next_eligible = (now + timedelta(days=days_remaining)).isoformat()
        return SchedulerDecision(
            action='defer_test',
            reason_code=f'rate_limit_{days_remaining:.1f}d_remaining',
            next_eligible_timestamp=next_eligible,
        )

    # GATE 3: Blackout credit (active window blocks test)
    if active_blackout_credit and active_blackout_credit.get('active'):
        try:
            credit_expires_str = active_blackout_credit.get('credit_expires')
            if credit_expires_str:
                credit_expires = datetime.fromisoformat(credit_expires_str)
                if credit_expires > now:
                    # Credit still active
                    return SchedulerDecision(
                        action='defer_test',
                        reason_code=f'blackout_credit_active_until_{credit_expires_str}',
                        next_eligible_timestamp=credit_expires_str,
                    )
        except (ValueError, TypeError):
            pass  # Invalid timestamp, continue to next gate

    # GATE 4: Grid stability (configurable, can be disabled)
    if grid_stability_cooldown_hours > 0 and last_blackout_timestamp:
        try:
            last_blackout_dt = datetime.fromisoformat(last_blackout_timestamp)
            time_since_blackout = (now - last_blackout_dt).total_seconds() / 3600.0  # Hours

            if time_since_blackout < grid_stability_cooldown_hours:
                hours_remaining = grid_stability_cooldown_hours - time_since_blackout
                next_eligible = (now + timedelta(hours=hours_remaining)).isoformat()
                return SchedulerDecision(
                    action='defer_test',
                    reason_code=f'grid_unstable_blackout_{time_since_blackout:.1f}h_ago',
                    next_eligible_timestamp=next_eligible,
                )
        except (ValueError, TypeError):
            pass  # Invalid timestamp, continue
    # Note: if grid_stability_cooldown_hours == 0, gate is skipped entirely (disabled)

    # GATE 5: Cycle budget (critical low)
    if cycle_budget_remaining < 5:
        next_eligible = (now + timedelta(days=60)).isoformat()  # Very long deferral
        return SchedulerDecision(
            action='block_test',
            reason_code=f'critical_cycle_budget_{cycle_budget_remaining}_remaining',
            next_eligible_timestamp=next_eligible,
        )

    # GATE 6: ROI threshold (marginal benefit)
    # Only defer if ROI is low AND we have plenty of cycles (conservative approach)
    if cycle_roi < roi_threshold and cycle_budget_remaining > 20:
        roi_rounded = round(cycle_roi, 2)
        next_eligible = (now + timedelta(days=2)).isoformat()
        return SchedulerDecision(
            action='defer_test',
            reason_code=f'marginal_roi_{roi_rounded}',
            next_eligible_timestamp=next_eligible,
        )

    # GATE 7: Sulfation threshold (decision logic)
    # All gates passed; now decide test type based on sulfation
    if sulfation_score > 0.65:
        # High sulfation: recommend deep test
        roi_rounded = round(cycle_roi, 2)
        sulfation_rounded = round(sulfation_score, 2)
        return SchedulerDecision(
            action='propose_test',
            test_type='deep',
            reason_code=f'sulfation_{sulfation_rounded}_roi_{roi_rounded}',
        )
    elif sulfation_score > 0.40:
        # Medium sulfation: recommend quick test
        ir_rounded = round(sulfation_score, 2)
        return SchedulerDecision(
            action='propose_test',
            test_type='quick',
            reason_code=f'sulfation_{ir_rounded}_ir_measure',
        )
    else:
        # Low sulfation: no test needed
        sulfation_rounded = round(sulfation_score, 2)
        next_eligible = (now + timedelta(days=2)).isoformat()
        return SchedulerDecision(
            action='defer_test',
            reason_code=f'low_sulfation_{sulfation_rounded}',
            next_eligible_timestamp=next_eligible,
        )
