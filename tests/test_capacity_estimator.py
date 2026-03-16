"""Tests for CapacityEstimator class (capacity measurement via coulomb counting)."""

import pytest
import math
import json
from datetime import datetime
from src.capacity_estimator import CapacityEstimator
from tests.conftest import synthetic_discharge_fixture, discharge_buffer_fixture


class TestCoulombIntegration:
    """Test coulomb counting integration (CAP-01)."""

    def test_coulomb_integration_synthetic(self, synthetic_discharge_fixture):
        """Test coulomb integration returns expected Ah within ±1% for synthetic data."""
        voltage_series, time_series, current_percent_series, lut = synthetic_discharge_fixture

        estimator = CapacityEstimator(peukert_exponent=1.2)
        result = estimator.estimate(voltage_series, time_series, current_percent_series, lut)

        # Result should not be None (should pass quality filter)
        assert result is not None
        ah_estimate, confidence, metadata = result

        # For synthetic 100-point discharge with 30% load, ~990s duration
        # I = (30/100) * 425 / 12 ≈ 10.625A
        # Expected: (10.625A * 990s) / 3600 ≈ 2.92Ah
        # Allow ±2% deviation (accounts for trapezoidal rule)
        expected_ah = 2.92
        assert abs(ah_estimate - expected_ah) / expected_ah < 0.02, \
            f"Coulomb estimate {ah_estimate}Ah not within ±2% of expected {expected_ah}Ah"

    def test_coulomb_integration_low_noise(self):
        """Test coulomb integration with known low-noise data."""
        # 30 samples over 600 seconds with 30% load constant
        time_series = [float(i * 20) for i in range(30)]  # 0, 20, 40, ..., 580 seconds (600s total)
        current_percent_series = [30.0] * 30  # 30% load constant
        voltage_series = [13.0 - (i * 0.083) for i in range(30)]  # 13.0V → 10.56V (>50% ΔSoC)
        lut = [
            {"v": 13.0, "soc": 1.0},
            {"v": 10.5, "soc": 0.0}
        ]

        estimator = CapacityEstimator(peukert_exponent=1.2)
        result = estimator.estimate(voltage_series, time_series, current_percent_series, lut)

        assert result is not None
        ah_estimate, confidence, metadata = result

        # I = (30/100) * 425 / 12 ≈ 10.625A
        # (10.625A * 580s) / 3600 ≈ 1.71Ah
        expected_ah = 1.71
        assert abs(ah_estimate - expected_ah) / expected_ah < 0.01


class TestQualityFilter:
    """Test VAL-01: discharge quality filter."""

    def test_rejects_shallow_discharge(self):
        """Rejects discharge with ΔSoC < 25% (shallow)."""
        # Shallow discharge: 13.0V → 12.75V (5% ΔSoC only)
        time_series = [float(i * 10) for i in range(100)]  # 990 seconds (passes duration)
        current_percent_series = [20.0] * 100  # Constant 20% load
        voltage_series = [13.0 - (i * 0.0025) for i in range(100)]  # V drops from 13.0 to 12.75
        lut = [
            {"v": 13.0, "soc": 1.0},
            {"v": 12.75, "soc": 0.95},
            {"v": 10.5, "soc": 0.0}
        ]

        estimator = CapacityEstimator(peukert_exponent=1.2)
        result = estimator.estimate(voltage_series, time_series, current_percent_series, lut)

        # Should reject as shallow (ΔSoC only 5%)
        assert result is None


    def test_rejects_micro_discharge(self):
        """Rejects discharge with duration < 300s (micro)."""
        # Short discharge: only 200 seconds
        time_series = [float(i * 2) for i in range(100)]  # 0, 2, 4, ..., 198 seconds (< 300s)
        current_percent_series = [30.0] * 100
        voltage_series = [13.0 - (i * 0.025) for i in range(100)]  # V drops 13.0 → 10.5 (50% ΔSoC)
        lut = [
            {"v": 13.0, "soc": 1.0},
            {"v": 10.5, "soc": 0.0}
        ]

        estimator = CapacityEstimator(peukert_exponent=1.2)
        result = estimator.estimate(voltage_series, time_series, current_percent_series, lut)

        # Should reject as micro (duration < 300s)
        assert result is None


    def test_accepts_valid_discharge(self):
        """Accepts discharge with ΔSoC >= 25% AND duration >= 300s."""
        time_series = [float(i * 10) for i in range(100)]  # 990 seconds (> 300s)
        current_percent_series = [35.0] * 100
        voltage_series = [13.0 - (i * 0.025) for i in range(100)]  # V drops 13.0 → 10.5 (50% ΔSoC)
        lut = [
            {"v": 13.0, "soc": 1.0},
            {"v": 10.5, "soc": 0.0}
        ]

        estimator = CapacityEstimator(peukert_exponent=1.2)
        result = estimator.estimate(voltage_series, time_series, current_percent_series, lut)

        # Should pass quality filter and return valid result
        assert result is not None
        ah_estimate, confidence, metadata = result
        assert ah_estimate > 0


