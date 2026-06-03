"""Unit tests for scheduler decision engine (pure function tests).

Tests cover the diagnostic time-cadence engine (Phase 25):
- First test from cold start (deadlock fix)
- Annual cadence propose
- Within-cadence defer (restart no-retrigger)
- Rate-limit via days_since_last_attempt (independent of cadence)
- Failed-attempt boundary cases (two-input timing split)
- SoH floor block
- Grid cooldown defer
- Cycle budget block
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.battery_math.scheduler import SchedulerDecision, evaluate_test_scheduling


class TestSchedulerDecision:
    """SchedulerDecision dataclass tests."""

    def test_decision_immutable(self):
        """SchedulerDecision is frozen (immutable)."""
        decision = SchedulerDecision(
            action="propose_test",
            test_type="quick",
            reason_code="diagnostic_cadence",
        )
        with pytest.raises(AttributeError):
            decision.action = "block_test"


class TestFirstTestDeadlockFix:
    """First test from cold start: deadlock fix (SCH-01).

    With days_since_last_test_success=inf and days_since_last_attempt=inf,
    the cadence gate sees overdue and proposes a quick test.
    """

    def test_first_test_proposes_quick(self):
        """Cold start (never tested): proposes quick test via diagnostic cadence."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=float("inf"),
            days_since_last_attempt=float("inf"),
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.action == "propose_test"
        assert decision.test_type == "quick"
        assert decision.reason_code == "diagnostic_cadence"

    def test_first_test_has_no_next_eligible(self):
        """Proposed test has no next_eligible_timestamp (it's being proposed now)."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=float("inf"),
            days_since_last_attempt=float("inf"),
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.next_eligible_timestamp is None

    def test_first_test_reason_detail_mentions_days(self):
        """Reason detail includes days since last successful test."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=float("inf"),
            days_since_last_attempt=float("inf"),
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert "inf" in decision.reason_detail.lower() or "since last" in decision.reason_detail.lower()


class TestAnnualCadence:
    """Annual cadence: propose when days_since_last_test_success >= 365."""

    def test_propose_on_cadence_overdue(self):
        """400 days since last successful test (>= 365): proposes."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=400.0,
            days_since_last_attempt=400.0,
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.action == "propose_test"
        assert decision.test_type == "quick"
        assert decision.reason_code == "diagnostic_cadence"

    def test_propose_at_exactly_365(self):
        """Exactly 365 days: boundary — propose (>= 365 threshold)."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=365.0,
            days_since_last_attempt=365.0,
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.action == "propose_test"
        assert decision.reason_code == "diagnostic_cadence"

    def test_defer_just_under_cadence(self):
        """364 days since last success (< 365): defers within_cadence."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=364.0,
            days_since_last_attempt=364.0,
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.action == "defer_test"
        assert decision.reason_code == "within_cadence"
        assert decision.next_eligible_timestamp is not None

    def test_within_cadence_next_eligible_approx(self):
        """within_cadence: next_eligible is approx 365-30 = 335 days from now."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=30.0,
            days_since_last_attempt=30.0,
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.action == "defer_test"
        assert decision.reason_code == "within_cadence"
        # next_eligible should be ~335 days out
        from datetime import datetime, timezone
        next_eligible_dt = datetime.fromisoformat(decision.next_eligible_timestamp)
        now = datetime.now(timezone.utc)
        days_until = (next_eligible_dt - now).total_seconds() / 86400.0
        assert 334 < days_until < 336, f"Expected ~335d, got {days_until:.1f}d"


class TestRestartNoRetrigger:
    """Restart-safety: days_since_last_test_success just under interval defers, not proposes (SCH-02)."""

    def test_restart_no_retrigger_30d(self):
        """30 days since success, 30 since attempt: defers within_cadence (not re-triggered)."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=30.0,
            days_since_last_attempt=30.0,
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.action == "defer_test"
        assert decision.reason_code == "within_cadence"

    def test_restart_no_retrigger_180d(self):
        """180 days since success (< 365): defers within_cadence."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=180.0,
            days_since_last_attempt=180.0,
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.action == "defer_test"
        assert decision.reason_code == "within_cadence"


