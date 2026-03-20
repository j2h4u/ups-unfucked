"""Tests for blackout credit and discharge classification logic."""

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
        model.state['last_upscmd_timestamp'] = upscmd_time.isoformat()

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
        model.state['last_upscmd_timestamp'] = upscmd_time.isoformat()

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
        model.state['last_upscmd_timestamp'] = datetime.now(timezone.utc).isoformat()

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
        assert model.state['blackout_credit']['active'] is False


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


class TestSulfationMethodSplit:
    """Tests for the three-way split of _score_and_persist_sulfation.

    Verifies that _compute_sulfation_metrics, _persist_sulfation_and_discharge,
    and _log_discharge_complete are independently callable and correct.
    """

    _REQUIRED_DATA_KEYS = {
        'now_iso', 'sulfation_state', 'roi',
        'sulfation_score_r', 'days_since_deep_r', 'ir_trend_r',
        'recovery_delta_r', 'discharge_duration', 'dod_r', 'roi_r',
        'soh_new', 'soh_delta', 'discharge_trigger', 'capacity_ah_ref',
        'confidence_level', 'depth_of_discharge',
    }

    def _make_handler(self, model):
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

    def _standard_buffer(self):
        buf = DischargeBuffer()
        buf.voltages = [13.0, 12.5, 12.0, 11.5]
        buf.times = [0.0, 300.0, 600.0, 900.0]
        buf.loads = [25.0, 25.0, 25.0, 25.0]
        return buf

    # ------------------------------------------------------------------ #
    # _compute_sulfation_metrics                                           #
    # ------------------------------------------------------------------ #

    def test_compute_returns_all_required_keys(self, temporary_model_path):
        """_compute_sulfation_metrics returns dict with all 16 required keys."""
        model = BatteryModel(temporary_model_path)
        handler = self._make_handler(model)

        data = handler._compute_sulfation_metrics(
            soh_new=0.95, soh_delta=-0.02,
            discharge_buffer=self._standard_buffer(),
            discharge_trigger='natural',
        )

        assert self._REQUIRED_DATA_KEYS.issubset(data.keys()), (
            f"Missing keys: {self._REQUIRED_DATA_KEYS - data.keys()}"
        )

    def test_compute_sets_last_state_fields(self, temporary_model_path):
        """_compute_sulfation_metrics updates self.last_* fields."""
        model = BatteryModel(temporary_model_path)
        handler = self._make_handler(model)

        with patch('src.discharge_handler.compute_sulfation_score') as mock_score, \
             patch('src.discharge_handler.compute_cycle_roi') as mock_roi:
            from src.battery_math.sulfation import SulfationState
            mock_score.return_value = SulfationState(
                score=0.42, days_since_deep=3.0,
                ir_trend_rate=0.001, recovery_delta=-0.02,
                temperature_celsius=35.0,
            )
            mock_roi.return_value = 0.25

            handler._compute_sulfation_metrics(
                soh_new=0.95, soh_delta=-0.02,
                discharge_buffer=self._standard_buffer(),
                discharge_trigger='natural',
            )

        assert handler.last_sulfation_score == 0.42
        assert handler.last_cycle_roi == 0.25
        assert handler.last_discharge_timestamp is not None

    def test_compute_returns_none_scored_dict_on_value_error(self, temporary_model_path):
        """_compute_sulfation_metrics returns dict with sulfation_state=None, roi=None on ValueError."""
        model = BatteryModel(temporary_model_path)
        handler = self._make_handler(model)

        with patch('src.discharge_handler.compute_sulfation_score',
                   side_effect=ValueError("bad input")):
            data = handler._compute_sulfation_metrics(
                soh_new=0.95, soh_delta=-0.02,
                discharge_buffer=self._standard_buffer(),
                discharge_trigger='natural',
            )

        assert data['sulfation_state'] is None
        assert data['roi'] is None
        assert self._REQUIRED_DATA_KEYS.issubset(data.keys())

    # ------------------------------------------------------------------ #
    # _persist_sulfation_and_discharge                                     #
    # ------------------------------------------------------------------ #

    def test_persist_calls_append_sulfation_history_once(self, temporary_model_path):
        """_persist_sulfation_and_discharge calls battery_model.append_sulfation_history once."""
        mock_model = Mock()
        mock_model.state = {}
        config = Mock()
        config.capacity_ah = 7.2
        handler = DischargeHandler(
            battery_model=mock_model,
            config=config,
            capacity_estimator=Mock(),
            rls_peukert=Mock(),
            reference_load_percent=20.0,
            soh_threshold=0.80,
        )
        handler.last_sulfation_confidence = 'medium'

        data = {
            'now_iso': '2026-01-01T00:00:00+00:00',
            'discharge_trigger': 'natural',
            'sulfation_score_r': 0.3,
            'days_since_deep_r': 2.0,
            'ir_trend_r': 0.001,
            'recovery_delta_r': -0.02,
            'confidence_level': 'medium',
            'sulfation_state': object(),  # truthy
            'roi': 0.1,
            'roi_r': 0.1,
            'discharge_duration': 900.0,
            'dod_r': 0.85,
            'depth_of_discharge': 0.85,
            'capacity_ah_ref': None,
            'soh_new': 0.95,
            'soh_delta': -0.02,
        }

        handler._persist_sulfation_and_discharge(data)

        mock_model.append_sulfation_history.assert_called_once()

    def test_persist_calls_append_discharge_event_once(self, temporary_model_path):
        """_persist_sulfation_and_discharge calls battery_model.append_discharge_event once."""
        mock_model = Mock()
        mock_model.state = {}
        config = Mock()
        config.capacity_ah = 7.2
        handler = DischargeHandler(
            battery_model=mock_model,
            config=config,
            capacity_estimator=Mock(),
            rls_peukert=Mock(),
            reference_load_percent=20.0,
            soh_threshold=0.80,
        )
        handler.last_sulfation_confidence = 'medium'

        data = {
            'now_iso': '2026-01-01T00:00:00+00:00',
            'discharge_trigger': 'natural',
            'sulfation_score_r': 0.3,
            'days_since_deep_r': 2.0,
            'ir_trend_r': 0.001,
            'recovery_delta_r': -0.02,
            'confidence_level': 'medium',
            'sulfation_state': object(),
            'roi': 0.1,
            'roi_r': 0.1,
            'discharge_duration': 900.0,
            'dod_r': 0.85,
            'depth_of_discharge': 0.85,
            'capacity_ah_ref': None,
            'soh_new': 0.95,
            'soh_delta': -0.02,
        }

        handler._persist_sulfation_and_discharge(data)

        mock_model.append_discharge_event.assert_called_once()

    def test_persist_calls_grant_blackout_credit_with_correct_args(self, temporary_model_path):
        """_persist_sulfation_and_discharge calls _grant_blackout_credit(trigger, depth_of_discharge)."""
        mock_model = Mock()
        mock_model.state = {}
        config = Mock()
        config.capacity_ah = 7.2
        handler = DischargeHandler(
            battery_model=mock_model,
            config=config,
            capacity_estimator=Mock(),
            rls_peukert=Mock(),
            reference_load_percent=20.0,
            soh_threshold=0.80,
        )
        handler.last_sulfation_confidence = 'high'

        data = {
            'now_iso': '2026-01-01T00:00:00+00:00',
            'discharge_trigger': 'natural',
            'sulfation_score_r': 0.3,
            'days_since_deep_r': 2.0,
            'ir_trend_r': 0.001,
            'recovery_delta_r': -0.02,
            'confidence_level': 'high',
            'sulfation_state': object(),
            'roi': 0.1,
            'roi_r': 0.1,
            'discharge_duration': 900.0,
            'dod_r': 0.92,
            'depth_of_discharge': 0.921,
            'capacity_ah_ref': None,
            'soh_new': 0.95,
            'soh_delta': -0.02,
        }

        with patch.object(handler, '_grant_blackout_credit') as mock_credit:
            handler._persist_sulfation_and_discharge(data)

        mock_credit.assert_called_once_with('natural', 0.921)

    # ------------------------------------------------------------------ #
    # _log_discharge_complete                                              #
    # ------------------------------------------------------------------ #

    def test_log_discharge_emits_discharge_complete_event(self, temporary_model_path):
        """_log_discharge_complete calls logger.info with event_type='discharge_complete'."""
        model = BatteryModel(temporary_model_path)
        handler = self._make_handler(model)
        handler.last_sulfation_confidence = 'medium'

        data = {
            'now_iso': '2026-01-01T00:00:00+00:00',
            'discharge_trigger': 'natural',
            'discharge_duration': 900.0,
            'dod_r': 0.85,
            'sulfation_score_r': 0.3,
            'recovery_delta_r': -0.02,
            'roi_r': 0.1,
            'capacity_ah_ref': None,
            'soh_new': 0.95,
            'soh_delta': -0.02,
        }

        with patch('src.discharge_handler.logger') as mock_logger:
            handler._log_discharge_complete(data)

        logged_extras = [
            c.kwargs.get('extra', {})
            for c in mock_logger.info.call_args_list
        ]
        discharge_events = [e for e in logged_extras if e.get('event_type') == 'discharge_complete']
        assert len(discharge_events) == 1

    def test_log_discharge_handles_none_sulfation_values(self, temporary_model_path):
        """_log_discharge_complete logs None sulfation_score and roi without raising."""
        model = BatteryModel(temporary_model_path)
        handler = self._make_handler(model)
        handler.last_sulfation_confidence = None

        data = {
            'now_iso': '2026-01-01T00:00:00+00:00',
            'discharge_trigger': 'natural',
            'discharge_duration': 900.0,
            'dod_r': 0.85,
            'sulfation_score_r': None,
            'recovery_delta_r': -0.02,
            'roi_r': None,
            'capacity_ah_ref': None,
            'soh_new': 0.95,
            'soh_delta': -0.02,
        }

        with patch('src.discharge_handler.logger') as mock_logger:
            handler._log_discharge_complete(data)  # must not raise

        logged_extras = [
            c.kwargs.get('extra', {})
            for c in mock_logger.info.call_args_list
        ]
        discharge_events = [e for e in logged_extras if e.get('event_type') == 'discharge_complete']
        assert len(discharge_events) == 1
        assert discharge_events[0]['sulfation_score'] is None
        assert discharge_events[0]['cycle_roi'] is None