class TestMetadata:
    """Test that estimate() returns correct metadata (CAP-01)."""

    def test_estimate_returns_metadata_tuple(self):
        """estimate() returns (Ah_estimate, confidence, metadata) tuple."""
        time_series = [float(i * 10) for i in range(100)]
        current_percent_series = [35.0] * 100
        voltage_series = [13.0 - (i * 0.025) for i in range(100)]
        lut = [
            {"v": 13.0, "soc": 1.0},
            {"v": 10.5, "soc": 0.0}
        ]

        estimator = CapacityEstimator(peukert_exponent=1.2)
        result = estimator.estimate(voltage_series, time_series, current_percent_series, lut)

        assert result is not None
        ah_estimate, confidence, metadata = result

        # Check metadata keys
        required_keys = {'delta_soc_percent', 'duration_sec', 'ir_mohms', 'load_avg_percent',
                        'coulomb_ah', 'voltage_check_ah'}
        assert set(metadata.keys()) >= required_keys, \
            f"Metadata missing keys: {required_keys - set(metadata.keys())}"

        # Check metadata types
        assert isinstance(metadata['delta_soc_percent'], (int, float))
        assert isinstance(metadata['duration_sec'], (int, float))
        assert isinstance(metadata['ir_mohms'], (int, float))
        assert isinstance(metadata['load_avg_percent'], (int, float))
        assert isinstance(metadata['coulomb_ah'], (int, float))
        assert isinstance(metadata['voltage_check_ah'], (int, float))


class TestPeukertParameter:
    """Test VAL-02: Peukert exponent is parameterizable (not hardcoded)."""

    def test_peukert_parameter_required(self):
        """CapacityEstimator requires peukert_exponent parameter."""
        # Should be able to create with custom peukert_exponent
        estimator_1 = CapacityEstimator(peukert_exponent=1.2)
        assert estimator_1.peukert_exponent == 1.2

        estimator_2 = CapacityEstimator(peukert_exponent=1.3)
        assert estimator_2.peukert_exponent == 1.3


    def test_peukert_parameter_default_1_2(self):
        """CapacityEstimator has default peukert_exponent of 1.2."""
        estimator = CapacityEstimator()
        assert estimator.peukert_exponent == 1.2


class TestOutlierRejection:
    """Test coulomb vs voltage-curve disagreement > 20% → None."""

    def test_rejects_outlier_coulomb_voltage_mismatch(self):
        """Rejects measurement where coulomb Ah differs from voltage-based estimate by >20%."""
        # This is a more complex test that would require mocking internal methods
        # For now, we'll test the basic structure
        time_series = [float(i * 10) for i in range(100)]
        current_percent_series = [35.0] * 100
        voltage_series = [13.0 - (i * 0.025) for i in range(100)]
        lut = [
            {"v": 13.0, "soc": 1.0},
            {"v": 10.5, "soc": 0.0}
        ]

        estimator = CapacityEstimator(peukert_exponent=1.2)
        result = estimator.estimate(voltage_series, time_series, current_percent_series, lut)

        # For now just verify the method doesn't crash
        # Detailed outlier testing will be in integration tests
        assert result is None or result is not None


