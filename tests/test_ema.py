"""Tests for EMAFilter class: convergence, stabilization gate, alpha factor, adaptive alpha."""

import pytest
import math
from src.ema_filter import EMAFilter, MetricEMA


def fill_samples(buf, n, voltage=12.0, load=50.0):
    """Add n identical samples to buffer."""
    for _ in range(n):
        buf.add_sample(voltage, load)


class TestEMAConvergence:
    """Test EMA convergence to constant input."""

    def test_ema_convergence(self):
        """EMA reaches ~90% convergence within 5 samples (window=120s, poll=10s)."""
        buf = EMAFilter(window_sec=120, poll_interval_sec=10)
        fill_samples(buf, 5, voltage=12.0)

        assert buf.voltage is not None
        assert buf.voltage >= 12.0 * 0.85
        assert buf.voltage <= 12.0 * 1.0

    def test_ema_asymptotic_convergence(self):
        """After 10 samples should be ~99% converged."""
        buf = EMAFilter(window_sec=120, poll_interval_sec=10)
        fill_samples(buf, 10, voltage=13.0)

        assert abs(buf.voltage - 13.0) < 0.05


class TestStabilizationGate:
    """Test stabilized property — requires 12 samples (2 min at default rate)."""

    def test_stabilization_false_before_12_samples(self):
        buf = EMAFilter(window_sec=120, poll_interval_sec=10)
        fill_samples(buf, 11)
        assert buf.stabilized is False

    def test_stabilization_true_at_12_samples(self):
        buf = EMAFilter(window_sec=120, poll_interval_sec=10)
        for i in range(12):
            buf.add_sample(12.0 + i * 0.05, 50.0)
        assert buf.stabilized is True

    def test_stabilization_remains_true(self):
        buf = EMAFilter(window_sec=120, poll_interval_sec=10)
        fill_samples(buf, 20)
        assert buf.stabilized is True

    def test_stabilization_adapts_to_window(self):
        """With larger window/poll ratio, min_samples = window/poll if > 12."""
        buf = EMAFilter(window_sec=200, poll_interval_sec=10)
        fill_samples(buf, 19)
        assert buf.stabilized is False

        buf.add_sample(12.0, 50.0)
        assert buf.stabilized is True


class TestEMAAlphaFactor:
    """Test alpha factor calculation."""

    def test_alpha_factor_calculation(self):
        buf = EMAFilter(window_sec=120, poll_interval_sec=10)
        expected_alpha = 1 - math.exp(-10 / 120)
        assert abs(buf.alpha - expected_alpha) < 1e-10
        assert 0.07 < buf.alpha < 0.09

    def test_alpha_increases_with_poll_interval(self):
        buf1 = EMAFilter(window_sec=120, poll_interval_sec=5)
        buf2 = EMAFilter(window_sec=120, poll_interval_sec=10)
        buf3 = EMAFilter(window_sec=120, poll_interval_sec=20)
        assert buf1.alpha < buf2.alpha < buf3.alpha

    def test_alpha_decreases_with_window(self):
        buf1 = EMAFilter(window_sec=60, poll_interval_sec=10)
        buf2 = EMAFilter(window_sec=120, poll_interval_sec=10)
        buf3 = EMAFilter(window_sec=240, poll_interval_sec=10)
        assert buf1.alpha > buf2.alpha > buf3.alpha


class TestEMAProperties:
    """Test EMA buffer properties and API."""

    def test_voltage_and_load_properties(self):
        buf = EMAFilter()
        buf.add_sample(12.5, 60.0)
        assert abs(buf.voltage - 12.5) < 0.01
        assert abs(buf.load - 60.0) < 0.01

    def test_initial_none_values(self):
        buf = EMAFilter()
        assert buf.voltage is None
        assert buf.load is None
        assert buf.stabilized is False

    def test_samples_since_init_counter(self):
        buf = EMAFilter()
        assert buf.samples_since_init == 0
        buf.add_sample(12.0, 50.0)
        assert buf.samples_since_init == 1
        buf.add_sample(12.1, 51.0)
        assert buf.samples_since_init == 2