class TestRateLimitGate:
    """Rate-limit gate keys off days_since_last_attempt (ANY attempt, OK or ERR).

    Gate 2 runs BEFORE cadence gate 5, so a recent attempt is rate-limited even if
    days_since_last_test_success = inf (never succeeded).
    """

    def test_rate_limit_recent_attempt_defers(self):
        """3 days since attempt (< 7): rate-limited, even if success=inf."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=float("inf"),
            days_since_last_attempt=3.0,
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.action == "defer_test"
        assert decision.reason_code == "rate_limit"
        assert decision.next_eligible_timestamp is not None

    def test_rate_limit_at_boundary_7d(self):
        """Exactly 7 days since attempt: passes rate-limit gate."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=float("inf"),
            days_since_last_attempt=7.0,
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.reason_code != "rate_limit"

    def test_rate_limit_never_attempted_passes(self):
        """inf days since attempt: passes rate-limit gate."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=float("inf"),
            days_since_last_attempt=float("inf"),
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.reason_code != "rate_limit"


class TestFailedAttemptBoundaries:
    """Two-input timing split boundary cases (resolves cycle-2 HIGH).

    Failed attempt 1d ago: rate-limited (days_since_last_attempt=1 < 7).
    Failed attempt 8d ago: cadence overdue (days_since_last_attempt=8 >= 7, success=inf >= 365).
    """

    def test_failed_attempt_1d_ago_is_rate_limited(self):
        """Failed dispatch 1d ago: rate-limited, NOT retried next daily run.

        days_since_last_attempt=1 (<7) → rate_limit
        days_since_last_test_success=inf → cadence overdue, but rate-limit fires first
        """
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=float("inf"),
            days_since_last_attempt=1.0,
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.action == "defer_test"
        assert decision.reason_code == "rate_limit"

    def test_failed_attempt_8d_ago_proposes(self):
        """Failed dispatch 8d ago: rate-limit cleared, cadence overdue → proposes.

        days_since_last_attempt=8 (>=7, rate-limit cleared)
        days_since_last_test_success=inf (>=365, cadence overdue)
        Transient error did NOT defer cadence ~365 days — two-input split holds.
        """
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=float("inf"),
            days_since_last_attempt=8.0,
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.action == "propose_test"
        assert decision.reason_code == "diagnostic_cadence"

    def test_successful_test_30d_ago_defers_within_cadence(self):
        """Successful test 30d ago: days_since_last_test_success=30 < 365 → within_cadence."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=30.0,
            days_since_last_attempt=30.0,
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.action == "defer_test"
        assert decision.reason_code == "within_cadence"