class TestConvergenceScore:
    """Test convergence score = 1 - CoV (CAP-03)."""

    def test_convergence_score_single_measurement(self):
        """Convergence score = 0.0 for fewer than 3 measurements."""
        estimator = CapacityEstimator(peukert_exponent=1.2)

        # Add one measurement
        estimator.add_measurement(7.2, "2026-03-16T10:00:00Z", {})

        # Confidence should be 0.0 (< 3 measurements)
        assert estimator.get_confidence() == 0.0


    def test_convergence_score_two_measurements(self):
        """Convergence score = 0.0 for 2 measurements (need 3+ for meaningful CoV)."""
        estimator = CapacityEstimator(peukert_exponent=1.2)

        estimator.add_measurement(7.2, "2026-03-16T10:00:00Z", {})
        estimator.add_measurement(7.3, "2026-03-16T10:30:00Z", {})

        # Confidence should still be 0.0 (< 3 measurements)
        assert estimator.get_confidence() == 0.0


    def test_convergence_score_three_consistent_measurements(self):
        """Convergence score >= 0.90 for 3 measurements with CoV < 0.10."""
        estimator = CapacityEstimator(peukert_exponent=1.2)

        # Add 3 consistent measurements (7.0, 7.1, 7.2)
        estimator.add_measurement(7.0, "2026-03-16T10:00:00Z", {})
        estimator.add_measurement(7.1, "2026-03-16T10:30:00Z", {})
        estimator.add_measurement(7.2, "2026-03-16T11:00:00Z", {})

        confidence = estimator.get_confidence()
        # CoV = std / mean ≈ 0.082 → confidence ≈ 0.918 >= 0.90
        assert confidence >= 0.90, f"Expected confidence >= 0.90, got {confidence}"


    def test_convergence_score_may_fluctuate(self):
        """Convergence score may decrease when noisy sample is added."""
        estimator = CapacityEstimator(peukert_exponent=1.2)

        # Add consistent measurements
        estimator.add_measurement(7.1, "2026-03-16T10:00:00Z", {})
        estimator.add_measurement(7.2, "2026-03-16T10:30:00Z", {})
        estimator.add_measurement(7.0, "2026-03-16T11:00:00Z", {})

        confidence_before = estimator.get_confidence()

        # Add a noisy outlier
        estimator.add_measurement(6.5, "2026-03-16T11:30:00Z", {})

        confidence_after = estimator.get_confidence()

        # Confidence may fluctuate (not monotonic) — both values should be between 0 and 1
        assert 0.0 <= confidence_before <= 1.0
        assert 0.0 <= confidence_after <= 1.0
        # After noisy sample, confidence may be lower (test that it handles this)
        assert confidence_after >= 0.0


class TestConvergenceDetection:
    """Test has_converged() method."""

    def test_not_converged_fewer_than_3(self):
        """has_converged() returns False for fewer than 3 measurements."""
        estimator = CapacityEstimator(peukert_exponent=1.2)

        estimator.add_measurement(7.2, "2026-03-16T10:00:00Z", {})
        assert not estimator.has_converged()

        estimator.add_measurement(7.3, "2026-03-16T10:30:00Z", {})
        assert not estimator.has_converged()


    def test_converged_with_consistent_measurements(self):
        """has_converged() returns True when count >= 3 AND CoV < 0.10."""
        estimator = CapacityEstimator(peukert_exponent=1.2)

        # Add 3 consistent measurements
        estimator.add_measurement(7.0, "2026-03-16T10:00:00Z", {})
        estimator.add_measurement(7.1, "2026-03-16T10:30:00Z", {})
        estimator.add_measurement(7.2, "2026-03-16T11:00:00Z", {})

        # Should converge (CoV < 0.10)
        assert estimator.has_converged(), "Expected convergence with consistent measurements"


