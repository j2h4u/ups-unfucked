"""Unit tests for SchedulerManager and module-level dispatch functions."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from src.battery_math.scheduler import SchedulerDecision
from src.monitor_config import SchedulingConfig
from src.scheduler_manager import (
    SchedulerManager,
    dispatch_test_with_audit,
    validate_preconditions_before_upscmd,
)

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


def _make_scheduler(
    battery_model=None, nut_client=None, scheduling_config=None, discharge_handler=None
):
    battery_model = battery_model or Mock()
    nut_client = nut_client or Mock()
    scheduling_config = scheduling_config or _make_scheduling_config()
    discharge_handler = discharge_handler or Mock(
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

        sm = SchedulerManager(
            battery_model=bm, nut_client=nc, scheduling_config=cfg, discharge_handler=dh
        )

        assert sm.battery_model is bm
        assert sm.nut_client is nc
        assert sm.scheduling_config is cfg
        assert sm.discharge_handler is dh

    def test_constructor_initial_state(self):
        """SchedulerManager initial state: not evaluated, reason='observing', timestamp=None."""
        sm = _make_scheduler()

        assert sm.scheduler_evaluated_today is False
        assert sm.last_scheduling_reason == "observing"
        assert sm.last_next_test_timestamp is None

    # --- Properties ---

    def test_last_scheduling_reason_default(self):
        """last_scheduling_reason defaults to 'observing'."""
        sm = _make_scheduler()
        assert sm.last_scheduling_reason == "observing"

    def test_last_scheduling_reason_settable(self):
        """last_scheduling_reason can be updated."""
        sm = _make_scheduler()
        sm.last_scheduling_reason = "soh_below_floor"
        assert sm.last_scheduling_reason == "soh_below_floor"

    def test_last_next_test_timestamp_default(self):
        """last_next_test_timestamp defaults to None."""
        sm = _make_scheduler()
        assert sm.last_next_test_timestamp is None

    def test_last_next_test_timestamp_settable(self):
        """last_next_test_timestamp can be set to an ISO timestamp string."""
        sm = _make_scheduler()
        ts = "2026-03-20T10:00:00+00:00"
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

    # --- _calculate_days_since_last_attempt ---

    def test_days_since_last_attempt_no_timestamp_returns_inf(self):
        """Returns inf when no upscmd timestamp in model."""
        bm = Mock()
        bm.get_last_upscmd_timestamp.return_value = None
        sm = _make_scheduler(battery_model=bm)
        result = sm._calculate_days_since_last_attempt()
        assert result == float("inf")

    def test_days_since_last_attempt_valid_timestamp(self):
        """Returns correct float days for a recent valid timestamp."""
        bm = Mock()
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        bm.get_last_upscmd_timestamp.return_value = two_days_ago
        sm = _make_scheduler(battery_model=bm)
        result = sm._calculate_days_since_last_attempt()
        assert 1.9 < result < 2.1

    def test_days_since_last_attempt_invalid_timestamp_returns_inf(self):
        """Returns inf for unparseable timestamp string."""
        bm = Mock()
        bm.get_last_upscmd_timestamp.return_value = "not-a-timestamp"
        sm = _make_scheduler(battery_model=bm)
        result = sm._calculate_days_since_last_attempt()
        assert result == float("inf")

    def test_days_since_last_attempt_status_agnostic(self):
        """_calculate_days_since_last_attempt returns a value regardless of upscmd status."""
        bm = Mock()
        one_day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        bm.get_last_upscmd_timestamp.return_value = one_day_ago
        # Should return ~1 even for error status — status is NOT checked
        sm = _make_scheduler(battery_model=bm)
        result = sm._calculate_days_since_last_attempt()
        assert 0.9 < result < 1.1

    # --- _calculate_days_since_last_test_success ---

    def test_days_since_last_test_success_never_attempted_returns_inf(self):
        """Returns inf when no upscmd status (never attempted)."""
        bm = Mock()
        bm.get_last_upscmd_status.return_value = None
        bm.get_last_upscmd_timestamp.return_value = None
        sm = _make_scheduler(battery_model=bm)
        result = sm._calculate_days_since_last_test_success()
        assert result == float("inf")

    def test_days_since_last_test_success_ok_returns_age(self):
        """Returns correct age when last status is OK."""
        bm = Mock()
        bm.get_last_upscmd_status.return_value = "OK"
        thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        bm.get_last_upscmd_timestamp.return_value = thirty_days_ago
        sm = _make_scheduler(battery_model=bm)
        result = sm._calculate_days_since_last_test_success()
        assert 29.9 < result < 30.1

    def test_days_since_last_test_success_err_returns_inf(self):
        """Returns inf when last status is an error (not OK)."""
        bm = Mock()
        bm.get_last_upscmd_status.return_value = "ERR_SOCKET: connection refused"
        one_day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        bm.get_last_upscmd_timestamp.return_value = one_day_ago
        sm = _make_scheduler(battery_model=bm)
        result = sm._calculate_days_since_last_test_success()
        assert result == float("inf")

    def test_days_since_last_test_success_invalid_timestamp_returns_inf(self):
        """Returns inf when status is OK but timestamp is unparseable."""
        bm = Mock()
        bm.get_last_upscmd_status.return_value = "OK"
        bm.get_last_upscmd_timestamp.return_value = "not-a-timestamp"
        sm = _make_scheduler(battery_model=bm)
        result = sm._calculate_days_since_last_test_success()
        assert result == float("inf")

    # --- _get_last_natural_blackout ---

    def test_get_last_natural_blackout_no_events_returns_none(self):
        """Returns None when discharge_events list is empty."""
        bm = Mock()
        bm.state = {"discharge_events": []}
        sm = _make_scheduler(battery_model=bm)
        assert sm._get_last_natural_blackout() is None

    def test_get_last_natural_blackout_returns_most_recent(self):
        """Returns the most recent natural event."""
        bm = Mock()
        bm.state = {
            "discharge_events": [
                {
                    "event_reason": "natural",
                    "timestamp": "2026-03-10T10:00:00Z",
                    "depth_of_discharge": 0.3,
                },
                {
                    "event_reason": "natural",
                    "timestamp": "2026-03-18T10:00:00Z",
                    "depth_of_discharge": 0.5,
                },
            ]
        }
        sm = _make_scheduler(battery_model=bm)
        result = sm._get_last_natural_blackout()
        assert result is not None
        assert result["timestamp"] == "2026-03-18T10:00:00Z"
        assert result["depth"] == 0.5

    def test_get_last_natural_blackout_skips_non_natural(self):
        """Skips test events; only returns natural blackout events."""
        bm = Mock()
        bm.state = {
            "discharge_events": [
                {
                    "event_reason": "natural",
                    "timestamp": "2026-03-10T10:00:00Z",
                    "depth_of_discharge": 0.3,
                },
                {
                    "event_reason": "test",
                    "timestamp": "2026-03-18T10:00:00Z",
                    "depth_of_discharge": 0.7,
                },
            ]
        }
        sm = _make_scheduler(battery_model=bm)
        result = sm._get_last_natural_blackout()
        assert result is not None
        assert result["timestamp"] == "2026-03-10T10:00:00Z"

    def test_get_last_natural_blackout_no_natural_events_returns_none(self):
        """Returns None when all events are tests (no natural blackouts)."""
        bm = Mock()
        bm.state = {
            "discharge_events": [
                {
                    "event_reason": "test",
                    "timestamp": "2026-03-18T10:00:00Z",
                    "depth_of_discharge": 0.7,
                },
            ]
        }
        sm = _make_scheduler(battery_model=bm)
        assert sm._get_last_natural_blackout() is None

    # --- _gather_scheduler_inputs ---

    def test_gather_scheduler_inputs_returns_correct_keys(self):
        """_gather_scheduler_inputs returns dict with required keys (no sulfation/cycle_roi)."""
        bm = Mock()
        bm.state = {"discharge_events": []}
        bm.get_soh.return_value = 0.85
        bm.get_last_upscmd_timestamp.return_value = None
        bm.get_last_upscmd_status.return_value = None

        dh = Mock()
        dh.last_cycle_budget_remaining = 80

        sm = _make_scheduler(battery_model=bm, discharge_handler=dh)
        inputs = sm._gather_scheduler_inputs()

        required_keys = {
            "soh_fraction",
            "days_since_last_test_success",
            "days_since_last_attempt",
            "last_blackout",
            "cycle_budget",
        }
        assert set(inputs.keys()) == required_keys

    def test_gather_scheduler_inputs_no_sulfation_keys(self):
        """_gather_scheduler_inputs does NOT include sulfation_score or cycle_roi."""
        bm = Mock()
        bm.state = {"discharge_events": []}
        bm.get_soh.return_value = 0.85
        bm.get_last_upscmd_timestamp.return_value = None
        bm.get_last_upscmd_status.return_value = None

        dh = Mock()
        dh.last_cycle_budget_remaining = 80

        sm = _make_scheduler(battery_model=bm, discharge_handler=dh)
        inputs = sm._gather_scheduler_inputs()

        assert "sulfation_score" not in inputs
        assert "cycle_roi" not in inputs
        assert "active_credit" not in inputs
        assert "days_since_last_test" not in inputs  # old single-value key gone

    def test_gather_scheduler_inputs_values(self):
        """_gather_scheduler_inputs returns correct values from dependencies."""
        bm = Mock()
        bm.state = {"discharge_events": []}
        bm.get_soh.return_value = 0.9
        bm.get_last_upscmd_timestamp.return_value = None
        bm.get_last_upscmd_status.return_value = None

        dh = Mock()
        dh.last_cycle_budget_remaining = 50

        sm = _make_scheduler(battery_model=bm, discharge_handler=dh)
        inputs = sm._gather_scheduler_inputs()

        assert inputs["soh_fraction"] == 0.9
        assert inputs["days_since_last_test_success"] == float("inf")
        assert inputs["days_since_last_attempt"] == float("inf")
        assert inputs["last_blackout"] is None
        assert inputs["cycle_budget"] == 50

    # --- Two-input timing split boundary tests (resolves cycle-2 HIGH) ---

    def test_recent_failed_attempt_is_rate_limited(self):
        """Recent failed dispatch (1d ago) is rate-limited, NOT retried next daily run.

        days_since_last_attempt ~= 1 (<7) → rate_limit
        days_since_last_test_success = inf (status ERR, cadence NOT deferred ~365d)
        """
        from src.model import BatteryModel

        bm = Mock()
        bm.state = {"discharge_events": []}
        bm.get_soh.return_value = 0.9
        one_day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        bm.get_last_upscmd_timestamp.return_value = one_day_ago
        bm.get_last_upscmd_status.return_value = "ERR_SOCKET: connection refused"

        dh = Mock()
        dh.last_cycle_budget_remaining = 100

        sm = _make_scheduler(battery_model=bm, discharge_handler=dh)

        # Verify individual timing inputs
        attempt_age = sm._calculate_days_since_last_attempt()
        success_age = sm._calculate_days_since_last_test_success()
        assert 0.9 < attempt_age < 1.1, f"Expected ~1d attempt age, got {attempt_age}"
        assert success_age == float("inf"), f"Expected inf success age, got {success_age}"

        # Verify gathered inputs produce rate_limit decision
        inputs = sm._gather_scheduler_inputs()
        from src.battery_math.scheduler import evaluate_test_scheduling
        decision = evaluate_test_scheduling(
            soh_fraction=inputs["soh_fraction"],
            days_since_last_test_success=inputs["days_since_last_test_success"],
            days_since_last_attempt=inputs["days_since_last_attempt"],
            last_blackout_timestamp=None,
            cycle_budget_remaining=int(inputs["cycle_budget"]),
        )
        assert decision.action == "defer_test"
        assert decision.reason_code == "rate_limit"

    def test_old_failed_attempt_does_not_defer_cadence(self):
        """Failed dispatch 8d ago: rate-limit cleared; cadence overdue → proposes.

        days_since_last_attempt ~= 8 (>=7, rate-limit cleared)
        days_since_last_test_success = inf (status ERR, so cadence was not reset ~365d)
        Result: propose_test/diagnostic_cadence
        """
        bm = Mock()
        bm.state = {"discharge_events": []}
        bm.get_soh.return_value = 0.9
        eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        bm.get_last_upscmd_timestamp.return_value = eight_days_ago
        bm.get_last_upscmd_status.return_value = "ERR_SOCKET: connection refused"

        dh = Mock()
        dh.last_cycle_budget_remaining = 100

        sm = _make_scheduler(battery_model=bm, discharge_handler=dh)

        attempt_age = sm._calculate_days_since_last_attempt()
        success_age = sm._calculate_days_since_last_test_success()
        assert 7.9 < attempt_age < 8.1, f"Expected ~8d attempt age, got {attempt_age}"
        assert success_age == float("inf"), f"Expected inf success age, got {success_age}"

        inputs = sm._gather_scheduler_inputs()
        from src.battery_math.scheduler import evaluate_test_scheduling
        decision = evaluate_test_scheduling(
            soh_fraction=inputs["soh_fraction"],
            days_since_last_test_success=inputs["days_since_last_test_success"],
            days_since_last_attempt=inputs["days_since_last_attempt"],
            last_blackout_timestamp=None,
            cycle_budget_remaining=int(inputs["cycle_budget"]),
        )
        assert decision.action == "propose_test"
        assert decision.reason_code == "diagnostic_cadence"

    def test_successful_test_resets_cadence_clock(self):
        """Successful test 30d ago: days_since_last_test_success=30 → within_cadence."""
        bm = Mock()
        bm.state = {"discharge_events": []}
        bm.get_soh.return_value = 0.9
        thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        bm.get_last_upscmd_timestamp.return_value = thirty_days_ago
        bm.get_last_upscmd_status.return_value = "OK"

        dh = Mock()
        dh.last_cycle_budget_remaining = 100

        sm = _make_scheduler(battery_model=bm, discharge_handler=dh)

        attempt_age = sm._calculate_days_since_last_attempt()
        success_age = sm._calculate_days_since_last_test_success()
        assert 29.9 < attempt_age < 30.1, f"Expected ~30d attempt age, got {attempt_age}"
        assert 29.9 < success_age < 30.1, f"Expected ~30d success age, got {success_age}"

        inputs = sm._gather_scheduler_inputs()
        from src.battery_math.scheduler import evaluate_test_scheduling
        decision = evaluate_test_scheduling(
            soh_fraction=inputs["soh_fraction"],
            days_since_last_test_success=inputs["days_since_last_test_success"],
            days_since_last_attempt=inputs["days_since_last_attempt"],
            last_blackout_timestamp=None,
            cycle_budget_remaining=int(inputs["cycle_budget"]),
        )
        assert decision.action == "defer_test"
        assert decision.reason_code == "within_cadence"

    # --- Deadlock proof: fresh model proposes test ---

    def test_fresh_model_proposes_quick_test(self):
        """Deadlock fix: fresh model (no upscmd history) with soh 0.9 → propose quick.

        This is the end-to-end proof that the bootstrap deadlock is resolved.
        """
        from src.model import BatteryModel

        bm = Mock()
        bm.state = {"discharge_events": []}
        bm.get_soh.return_value = 0.9
        bm.get_last_upscmd_timestamp.return_value = None
        bm.get_last_upscmd_status.return_value = None

        dh = Mock()
        dh.last_cycle_budget_remaining = 100

        sm = _make_scheduler(battery_model=bm, discharge_handler=dh)
        inputs = sm._gather_scheduler_inputs()

        from src.battery_math.scheduler import evaluate_test_scheduling
        decision = evaluate_test_scheduling(
            soh_fraction=inputs["soh_fraction"],
            days_since_last_test_success=inputs["days_since_last_test_success"],
            days_since_last_attempt=inputs["days_since_last_attempt"],
            last_blackout_timestamp=None,
            cycle_budget_remaining=int(inputs["cycle_budget"]),
        )
        assert decision.action == "propose_test"
        assert decision.test_type == "quick"
        assert decision.reason_code == "diagnostic_cadence"

    # --- run_daily ---

    def test_run_daily_skips_wrong_hour(self):
        """run_daily does nothing when _should_run_scheduler returns False (wrong hour)."""
        sm = _make_scheduler(scheduling_config=_make_scheduling_config(scheduler_eval_hour_utc=10))
        now = datetime(2026, 3, 20, 11, 5, 0, tzinfo=timezone.utc)

        with patch.object(sm, "_gather_scheduler_inputs") as mock_gather:
            sm.run_daily(now, Mock())
            mock_gather.assert_not_called()

    def test_run_daily_skips_already_evaluated(self):
        """run_daily does nothing when already evaluated today."""
        sm = _make_scheduler(scheduling_config=_make_scheduling_config(scheduler_eval_hour_utc=10))
        sm.scheduler_evaluated_today = True
        now = datetime(2026, 3, 20, 10, 5, 0, tzinfo=timezone.utc)

        with patch.object(sm, "_gather_scheduler_inputs") as mock_gather:
            sm.run_daily(now, Mock())
            mock_gather.assert_not_called()

    def test_run_daily_skips_minute_gte_10(self):
        """run_daily does nothing when minute >= 10."""
        sm = _make_scheduler(scheduling_config=_make_scheduling_config(scheduler_eval_hour_utc=10))
        now = datetime(2026, 3, 20, 10, 15, 0, tzinfo=timezone.utc)

        with patch.object(sm, "_gather_scheduler_inputs") as mock_gather:
            sm.run_daily(now, Mock())
            mock_gather.assert_not_called()

    def test_run_daily_sets_evaluated_today_flag(self):
        """run_daily sets scheduler_evaluated_today=True after running."""
        bm = Mock()
        bm.state = {"discharge_events": []}
        bm.get_soh.return_value = 0.85
        bm.get_last_upscmd_timestamp.return_value = None
        bm.get_last_upscmd_status.return_value = None

        sm = _make_scheduler(battery_model=bm)
        now = datetime(2026, 3, 20, 10, 5, 0, tzinfo=timezone.utc)

        decision = SchedulerDecision(action="defer_test", test_type=None, reason_code="within_cadence")

        with patch("src.scheduler_manager.evaluate_test_scheduling", return_value=decision):
            sm.run_daily(now, Mock())

        assert sm.scheduler_evaluated_today is True

    def test_run_daily_updates_last_scheduling_reason(self):
        """run_daily updates last_scheduling_reason from decision."""
        bm = Mock()
        bm.state = {"discharge_events": []}
        bm.get_soh.return_value = 0.85
        bm.get_last_upscmd_timestamp.return_value = None
        bm.get_last_upscmd_status.return_value = None

        sm = _make_scheduler(battery_model=bm)
        now = datetime(2026, 3, 20, 10, 5, 0, tzinfo=timezone.utc)

        decision = SchedulerDecision(
            action="defer_test", test_type=None, reason_code="grid_unstable"
        )

        with patch("src.scheduler_manager.evaluate_test_scheduling", return_value=decision):
            sm.run_daily(now, Mock())

        assert sm.last_scheduling_reason == "grid_unstable"


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
            action="propose_test",
            test_type="quick",
            reason_code="diagnostic_cadence",
        )

        with patch("src.scheduler_manager.logger"):
            success = dispatch_test_with_audit(
                nut_client=nut_client_mock,
                battery_model=model,
                decision=decision,
                current_metrics=current_metrics,
            )

        assert success is True
        assert model.state.get("test_running") is True
        assert model.state["last_upscmd_status"] == "OK"
