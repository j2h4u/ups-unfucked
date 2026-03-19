"""Tests for precondition validator and test dispatch functions (Phase 17)."""

import pytest
from datetime import datetime, timezone
from unittest.mock import Mock, MagicMock, patch
from src.monitor import validate_preconditions_before_upscmd, dispatch_test_with_audit
from src.battery_math.scheduler import SchedulerDecision
from src.model import BatteryModel


class TestPreconditionValidator:
    """Tests for validate_preconditions_before_upscmd function."""

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

    def test_precondition_blocks_low_soc(self):
        """SoC <95% blocks dispatch."""
        can_proceed, reason = validate_preconditions_before_upscmd(
            ups_status="OL",
            soc=0.92,  # Below 95%
            recent_power_glitches=0,
            test_already_running=False,
        )
        assert can_proceed is False
        assert "soc" in reason

    def test_precondition_blocks_ob_state(self):
        """UPS on battery (OB) blocks dispatch."""
        can_proceed, reason = validate_preconditions_before_upscmd(
            ups_status="OB DISCHRG",  # On battery
            soc=0.98,
            recent_power_glitches=0,
            test_already_running=False,
        )
        assert can_proceed is False
        assert "online" in reason.lower()

    def test_precondition_blocks_cal_state(self):
        """UPS in calibration (CAL) blocks dispatch."""
        can_proceed, reason = validate_preconditions_before_upscmd(
            ups_status="OL CAL",  # Calibration mode
            soc=0.98,
            recent_power_glitches=0,
            test_already_running=False,
        )
        assert can_proceed is False
        assert "online" in reason.lower()

    def test_precondition_blocks_glitches(self):
        """Grid glitches >2 in 4h blocks dispatch."""
        can_proceed, reason = validate_preconditions_before_upscmd(
            ups_status="OL",
            soc=0.97,
            recent_power_glitches=3,  # >2 glitches
            test_already_running=False,
        )
        assert can_proceed is False
        assert "glitch" in reason.lower() or "transition" in reason.lower()

    def test_precondition_blocks_test_running(self):
        """Test already running blocks dispatch."""
        can_proceed, reason = validate_preconditions_before_upscmd(
            ups_status="OL",
            soc=0.97,
            recent_power_glitches=0,
            test_already_running=True,
        )
        assert can_proceed is False
        assert "already_running" in reason.lower()

    def test_precondition_at_soc_boundary(self):
        """SoC at exactly 95%: passes (boundary)."""
        can_proceed, reason = validate_preconditions_before_upscmd(
            ups_status="OL",
            soc=0.95,
            recent_power_glitches=0,
            test_already_running=False,
        )
        assert can_proceed is True
        assert reason == ""

    def test_precondition_at_glitch_boundary(self):
        """Glitches at exactly 2: passes (boundary)."""
        can_proceed, reason = validate_preconditions_before_upscmd(
            ups_status="OL",
            soc=0.97,
            recent_power_glitches=2,
            test_already_running=False,
        )
        assert can_proceed is True
        assert reason == ""


class TestDispatchFunction:
    """Tests for dispatch_test_with_audit function."""

    def test_dispatch_success_updates_model(self, temporary_model_path):
        """Successful dispatch updates model.json with timestamp and status."""
        model = BatteryModel(temporary_model_path)

        # Create mocks
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

        # Call dispatch
        with patch('src.monitor.logger'):
            success = dispatch_test_with_audit(
                nut_client=nut_client_mock,
                battery_model=model,
                decision=decision,
                current_metrics=current_metrics,
            )

        assert success is True
        assert model.data['last_upscmd_timestamp'] is not None
        assert model.data['last_upscmd_type'] == 'test.battery.start.deep'
        assert model.data['last_upscmd_status'] == 'OK'
        assert model.data.get('test_running') is True

    def test_dispatch_precondition_blocked(self, temporary_model_path):
        """Dispatch blocked by precondition (low SoC): returns False."""
        model = BatteryModel(temporary_model_path)

        nut_client_mock = Mock()
        nut_client_mock.send_instcmd = Mock(return_value=(True, None))

        current_metrics = Mock()
        current_metrics.ups_status_override = "OL"
        current_metrics.soc = 0.90  # Below 95%

        decision = SchedulerDecision(
            action='propose_test',
            test_type='deep',
            reason_code='sulfation_high',
        )

        # Call dispatch
        with patch('src.monitor.logger'):
            success = dispatch_test_with_audit(
                nut_client=nut_client_mock,
                battery_model=model,
                decision=decision,
                current_metrics=current_metrics,
            )

        assert success is False
        # Should not have called send_instcmd
        nut_client_mock.send_instcmd.assert_not_called()

    def test_dispatch_nut_command_fails(self, temporary_model_path):
        """NUT command fails: dispatch returns False, updates model with error."""
        model = BatteryModel(temporary_model_path)

        nut_client_mock = Mock()
        nut_client_mock.send_instcmd = Mock(return_value=(False, "ERR_CMD_NOT_SUPPORTED"))

        current_metrics = Mock()
        current_metrics.ups_status_override = "OL"
        current_metrics.soc = 0.98

        decision = SchedulerDecision(
            action='propose_test',
            test_type='quick',
            reason_code='sulfation_moderate',
        )

        # Call dispatch
        with patch('src.monitor.logger'):
            success = dispatch_test_with_audit(
                nut_client=nut_client_mock,
                battery_model=model,
                decision=decision,
                current_metrics=current_metrics,
            )

        assert success is False
        assert model.data['last_upscmd_status'] == 'ERR_CMD_NOT_SUPPORTED'

    def test_dispatch_upscmd_result_persisted(self, temporary_model_path):
        """Upscmd result persisted to model.json via update_upscmd_result()."""
        model = BatteryModel(temporary_model_path)

        nut_client_mock = Mock()
        nut_client_mock.send_instcmd = Mock(return_value=(True, None))

        current_metrics = Mock()
        current_metrics.ups_status_override = "OL"
        current_metrics.soc = 0.98

        decision = SchedulerDecision(
            action='propose_test',
            test_type='deep',
            reason_code='test_reason',
        )

        with patch('src.monitor.logger'):
            dispatch_test_with_audit(
                nut_client=nut_client_mock,
                battery_model=model,
                decision=decision,
                current_metrics=current_metrics,
            )

        # Verify model methods were called
        assert model.get_last_upscmd_timestamp() is not None
        assert model.data['last_upscmd_type'] == 'test.battery.start.deep'


class TestDispatchIntegration:
    """Integration-like tests for dispatch with real CurrentMetrics."""

    def test_dispatch_with_real_metrics(self, temporary_model_path, current_metrics_fixture):
        """Dispatch with real CurrentMetrics fixture."""
        model = BatteryModel(temporary_model_path)

        nut_client_mock = Mock()
        nut_client_mock.send_instcmd = Mock(return_value=(True, None))

        # Modify fixture to have good SoC
        current_metrics_fixture.soc = 0.98
        current_metrics_fixture.ups_status_override = "OL"

        decision = SchedulerDecision(
            action='propose_test',
            test_type='deep',
            reason_code='test_reason',
        )

        with patch('src.monitor.logger'):
            success = dispatch_test_with_audit(
                nut_client=nut_client_mock,
                battery_model=model,
                decision=decision,
                current_metrics=current_metrics_fixture,
            )

        assert success is True
        assert nut_client_mock.send_instcmd.called
