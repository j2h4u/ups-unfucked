"""Unit tests for scheduler decision engine (pure function tests)."""

import pytest
from datetime import datetime, timedelta, timezone
from src.battery_math.scheduler import evaluate_test_scheduling, SchedulerDecision


class TestSchedulerDecision:
    """SchedulerDecision dataclass tests."""

    def test_decision_immutable(self):
        """SchedulerDecision is frozen (immutable)."""
        decision = SchedulerDecision(
            action='propose_test',
            test_type='deep',
            reason_code='test_reason',
        )
        with pytest.raises(AttributeError):
            decision.action = 'block_test'


class TestSoHFloorGate:
    """SoH floor gate (SCHED-05): blocks test if SoH < SOH_FLOOR (0.60)."""

    def test_soh_floor_blocks_below_threshold(self):
        """SoH below 60%: blocks (hard block)."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.72,
            cycle_roi=0.4,
            soh_percent=0.59,  # Below 60% constant
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
        )
        assert decision.action == 'block_test'
        assert 'soh_floor' in decision.reason_code
        assert decision.next_eligible_timestamp is not None

    def test_soh_floor_at_boundary(self):
        """SoH at exactly 60%: passes (boundary condition)."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.60,  # Exactly at threshold
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
        )
        # Should pass SoH floor gate and evaluate other gates
        assert decision.action in ['propose_test', 'defer_test', 'block_test']
        assert 'soh_floor' not in decision.reason_code


class TestRateLimitGate:
    """Rate limiting gate (SCHED-01): enforces ≤1 test per week (MIN_DAYS_BETWEEN_TESTS=7)."""

    def test_rate_limit_defers_recent_test(self):
        """Test <7 days since last: deferred."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=3.0,  # 3 days, less than 7
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
        )
        assert decision.action == 'defer_test'
        assert 'rate_limit' in decision.reason_code
        assert decision.next_eligible_timestamp is not None

    def test_rate_limit_at_boundary(self):
        """Test at exactly 7 days: passes rate limit gate."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=7.0,  # Exactly at threshold
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
        )
        # Should pass rate limit gate
        assert 'rate_limit' not in decision.reason_code

    def test_rate_limit_never_tested(self):
        """Test with infinite days since last: passes rate limit."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=float('inf'),  # Never tested
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
        )
        assert 'rate_limit' not in decision.reason_code


class TestBlackoutCreditGate:
    """Blackout credit gate (SCHED-03): defer test when credit is active."""

    def test_blackout_credit_active_defers(self):
        """Active blackout credit defers test."""
        credit_expires = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit={
                'active': True,
                'credit_expires': credit_expires,
            },
            cycle_budget_remaining=50,
        )
        assert decision.action == 'defer_test'
        assert 'blackout_credit' in decision.reason_code

    def test_blackout_credit_expired_passes(self):
        """Expired blackout credit: passes gate."""
        credit_expires = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit={
                'active': True,
                'credit_expires': credit_expires,
            },
            cycle_budget_remaining=50,
        )
        assert 'blackout_credit' not in decision.reason_code

    def test_blackout_credit_inactive_passes(self):
        """Inactive blackout credit (active=False): passes gate."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit={
                'active': False,
                'credit_expires': None,
            },
            cycle_budget_remaining=50,
        )
        assert 'blackout_credit' not in decision.reason_code

    def test_blackout_credit_none_passes(self):
        """No blackout credit: passes gate."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
        )
        assert 'blackout_credit' not in decision.reason_code

    def test_blackout_credit_invalid_timestamp(self):
        """Invalid credit_expires timestamp: treated as expired, passes."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit={
                'active': True,
                'credit_expires': 'INVALID_TIMESTAMP',
            },
            cycle_budget_remaining=50,
        )
        # Invalid timestamp ignored, continues to next gate
        assert 'blackout_credit' not in decision.reason_code


class TestGridStabilityGate:
    """Grid stability gate (SCHED-06): configurable, can be disabled."""

    def test_grid_instability_defers_when_enabled(self):
        """Recent blackout with cooldown enabled: defers test."""
        last_blackout = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=last_blackout,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
            grid_stability_cooldown_hours=4.0,  # 4h cooldown enabled
        )
        assert decision.action == 'defer_test'
        assert 'grid_unstable' in decision.reason_code or 'blackout' in decision.reason_code

    def test_grid_stability_disabled_ignores_blackout(self):
        """Cooldown hours = 0: gate disabled, recent blackout is ignored."""
        last_blackout = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=last_blackout,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
            grid_stability_cooldown_hours=0.0,  # DISABLED
        )
        # Should NOT defer due to recent blackout
        assert 'grid_unstable' not in decision.reason_code
        # Should evaluate other gates and propose test
        assert decision.action in ['propose_test', 'defer_test', 'block_test']

    def test_grid_stability_old_blackout_passes(self):
        """Blackout older than cooldown window: passes gate."""
        last_blackout = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=last_blackout,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
            grid_stability_cooldown_hours=4.0,  # 4h cooldown
        )
        assert 'grid_unstable' not in decision.reason_code

    def test_grid_stability_no_blackout_passes(self):
        """No blackout timestamp: passes gate."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
            grid_stability_cooldown_hours=4.0,
        )
        assert 'grid_unstable' not in decision.reason_code

    def test_grid_stability_invalid_timestamp(self):
        """Invalid blackout timestamp: treated as unavailable, passes."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp='INVALID_TIMESTAMP',

            active_blackout_credit=None,
            cycle_budget_remaining=50,
            grid_stability_cooldown_hours=4.0,
        )
        assert 'grid_unstable' not in decision.reason_code


