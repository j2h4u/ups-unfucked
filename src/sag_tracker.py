"""Voltage sag state machine and ir_k auto-calibration.

Extracted from MonitorDaemon to reduce its responsibility surface (ARCH-03).
SagTracker owns the IDLE -> MEASURING -> COMPLETE state machine, sag recording,
and RLS ir_k calibration — all stateful collaborator behavior that does not
belong inline in the daemon orchestration loop.
"""

import logging
from datetime import datetime
from typing import Optional

from src.model import BatteryModel
from src.battery_math.rls import ScalarRLS
from src.monitor_config import SagState, SAG_SAMPLES_REQUIRED

logger = logging.getLogger('ups-battery-monitor')

# Physical bounds for ir_k: below 0.005 is noise, above 0.025 is implausible for VRLA.
IR_K_MIN = 0.005
IR_K_MAX = 0.025


class SagTracker:
    """Voltage sag state machine and ir_k auto-calibration.

    Measures voltage sag on OL->OB transitions to estimate battery internal
    resistance. Owns the IDLE -> MEASURING -> COMPLETE state machine and
    the RLS estimator for ir_k calibration.

    Usage:
        tracker = SagTracker(battery_model, rls_ir_k, ir_k)
        # Each poll:
        tracker.track(voltage, event_type, transition_occurred, current_load)
        # In _compute_metrics:
        v_norm = ir_compensate(v_ema, l_ema, ref_load, tracker.ir_k)
        # On polling error:
        tracker.reset_idle()
        # On battery replacement:
        tracker.reset_rls(theta=0.015, P=1.0)
    """

    def __init__(
        self,
        battery_model: BatteryModel,
        rls_ir_k: ScalarRLS,
        ir_k: float,
    ):
        """Initialize SagTracker.

        Args:
            battery_model: Persistent battery model — used for get_nominal_voltage(),
                get_nominal_power_watts(), add_r_internal_entry(), set_ir_k(),
                set_rls_state().
            rls_ir_k: Pre-seeded RLS estimator (restored from model.json on startup
                so calibration survives restarts).
            ir_k: Current ir_k value from model (restored on startup).
        """
        self.battery_model = battery_model
        self.rls_ir_k = rls_ir_k
        self.ir_k = ir_k

        self._state = SagState.IDLE
        self._v_before_sag: Optional[float] = None
        self._sag_buffer: list[float] = []
        self._current_load: Optional[float] = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def is_measuring(self) -> bool:
        """True while collecting voltage samples after OL->OB transition."""
        return self._state == SagState.MEASURING

    def track(
        self,
        voltage: float,
        event_type,
        transition_occurred: bool,
        current_load: Optional[float],
    ) -> None:
        """Drive the sag state machine for one poll tick.

        Call this once per poll after EMA update and event classification.

        State transitions:
          IDLE    + transition + not ONLINE  -> MEASURING (capture pre-sag voltage)
          MEASURING + transition + ONLINE    -> IDLE      (cancelled, power restored)
          MEASURING + SAG_SAMPLES_REQUIRED   -> COMPLETE  (sag recorded)

        Args:
            voltage: Current EMA voltage reading.
            event_type: Classified event (EventType enum). None treated as ONLINE.
            transition_occurred: True if event_type changed since last poll.
            current_load: EMA load percentage [0-100] or None if unavailable.
        """
        from src.event_classifier import EventType

        # Store load for use by _record_voltage_sag (called within this tick).
        self._current_load = current_load

        # OL->OB: start measuring. Capture the EMA voltage *before* the sag
        # develops — this is the pre-sag reference voltage.
        if transition_occurred and event_type not in (EventType.ONLINE,):
            self._v_before_sag = voltage
            self._sag_buffer = []
            self._state = SagState.MEASURING

        # OB->OL: cancel if still collecting (power restored before enough samples).
        if transition_occurred and event_type == EventType.ONLINE:
            if self._state == SagState.MEASURING:
                self._state = SagState.IDLE

        # Collect samples during MEASURING phase.
        if self._state == SagState.MEASURING:
            self._sag_buffer.append(voltage)
            if len(self._sag_buffer) >= SAG_SAMPLES_REQUIRED:
                # Median of last 3 samples for noise rejection.
                v_sag = sorted(self._sag_buffer[-3:])[1]
                self._record_voltage_sag(v_sag, event_type)
                self._state = SagState.COMPLETE

    def reset_idle(self) -> None:
        """Reset state to IDLE (called on polling error to prevent stuck 1s sleep)."""
        self._state = SagState.IDLE

    def reset_rls(self, theta: float, P: float) -> None:
        """Create fresh RLS estimator and reset ir_k to theta (called on battery replacement).

        Args:
            theta: Initial ir_k estimate (typically 0.015 for VRLA).
            P: Initial error covariance (1.0 = high uncertainty / fresh start).
        """
        self.rls_ir_k = ScalarRLS(theta=theta, P=P)
        self.ir_k = theta

    # ------------------------------------------------------------------
    # Private methods
    # ------------------------------------------------------------------

    def _record_voltage_sag(self, v_sag: float, event_type) -> None:
        """Record voltage sag measurement and update ir_k via RLS calibration.

        Computes R_internal from delta_v / I_actual, logs the measurement,
        then updates the RLS estimator and persists to battery_model.

        Skips silently when:
          - v_before_sag is None (no pre-sag reference captured)
          - current_load is None or zero (no current flowing, can't compute R)
        """
        if self._v_before_sag is None or self._current_load is None:
            return

        load = self._current_load
        nominal_voltage = self.battery_model.get_nominal_voltage()
        nominal_power_watts = self.battery_model.get_nominal_power_watts()

        I_actual = load / 100.0 * nominal_power_watts / nominal_voltage
        if I_actual <= 0:
            return

        delta_v = self._v_before_sag - v_sag
        r_ohm = delta_v / I_actual
        today = datetime.now().strftime('%Y-%m-%d')
        self.battery_model.add_r_internal_entry(
            today, r_ohm, self._v_before_sag, v_sag, load, event_type.name)

        # RLS auto-calibration of ir_k from measured sag data.
        if nominal_voltage > 0:
            ir_k_measured = r_ohm * nominal_power_watts / (nominal_voltage * 100.0)
            new_ir_k, new_P = self.rls_ir_k.update(ir_k_measured)
            new_ir_k = max(IR_K_MIN, min(IR_K_MAX, new_ir_k))
            self.ir_k = new_ir_k
            self.battery_model.set_ir_k(new_ir_k)
            self.battery_model.set_rls_state(
                'ir_k', new_ir_k, new_P, self.rls_ir_k.sample_count)
            logger.info(
                f"ir_k calibrated: {new_ir_k:.4f} (P={new_P:.4f}, "
                f"confidence={self.rls_ir_k.confidence:.0%}, "
                f"measured={ir_k_measured:.4f})",
                extra={
                    'event_type': 'ir_k_calibration',
                    'ir_k': f'{new_ir_k:.4f}',
                    'ir_k_measured': f'{ir_k_measured:.4f}',
                    'rls_p': f'{new_P:.4f}',
                    'rls_confidence': f'{self.rls_ir_k.confidence:.3f}',
                    'sample_count': str(self.rls_ir_k.sample_count),
                })

        logger.info(
            f"Voltage sag: {self._v_before_sag:.2f}V -> {v_sag:.2f}V, "
            f"R_internal={r_ohm*1000:.1f}mOhm at {load:.1f}% load",
            extra={
                'event_type': 'voltage_sag',
                'v_before': f'{self._v_before_sag:.2f}',
                'v_sag': f'{v_sag:.2f}',
                'r_internal_mohm': f'{r_ohm*1000:.1f}',
                'load_pct': f'{load:.1f}',
            }
        )
