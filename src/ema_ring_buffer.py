import collections
import math
from typing import Optional, Tuple


class EMABuffer:
    """
    Exponential moving average buffer with ring buffer storage.

    Maintains separate EMA tracks for voltage and load, with automatic
    stabilization detection (after 3+ samples, safe for predictions).
    """

    def __init__(self, window_sec=120, poll_interval_sec=10):
        """
        Initialize EMA buffer.

        Args:
            window_sec: EMA smoothing window in seconds (tau, ~2 min recommended)
            poll_interval_sec: Time between polls in seconds
        """
        self.window_sec = window_sec
        self.poll_interval_sec = poll_interval_sec

        # Calculate alpha (smoothing factor)
        # α = 1 - exp(-Δt/τ) where Δt = poll_interval, τ = window
        self.alpha = 1 - math.exp(-poll_interval_sec / window_sec)

        # Ring buffer: store (timestamp, value) pairs
        # Size: at least window_sec/poll_interval + headroom for EMA tail
        max_samples = max(int(window_sec / poll_interval_sec) + 10, 24)
        self.buffer_voltage = collections.deque(maxlen=max_samples)
        self.buffer_load = collections.deque(maxlen=max_samples)

        # EMA state
        self.ema_voltage = None
        self.ema_load = None
        self.samples_since_init = 0

    def add_sample(self, timestamp, voltage, load):
        """
        Add new voltage and load reading; update both EMA values.

        Args:
            timestamp: Unix timestamp of measurement
            voltage: Battery voltage (float, volts)
            load: UPS load (float, percent 0-100)
        """
        self.buffer_voltage.append((timestamp, voltage))
        self.buffer_load.append((timestamp, load))
        self.samples_since_init += 1

        # Update voltage EMA
        if self.ema_voltage is None:
            self.ema_voltage = voltage
        else:
            self.ema_voltage = self.alpha * voltage + (1 - self.alpha) * self.ema_voltage

        # Update load EMA
        if self.ema_load is None:
            self.ema_load = load
        else:
            self.ema_load = self.alpha * load + (1 - self.alpha) * self.ema_load

    @property
    def stabilized(self) -> bool:
        """True if EMA has settled (≥3 readings)."""
        return self.samples_since_init >= 3

    @property
    def voltage(self) -> Optional[float]:
        """Current EMA voltage, or None if not initialized."""
        return self.ema_voltage

    @property
    def load(self) -> Optional[float]:
        """Current EMA load, or None if not initialized."""
        return self.ema_load

    def get_values(self) -> Tuple[Optional[float], Optional[float]]:
        """Return (voltage, load) EMA values as tuple."""
        return (self.ema_voltage, self.ema_load)

    def buffer_size(self) -> Tuple[int, int]:
        """Return (voltage_buffer_size, load_buffer_size) for diagnostics."""
        return (len(self.buffer_voltage), len(self.buffer_load))


def ir_compensate(v_ema, l_ema, l_base=20.0, k=0.015):
    """
    Apply IR (internal resistance) compensation to normalize voltage.

    Compensates for voltage drop caused by load variation, enabling
    load-independent battery model lookup.

    Args:
        v_ema: EMA voltage (volts)
        l_ema: EMA load (percent, 0-100)
        l_base: Reference load for normalization (percent, default 20%)
        k: IR compensation coefficient (V per % load, default 0.015)

    Returns:
        Normalized voltage (v_ema + k*(l_ema - l_base))

    Formula: V_norm = V_ema + k * (L_ema - L_base)

    This corrects voltage to what it would be at reference load,
    enabling accurate SoC lookup independent of current draw.
    """
    if v_ema is None or l_ema is None:
        return None

    v_norm = v_ema + k * (l_ema - l_base)
    return v_norm