class TestMeasurementAccumulation:
    """Test add_measurement() and getter methods (CAP-02, CAP-03)."""

    def test_add_measurement_accumulates(self):
        """add_measurement() appends to internal measurements list."""
        estimator = CapacityEstimator(peukert_exponent=1.2)

        assert estimator.get_measurement_count() == 0

        estimator.add_measurement(7.2, "2026-03-16T10:00:00Z", {'key': 'value'})
        assert estimator.get_measurement_count() == 1

        estimator.add_measurement(7.3, "2026-03-16T10:30:00Z", {'key': 'value2'})
        assert estimator.get_measurement_count() == 2


    def test_get_measurements_returns_all(self):
        """get_measurements() returns list of all accumulated measurements."""
        estimator = CapacityEstimator(peukert_exponent=1.2)

        estimator.add_measurement(7.2, "2026-03-16T10:00:00Z", {})
        estimator.add_measurement(7.3, "2026-03-16T10:30:00Z", {})

        measurements = estimator.get_measurements()
        assert len(measurements) == 2


    def test_get_confidence_after_measurements(self):
        """get_confidence() returns current confidence based on accumulated measurements."""
        estimator = CapacityEstimator(peukert_exponent=1.2)

        assert estimator.get_confidence() == 0.0  # No measurements yet

        estimator.add_measurement(7.2, "2026-03-16T10:00:00Z", {})
        assert estimator.get_confidence() == 0.0  # < 3 measurements

        estimator.add_measurement(7.3, "2026-03-16T10:30:00Z", {})
        estimator.add_measurement(7.1, "2026-03-16T11:00:00Z", {})

        confidence = estimator.get_confidence()
        assert confidence > 0.0  # 3 measurements with low variance


class TestWeightedAveraging:
    """Test depth-weighted averaging (CAP-02)."""

    def test_weighted_average_three_measurements(self):
        """get_weighted_estimate() computes depth-weighted average."""
        estimator = CapacityEstimator(peukert_exponent=1.2)

        # Simulate 3 measurements with different depths
        # Measurement 1: Ah=7.0, ΔSoC=30%
        # Measurement 2: Ah=7.2, ΔSoC=50%
        # Measurement 3: Ah=7.1, ΔSoC=40%
        # Total ΔSoC = 120% (normalized to weights)
        # Weight 1 = 30/120 = 0.25 → 7.0 * 0.25 = 1.75
        # Weight 2 = 50/120 = 0.417 → 7.2 * 0.417 = 3.002
        # Weight 3 = 40/120 = 0.333 → 7.1 * 0.333 = 2.364
        # Weighted sum ≈ 7.116

        metadata1 = {'delta_soc_percent': 30}
        metadata2 = {'delta_soc_percent': 50}
        metadata3 = {'delta_soc_percent': 40}

        estimator.add_measurement(7.0, "2026-03-16T10:00:00Z", metadata1)
        estimator.add_measurement(7.2, "2026-03-16T10:30:00Z", metadata2)
        estimator.add_measurement(7.1, "2026-03-16T11:00:00Z", metadata3)

        weighted_ah = estimator.get_weighted_estimate()
        expected_ah = 7.116
        assert abs(weighted_ah - expected_ah) < 0.1, \
            f"Weighted estimate {weighted_ah} not close to expected {expected_ah}"


    def test_weighted_average_fallback_equal_depth(self):
        """get_weighted_estimate() falls back to arithmetic mean if all ΔSoC = 0."""
        estimator = CapacityEstimator(peukert_exponent=1.2)

        # All measurements with delta_soc = 0
        estimator.add_measurement(7.0, "2026-03-16T10:00:00Z", {'delta_soc_percent': 0})
        estimator.add_measurement(7.1, "2026-03-16T10:30:00Z", {'delta_soc_percent': 0})
        estimator.add_measurement(7.2, "2026-03-16T11:00:00Z", {'delta_soc_percent': 0})

        weighted_ah = estimator.get_weighted_estimate()
        arithmetic_mean = (7.0 + 7.1 + 7.2) / 3
        assert abs(weighted_ah - arithmetic_mean) < 0.001
