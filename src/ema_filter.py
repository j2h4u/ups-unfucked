import math
import time
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

        # Used when signal is stable; adaptive alpha takes over during transients
        self.alpha = 1 - math.exp(-poll_interval_sec / window_sec)

        # EMA state
        self.ema_value: Optional[float] = None
        self._first_sample_time: Optional[float] = None

    def update(self, new_value: float) -> float:
        """Update EMA with new value; return smoothed value."""
        if self._first_sample_time is None:
            self._first_sample_time = time.monotonic()
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
        deviation_fraction = min(deviation / self.sensitivity, 1.0)
        return self.alpha + (1.0 - self.alpha) * deviation_fraction

    def _update_ema(self, new_value: float, current_ema: Optional[float]) -> float:
        """Apply adaptive EMA update; returns new EMA value."""
        if current_ema is None:
            return new_value
        alpha = self._adaptive_alpha(new_value, current_ema)
        return alpha * new_value + (1 - alpha) * current_ema

    @property
    def stabilized(self) -> bool:
        """True if enough real time has elapsed for EMA to converge (≥ window_sec)."""
        if self._first_sample_time is None:
            return False
        return (time.monotonic() - self._first_sample_time) >= self.window_sec

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

    Known limitations (audit 2026-03-17):
    - F1: Adaptive alpha amplifies ADC quantization ~3x (0.06V band at 13.5V).
      <1% SoC impact — acceptable for shutdown decision accuracy.
    - F2: ``abs(ema) < 1e-6`` guard in _adaptive_alpha makes alpha=1.0 at
      near-zero load (no smoothing). Server runs 14-20% load — unreachable.
    - F3: IR compensation uses a linear model that is invalid during
      electrochemical discharge (OB). Error bounded ≤0.06V at typical loads
      (cross-ref F8). Acceptable because RLS calibration at the actual
      operating point absorbs systematic bias.
    - F4: First sample seeds EMA directly — a stale NUT reading biases
      the EMA for up to 120s (one window). Mitigated: NUT usbhid-ups
      pollinterval=2s, daemon polls every 10s, so first reading is at
      most 2s old.
    """

    def __init__(self, window_sec=120, poll_interval_sec=10, sensitivity=0.05):
        """Initialize EMAFilter for voltage and load tracking.

        Args:
            window_sec: Smoothing window in seconds (convergence time).
            poll_interval_sec: Expected interval between samples.
            sensitivity: Relative deviation threshold [0, 1] for adaptive alpha.
                When |new - ema| / |ema| exceeds sensitivity, alpha ramps toward
                1.0 for instant reaction. Lower values = more reactive to small
                changes. Default 0.05 (5% deviation triggers fast tracking).
        """
        self.window_sec = window_sec
        self.poll_interval_sec = poll_interval_sec
        self.sensitivity = sensitivity

        # Create MetricEMA instances for voltage and load
        self.voltage_ema = MetricEMA("voltage", window_sec, poll_interval_sec, sensitivity)
        self.load_ema = MetricEMA("load", window_sec, poll_interval_sec, sensitivity)

        self.alpha = self.voltage_ema.alpha

    def add_sample(self, voltage: float, load: float):
        """Add new voltage and load reading; update both EMA values.

        Precondition: voltage and load must not be None (caller must guard).
        """
        self.voltage_ema.update(voltage)
        self.load_ema.update(load)

    @property
    def stabilized(self) -> bool:
        """True if enough wall-clock time has elapsed (≥ window_sec) for EMA to converge."""
        return self.voltage_ema.stabilized and self.load_ema.stabilized

    @property
    def voltage(self) -> Optional[float]:
        """Current EMA voltage, or None if not initialized."""
        return self.voltage_ema.value

    @property
    def load(self) -> Optional[float]:
        """Current EMA load, or None if not initialized."""
        return self.load_ema.value


def ir_compensate(v_ema: Optional[float], l_ema: Optional[float],
                   l_base: float = 20.0, k: float = 0.015) -> Optional[float]:
    """Apply IR compensation to normalize voltage for load-independent SoC lookup.

    Formula: V_norm = V_ema + k * (L_ema - L_base)

    Args:
        v_ema: EMA-smoothed voltage (V), or None if not yet initialized.
        l_ema: EMA-smoothed load (%), or None if not yet initialized.
        l_base: Reference load percent for normalization (default 20%).
        k: IR coefficient (volts per load-percent). Calibrated by RLS (ir_k).

    Returns:
        Compensated voltage, or None if either input is None.

    NOTE: Linear model valid at <50% load only. Above 50%, concentration
    polarization effects make the approximation inaccurate.
    """
    if v_ema is None or l_ema is None:
        return None

    return v_ema + k * (l_ema - l_base)
