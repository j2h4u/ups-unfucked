"""Voltage sag state machine and observation-only OL→OB capture.

Extracted from MonitorDaemon to reduce its responsibility surface (ARCH-03).
SagTracker owns the IDLE -> MEASURING -> COMPLETE state machine and returns
immutable sag observations.  OL→OB observations do not update the persisted
IR model: that voltage step mixes charger surface-charge collapse with true
internal resistance.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.battery_math.rls import ScalarRLS
from src.model import BatteryModel
from src.monitor_config import SAG_SAMPLES_REQUIRED, SagState

logger = logging.getLogger("ups-battery-monitor")


@dataclass(frozen=True)
class VoltageSagObservation:
    """Immutable OL→OB sag observation.

    This is deliberately a journal/reporting value, not a model update.  The
    initial OL→OB drop includes charger surface-charge collapse and therefore
    is not independent evidence for changing ``ir_k``.
    """

    observed_at: datetime
    event_type: str
    voltage_before: float
    voltage_sag: float
    load_percent: float
    apparent_r_internal_ohm: float


class SagTracker:
    """Voltage sag state machine with observation-only OL→OB capture.

    Measures voltage sag on OL->OB transitions and returns a frozen
    :class:`VoltageSagObservation`.  The RLS estimator is retained as the
    current model input but is intentionally not updated by this path.

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
            battery_model: Persistent battery model — used only to read nominal
                voltage and power while constructing an observation.
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
    ) -> Optional[VoltageSagObservation]:
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
        observation = None

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
                observation = self._record_voltage_sag(v_sag, event_type)
                self._state = SagState.COMPLETE

        return observation

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

    def _record_voltage_sag(self, v_sag: float, event_type) -> Optional[VoltageSagObservation]:
        """Return an immutable sag observation without changing model state.

        Computes R_internal from delta_v / I_actual.  The old implementation
        persisted the measurement and updated the RLS estimator here.  That is
        unsafe for OL→OB because the voltage step also contains surface-charge
        collapse; Release A records the observation only and leaves ``ir_k``,
        RLS state, and ``r_internal_history`` untouched.

        Skips silently when:
          - v_before_sag is None (no pre-sag reference captured)
          - current_load is None or zero (no current flowing, can't compute R)
        """
        if self._v_before_sag is None or self._current_load is None:
            return None

        load = self._current_load
        nominal_voltage = self.battery_model.get_nominal_voltage()
        nominal_power_watts = self.battery_model.get_nominal_power_watts()

        if nominal_voltage <= 0 or nominal_power_watts <= 0:
            return None
        I_actual = load / 100.0 * nominal_power_watts / nominal_voltage
        if I_actual <= 0:
            return None

        delta_v = self._v_before_sag - v_sag
        # Guard: the UPS can return to OL before the sag fully develops, leaving
        # v_sag == v_before (delta_v <= 0). A zero/negative R_internal is physically
        # meaningless and must not become history or calibration input.
        # Skip these non-measurements rather than record them. (Observed May 2026:
        # 2 of 3 calibration tests returned delta_v=0 and corrupted ir_k.)
        if delta_v <= 0:
            logger.debug(
                "Voltage sag skipped: delta_v=%.3fV <= 0 (UPS returned to OL before sag developed)",
                delta_v,
            )
            return None
        r_ohm = delta_v / I_actual
        logger.info(
            "Voltage sag observation: %.2fV -> %.2fV, R_internal=%.1fmOhm at %.1f%% load",
            self._v_before_sag,
            v_sag,
            r_ohm * 1000,
            load,
            extra={
                "event_type": "voltage_sag_observation",
                "v_before": f"{self._v_before_sag:.2f}",
                "v_sag": f"{v_sag:.2f}",
                "r_internal_mohm": f"{r_ohm * 1000:.1f}",
                "load_pct": f"{load:.1f}",
            },
        )
        return VoltageSagObservation(
            observed_at=datetime.now(timezone.utc),
            event_type=getattr(event_type, "name", str(event_type)),
            voltage_before=self._v_before_sag,
            voltage_sag=v_sag,
            load_percent=load,
            apparent_r_internal_ohm=r_ohm,
        )
