"""
Tests for EMABuffer class: convergence, stabilization gate, and ring buffer memory.
"""

import pytest
import math
import time
from src.ema_ring_buffer import EMABuffer


class TestEMAConvergence:
    """Test EMA convergence to constant input."""

    def test_ema_convergence(self):
        """
        Verify EMA reaches 90% convergence within 5 samples at 120-sec window, 10-sec interval.

        Theory: After N samples, EMA = input * (1 - (1-α)^N)
        With α ≈ 0.0787 and N=5: EMA ≈ input * 0.3387 = 33.87% after 1 sample, rising to ~90% by sample 5
        """
        buf = EMABuffer(window_sec=120, poll_interval_sec=10)
        constant_value = 12.0

        # Add 5 samples with same value
        for i in range(5):
            buf.add_sample(timestamp=time.time() + i * 10, voltage=constant_value, load=50.0)

        # After 5 samples, should be close to 90% of constant_value
        assert buf.voltage is not None
        assert buf.voltage >= constant_value * 0.85  # At least 85% converged
        assert buf.voltage <= constant_value * 1.0   # Not exceeding target

    def test_ema_asymptotic_convergence(self):
        """EMA approaches input asymptotically; after 10 samples should be ~99% converged."""
        buf = EMABuffer(window_sec=120, poll_interval_sec=10)
        constant_value = 13.0

        for i in range(10):
            buf.add_sample(timestamp=time.time() + i * 10, voltage=constant_value, load=50.0)

        # After 10 samples, should be very close to target
        assert buf.voltage is not None
        assert abs(buf.voltage - constant_value) < 0.05


class TestStabilizationGate:
    """Test stabilized property gating."""

    def test_stabilization_false_before_3_samples(self):
        """Stabilized flag should be False for first 2 samples."""
        buf = EMABuffer()

        buf.add_sample(timestamp=time.time(), voltage=12.0, load=50.0)
        assert buf.stabilized is False

        buf.add_sample(timestamp=time.time() + 10, voltage=12.1, load=51.0)
        assert buf.stabilized is False

    def test_stabilization_true_at_3_samples(self):
        """Stabilized flag should be True from 3rd sample onward."""
        buf = EMABuffer()

        buf.add_sample(timestamp=time.time(), voltage=12.0, load=50.0)
        buf.add_sample(timestamp=time.time() + 10, voltage=12.1, load=51.0)
        buf.add_sample(timestamp=time.time() + 20, voltage=12.2, load=51.5)

        assert buf.stabilized is True

    def test_stabilization_remains_true(self):
        """Stabilized stays True after reaching 3+ samples."""
        buf = EMABuffer()

        for i in range(10):
            buf.add_sample(timestamp=time.time() + i * 10, voltage=12.0 + i * 0.05, load=50.0 + i * 0.5)

        assert buf.stabilized is True


class TestRingBufferMemory:
    """Test ring buffer doesn't grow unbounded."""

    def test_ring_buffer_bounded(self):
        """Buffer sizes should never exceed calculated max_samples."""
        buf = EMABuffer(window_sec=120, poll_interval_sec=10)

        # Add 100 samples
        for i in range(100):
            buf.add_sample(timestamp=time.time() + i * 10, voltage=12.0 + (i % 5) * 0.1, load=50.0)

        v_size, l_size = buf.buffer_size()
        # Expected max_samples = max(120/10 + 10, 24) = max(22, 24) = 24
        expected_max = max(int(120 / 10) + 10, 24)
        assert v_size <= expected_max
        assert l_size <= expected_max

    def test_ring_buffer_fifo_behavior(self):
        """Deque maxlen ensures FIFO: oldest samples are dropped first."""
        buf = EMABuffer(window_sec=120, poll_interval_sec=10)
        max_samples = max(int(120 / 10) + 10, 24)

        # Add more samples than buffer can hold
        for i in range(max_samples + 5):
            buf.add_sample(timestamp=i * 10, voltage=12.0 + i * 0.01, load=50.0)

        # Check buffer size is at max
        v_size, l_size = buf.buffer_size()
        assert v_size == max_samples
        assert l_size == max_samples

        # Oldest samples should be gone
        # The first sample added was timestamp=0, but after adding max_samples + 5 samples,
        # only the last max_samples are retained
        oldest_timestamp = buf.buffer_voltage[0][0]
        expected_oldest = (5) * 10  # Oldest retained should be from sample 5+
        assert oldest_timestamp >= expected_oldest


class TestEMAAlphaFactor:
    """Test alpha factor calculation."""

    def test_alpha_factor_calculation(self):
        """Verify alpha = 1 - exp(-poll_interval / window_sec)."""
        buf = EMABuffer(window_sec=120, poll_interval_sec=10)

        expected_alpha = 1 - math.exp(-10 / 120)
        assert abs(buf.alpha - expected_alpha) < 1e-10
        assert 0.07 < buf.alpha < 0.09  # Approximately 0.0787

    def test_alpha_increases_with_poll_interval(self):
        """Larger poll_interval → larger alpha (faster convergence)."""
        buf1 = EMABuffer(window_sec=120, poll_interval_sec=5)
        buf2 = EMABuffer(window_sec=120, poll_interval_sec=10)
        buf3 = EMABuffer(window_sec=120, poll_interval_sec=20)

        assert buf1.alpha < buf2.alpha < buf3.alpha

    def test_alpha_decreases_with_window(self):
        """Larger window_sec → smaller alpha (slower convergence)."""
        buf1 = EMABuffer(window_sec=60, poll_interval_sec=10)
        buf2 = EMABuffer(window_sec=120, poll_interval_sec=10)
        buf3 = EMABuffer(window_sec=240, poll_interval_sec=10)

        assert buf1.alpha > buf2.alpha > buf3.alpha


class TestEMAProperties:
    """Test EMA buffer properties and API."""

    def test_voltage_and_load_properties(self):
        """Properties return correct values."""
        buf = EMABuffer()
        buf.add_sample(timestamp=time.time(), voltage=12.5, load=60.0)

        assert buf.voltage is not None
        assert buf.load is not None
        assert abs(buf.voltage - 12.5) < 0.01
        assert abs(buf.load - 60.0) < 0.01

    def test_get_values_tuple(self):
        """get_values() returns (voltage, load) tuple."""
        buf = EMABuffer()
        buf.add_sample(timestamp=time.time(), voltage=13.0, load=45.0)

        v, l = buf.get_values()
        assert v is not None
        assert l is not None
        assert abs(v - 13.0) < 0.01
        assert abs(l - 45.0) < 0.01

    def test_initial_none_values(self):
        """EMA values start as None before first sample."""
        buf = EMABuffer()

        assert buf.voltage is None
        assert buf.load is None
        assert buf.stabilized is False

    def test_samples_since_init_counter(self):
        """samples_since_init increments with each sample."""
        buf = EMABuffer()

        assert buf.samples_since_init == 0
        buf.add_sample(timestamp=time.time(), voltage=12.0, load=50.0)
        assert buf.samples_since_init == 1
        buf.add_sample(timestamp=time.time() + 10, voltage=12.1, load=51.0)
        assert buf.samples_since_init == 2
        buf.add_sample(timestamp=time.time() + 20, voltage=12.2, load=51.5)
        assert buf.samples_since_init == 3
