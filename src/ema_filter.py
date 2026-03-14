import math
from typing import Optional


class MetricEMA:
    """Generic exponential moving average for any metric (voltage, load, temperature, etc.)."""

    def __init__(
        self,
        metric_name: str,
        window_sec: int = 120,
        poll_interval_sec: int = 10,
        sensitivity: float = 0.05
    ):
        """Initialize MetricEMA for a named metric.

        Args:
            metric_name: Name of metric (for debugging/logging)
            window_sec: Smoothing window in seconds
            poll_interval_sec: Time between polls (10s typical)
            sensitivity: Threshold for adaptive alpha activation
        """
        self.metric_name = metric_name
        self.window_sec = window_sec
        self.poll_interval_sec = poll_interval_sec
        self.sensitivity = sensitivity

        # Base α = 1 - exp(-Δt/τ), used when signal is stable
        self.alpha = 1 - math.exp(-poll_interval_sec / window_sec)
        self._min_samples = max(12, int(window_sec / poll_interval_sec))

        # EMA state
        self.ema_value: Optional[float] = None
        self.samples_since_init = 0

    def update(self, new_value: float) -> float:
        """Update EMA with new value; return smoothed value."""
        self.samples_since_init += 1
        self.ema_value = self._update_ema(new_value, self.ema_value)
        return self.ema_value

    def _adaptive_alpha(self, new_value: float, current_ema: Optional[float]) -> float:
        """Compute adaptive alpha based on deviation from current EMA.

        Small deviation → alpha_base (smooth filtering).
        Large deviation (≥ sensitivity) → approaches 1.0 (instant reaction).
        """
        if current_ema is None or abs(current_ema) < 1e-6:
            return 1.0
        deviation = abs(new_value - current_ema) / abs(current_ema)
        blend = min(deviation / self.sensitivity, 1.0)
        return self.alpha + (1.0 - self.alpha) * blend

    def _update_ema(self, new_value: float, current_ema: Optional[float]) -> float:
        """Apply adaptive EMA update; returns new EMA value."""
        if current_ema is None:
            return new_value
        alpha = self._adaptive_alpha(new_value, current_ema)
        return alpha * new_value + (1 - alpha) * current_ema

    @property
    def stabilized(self) -> bool:
        """True if EMA has settled (≥12 readings, ~2 min at default poll rate)."""
        return self.samples_since_init >= self._min_samples

    @property
    def value(self) -> Optional[float]:
        """Current smoothed value."""
        return self.ema_value


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

        # Create MetricEMA instances for voltage and load
        self.voltage_ema = MetricEMA("voltage", window_sec, poll_interval_sec, sensitivity)
        self.load_ema = MetricEMA("load", window_sec, poll_interval_sec, sensitivity)

        # Expose alpha for backward compatibility
        self.alpha = self.voltage_ema.alpha
        self._min_samples = self.voltage_ema._min_samples

    def add_sample(self, voltage, load):
        """Add new voltage and load reading; update both EMA values."""
        self.voltage_ema.update(voltage)
        self.load_ema.update(load)

    @property
    def stabilized(self) -> bool:
        """True if EMA has settled (≥12 readings, ~2 min at default poll rate)."""
        return self.voltage_ema.stabilized and self.load_ema.stabilized

    @property
    def voltage(self) -> Optional[float]:
        """Current EMA voltage, or None if not initialized."""
        return self.voltage_ema.value

    @property
    def load(self) -> Optional[float]:
        """Current EMA load, or None if not initialized."""
        return self.load_ema.value


def ir_compensate(v_ema, l_ema, l_base=20.0, k=0.015):
    """
    Apply IR compensation to normalize voltage for load-independent SoC lookup.

    Formula: V_norm = V_ema + k * (L_ema - L_base)

    NOTE: F23 — Linear model valid at <50% load only. Above 50% load,
    concentration polarization effects make the linear approximation
    inaccurate; higher-order effects dominate the voltage response.
    """
    if v_ema is None or l_ema is None:
        return None

    return v_ema + k * (l_ema - l_base)
