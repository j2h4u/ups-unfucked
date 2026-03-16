"""Unit tests for soh_calculator.py orchestrator — capacity selection logic."""

import pytest
from unittest.mock import Mock, patch
from src.soh_calculator import calculate_soh_from_discharge


class TestSoHWithMeasuredCapacity:
    """SOH-01: When Phase 12 capacity converges, use measured capacity."""

    def test_soh_with_measured_capacity(self):
        """When battery_model.get_convergence_status() returns converged=True,
        use latest_ah (measured capacity) instead of rated."""

        # Arrange
        mock_battery_model = Mock()
        mock_battery_model.get_convergence_status.return_value = {
            'converged': True,
            'latest_ah': 6.8,  # Measured capacity
            'sample_count': 3,
        }
        mock_battery_model.get_capacity_ah.return_value = 7.2  # Rated (should not be used)

        mock_voltage_series = [12.0, 11.8, 11.5, 10.8, 10.5]
        mock_time_series = [0, 100, 200, 300, 400]

        with patch('src.soh_calculator.battery_math_soh.calculate_soh_from_discharge') as mock_kernel:
            mock_kernel.return_value = 0.92

            result = calculate_soh_from_discharge(
                discharge_voltage_series=mock_voltage_series,
                discharge_time_series=mock_time_series,
                reference_soh=0.95,
                anchor_voltage=10.5,
                battery_model=mock_battery_model,
                load_percent=15.0,
                nominal_power_watts=850,
                nominal_voltage=120,
                peukert_exponent=1.2,
            )

        # Assert
        assert result is not None
        soh_new, capacity_used = result
        assert soh_new == 0.92
        assert capacity_used == 6.8  # Measured, not rated
        # Verify kernel was called with capacity_ah=6.8
        mock_kernel.assert_called_once()
        call_kwargs = mock_kernel.call_args[1]
        assert call_kwargs['capacity_ah'] == 6.8

    def test_soh_with_rated_capacity_fallback(self):
        """When battery_model.get_convergence_status() returns converged=False,
        use rated capacity (7.2Ah) instead of waiting for measured."""

        # Arrange
        mock_battery_model = Mock()
        mock_battery_model.get_convergence_status.return_value = {
            'converged': False,
            'sample_count': 1,
        }
        mock_battery_model.get_capacity_ah.return_value = 7.2  # Rated (should be used)

        mock_voltage_series = [12.0, 11.8, 11.5, 10.8, 10.5]
        mock_time_series = [0, 100, 200, 300, 400]

        with patch('src.soh_calculator.battery_math_soh.calculate_soh_from_discharge') as mock_kernel:
            mock_kernel.return_value = 0.95

            result = calculate_soh_from_discharge(
                discharge_voltage_series=mock_voltage_series,
                discharge_time_series=mock_time_series,
                reference_soh=0.95,
                anchor_voltage=10.5,
                battery_model=mock_battery_model,
                load_percent=15.0,
                nominal_power_watts=850,
                nominal_voltage=120,
                peukert_exponent=1.2,
            )

        # Assert
        assert result is not None
        soh_new, capacity_used = result
        assert soh_new == 0.95
        assert capacity_used == 7.2  # Rated, not measured
        # Verify kernel was called with capacity_ah=7.2
        mock_kernel.assert_called_once()
        call_kwargs = mock_kernel.call_args[1]
        assert call_kwargs['capacity_ah'] == 7.2