class TestSoHFloorGate:
    """SoH floor gate: blocks test if soh_fraction < SOH_FLOOR (0.60)."""

    def test_soh_floor_blocks_below_threshold(self):
        """SoH 0.55 < 60%: hard block regardless of cadence."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.55,
            days_since_last_test_success=float("inf"),
            days_since_last_attempt=float("inf"),
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.action == "block_test"
        assert decision.reason_code == "soh_floor"
        assert decision.next_eligible_timestamp is not None

    def test_soh_floor_at_boundary_passes(self):
        """SoH exactly 60%: passes floor gate."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.60,
            days_since_last_test_success=float("inf"),
            days_since_last_attempt=float("inf"),
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.reason_code != "soh_floor"

    def test_soh_floor_before_rate_limit(self):
        """SoH floor (gate 1) fires before rate-limit (gate 2)."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.55,  # Below floor
            days_since_last_test_success=float("inf"),
            days_since_last_attempt=3.0,  # Also rate-limited
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.reason_code == "soh_floor"


class TestGridCooldownGate:
    """Grid stability gate: defers test after recent blackout."""

    def test_recent_blackout_defers(self):
        """Blackout 1h ago with 4h cooldown: defers grid_unstable."""
        last_blackout = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=400.0,
            days_since_last_attempt=400.0,
            last_blackout_timestamp=last_blackout,
            cycle_budget_remaining=100,
            grid_stability_cooldown_hours=4.0,
        )
        assert decision.action == "defer_test"
        assert decision.reason_code == "grid_unstable"

    def test_old_blackout_passes(self):
        """Blackout 5h ago with 4h cooldown: passes grid gate."""
        last_blackout = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=400.0,
            days_since_last_attempt=400.0,
            last_blackout_timestamp=last_blackout,
            cycle_budget_remaining=100,
            grid_stability_cooldown_hours=4.0,
        )
        assert decision.reason_code != "grid_unstable"

    def test_no_blackout_passes(self):
        """No blackout timestamp: passes grid gate."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=400.0,
            days_since_last_attempt=400.0,
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
            grid_stability_cooldown_hours=4.0,
        )
        assert decision.reason_code != "grid_unstable"

    def test_cooldown_disabled_ignores_blackout(self):
        """cooldown_hours=0: gate disabled; recent blackout ignored."""
        last_blackout = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=400.0,
            days_since_last_attempt=400.0,
            last_blackout_timestamp=last_blackout,
            cycle_budget_remaining=100,
            grid_stability_cooldown_hours=0.0,
        )
        assert decision.reason_code != "grid_unstable"

    def test_invalid_blackout_timestamp_passes(self):
        """Invalid blackout timestamp: treated as unavailable, passes."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=400.0,
            days_since_last_attempt=400.0,
            last_blackout_timestamp="INVALID_TIMESTAMP",
            cycle_budget_remaining=100,
            grid_stability_cooldown_hours=4.0,
        )
        assert decision.reason_code != "grid_unstable"


class TestCycleBudgetGate:
    """Cycle budget gate: blocks test when < CRITICAL_CYCLE_BUDGET (5)."""

    def test_critical_budget_blocks(self):
        """Cycle budget 3 < 5: hard block."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=400.0,
            days_since_last_attempt=400.0,
            last_blackout_timestamp=None,
            cycle_budget_remaining=3,
        )
        assert decision.action == "block_test"
        assert decision.reason_code == "critical_cycle_budget"

    def test_budget_at_boundary_passes(self):
        """Cycle budget exactly 5: passes (boundary)."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=400.0,
            days_since_last_attempt=400.0,
            last_blackout_timestamp=None,
            cycle_budget_remaining=5,
        )
        assert decision.reason_code != "critical_cycle_budget"


class TestGateOrderIntegration:
    """Verify gate ordering: SoH → rate-limit → grid → cycle-budget → cadence."""

    def test_all_gates_pass_proposes_quick(self):
        """All gates pass: proposes quick test via diagnostic_cadence."""
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=float("inf"),
            days_since_last_attempt=float("inf"),
            last_blackout_timestamp=None,
            cycle_budget_remaining=100,
        )
        assert decision.action == "propose_test"
        assert decision.test_type == "quick"

    def test_never_proposes_deep(self):
        """Engine only autonomously proposes quick (not deep) per SCH-03."""
        # Run many scenarios that would have been "deep" in the old engine
        for soh in [0.65, 0.85, 0.95]:
            decision = evaluate_test_scheduling(
                soh_fraction=soh,
                days_since_last_test_success=400.0,
                days_since_last_attempt=400.0,
                last_blackout_timestamp=None,
                cycle_budget_remaining=100,
            )
            if decision.action == "propose_test":
                assert decision.test_type == "quick", f"Expected quick, got deep for soh={soh}"

    def test_rate_limit_before_grid_cooldown(self):
        """Rate-limit (gate 2) fires before grid cooldown (gate 3)."""
        last_blackout = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=float("inf"),
            days_since_last_attempt=2.0,  # Rate-limited
            last_blackout_timestamp=last_blackout,  # Also grid unstable
            cycle_budget_remaining=100,
            grid_stability_cooldown_hours=4.0,
        )
        assert decision.reason_code == "rate_limit"

    def test_grid_before_cycle_budget(self):
        """Grid stability (gate 3) fires before cycle budget (gate 4)."""
        last_blackout = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        decision = evaluate_test_scheduling(
            soh_fraction=0.9,
            days_since_last_test_success=float("inf"),
            days_since_last_attempt=float("inf"),
            last_blackout_timestamp=last_blackout,
            cycle_budget_remaining=3,  # Also critical budget
            grid_stability_cooldown_hours=4.0,
        )
        assert decision.reason_code == "grid_unstable"
