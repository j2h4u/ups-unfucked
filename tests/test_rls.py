"""Tests for ScalarRLS kernel: convergence, forgetting, confidence, serialization."""

import pytest

from src.battery_math.rls import ScalarRLS


class TestScalarRLS:

    def test_convergence(self):
        """50 identical measurements → theta converges to that value, P shrinks."""
        rls = ScalarRLS(theta=0.0, P=1.0, forgetting_factor=0.97)
        target = 0.015

        for _ in range(50):
            rls.update(target)

        assert abs(rls.theta - target) < 1e-3  # λ=0.97 forgetting limits exact convergence
        assert rls.P < 0.1  # P should be very small after 50 samples

    def test_bounds_not_in_kernel(self):
        """Kernel doesn't clamp — that's the caller's job."""
        rls = ScalarRLS(theta=0.5, P=1.0)

        # Feed extreme value: kernel should track it without clamping
        rls.update(100.0)
        assert rls.theta > 0.5  # Moved toward 100
        # No bounds applied by kernel itself

    def test_forgetting_factor(self):
        """Lower λ → faster adaptation to new data."""
        fast = ScalarRLS(theta=1.0, P=0.1, forgetting_factor=0.90)
        slow = ScalarRLS(theta=1.0, P=0.1, forgetting_factor=0.99)

        # Feed same new value to both
        fast.update(2.0)
        slow.update(2.0)

        # Fast forgetting should move theta more toward 2.0
        assert fast.theta > slow.theta

    def test_confidence_increases(self):
        """P shrinks → confidence grows with more samples."""
        rls = ScalarRLS(theta=0.015, P=1.0)
        initial_confidence = rls.confidence

        for _ in range(10):
            rls.update(0.015)

        assert rls.confidence > initial_confidence
        assert rls.confidence > 0.5  # Should be reasonably confident after 10 samples

    def test_serialization_roundtrip(self):
        """to_dict/from_dict preserves full state."""
        original = ScalarRLS(theta=0.018, P=0.25, forgetting_factor=0.95)
        original.sample_count = 7

        d = original.to_dict()
        restored = ScalarRLS.from_dict(d)

        assert restored.theta == original.theta
        assert restored.P == original.P
        assert restored.forgetting_factor == original.forgetting_factor
        assert restored.sample_count == original.sample_count

    def test_first_measurement_high_weight(self):
        """P=1.0 initial → first observation dominates (high Kalman gain)."""
        rls = ScalarRLS(theta=0.0, P=1.0, forgetting_factor=0.97)
        rls.update(0.020)

        # With P=1.0, K = 1/(0.97+1) ≈ 0.508 — first measurement pulls theta ~halfway
        assert rls.theta > 0.009  # At least 45% of the way to 0.020
        assert rls.theta < 0.020  # But not all the way

    def test_sample_count_tracks(self):
        """sample_count increments on each update."""
        rls = ScalarRLS(theta=0.0, P=1.0)
        assert rls.sample_count == 0

        rls.update(1.0)
        assert rls.sample_count == 1

        rls.update(1.0)
        assert rls.sample_count == 2

    def test_from_dict_defaults(self):
        """from_dict with empty dict uses sensible defaults."""
        rls = ScalarRLS.from_dict({})
        assert rls.theta == 0.0
        assert rls.P == 1.0
        assert rls.sample_count == 0
        assert rls.forgetting_factor == 0.97

    def test_confidence_range(self):
        """Confidence is always in [0, 1]."""
        # High P (uncertain)
        rls = ScalarRLS(theta=0.0, P=100.0)
        assert 0.0 <= rls.confidence <= 1.0

        # Low P (confident)
        rls2 = ScalarRLS(theta=0.0, P=0.001)
        assert 0.0 <= rls2.confidence <= 1.0
        assert rls2.confidence > 0.99
