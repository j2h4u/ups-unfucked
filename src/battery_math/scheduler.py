"""Pure scheduler decision engine for test scheduling with safety gates.

No I/O, no daemon coupling. Fully testable offline.
Logging limited to safety-gate corruption warnings.

The scheduler evaluates test candidacy daily based on:
- Sulfation score (0.0-1.0) indicating crystal growth
- Cycle ROI (desulfation benefit vs wear cost)
- Safety constraints (SoH floor, rate limiting, blackout credit, grid stability)

Output is a SchedulerDecision with action (propose/defer/block) and reason code
for audit trail and decision debugging.

All timestamps use ISO8601 format in UTC.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

logger = logging.getLogger("ups-battery-monitor")

# Algorithmic constants — internal to scheduler, not user-configurable.
SOH_FLOOR = 0.60  # Below 60% SoH, testing accelerates degradation (IEEE-450 guidance)
MIN_DAYS_BETWEEN_TESTS = 7.0  # 7 days: balance sulfation monitoring vs cycle wear
ROI_THRESHOLD = 0.2  # Minimum benefit/cost ratio to justify a test cycle
CRITICAL_CYCLE_BUDGET = 5  # At ≤5 cycles remaining, every cycle counts — hard block
DEEP_SULFATION_THRESHOLD = 0.65  # Score above 0.65 indicates crystal growth worth a deep test
QUICK_SULFATION_THRESHOLD = 0.40  # Moderate sulfation: quick test suffices (less wear than deep)


@dataclass(frozen=True)
class SchedulerDecision:
    """Immutable scheduling decision with full audit trail.

    Attributes:
        action: Decision outcome (propose_test, defer_test, or block_test)
        test_type: Type of test (deep or quick) if action='propose_test', else None
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
    sulfation_score: float,
    cycle_roi: float,
    soh_fraction: float,
    days_since_last_test: float,
    last_blackout_timestamp: Optional[str],
    active_blackout_credit: Optional[dict],
    cycle_budget_remaining: int,
    grid_stability_cooldown_hours: float = 4.0,
) -> SchedulerDecision:
    """Pure scheduler decision engine: evaluate test candidacy with safety gates.

    Args:
        sulfation_score: Current sulfation level [0.0, 1.0] where 1.0 = critical
        cycle_roi: Return-on-investment for discharge [-1.0, 1.0]
        soh_fraction: State of health as decimal [0.0, 1.0]
        days_since_last_test: Days elapsed since last upscmd (inf if never tested)
        last_blackout_timestamp: ISO8601 timestamp of most recent natural blackout, or None
        active_blackout_credit: Dict with 'active', 'credit_expires' fields, or None
        cycle_budget_remaining: Cycles left before SoH=65% (mandatory replacement)
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

    # Gate 1: SoH floor (SCHED-05)
    if soh_fraction < SOH_FLOOR:
        floor_percent = int(soh_fraction * 100)
        return _decision(
            "block_test",
            "soh_floor",
            f"soh={floor_percent}%, floor={int(SOH_FLOOR * 100)}%",
            eligible_in=timedelta(days=30),
        )

    # Gate 2: Rate limiting (SCHED-01)
    if days_since_last_test < MIN_DAYS_BETWEEN_TESTS:
        days_remaining = MIN_DAYS_BETWEEN_TESTS - days_since_last_test
        return _decision(
            "defer_test",
            "rate_limit",
            f"{days_remaining:.1f}d remaining",
            eligible_in=timedelta(days=days_remaining),
        )

    # Gate 3: Blackout credit (SCHED-03)
    if active_blackout_credit and active_blackout_credit.get("active"):
        credit_expires_str = active_blackout_credit.get("credit_expires")
        if credit_expires_str:
            credit_expires = _parse_iso_or_warn(credit_expires_str, "credit_expires")
            if credit_expires and credit_expires > now:
                # Credit still active
                return _decision(
                    "defer_test",
                    "blackout_credit_active",
                    f"expires {credit_expires_str}",
                    eligible_at=credit_expires_str,
                )

    # Gate 4: Grid stability (SCHED-06)
    if grid_stability_cooldown_hours > 0 and last_blackout_timestamp:
        last_blackout_dt = _parse_iso_or_warn(last_blackout_timestamp, "last_blackout_timestamp")
        if last_blackout_dt:
            time_since_blackout = (now - last_blackout_dt).total_seconds() / 3600.0  # Hours
            if time_since_blackout < grid_stability_cooldown_hours:
                hours_remaining = grid_stability_cooldown_hours - time_since_blackout
                return _decision(
                    "defer_test",
                    "grid_unstable",
                    f"blackout {time_since_blackout:.1f}h ago",
                    eligible_in=timedelta(hours=hours_remaining),
                )

    # Gate 5: Cycle budget
    if cycle_budget_remaining < CRITICAL_CYCLE_BUDGET:
        return _decision(
            "block_test",
            "critical_cycle_budget",
            f"{cycle_budget_remaining} cycles remaining",
            eligible_in=timedelta(days=60),  # Very long deferral
        )

    # Gate 6: ROI threshold
    # Only defer if ROI is low AND we have plenty of cycles (conservative approach)
    if cycle_roi < ROI_THRESHOLD and cycle_budget_remaining > 20:
        return _decision(
            "defer_test",
            "marginal_roi",
            f"roi={cycle_roi:.2f}, threshold={ROI_THRESHOLD}",
            eligible_in=timedelta(days=2),
        )

    # Gate 7: Sulfation threshold — all gates passed, decide test type
    if sulfation_score > DEEP_SULFATION_THRESHOLD:
        # High sulfation: recommend deep test
        return _decision(
            "propose_test",
            "sulfation_high",
            f"score={sulfation_score:.2f}, roi={cycle_roi:.2f}",
            test_type="deep",
        )
    elif sulfation_score > QUICK_SULFATION_THRESHOLD:
        # Medium sulfation: recommend quick test
        return _decision(
            "propose_test",
            "sulfation_moderate",
            f"score={sulfation_score:.2f}",
            test_type="quick",
        )
    else:
        # Low sulfation: no test needed
        return _decision(
            "defer_test",
            "low_sulfation",
            f"score={sulfation_score:.2f}",
            eligible_in=timedelta(days=2),
        )
