"""Unit tests for SagTracker — voltage sag state machine and ir_k calibration.

Tests the SagTracker class directly without constructing MonitorDaemon.
BatteryModel is mocked; ScalarRLS is used as a real object (pure math kernel).
"""

import pytest
from unittest.mock import MagicMock, call, patch
from src.sag_tracker import SagTracker
from src.battery_math.rls import ScalarRLS
from src.monitor_config import SagState, SAG_SAMPLES_REQUIRED
from src.event_classifier import EventType


def make_tracker(ir_k=0.015, rls_theta=0.015, rls_P=1.0, nominal_voltage=13.0, nominal_power_watts=425.0):
    """Build a SagTracker with a mocked BatteryModel and real ScalarRLS."""
    mock_model = MagicMock()
    mock_model.get_nominal_voltage.return_value = nominal_voltage
    mock_model.get_nominal_power_watts.return_value = nominal_power_watts
    rls = ScalarRLS(theta=rls_theta, P=rls_P, forgetting_factor=0.97)
    tracker = SagTracker(battery_model=mock_model, rls_ir_k=rls, ir_k=ir_k)
    return tracker, mock_model


# ------------------------------------------------------------------
# Initial state
# ------------------------------------------------------------------

def test_initial_state_is_idle():
    """SagTracker starts in IDLE state."""
    tracker, _ = make_tracker()
    assert tracker._state == SagState.IDLE
    assert tracker.is_measuring is False


def test_ir_k_property_returns_initial_value():
    """ir_k property returns the value passed to __init__."""
    tracker, _ = make_tracker(ir_k=0.018)
    assert tracker.ir_k == 0.018


# ------------------------------------------------------------------
# State machine: OL->OB transition starts MEASURING
# ------------------------------------------------------------------

def test_track_ol_ob_transition_starts_measuring():
    """transition_occurred=True with non-ONLINE event starts MEASURING and captures v_before_sag."""
    tracker, _ = make_tracker()
    tracker.track(voltage=13.5, event_type=EventType.BLACKOUT_REAL,
                  transition_occurred=True, current_load=20.0)
    assert tracker._state == SagState.MEASURING
    assert tracker.is_measuring is True
    assert tracker._v_before_sag == 13.5


def test_track_ol_ob_test_transition_starts_measuring():
    """Blackout test event also triggers MEASURING."""
    tracker, _ = make_tracker()
    tracker.track(voltage=13.2, event_type=EventType.BLACKOUT_TEST,
                  transition_occurred=True, current_load=15.0)
    assert tracker._state == SagState.MEASURING
    assert tracker._v_before_sag == 13.2


def test_track_no_transition_does_not_start_measuring():
    """transition_occurred=False leaves state IDLE."""
    tracker, _ = make_tracker()
    tracker.track(voltage=13.5, event_type=EventType.BLACKOUT_REAL,
                  transition_occurred=False, current_load=20.0)
    assert tracker._state == SagState.IDLE
    assert tracker.is_measuring is False


# ------------------------------------------------------------------
# State machine: sample collection and sag recording
# ------------------------------------------------------------------

def test_track_collecting_samples_in_measuring():
    """Samples accumulate in buffer during MEASURING; no completion before SAG_SAMPLES_REQUIRED."""
    tracker, _ = make_tracker()
    # Enter MEASURING — transition tick itself adds sample #1 to buffer
    tracker.track(voltage=13.5, event_type=EventType.BLACKOUT_REAL,
                  transition_occurred=True, current_load=20.0)
    # Feed SAG_SAMPLES_REQUIRED - 2 more samples (total = SAG_SAMPLES_REQUIRED - 1, not enough)
    for i in range(SAG_SAMPLES_REQUIRED - 2):
        tracker.track(voltage=12.8 - i * 0.01, event_type=EventType.BLACKOUT_REAL,
                      transition_occurred=False, current_load=20.0)
    assert tracker._state == SagState.MEASURING
    assert len(tracker._sag_buffer) == SAG_SAMPLES_REQUIRED - 1


