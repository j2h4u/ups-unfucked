"""Unit tests for SchedulerManager and module-level dispatch functions."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, MagicMock, patch

from src.scheduler_manager import (
    SchedulerManager,
    validate_preconditions_before_upscmd,
    dispatch_test_with_audit,
)
from src.battery_math.scheduler import SchedulerDecision
from src.monitor_config import SchedulingConfig, CurrentMetrics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scheduling_config(**kwargs):
    defaults = dict(
        scheduler_eval_hour_utc=10,
        grid_stability_cooldown_hours=4.0,
        verbose_scheduling=False,
    )
    defaults.update(kwargs)
    return SchedulingConfig(**defaults)


def _make_scheduler(battery_model=None, nut_client=None, scheduling_config=None, discharge_handler=None):
    battery_model = battery_model or Mock()
    nut_client = nut_client or Mock()
    scheduling_config = scheduling_config or _make_scheduling_config()
    discharge_handler = discharge_handler or Mock(
        last_sulfation_score=0.1,
        last_cycle_roi=0.5,
        last_cycle_budget_remaining=100,
    )
    return SchedulerManager(
        battery_model=battery_model,
        nut_client=nut_client,
        scheduling_config=scheduling_config,
        discharge_handler=discharge_handler,
    )


# ---------------------------------------------------------------------------
# TestSchedulerManager
# ---------------------------------------------------------------------------

class TestSchedulerManager:
    """Direct unit tests for SchedulerManager without constructing MonitorDaemon."""

    # --- Constructor ---

    def test_constructor_stores_dependencies(self):
        """SchedulerManager stores all constructor arguments as attributes."""
        bm = Mock()
        nc = Mock()
        cfg = _make_scheduling_config()
        dh = Mock()

        sm = SchedulerManager(battery_model=bm, nut_client=nc, scheduling_config=cfg, discharge_handler=dh)

        assert sm.battery_model is bm
        assert sm.nut_client is nc
        assert sm.scheduling_config is cfg
        assert sm.discharge_handler is dh

    def test_constructor_initial_state(self):
        """SchedulerManager initial state: not evaluated, reason='observing', timestamp=None."""
        sm = _make_scheduler()

        assert sm.scheduler_evaluated_today is False
        assert sm.last_scheduling_reason == 'observing'
        assert sm.last_next_test_timestamp is None

    # --- Properties ---

    def test_last_scheduling_reason_default(self):
        """last_scheduling_reason defaults to 'observing'."""
        sm = _make_scheduler()
        assert sm.last_scheduling_reason == 'observing'

    def test_last_scheduling_reason_settable(self):
        """last_scheduling_reason can be updated."""
        sm = _make_scheduler()
        sm.last_scheduling_reason = 'soh_below_floor'
        assert sm.last_scheduling_reason == 'soh_below_floor'

    def test_last_next_test_timestamp_default(self):
        """last_next_test_timestamp defaults to None."""
        sm = _make_scheduler()
        assert sm.last_next_test_timestamp is None

    def test_last_next_test_timestamp_settable(self):
        """last_next_test_timestamp can be set to an ISO timestamp string."""
        sm = _make_scheduler()
        ts = '2026-03-20T10:00:00+00:00'
        sm.last_next_test_timestamp = ts
        assert sm.last_next_test_timestamp == ts

    # --- _should_run_scheduler ---

    def test_should_run_correct_hour_not_evaluated(self):
        """Returns True when hour matches, not yet evaluated, minute < 10."""
        sm = _make_scheduler(scheduling_config=_make_scheduling_config(scheduler_eval_hour_utc=10))
        now = datetime(2026, 3, 20, 10, 5, 0, tzinfo=timezone.utc)
        assert sm._should_run_scheduler(now) is True

    def test_should_run_wrong_hour_returns_false(self):
        """Returns False when current hour != configured eval hour."""
        sm = _make_scheduler(scheduling_config=_make_scheduling_config(scheduler_eval_hour_utc=10))
        now = datetime(2026, 3, 20, 11, 5, 0, tzinfo=timezone.utc)
        assert sm._should_run_scheduler(now) is False

    def test_should_run_wrong_hour_resets_evaluated_flag(self):
        """Wrong hour resets scheduler_evaluated_today to False."""
        sm = _make_scheduler(scheduling_config=_make_scheduling_config(scheduler_eval_hour_utc=10))
        sm.scheduler_evaluated_today = True
        now = datetime(2026, 3, 20, 11, 5, 0, tzinfo=timezone.utc)
        sm._should_run_scheduler(now)
        assert sm.scheduler_evaluated_today is False

    def test_should_run_already_evaluated_today_returns_false(self):
        """Returns False when scheduler_evaluated_today is True."""
        sm = _make_scheduler(scheduling_config=_make_scheduling_config(scheduler_eval_hour_utc=10))
        sm.scheduler_evaluated_today = True
        now = datetime(2026, 3, 20, 10, 5, 0, tzinfo=timezone.utc)
        assert sm._should_run_scheduler(now) is False

    def test_should_run_minute_gte_10_returns_false(self):
        """Returns False when minute >= 10 (window has passed)."""
        sm = _make_scheduler(scheduling_config=_make_scheduling_config(scheduler_eval_hour_utc=10))
        now = datetime(2026, 3, 20, 10, 10, 0, tzinfo=timezone.utc)
        assert sm._should_run_scheduler(now) is False

    def test_should_run_minute_9_returns_true(self):
        """Returns True at minute 9 (still within window)."""
        sm = _make_scheduler(scheduling_config=_make_scheduling_config(scheduler_eval_hour_utc=10))
        now = datetime(2026, 3, 20, 10, 9, 0, tzinfo=timezone.utc)
        assert sm._should_run_scheduler(now) is True

    # --- _calculate_days_since_last_test ---

    def test_days_since_last_test_no_timestamp_returns_inf(self):
        """Returns inf when no upscmd timestamp in model."""
        bm = Mock()
        bm.get_last_upscmd_timestamp.return_value = None
        sm = _make_scheduler(battery_model=bm)
        result = sm._calculate_days_since_last_test()
        assert result == float('inf')

    def test_days_since_last_test_valid_timestamp(self):
        """Returns correct float days for a recent valid timestamp."""
        bm = Mock()
        # 2 days ago
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        bm.get_last_upscmd_timestamp.return_value = two_days_ago
        sm = _make_scheduler(battery_model=bm)
        result = sm._calculate_days_since_last_test()
        assert 1.9 < result < 2.1

    def test_days_since_last_test_invalid_timestamp_returns_inf(self):
        """Returns inf for unparseable timestamp string."""
        bm = Mock()
        bm.get_last_upscmd_timestamp.return_value = "not-a-timestamp"
        sm = _make_scheduler(battery_model=bm)
        result = sm._calculate_days_since_last_test()
        assert result == float('inf')

    # --- _get_last_natural_blackout ---

    def test_get_last_natural_blackout_no_events_returns_none(self):
        """Returns None when discharge_events list is empty."""
        bm = Mock()
        bm.data = {'discharge_events': []}
        sm = _make_scheduler(battery_model=bm)
        assert sm._get_last_natural_blackout() is None

    def test_get_last_natural_blackout_returns_most_recent(self):
        """Returns the most recent natural event."""
        bm = Mock()
        bm.data = {
            'discharge_events': [
                {'event_reason': 'natural', 'timestamp': '2026-03-10T10:00:00Z', 'depth_of_discharge': 0.3},
                {'event_reason': 'natural', 'timestamp': '2026-03-18T10:00:00Z', 'depth_of_discharge': 0.5},
            ]
        }
        sm = _make_scheduler(battery_model=bm)
        result = sm._get_last_natural_blackout()
        assert result is not None
        assert result['timestamp'] == '2026-03-18T10:00:00Z'
        assert result['depth'] == 0.5

    def test_get_last_natural_blackout_skips_non_natural(self):
        """Skips test events; only returns natural blackout events."""
        bm = Mock()
        bm.data = {
            'discharge_events': [
                {'event_reason': 'natural', 'timestamp': '2026-03-10T10:00:00Z', 'depth_of_discharge': 0.3},
                {'event_reason': 'test', 'timestamp': '2026-03-18T10:00:00Z', 'depth_of_discharge': 0.7},
            ]
        }
        sm = _make_scheduler(battery_model=bm)
        result = sm._get_last_natural_blackout()
        assert result is not None
        assert result['timestamp'] == '2026-03-10T10:00:00Z'

    def test_get_last_natural_blackout_no_natural_events_returns_none(self):
        """Returns None when all events are tests (no natural blackouts)."""
        bm = Mock()
        bm.data = {
            'discharge_events': [
                {'event_reason': 'test', 'timestamp': '2026-03-18T10:00:00Z', 'depth_of_discharge': 0.7},
            ]
        }
        sm = _make_scheduler(battery_model=bm)
        assert sm._get_last_natural_blackout() is None

    # --- _gather_scheduler_inputs ---

    def test_gather_scheduler_inputs_returns_all_7_keys(self):
        """_gather_scheduler_inputs returns dict with all 7 required keys."""
        bm = Mock()
        bm.data = {'discharge_events': []}
        bm.get_soh.return_value = 0.85
        bm.get_last_upscmd_timestamp.return_value = None
        bm.get_blackout_credit.return_value = None

        dh = Mock()
        dh.last_sulfation_score = 0.2
        dh.last_cycle_roi = 0.6
        dh.last_cycle_budget_remaining = 80

        sm = _make_scheduler(battery_model=bm, discharge_handler=dh)
        inputs = sm._gather_scheduler_inputs()

        required_keys = {'sulfation_score', 'cycle_roi', 'soh_fraction',
                         'days_since_last_test', 'last_blackout', 'active_credit', 'cycle_budget'}
        assert set(inputs.keys()) == required_keys

    def test_gather_scheduler_inputs_values(self):
        """_gather_scheduler_inputs returns correct values from dependencies."""
        bm = Mock()
        bm.data = {'discharge_events': []}
        bm.get_soh.return_value = 0.9
        bm.get_last_upscmd_timestamp.return_value = None
        bm.get_blackout_credit.return_value = {'active': True}

        dh = Mock()
        dh.last_sulfation_score = 0.3
        dh.last_cycle_roi = 0.7
        dh.last_cycle_budget_remaining = 50

        sm = _make_scheduler(battery_model=bm, discharge_handler=dh)
        inputs = sm._gather_scheduler_inputs()

        assert inputs['sulfation_score'] == 0.3
        assert inputs['cycle_roi'] == 0.7
        assert inputs['soh_fraction'] == 0.9
        assert inputs['days_since_last_test'] == float('inf')
        assert inputs['last_blackout'] is None
        assert inputs['active_credit'] == {'active': True}
        assert inputs['cycle_budget'] == 50

    # --- run_daily ---

    def test_run_daily_skips_wrong_hour(self):
        """run_daily does nothing when _should_run_scheduler returns False (wrong hour)."""
        sm = _make_scheduler(scheduling_config=_make_scheduling_config(scheduler_eval_hour_utc=10))
        now = datetime(2026, 3, 20, 11, 5, 0, tzinfo=timezone.utc)

        with patch.object(sm, '_gather_scheduler_inputs') as mock_gather:
            sm.run_daily(now, Mock())
            mock_gather.assert_not_called()

    def test_run_daily_skips_already_evaluated(self):
        """run_daily does nothing when already evaluated today."""
        sm = _make_scheduler(scheduling_config=_make_scheduling_config(scheduler_eval_hour_utc=10))
        sm.scheduler_evaluated_today = True
        now = datetime(2026, 3, 20, 10, 5, 0, tzinfo=timezone.utc)

        with patch.object(sm, '_gather_scheduler_inputs') as mock_gather:
            sm.run_daily(now, Mock())
            mock_gather.assert_not_called()

    def test_run_daily_skips_minute_gte_10(self):
        """run_daily does nothing when minute >= 10."""
        sm = _make_scheduler(scheduling_config=_make_scheduling_config(scheduler_eval_hour_utc=10))
        now = datetime(2026, 3, 20, 10, 15, 0, tzinfo=timezone.utc)

        with patch.object(sm, '_gather_scheduler_inputs') as mock_gather:
            sm.run_daily(now, Mock())
            mock_gather.assert_not_called()

    def test_run_daily_sets_evaluated_today_flag(self):
        """run_daily sets scheduler_evaluated_today=True after running."""
        bm = Mock()
        bm.data = {'discharge_events': []}
        bm.get_soh.return_value = 0.85
        bm.get_last_upscmd_timestamp.return_value = None
        bm.get_blackout_credit.return_value = None

        sm = _make_scheduler(battery_model=bm)
        now = datetime(2026, 3, 20, 10, 5, 0, tzinfo=timezone.utc)

        decision = SchedulerDecision(action='defer_test', test_type='deep', reason_code='soh_ok')

        with patch('src.scheduler_manager.evaluate_test_scheduling', return_value=decision):
            sm.run_daily(now, Mock())

        assert sm.scheduler_evaluated_today is True

    def test_run_daily_updates_last_scheduling_reason(self):
        """run_daily updates last_scheduling_reason from decision."""
        bm = Mock()
        bm.data = {'discharge_events': []}
        bm.get_soh.return_value = 0.85
        bm.get_last_upscmd_timestamp.return_value = None
        bm.get_blackout_credit.return_value = None

        sm = _make_scheduler(battery_model=bm)
        now = datetime(2026, 3, 20, 10, 5, 0, tzinfo=timezone.utc)

        decision = SchedulerDecision(action='defer_test', test_type='deep', reason_code='blackout_credit_active')

        with patch('src.scheduler_manager.evaluate_test_scheduling', return_value=decision):
            sm.run_daily(now, Mock())

        assert sm.last_scheduling_reason == 'blackout_credit_active'


# ---------------------------------------------------------------------------
# Module-level function tests (import path verification)
# ---------------------------------------------------------------------------

class TestValidatePreconditionsImport:
    """Verify validate_preconditions_before_upscmd is importable from scheduler_manager."""

    def test_all_checks_pass(self):
        """All preconditions met: test can proceed."""
        can_proceed, reason = validate_preconditions_before_upscmd(
            ups_status="OL",
            soc=0.98,
            recent_power_glitches=0,
            test_already_running=False,
        )
        assert can_proceed is True
        assert reason == ""

    def test_blocks_ob_status(self):
        """OB status blocks dispatch."""
        can_proceed, reason = validate_preconditions_before_upscmd(
            ups_status="OB DISCHRG",
            soc=0.98,
            recent_power_glitches=0,
            test_already_running=False,
        )
        assert can_proceed is False
        assert "online" in reason.lower()


class TestDispatchWithAuditImport:
    """Verify dispatch_test_with_audit is importable from scheduler_manager and works."""

    def test_dispatch_success_updates_model(self, temporary_model_path):
        """Successful dispatch updates model and returns True."""
        from src.model import BatteryModel

        model = BatteryModel(temporary_model_path)
        nut_client_mock = Mock()
        nut_client_mock.send_instcmd = Mock(return_value=(True, None))

        current_metrics = Mock()
        current_metrics.ups_status_override = "OL"
        current_metrics.soc = 0.98

        decision = SchedulerDecision(
            action='propose_test',
            test_type='deep',
            reason_code='sulfation_high',
        )

        with patch('src.scheduler_manager.logger'):
            success = dispatch_test_with_audit(
                nut_client=nut_client_mock,
                battery_model=model,
                decision=decision,
                current_metrics=current_metrics,
            )

        assert success is True
        assert model.data.get('test_running') is True
        assert model.data['last_upscmd_status'] == 'OK'
