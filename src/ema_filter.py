import math
from typing import Optional


class EMAFilter:
    """
    Adaptive exponential moving average for voltage and load.

    Uses dynamic alpha that increases when input deviates significantly
    from current EMA — fast reaction to real changes (power events,
    battery tests), smooth filtering of sensor noise.

    Inspired by DynamicAdaptiveFilterV2 (Arduino adaptive sensor filtering).

    Tracks separate EMA for voltage and load, with stabilization gate
    (requires sufficient samples before predictions are reliable).
    """

    def __init__(self, window_sec=120, poll_interval_sec=10, sensitivity=0.05):
        self.window_sec = window_sec
        self.poll_interval_sec = poll_interval_sec
        self.sensitivity = sensitivity

        # Base α = 1 - exp(-Δt/τ), used when signal is stable
        self.alpha = 1 - math.exp(-poll_interval_sec / window_sec)
        self._min_samples = max(12, int(window_sec / poll_interval_sec))

        # EMA state
        self.ema_voltage = None
        self.ema_load = None
        self.samples_since_init = 0

    def _adaptive_alpha(self, new_value, current_ema):
        """
        Compute effective alpha based on deviation from current EMA.

        Small deviation → alpha_base (smooth filtering).
        Large deviation (≥ sensitivity) → approaches 1.0 (instant reaction).
        """
        if abs(current_ema) < 1e-6:
            return 1.0
        deviation = abs(new_value - current_ema) / abs(current_ema)
        blend = min(deviation / self.sensitivity, 1.0)
        return self.alpha + (1.0 - self.alpha) * blend

    def _update_ema(self, new_value, current_ema):
        """Apply adaptive EMA update; returns new EMA value."""
        if current_ema is None:
            return new_value
        alpha = self._adaptive_alpha(new_value, current_ema)
        return alpha * new_value + (1 - alpha) * current_ema

    def add_sample(self, voltage, load):
        """Add new voltage and load reading; update both EMA values."""
        self.samples_since_init += 1
        self.ema_voltage = self._update_ema(voltage, self.ema_voltage)
        self.ema_load = self._update_ema(load, self.ema_load)

    @property
    def stabilized(self) -> bool:
        """True if EMA has settled (≥12 readings, ~2 min at default poll rate)."""
        return self.samples_since_init >= self._min_samples

    @property
    def voltage(self) -> Optional[float]:
        """Current EMA voltage, or None if not initialized."""
        return self.ema_voltage

    @property
    def load(self) -> Optional[float]:
        """Current EMA load, or None if not initialized."""
        return self.ema_load



def ir_compensate(v_ema, l_ema, l_base=20.0, k=0.015):
    """
    Apply IR compensation to normalize voltage for load-independent SoC lookup.

    Formula: V_norm = V_ema + k * (L_ema - L_base)
    """
    if v_ema is None or l_ema is None:
        return None

    return v_ema + k * (l_ema - l_base)
