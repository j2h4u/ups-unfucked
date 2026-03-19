"""Tests for Phase 17 blackout credit and discharge classification logic."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch
from src.discharge_handler import DischargeHandler
from src.model import BatteryModel
from src.monitor_config import DischargeBuffer


class TestDischargeClassification:
    """Tests for test-initiated vs natural discharge classification."""

    def test_classify_natural_no_upscmd_record(self, temporary_model_path):
        """No upscmd timestamp: classify as natural."""
        model = BatteryModel(temporary_model_path)
        buffer = Mock(spec=DischargeBuffer)

        # Create handler (minimal mock config)
        config = Mock()
        config.capacity_ah = 7.2
        handler = DischargeHandler(
            battery_model=model,
            config=config,
            capacity_estimator=Mock(),
            rls_peukert=Mock(),
            reference_load_percent=20.0,
            soh_threshold=0.80,
        )

        reason = handler._classify_discharge_trigger(buffer)
        assert reason == 'natural'

    def test_classify_test_initiated_recent_upscmd(self, temporary_model_path):
        """Discharge within 60s of upscmd: classify as test-initiated."""
        model = BatteryModel(temporary_model_path)

        # Set upscmd timestamp
        upscmd_time = datetime.now(timezone.utc)
        model.data['last_upscmd_timestamp'] = upscmd_time.isoformat()

        # Buffer with times starting 30s after upscmd
        buffer = DischargeBuffer()
        buffer_start = upscmd_time.timestamp() + 30
        buffer.times = [buffer_start, buffer_start + 10, buffer_start + 20]
        buffer.voltages = [13.0, 12.8, 12.6]
        buffer.loads = [20, 20, 20]

        # Create handler
        config = Mock()
        config.capacity_ah = 7.2
        handler = DischargeHandler(
            battery_model=model,
            config=config,
            capacity_estimator=Mock(),
            rls_peukert=Mock(),
            reference_load_percent=20.0,
            soh_threshold=0.80,
        )

        reason = handler._classify_discharge_trigger(buffer)
        assert reason == 'test_initiated'

    def test_classify_natural_old_upscmd(self, temporary_model_path):
        """Discharge >60s after upscmd: classify as natural."""
        model = BatteryModel(temporary_model_path)

        # Set upscmd timestamp
        upscmd_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        model.data['last_upscmd_timestamp'] = upscmd_time.isoformat()

        buffer = Mock(spec=DischargeBuffer)

        # Create handler
        config = Mock()
        config.capacity_ah = 7.2
        handler = DischargeHandler(
            battery_model=model,
            config=config,
            capacity_estimator=Mock(),
            rls_peukert=Mock(),
            reference_load_percent=20.0,
            soh_threshold=0.80,
        )

        reason = handler._classify_discharge_trigger(buffer)
        assert reason == 'natural'

    def test_classify_natural_no_buffer(self, temporary_model_path):
        """No discharge buffer: classify as natural."""
        model = BatteryModel(temporary_model_path)
        model.data['last_upscmd_timestamp'] = datetime.now(timezone.utc).isoformat()

        config = Mock()
        config.capacity_ah = 7.2
        handler = DischargeHandler(
            battery_model=model,
            config=config,
            capacity_estimator=Mock(),
            rls_peukert=Mock(),
            reference_load_percent=20.0,
            soh_threshold=0.80,
        )

        reason = handler._classify_discharge_trigger(None)
        assert reason == 'natural'


class TestBlackoutCreditLogic:
    """Tests for blackout credit granting after natural deep discharges."""

    def _make_handler(self, model):
        """Create a DischargeHandler with minimal mocked config."""
        config = Mock()
        config.capacity_ah = 7.2
        return DischargeHandler(
            battery_model=model,
            config=config,
            capacity_estimator=Mock(),
            rls_peukert=Mock(),
            reference_load_percent=20.0,
            soh_threshold=0.80,
        )

    def _deep_discharge_buffer(self):
        """Buffer with ≥90% DoD: voltage 13.0→11.5V (delta 1.5V / 1.5V range = 100%)."""
        buf = DischargeBuffer()
        buf.voltages = [13.0, 12.5, 12.0, 11.5]
        buf.times = [0.0, 300.0, 600.0, 900.0]
        buf.loads = [25.0, 25.0, 25.0, 25.0]
        return buf

    def _shallow_discharge_buffer(self):
        """Buffer with <90% DoD: voltage 13.0→12.5V (delta 0.5V / 1.5V = 33%)."""
        buf = DischargeBuffer()
        buf.voltages = [13.0, 12.8, 12.6, 12.5]
        buf.times = [0.0, 300.0, 600.0, 900.0]
        buf.loads = [25.0, 25.0, 25.0, 25.0]
        return buf

    def test_grant_blackout_credit_on_deep_natural_discharge(self, temporary_model_path):
        """Deep natural discharge (≥90% DoD): grants 7-day blackout credit."""
        model = BatteryModel(temporary_model_path)
        handler = self._make_handler(model)

        with patch('src.discharge_handler.logger'):
            handler._score_and_persist_sulfation(
                soh_new=0.95, soh_delta=-0.02,
                discharge_buffer=self._deep_discharge_buffer(),
                discharge_trigger='natural',
            )

        credit = model.get_blackout_credit()
        assert credit is not None
        assert credit['active'] is True
        assert credit['desulfation_credit'] == 0.15

    def test_no_blackout_credit_shallow_discharge(self, temporary_model_path):
        """Shallow discharge (<90% DoD): no blackout credit."""
        model = BatteryModel(temporary_model_path)
        handler = self._make_handler(model)

        with patch('src.discharge_handler.logger'):
            handler._score_and_persist_sulfation(
                soh_new=0.95, soh_delta=-0.01,
                discharge_buffer=self._shallow_discharge_buffer(),
                discharge_trigger='natural',
            )

        credit = model.get_blackout_credit()
        assert credit is None

    def test_no_blackout_credit_on_test_initiated_discharge(self, temporary_model_path):
        """Test-initiated discharge: no blackout credit even if deep."""
        model = BatteryModel(temporary_model_path)
        handler = self._make_handler(model)

        with patch('src.discharge_handler.logger'):
            handler._score_and_persist_sulfation(
                soh_new=0.95, soh_delta=-0.03,
                discharge_buffer=self._deep_discharge_buffer(),
                discharge_trigger='test_initiated',
            )

        credit = model.get_blackout_credit()
        assert credit is None

    def test_blackout_credit_expires(self, temporary_model_path):
        """Blackout credit expires after 7 days: callers check credit_expires < now."""
        model = BatteryModel(temporary_model_path)

        # Set expired credit (expired 1 day ago)
        credit_expires = datetime.now(timezone.utc) - timedelta(days=1)
        model.set_blackout_credit({
            'active': True,
            'credited_event_timestamp': (credit_expires - timedelta(days=7)).isoformat(),
            'credit_expires': credit_expires.isoformat(),
            'desulfation_credit': 0.15,
        })

        # Verify get_blackout_credit() returns the credit dict
        credit = model.get_blackout_credit()
        assert credit is not None
        assert credit['active'] is True

        # Verify the expiry check that callers (scheduler) must perform
        expires_dt = datetime.fromisoformat(credit['credit_expires'])
        now = datetime.now(timezone.utc)
        is_expired = expires_dt < now
        assert is_expired, "Credit with credit_expires in the past should be detected as expired"

        # Verify clear_blackout_credit() deactivates it
        model.clear_blackout_credit()
        credit_after = model.get_blackout_credit()
        assert credit_after['active'] is False

    def test_blackout_credit_cleared_manually(self, temporary_model_path):
        """clear_blackout_credit() sets active=False."""
        model = BatteryModel(temporary_model_path)

        # Grant credit
        model.set_blackout_credit({
            'active': True,
            'credit_expires': (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        })

        # Clear it
        model.clear_blackout_credit()

        # Verify cleared
        assert model.data['blackout_credit']['active'] is False


class TestBlackoutCreditEventLogging:
    """Tests for journald event logging when blackout credit is granted."""

    def test_blackout_credit_logged_to_journald(self, temporary_model_path):
        """Blackout credit grant is logged with event_type='blackout_credit_granted'."""
        model = BatteryModel(temporary_model_path)

        config = Mock()
        config.capacity_ah = 7.2
        handler = DischargeHandler(
            battery_model=model,
            config=config,
            capacity_estimator=Mock(),
            rls_peukert=Mock(),
            reference_load_percent=20.0,
            soh_threshold=0.80,
        )

        # Deep discharge buffer (≥90% DoD)
        buf = DischargeBuffer()
        buf.voltages = [13.0, 12.5, 12.0, 11.5]
        buf.times = [0.0, 300.0, 600.0, 900.0]
        buf.loads = [25.0, 25.0, 25.0, 25.0]

        with patch('src.discharge_handler.logger') as mock_logger:
            handler._score_and_persist_sulfation(
                soh_new=0.95, soh_delta=-0.02,
                discharge_buffer=buf, discharge_trigger='natural',
            )

        # Verify blackout_credit_granted was logged
        log_calls = [c for c in mock_logger.info.call_args_list
                     if c.kwargs.get('extra', {}).get('event_type') == 'blackout_credit_granted']
        assert len(log_calls) == 1
        assert model.get_blackout_credit()['active'] is True