class TestAdaptiveAlpha:
    """Test adaptive alpha — fast reaction to large deviations, smooth on noise."""

    def test_spike_recovery_within_2_samples(self):
        """After a voltage spike, EMA recovers to within 1% in ≤2 samples."""
        buf = EMAFilter(window_sec=120, poll_interval_sec=10)
        fill_samples(buf, 20, voltage=13.5, load=20.0)
        assert abs(buf.voltage - 13.5) < 0.01

        # Spike down to 12.8V (like quick battery test)
        buf.add_sample(12.8, 20.0)
        assert abs(buf.voltage - 12.8) < 0.1

        # Return to 13.5V — should recover fast, not drag like classic EMA
        buf.add_sample(13.5, 20.0)
        buf.add_sample(13.5, 20.0)
        assert abs(buf.voltage - 13.5) < 0.15

    def test_noise_still_smoothed(self):
        """Small noise (±0.02V) is still smoothed, not passed through."""
        buf = EMAFilter(window_sec=120, poll_interval_sec=10)
        fill_samples(buf, 20, voltage=13.5, load=20.0)

        # +0.02V, ~0.15% deviation — well below 5% sensitivity
        buf.add_sample(13.52, 20.0)
        assert buf.voltage < 13.51

    def test_adaptive_alpha_bounds(self):
        """_adaptive_alpha always returns value in [alpha_base, 1.0]."""
        buf = EMAFilter(window_sec=120, poll_interval_sec=10)
        assert buf._adaptive_alpha(13.5, 13.5) == pytest.approx(buf.alpha)  # no deviation
        assert buf._adaptive_alpha(14.5, 13.5) <= 1.0  # large deviation
        assert buf._adaptive_alpha(14.5, 13.5) > buf.alpha  # larger than base


class TestMetricEMA:
    """Test generic MetricEMA class for single metric tracking."""

    def test_metric_ema_single_metric(self):
        """MetricEMA initializes with metric_name and tracks single value."""
        ema = MetricEMA("voltage", window_sec=120, poll_interval_sec=10)
        assert ema.metric_name == "voltage"
        assert ema.value is None
        assert ema.samples_since_init == 0

        # After first update
        val = ema.update(12.5)
        assert abs(val - 12.5) < 0.01
        assert ema.value == val
        assert ema.samples_since_init == 1

    def test_metric_ema_multiple_independent(self):
        """Multiple MetricEMA instances track voltage, load, temperature independently."""
        voltage_ema = MetricEMA("voltage", window_sec=120, poll_interval_sec=10)
        load_ema = MetricEMA("load", window_sec=120, poll_interval_sec=10)
        temp_ema = MetricEMA("temperature", window_sec=120, poll_interval_sec=10)

        # Update with different patterns
        for i in range(5):
            voltage_ema.update(12.0 + i * 0.1)
            load_ema.update(50.0 + i * 5.0)
            temp_ema.update(25.0 + i * 0.2)

        # Each tracks independently
        assert voltage_ema.value is not None
        assert load_ema.value is not None
        assert temp_ema.value is not None
        assert voltage_ema.metric_name == "voltage"
        assert load_ema.metric_name == "load"
        assert temp_ema.metric_name == "temperature"

    def test_ema_filter_backward_compatible(self):
        """EMAFilter.update(v, l) returns tuple; .ema_voltage and .ema_load still work."""
        buf = EMAFilter(window_sec=120, poll_interval_sec=10)

        # Old API: add_sample
        buf.add_sample(12.5, 60.0)
        assert buf.voltage == buf.ema_voltage
        assert buf.load == buf.ema_load

        # New properties still accessible
        assert buf.ema_voltage is not None
        assert buf.ema_load is not None

    def test_metric_ema_stabilized_flag(self):
        """MetricEMA.stabilized false for < min_samples, true after."""
        ema = MetricEMA("voltage", window_sec=120, poll_interval_sec=10)
        assert ema.stabilized is False

        # Add 11 samples (< 12)
        for _ in range(11):
            ema.update(12.0)
        assert ema.stabilized is False

        # Add 12th sample
        ema.update(12.0)
        assert ema.stabilized is True