def test_track_completes_after_required_samples():
    """After SAG_SAMPLES_REQUIRED (5) samples, state transitions to COMPLETE."""
    tracker, mock_model = make_tracker(nominal_voltage=13.0, nominal_power_watts=425.0)
    # Enter MEASURING
    tracker.track(voltage=13.5, event_type=EventType.BLACKOUT_REAL,
                  transition_occurred=True, current_load=20.0)
    # Feed exactly SAG_SAMPLES_REQUIRED samples
    for _ in range(SAG_SAMPLES_REQUIRED):
        tracker.track(voltage=12.8, event_type=EventType.BLACKOUT_REAL,
                      transition_occurred=False, current_load=20.0)
    assert tracker._state == SagState.COMPLETE
    assert tracker.is_measuring is False
    # Should have recorded the sag
    mock_model.add_r_internal_entry.assert_called_once()


def test_track_sag_uses_median_of_last_3():
    """Sag voltage computed as sorted median of last 3 buffer samples."""
    tracker, mock_model = make_tracker(nominal_voltage=13.0, nominal_power_watts=425.0)
    # Enter MEASURING — transition tick adds sample #1 (13.5) to buffer
    tracker.track(voltage=13.5, event_type=EventType.BLACKOUT_REAL,
                  transition_occurred=True, current_load=25.0)
    # Feed 4 more samples to reach SAG_SAMPLES_REQUIRED (5 total including transition)
    # Buffer = [13.5, 12.90, 12.85, 12.80, 12.75]
    # Last 3 = [12.85, 12.80, 12.75] -> sorted [12.75, 12.80, 12.85] -> median 12.80
    voltages = [12.90, 12.85, 12.80, 12.75]
    for v in voltages:
        tracker.track(voltage=v, event_type=EventType.BLACKOUT_REAL,
                      transition_occurred=False, current_load=25.0)

    call_args = mock_model.add_r_internal_entry.call_args
    v_sag_recorded = call_args[0][3]  # 4th positional arg: v_sag
    assert abs(v_sag_recorded - 12.80) < 1e-9


# ------------------------------------------------------------------
# State machine: OB->OL cancels measurement
# ------------------------------------------------------------------

def test_track_ob_ol_transition_cancels_measuring():
    """transition_occurred=True with ONLINE event cancels MEASURING back to IDLE."""
    tracker, _ = make_tracker()
    # Enter MEASURING
    tracker.track(voltage=13.5, event_type=EventType.BLACKOUT_REAL,
                  transition_occurred=True, current_load=20.0)
    assert tracker._state == SagState.MEASURING

    # Power restored before enough samples collected
    tracker.track(voltage=13.4, event_type=EventType.ONLINE,
                  transition_occurred=True, current_load=20.0)
    assert tracker._state == SagState.IDLE
    assert tracker.is_measuring is False


def test_track_online_event_while_idle_stays_idle():
    """ONLINE transition while IDLE does nothing (no crash, no state change)."""
    tracker, _ = make_tracker()
    tracker.track(voltage=13.5, event_type=EventType.ONLINE,
                  transition_occurred=True, current_load=20.0)
    assert tracker._state == SagState.IDLE


# ------------------------------------------------------------------
# _record_voltage_sag skip conditions
# ------------------------------------------------------------------

def test_record_sag_skipped_when_v_before_sag_is_none():
    """_record_voltage_sag does nothing when _v_before_sag is None."""
    tracker, mock_model = make_tracker()
    # Call directly without setting _v_before_sag
    tracker._current_load = 20.0
    tracker._record_voltage_sag(v_sag=12.5, event_type=EventType.BLACKOUT_REAL)
    mock_model.add_r_internal_entry.assert_not_called()


def test_record_sag_skipped_when_load_is_none():
    """_record_voltage_sag skips when current_load is None."""
    tracker, mock_model = make_tracker()
    tracker._v_before_sag = 13.5
    tracker._current_load = None
    tracker._record_voltage_sag(v_sag=12.5, event_type=EventType.BLACKOUT_REAL)
    mock_model.add_r_internal_entry.assert_not_called()


def test_record_sag_skipped_when_load_is_zero():
    """_record_voltage_sag skips when load is zero (I_actual would be 0)."""
    tracker, mock_model = make_tracker()
    tracker._v_before_sag = 13.5
    tracker._current_load = 0.0
    tracker._record_voltage_sag(v_sag=12.5, event_type=EventType.BLACKOUT_REAL)
    mock_model.add_r_internal_entry.assert_not_called()


# ------------------------------------------------------------------
# _record_voltage_sag: r_internal and RLS calibration
# ------------------------------------------------------------------

