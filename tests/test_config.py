"""Tests for Phase 17 scheduling configuration schema and validation."""

import pytest
from src.monitor_config import SchedulingConfig, get_scheduling_config


class TestSchedulingConfigValidation:
    """Test SchedulingConfig schema and range validation."""

    def test_valid_config_with_all_defaults(self):
        """Valid scheduling config with all default values passes validation."""
        config = SchedulingConfig(
            grid_stability_cooldown_hours=4.0,
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
        assert config.scheduler_eval_hour_utc == 8
        assert config.verbose_scheduling is False

    def test_partial_override_with_defaults(self):
        """Specifying one parameter leaves others at defaults."""
        config = SchedulingConfig(grid_stability_cooldown_hours=2.0)
        assert config.grid_stability_cooldown_hours == 2.0
        assert config.scheduler_eval_hour_utc == 8  # default



class TestGetSchedulingConfigFromDict:
    """Test get_scheduling_config() helper function."""

    def test_get_scheduling_config_with_full_dict(self):
        """get_scheduling_config extracts and validates config dict."""
        cfg_dict = {
            'scheduling': {
                'grid_stability_cooldown_hours': 3.0,
                'scheduler_eval_hour_utc': 10,
                'verbose_scheduling': True,
            }
        }
        sched = get_scheduling_config(cfg_dict)
        assert sched.grid_stability_cooldown_hours == 3.0
        assert sched.scheduler_eval_hour_utc == 10
        assert sched.verbose_scheduling is True

    def test_get_scheduling_config_with_empty_section(self):
        """get_scheduling_config applies defaults when section is empty."""
        cfg_dict = {'scheduling': {}}
        sched = get_scheduling_config(cfg_dict)
        assert sched.grid_stability_cooldown_hours == 4.0
        assert sched.scheduler_eval_hour_utc == 8

    def test_get_scheduling_config_no_section(self):
        """get_scheduling_config applies defaults when section missing."""
        cfg_dict = {}
        sched = get_scheduling_config(cfg_dict)
        assert sched.grid_stability_cooldown_hours == 4.0

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

    def test_get_scheduling_config_ignores_unknown_keys(self):
        """Unknown keys in [scheduling] are filtered out with a warning, not crash."""
        cfg_dict = {
            'scheduling': {
                'grid_stability_cooldown_hours': 4.0,
                'soh_floor_threshold': 0.60,  # unknown key — filtered
            }
        }
        result = get_scheduling_config(cfg_dict)
        assert result.grid_stability_cooldown_hours == 4.0
        assert not hasattr(result, 'soh_floor_threshold')
