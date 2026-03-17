"""Tests for Phase 17 scheduling configuration schema and validation."""

import pytest
from src.monitor_config import SchedulingConfig, get_scheduling_config


class TestSchedulingConfigValidation:
    """Test SchedulingConfig schema and range validation."""

    def test_valid_config_with_all_defaults(self):
        """Valid scheduling config with all default values passes validation."""
        config = SchedulingConfig(
            grid_stability_cooldown_hours=4.0,
            soh_floor_threshold=0.60,
            min_days_between_tests=7.0,
            roi_threshold=0.2,
            blackout_credit_window_days=7.0,
            critical_cycle_budget_threshold=5,
            deep_test_sulfation_threshold=0.65,
            quick_test_sulfation_threshold=0.40,
            scheduler_eval_hour_utc=8,
            verbose_scheduling=False,
        )
        errors = config.validate()
        assert errors == [], f"Expected no errors, got {errors}"

    def test_grid_stability_cooldown_zero_valid(self):
        """grid_stability_cooldown_hours=0 is valid (disables gate)."""
        config = SchedulingConfig(grid_stability_cooldown_hours=0.0)
        errors = config.validate()
        assert not any('grid_stability_cooldown' in e for e in errors), \
            f"0.0 should be valid, got {errors}"

    def test_grid_stability_cooldown_negative_invalid(self):
        """grid_stability_cooldown_hours <0 is invalid."""
        config = SchedulingConfig(grid_stability_cooldown_hours=-1.0)
        errors = config.validate()
        assert any('grid_stability_cooldown' in e for e in errors), \
            f"Negative value should be invalid, got {errors}"

    def test_soh_floor_threshold_range_valid(self):
        """soh_floor_threshold must be in [0.0, 1.0]."""
        for val in [0.0, 0.5, 1.0]:
            config = SchedulingConfig(soh_floor_threshold=val)
            errors = config.validate()
            assert not any('soh_floor' in e for e in errors), \
                f"Value {val} should be valid, got {errors}"

    def test_soh_floor_threshold_range_invalid(self):
        """soh_floor_threshold outside [0.0, 1.0] is invalid."""
        for val in [-0.1, 1.5]:
            config = SchedulingConfig(soh_floor_threshold=val)
            errors = config.validate()
            assert any('soh_floor' in e for e in errors), \
                f"Value {val} should be invalid, got {errors}"

    def test_min_days_between_tests_minimum(self):
        """min_days_between_tests must be ≥1.0."""
        config = SchedulingConfig(min_days_between_tests=0.5)
        errors = config.validate()
        assert any('min_days_between_tests' in e for e in errors)

    def test_min_days_between_tests_valid_boundary(self):
        """min_days_between_tests=1.0 is valid."""
        config = SchedulingConfig(min_days_between_tests=1.0)
        errors = config.validate()
        assert not any('min_days_between_tests' in e for e in errors)

    def test_roi_threshold_range_valid(self):
        """roi_threshold must be in [0.0, 1.0]."""
        for val in [0.0, 0.2, 1.0]:
            config = SchedulingConfig(roi_threshold=val)
            errors = config.validate()
            assert not any('roi_threshold' in e for e in errors)

    def test_roi_threshold_range_invalid(self):
        """roi_threshold outside [0.0, 1.0] is invalid."""
        for val in [-0.1, 1.5]:
            config = SchedulingConfig(roi_threshold=val)
            errors = config.validate()
            assert any('roi_threshold' in e for e in errors)

    def test_blackout_credit_window_range_valid(self):
        """blackout_credit_window_days must be in [1.0, 30.0]."""
        for val in [1.0, 7.0, 30.0]:
            config = SchedulingConfig(blackout_credit_window_days=val)
            errors = config.validate()
            assert not any('blackout_credit_window' in e for e in errors)

    def test_blackout_credit_window_range_invalid(self):
        """blackout_credit_window_days outside [1.0, 30.0] is invalid."""
        for val in [0.5, 35.0]:
            config = SchedulingConfig(blackout_credit_window_days=val)
            errors = config.validate()
            assert any('blackout_credit_window' in e for e in errors)

    def test_critical_cycle_budget_threshold_minimum(self):
        """critical_cycle_budget_threshold must be ≥1."""
        config = SchedulingConfig(critical_cycle_budget_threshold=0)
        errors = config.validate()
        assert any('critical_cycle_budget_threshold' in e for e in errors)

    def test_critical_cycle_budget_threshold_valid(self):
        """critical_cycle_budget_threshold=1 is valid."""
        config = SchedulingConfig(critical_cycle_budget_threshold=1)
        errors = config.validate()
        assert not any('critical_cycle_budget_threshold' in e for e in errors)

    def test_deep_test_sulfation_threshold_range_valid(self):
        """deep_test_sulfation_threshold must be in [0.0, 1.0]."""
        for val in [0.0, 0.65, 1.0]:
            config = SchedulingConfig(
                deep_test_sulfation_threshold=val,
                quick_test_sulfation_threshold=min(val, 0.40)  # Keep quick ≤ deep
            )
            errors = config.validate()
            assert not any('deep_test_sulfation_threshold' in e for e in errors)

    def test_deep_test_sulfation_threshold_range_invalid(self):
        """deep_test_sulfation_threshold outside [0.0, 1.0] is invalid."""
        for val in [-0.1, 1.5]:
            config = SchedulingConfig(deep_test_sulfation_threshold=val)
            errors = config.validate()
            assert any('deep_test_sulfation_threshold' in e for e in errors)

    def test_quick_test_sulfation_threshold_range_valid(self):
        """quick_test_sulfation_threshold must be in [0.0, 1.0]."""
        for val in [0.0, 0.40, 1.0]:
            config = SchedulingConfig(
                quick_test_sulfation_threshold=val,
                deep_test_sulfation_threshold=max(val, 0.65)  # Keep deep ≥ quick
            )
            errors = config.validate()
            assert not any('quick_test_sulfation_threshold' in e for e in errors)

    def test_quick_test_sulfation_threshold_range_invalid(self):
        """quick_test_sulfation_threshold outside [0.0, 1.0] is invalid."""
        for val in [-0.1, 1.5]:
            config = SchedulingConfig(quick_test_sulfation_threshold=val)
            errors = config.validate()
            assert any('quick_test_sulfation_threshold' in e for e in errors)

    def test_sulfation_thresholds_ordering_valid(self):
        """quick_test_sulfation_threshold must be ≤ deep_test_sulfation_threshold."""
        config = SchedulingConfig(
            quick_test_sulfation_threshold=0.40,
            deep_test_sulfation_threshold=0.65,
        )
        errors = config.validate()
        assert not any('sulfation_threshold' in e for e in errors)

    def test_sulfation_thresholds_ordering_equal_valid(self):
        """quick_test_sulfation_threshold == deep_test_sulfation_threshold is valid."""
        config = SchedulingConfig(
            quick_test_sulfation_threshold=0.50,
            deep_test_sulfation_threshold=0.50,
        )
        errors = config.validate()
        assert not any('sulfation_threshold' in e for e in errors)

    def test_sulfation_thresholds_ordering_invalid(self):
        """quick_test_sulfation_threshold > deep_test_sulfation_threshold is invalid."""
        config = SchedulingConfig(
            quick_test_sulfation_threshold=0.70,
            deep_test_sulfation_threshold=0.60,
        )
        errors = config.validate()
        assert any('sulfation_threshold' in e for e in errors)

    def test_scheduler_eval_hour_range_valid(self):
        """scheduler_eval_hour_utc must be in [0, 23]."""
        for hour in [0, 8, 23]:
            config = SchedulingConfig(scheduler_eval_hour_utc=hour)
            errors = config.validate()
            assert not any('scheduler_eval_hour' in e for e in errors)

    def test_scheduler_eval_hour_range_invalid(self):
        """scheduler_eval_hour_utc outside [0, 23] is invalid."""
        for hour in [-1, 24]:
            config = SchedulingConfig(scheduler_eval_hour_utc=hour)
            errors = config.validate()
            assert any('scheduler_eval_hour' in e for e in errors)