def test_record_sag_computes_r_internal_and_calls_model():
    """_record_voltage_sag computes R_internal and calls add_r_internal_entry."""
    tracker, mock_model = make_tracker(nominal_voltage=13.0, nominal_power_watts=425.0)
    tracker._v_before_sag = 13.0
    tracker._current_load = 25.0

    tracker._record_voltage_sag(v_sag=12.5, event_type=EventType.BLACKOUT_REAL)

    # I_actual = 25/100 * 425 / 13 ≈ 0.817A
    # delta_v = 13.0 - 12.5 = 0.5V
    # r_ohm = 0.5 / (25/100 * 425/13) ≈ 0.612 Ohm
    mock_model.add_r_internal_entry.assert_called_once()
    call_args = mock_model.add_r_internal_entry.call_args[0]
    r_ohm = call_args[1]
    assert r_ohm > 0
    v_before = call_args[2]
    v_sag_arg = call_args[3]
    assert abs(v_before - 13.0) < 1e-9
    assert abs(v_sag_arg - 12.5) < 1e-9
    assert call_args[5] == EventType.BLACKOUT_REAL.name


def test_record_sag_updates_ir_k_via_rls():
    """_record_voltage_sag runs RLS update and stores updated ir_k."""
    tracker, mock_model = make_tracker(nominal_voltage=13.0, nominal_power_watts=425.0)
    tracker._v_before_sag = 13.0
    tracker._current_load = 25.0
    old_ir_k = tracker.ir_k

    tracker._record_voltage_sag(v_sag=12.5, event_type=EventType.BLACKOUT_REAL)

    # ir_k should have changed
    assert tracker.ir_k != old_ir_k
    # Should be within physical bounds
    assert 0.005 <= tracker.ir_k <= 0.025
    # model.set_ir_k and set_rls_state should be called
    mock_model.set_ir_k.assert_called_once_with(tracker.ir_k)
    mock_model.set_rls_state.assert_called_once()
    # RLS sample count should be 1
    assert tracker.rls_ir_k.sample_count == 1


def test_record_sag_clamps_ir_k_at_lower_bound():
    """ir_k clamped to IR_K_MIN (0.005) when RLS estimate goes below."""
    # Use tiny sag (near zero delta_v) to drive ir_k_measured near zero
    tracker, mock_model = make_tracker(
        ir_k=0.005,
        rls_theta=0.005,
        nominal_voltage=13.0,
        nominal_power_watts=425.0,
    )
    tracker._v_before_sag = 13.0
    tracker._current_load = 25.0
    # Very small sag -> very small ir_k_measured -> RLS pulls theta below 0.005
    tracker._record_voltage_sag(v_sag=12.999, event_type=EventType.BLACKOUT_REAL)
    assert tracker.ir_k >= 0.005


def test_record_sag_clamps_ir_k_at_upper_bound():
    """ir_k clamped to IR_K_MAX (0.025) when RLS estimate goes above."""
    tracker, mock_model = make_tracker(
        ir_k=0.025,
        rls_theta=0.025,
        nominal_voltage=13.0,
        nominal_power_watts=425.0,
    )
    tracker._v_before_sag = 13.0
    tracker._current_load = 25.0
    # Very large sag -> ir_k_measured >> 0.025 -> clamp to 0.025
    tracker._record_voltage_sag(v_sag=10.0, event_type=EventType.BLACKOUT_REAL)
    assert tracker.ir_k <= 0.025


# ------------------------------------------------------------------
# reset_idle and reset_rls
# ------------------------------------------------------------------

def test_reset_idle_sets_state_to_idle():
    """reset_idle() moves state to IDLE regardless of current state."""
    tracker, _ = make_tracker()
    tracker._state = SagState.MEASURING
    tracker.reset_idle()
    assert tracker._state == SagState.IDLE
    assert tracker.is_measuring is False


def test_reset_rls_creates_fresh_estimator():
    """reset_rls() replaces rls_ir_k with a fresh ScalarRLS and sets ir_k to theta."""
    tracker, _ = make_tracker(ir_k=0.020, rls_theta=0.020)
    # Feed some data to build history
    tracker.rls_ir_k.update(0.018)
    tracker.rls_ir_k.update(0.019)
    assert tracker.rls_ir_k.sample_count == 2

    tracker.reset_rls(theta=0.015, P=1.0)

    assert tracker.ir_k == 0.015
    assert tracker.rls_ir_k.theta == 0.015
    assert tracker.rls_ir_k.P == 1.0
    assert tracker.rls_ir_k.sample_count == 0