class TestCycleBudgetGate:
    """Cycle budget gate: blocks test when cycles critical (< CRITICAL_CYCLE_BUDGET=5)."""

    def test_critical_cycle_budget_blocks(self):
        """Cycle budget < 5: blocks test."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=3,  # Critical: only 3 cycles left
        )
        assert decision.action == 'block_test'
        assert 'critical_cycle_budget' in decision.reason_code

    def test_cycle_budget_at_boundary(self):
        """Cycle budget = 5: passes gate (boundary)."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=5,  # At threshold
        )
        # Should pass cycle budget gate
        assert 'critical_cycle_budget' not in decision.reason_code


class TestROIGate:
    """ROI threshold gate: defers low ROI (marginal benefit)."""

    def test_low_roi_defers_with_plenty_cycles(self):
        """ROI < ROI_THRESHOLD (0.2) with >20 cycles remaining: defers."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.1,  # Low ROI
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=50,  # Plenty of cycles
        )
        assert decision.action == 'defer_test'
        assert 'marginal_roi' in decision.reason_code

    def test_low_roi_ignores_gate_when_cycles_critical(self):
        """ROI < threshold but cycles < 20: gate doesn't apply (other gates take priority)."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.1,  # Low ROI
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=15,  # < 20, so ROI gate doesn't apply
        )
        # Should evaluate sulfation gate instead
        assert 'marginal_roi' not in decision.reason_code

    def test_positive_roi_passes_gate(self):
        """ROI >= threshold: passes gate."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.25,  # Above 0.2 threshold
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
        )
        assert 'marginal_roi' not in decision.reason_code


class TestSulfationThreshold:
    """Sulfation threshold gate: proposes deep/quick or defers low sulfation."""

    def test_propose_deep_test_high_sulfation(self):
        """Sulfation > DEEP_SULFATION_THRESHOLD (0.65): proposes deep test."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.72,
            cycle_roi=0.35,
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
        )
        assert decision.action == 'propose_test'
        assert decision.test_type == 'deep'
        assert 'sulfation' in decision.reason_code

    def test_propose_quick_test_medium_sulfation(self):
        """Sulfation 0.40-0.65: proposes quick test."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.52,
            cycle_roi=0.25,
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
        )
        assert decision.action == 'propose_test'
        assert decision.test_type == 'quick'
        assert 'sulfation' in decision.reason_code

    def test_defer_low_sulfation(self):
        """Sulfation < QUICK_SULFATION_THRESHOLD (0.40): defers (no test needed)."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.32,
            cycle_roi=0.25,  # Good ROI so ROI gate doesn't interfere
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
        )
        assert decision.action == 'defer_test'
        assert 'low_sulfation' in decision.reason_code
        assert decision.next_eligible_timestamp is not None

    def test_sulfation_boundary_0_65(self):
        """Sulfation = 0.65: boundary between quick and deep."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
        )
        assert decision.action == 'propose_test'
        # At boundary 0.65: if sulfation_score > 0.65 is False, elif sulfation_score > 0.40 is True
        assert decision.test_type == 'quick'  # Boundary: <= 0.65 → quick

    def test_sulfation_boundary_0_40(self):
        """Sulfation = 0.40: boundary between medium and low."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.40,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
        )
        # At boundary 0.40: elif sulfation_score > 0.40 is False, so goes to else (low sulfation)
        assert decision.action == 'defer_test'
        assert 'low_sulfation' in decision.reason_code


class TestGateOrdering:
    """Test that gates enforce in correct order (SoH first, then rate limit, etc.)."""

    def test_soh_floor_before_rate_limit(self):
        """SoH floor gate evaluated before rate limit."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.55,  # Below floor
            days_since_last_test=2.0,  # Also below rate limit
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
        )
        # SoH floor should be the reason (evaluated first)
        assert 'soh_floor' in decision.reason_code
        assert 'rate_limit' not in decision.reason_code

    def test_rate_limit_before_blackout_credit(self):
        """Rate limit evaluated before blackout credit."""
        credit_expires = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=2.0,  # Below rate limit
            last_blackout_timestamp=None,

            active_blackout_credit={
                'active': True,
                'credit_expires': credit_expires,
            },
            cycle_budget_remaining=50,
        )
        # Rate limit should be the reason (evaluated first)
        assert 'rate_limit' in decision.reason_code
        assert 'blackout_credit' not in decision.reason_code


class TestRealWorldScenarios:
    """Integration-like scenarios with multiple gates."""

    def test_typical_good_condition(self):
        """Typical battery in good condition: proposes deep test."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.72,  # > 0.65 for deep test
            cycle_roi=0.34,
            soh_percent=0.85,
            days_since_last_test=10.0,
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
        )
        assert decision.action == 'propose_test'
        assert decision.test_type == 'deep'

    def test_battery_approaching_eol(self):
        """Battery SoH low: test is blocked."""
        decision = evaluate_test_scheduling(
            sulfation_score=0.8,  # High sulfation, would propose
            cycle_roi=0.5,  # Good ROI
            soh_percent=0.55,  # Below 60% floor
            days_since_last_test=30.0,  # Long time since test
            last_blackout_timestamp=None,

            active_blackout_credit=None,
            cycle_budget_remaining=50,
        )
        assert decision.action == 'block_test'
        assert 'soh_floor' in decision.reason_code

    def test_recent_natural_blackout(self):
        """Battery recently had natural deep discharge: blackout credit defers test."""
        credit_expires = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        decision = evaluate_test_scheduling(
            sulfation_score=0.65,
            cycle_roi=0.3,
            soh_percent=0.85,
            days_since_last_test=15.0,  # Long enough for rate limit
            last_blackout_timestamp=None,

            active_blackout_credit={
                'active': True,
                'credit_expires': credit_expires,
            },
            cycle_budget_remaining=50,
        )
        assert decision.action == 'defer_test'
        assert 'blackout_credit' in decision.reason_code