class TestSchedulingConfigDefaults:
    """Test that defaults are applied correctly."""

    def test_defaults_applied_on_instantiation(self):
        """All parameters get default values when not specified."""
        config = SchedulingConfig()
        assert config.grid_stability_cooldown_hours == 4.0
        assert config.soh_floor_threshold == 0.60
        assert config.min_days_between_tests == 7.0
        assert config.roi_threshold == 0.2
        assert config.blackout_credit_window_days == 7.0
        assert config.critical_cycle_budget_threshold == 5
        assert config.deep_test_sulfation_threshold == 0.65
        assert config.quick_test_sulfation_threshold == 0.40
        assert config.scheduler_eval_hour_utc == 8
        assert config.verbose_scheduling is False

    def test_partial_override_with_defaults(self):
        """Specifying one parameter leaves others at defaults."""
        config = SchedulingConfig(grid_stability_cooldown_hours=2.0)
        assert config.grid_stability_cooldown_hours == 2.0
        assert config.soh_floor_threshold == 0.60  # default
        assert config.min_days_between_tests == 7.0  # default


class TestGetSchedulingConfigFromDict:
    """Test get_scheduling_config() helper function."""

    def test_get_scheduling_config_with_full_dict(self):
        """get_scheduling_config extracts and validates full config dict."""
        cfg_dict = {
            'scheduling': {
                'grid_stability_cooldown_hours': 3.0,
                'soh_floor_threshold': 0.55,
                'min_days_between_tests': 5.0,
                'roi_threshold': 0.25,
                'blackout_credit_window_days': 5.0,
                'critical_cycle_budget_threshold': 3,
                'deep_test_sulfation_threshold': 0.70,
                'quick_test_sulfation_threshold': 0.45,
                'scheduler_eval_hour_utc': 10,
                'verbose_scheduling': True,
            }
        }
        sched = get_scheduling_config(cfg_dict)
        assert sched.grid_stability_cooldown_hours == 3.0
        assert sched.soh_floor_threshold == 0.55
        assert sched.min_days_between_tests == 5.0
        assert sched.scheduler_eval_hour_utc == 10
        assert sched.verbose_scheduling is True

    def test_get_scheduling_config_with_empty_section(self):
        """get_scheduling_config applies defaults when section is empty."""
        cfg_dict = {'scheduling': {}}
        sched = get_scheduling_config(cfg_dict)
        assert sched.grid_stability_cooldown_hours == 4.0
        assert sched.soh_floor_threshold == 0.60
        assert sched.min_days_between_tests == 7.0

    def test_get_scheduling_config_no_section(self):
        """get_scheduling_config applies defaults when section missing."""
        cfg_dict = {}
        sched = get_scheduling_config(cfg_dict)
        assert sched.grid_stability_cooldown_hours == 4.0
        assert sched.soh_floor_threshold == 0.60

    def test_get_scheduling_config_invalid_raises_error(self):
        """get_scheduling_config raises ValueError for invalid config."""
        cfg_dict = {
            'scheduling': {
                'grid_stability_cooldown_hours': -1.0,  # invalid
            }
        }
        with pytest.raises(ValueError) as exc_info:
            get_scheduling_config(cfg_dict)
        assert 'grid_stability_cooldown' in str(exc_info.value)

    def test_get_scheduling_config_multiple_errors(self):
        """get_scheduling_config reports all validation errors."""
        cfg_dict = {
            'scheduling': {
                'soh_floor_threshold': 1.5,  # invalid
                'scheduler_eval_hour_utc': 25,  # invalid
            }
        }
        with pytest.raises(ValueError) as exc_info:
            get_scheduling_config(cfg_dict)
        error_msg = str(exc_info.value)
        assert 'soh_floor_threshold' in error_msg or 'scheduler_eval_hour' in error_msg


class TestConfigBackwardCompatibility:
    """Test that old configs without [scheduling] section work."""

    def test_backward_compatible_no_scheduling_section(self):
        """Daemon loads gracefully if config has no [scheduling] section."""
        cfg_dict = {
            'ups_name': 'cyberpower',
            'shutdown_minutes': 5,
        }
        # Should not raise, should apply defaults
        sched = get_scheduling_config(cfg_dict)
        assert sched.grid_stability_cooldown_hours == 4.0
        assert sched.soh_floor_threshold == 0.60
        assert sched.min_days_between_tests == 7.0
        assert sched.scheduler_eval_hour_utc == 8
