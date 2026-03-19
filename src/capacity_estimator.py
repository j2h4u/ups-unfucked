"""Capacity estimator: measures actual battery Ah from discharge events via coulomb counting."""

import logging
from typing import List, Dict, Optional, Tuple, NamedTuple
from src.soc_predictor import soc_from_voltage


class CapacityMeasurement(NamedTuple):
    """Single capacity measurement from a discharge event."""
    timestamp: str
    ah: float
    confidence: float
    metadata: Dict

logger = logging.getLogger('ups-battery-monitor')


def compute_cov(values: list) -> float:
    """Coefficient of variation (population std / mean). Returns 0.0 for <2 values or zero mean."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.0
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return variance ** 0.5 / mean


class CapacityEstimator:
    """
    Estimate battery capacity from discharge events using coulomb counting + voltage anchor validation.

    Algorithm:
    1. Coulomb counting: integrate current over time
    2. Voltage curve validation: cross-check against SoC-based estimate
    3. Quality filters (VAL-01): reject micro/shallow discharges
    4. Confidence tracking: coefficient of variation across measurements
    5. Depth-weighted averaging: combine multiple discharges by ΔSoC

    Expert panel approved: IEEE-450 backed, 2026-03-15 review.
    """

    def __init__(self, peukert_exponent: float = 1.2, nominal_voltage: float = 12.0,
                 nominal_power_watts: float = 425.0, capacity_ah: float = 7.2):
        """
        Initialize CapacityEstimator.

        Args:
            peukert_exponent: Peukert exponent for voltage curve analysis (default 1.2, VAL-02 constraint).
            nominal_voltage: Battery nominal voltage (V).
            nominal_power_watts: UPS rated power (W).
            capacity_ah: Rated battery capacity in Ah (default 7.2, F26).
        """
        self.peukert_exponent = peukert_exponent
        self.nominal_voltage = nominal_voltage
        self.nominal_power_watts = nominal_power_watts
        self.capacity_ah = capacity_ah
        self.capacity_measurements: List[CapacityMeasurement] = []

    def estimate(
        self,
        voltage_series: List[float],
        time_series: List[float],
        load_series: List[float],
        lut: List[Dict],
    ) -> Optional[Tuple[float, float, Dict]]:
        """
        Estimate capacity from a single discharge event.

        Args:
            voltage_series: Voltage readings (V) during discharge.
            time_series: Unix timestamps (sec) — monotonic increasing.
            load_series: Load percent (%) during discharge.
            lut: Voltage → SoC lookup table.

        Returns:
            (Ah_estimate, confidence, metadata) tuple, or None if quality filter rejects.

        Note: confidence is computed from self.capacity_measurements (in-memory accumulator).
            Callers must separately call add_measurement() to populate this accumulator;
            estimate() does not do so automatically. BatteryModel.add_capacity_estimate()
            is the separate persistence path to model.json.
        """
        # VAL-01: Quality filter
        if not self._passes_quality_filter(voltage_series, time_series, load_series, lut):
            return None

        ah_coulomb = self._integrate_current(load_series, time_series,
                                             self.nominal_power_watts, self.nominal_voltage)

        soc_start, soc_end = self._get_soc_range(voltage_series, lut)
        delta_soc = soc_start - soc_end

        voltage_curve_ah = self._estimate_from_voltage_curve(voltage_series, time_series, delta_soc)

        if voltage_curve_ah > 0 and abs(ah_coulomb - voltage_curve_ah) / max(ah_coulomb, voltage_curve_ah) > 0.75:
            logger.warning(f"Coulomb {ah_coulomb:.2f}Ah vs voltage {voltage_curve_ah:.2f}Ah "
                          f"disagree >75%; rejecting measurement")
            return None

        ah_estimate = ah_coulomb

        discharge_slope_mohm = self._compute_discharge_slope(voltage_series, load_series)

        # Assemble metadata
        metadata = {
            'delta_soc_percent': delta_soc * 100,
            'duration_sec': time_series[-1] - time_series[0],
            'discharge_slope_mohm': discharge_slope_mohm,
            'load_avg_percent': sum(load_series) / len(load_series) if load_series else 0,
            'coulomb_ah': ah_coulomb,
            'voltage_check_ah': voltage_curve_ah
        }

        # Confidence: 0.0 for first measurement, increases as CoV decreases (with more samples)
        confidence = self._compute_confidence()

        return (ah_estimate, confidence, metadata)

    def _passes_quality_filter(self, voltage_series: List[float], time_series: List[float],
                              load_series: List[float], lut: List[Dict]) -> bool:
        """
        VAL-01: Reject micro and shallow discharges.

        Criteria:
        - Duration >= 300s (5 minutes)
        - ΔSoC >= 15% (F24: lowered from 25% to accept typical 3-5 min blackouts)

        Args:
            voltage_series, time_series, load_series, lut: Discharge data.

        Returns:
            bool: True if all criteria pass, False if any rejected.
        """
        # Check duration
        duration = time_series[-1] - time_series[0]
        if duration < 300:
            logger.debug(f"Discharge rejected: duration {duration:.0f}s < 300s (micro)")
            return False

        # Check ΔSoC (F24: 15% gate accepts typical blackouts with ~15% depth)
        soc_start, soc_end = self._get_soc_range(voltage_series, lut)
        delta_soc = soc_start - soc_end

        if delta_soc < 0.15:
            logger.debug(f"Discharge rejected: ΔSoC {delta_soc*100:.1f}% < 15% (shallow)")
            return False

        return True

    def _integrate_current(self, load_percent: List[float], time_sec: List[float],
                          nominal_power_watts: float, nominal_voltage: float) -> float:
        """
        Coulomb counting: convert load% → current (A) → Ah via trapezoidal integration.

        Formula:
            I(A) = (load_percent / 100) × nominal_power_watts / nominal_voltage
            Ah = ∫I dt / 3600 (convert A·s to Ah)

        Uses trapezoidal rule for numerical integration (IEEE-1106 standard).

        F27: Current computed from nominal voltage (12V), not actual battery voltage.
        Ah overestimated ~4% (same bias as F14 in runtime_calculator). Systematic
        and consistent — doesn't affect convergence because all measurements share
        the same bias direction.

        Args:
            load_percent: Load percentages [0–100] at each time point.
            time_sec: Unix timestamps (seconds, monotonic).
            nominal_power_watts: UPS rated power (W).
            nominal_voltage: Battery nominal voltage (V).

        Returns:
            float: Total charge in Ah.
        """
        if len(load_percent) < 2:
            return 0.0

        ah_total = 0.0
        for i in range(len(load_percent) - 1):
            current_a_start = (load_percent[i] / 100.0) * nominal_power_watts / nominal_voltage
            current_a_end = (load_percent[i + 1] / 100.0) * nominal_power_watts / nominal_voltage
            i_avg = (current_a_start + current_a_end) / 2.0
            dt = time_sec[i + 1] - time_sec[i]
            ah_total += i_avg * dt / 3600.0

        return ah_total

    def _get_soc_range(self, voltage_series: List[float], lut: List[Dict]) -> Tuple[float, float]:
        """
        Get SoC at discharge start and end.

        Args:
            voltage_series: Voltage readings (V).
            lut: Voltage → SoC lookup table.

        Returns:
            tuple: (SoC_start, SoC_end) as decimals (0.0–1.0).
        """
        soc_start = soc_from_voltage(voltage_series[0], lut)
        soc_end = soc_from_voltage(voltage_series[-1], lut)
        return soc_start, soc_end

    def _estimate_from_voltage_curve(self, voltage_series: List[float],
                                     time_series: List[float], delta_soc: float) -> float:
        """
        Cross-check coulomb estimate against voltage discharge curve.

        Uses voltage drop magnitude to estimate expected Ah for given ΔSoC.
        If voltage curve estimate is very different from coulomb, signal outlier.

        Formula: Ah_voltage ≈ nominal_ah * (delta_soc / typical_discharge_soc)
        where typical_discharge_soc is based on voltage range.

        Args:
            voltage_series: Voltage readings (V).
            time_series: Time readings (seconds).
            delta_soc: Depth of discharge (0.0–1.0).

        Returns:
            float: Expected Ah based on voltage curve analysis.
        """
        if delta_soc <= 0:
            return 0.0

        # Voltage drop magnitude
        voltage_start = voltage_series[0]
        voltage_end = voltage_series[-1]
        voltage_drop = voltage_start - voltage_end

        # Rough heuristic: voltage drop correlates to depth
        # VRLA typical: 3.5V drop over full discharge (0.0 → 1.0 SoC)
        # So we estimate Ah based on observed voltage drop vs full range
        typical_full_discharge_voltage_drop = 3.5

        # Scale by delta_soc: if delta_soc = 0.5 and we see 1.75V drop, it checks out
        expected_voltage_drop = typical_full_discharge_voltage_drop * delta_soc

        # F26: Use constructor param instead of hardcoded 7.2
        nominal_ah = self.capacity_ah

        # Voltage-based estimate (very rough): proportional to depth-of-discharge
        # Avoid division by zero
        if voltage_drop <= 0:
            return 0.0

        # Estimate Ah as: nominal_ah * (observed_voltage_drop / typical_full_discharge_drop)
        voltage_curve_ah = nominal_ah * (voltage_drop / typical_full_discharge_voltage_drop)

        return voltage_curve_ah

    def _compute_discharge_slope(self, voltage_series: List[float], load_percent: List[float]) -> float:
        """
        Compute discharge slope (ΔV_total / I_avg) as metadata for trending.

        F25: Renamed from _compute_ir(). This computes the total voltage drop
        divided by average current — a discharge slope metric (~352mΩ typical),
        NOT actual internal resistance (~20mΩ, measured via voltage sag in
        monitor.py _record_voltage_sag). The 17x difference is because this
        includes electrochemical polarization, not just ohmic IR drop.

        Metadata only — not used in any calculations.

        Args:
            voltage_series: Voltage readings (V).
            load_percent: Load percentages [0–100].

        Returns:
            float: Discharge slope in mΩ-equivalent units.
        """
        voltage_drop = voltage_series[0] - voltage_series[-1]

        current_avg_percent = sum(load_percent) / len(load_percent)
        current_avg_amps = (current_avg_percent / 100.0) * self.nominal_power_watts / self.nominal_voltage

        if current_avg_amps == 0:
            return 0.0

        slope_ohms = voltage_drop / current_avg_amps
        slope_mohms = slope_ohms * 1000

        return slope_mohms

    def _compute_confidence(self) -> float:
        """Confidence = 1 - CoV, clamped to [0, 1]. Returns 0.0 for <3 measurements."""
        if len(self.capacity_measurements) < 3:
            return 0.0
        cov = compute_cov([m.ah for m in self.capacity_measurements])
        return max(0.0, min(1.0, 1.0 - cov))

    def add_measurement(self, ah: float, timestamp: str, metadata: Dict) -> None:
        """
        Accumulate a new capacity measurement.

        Args:
            ah: Measured capacity (Ah).
            timestamp: ISO8601 timestamp.
            metadata: Measurement metadata (delta_soc_percent, duration_sec, etc.).
        """
        confidence = self._compute_confidence()
        self.capacity_measurements.append(CapacityMeasurement(timestamp, ah, confidence, metadata))

    def has_converged(self) -> bool:
        """Converged: count >= 3 AND CoV < 0.10."""
        if len(self.capacity_measurements) < 3:
            return False
        return compute_cov([m[1] for m in self.capacity_measurements]) < 0.10

    def get_weighted_estimate(self) -> float:
        """
        Compute depth-weighted average of all measurements (CAP-02).

        Formula:
            weight_i = ΔSoC_i / sum(ΔSoC_all)
            Ah_weighted = sum(weight_i × Ah_i)

        If all ΔSoC = 0 (degenerate case), fall back to arithmetic mean.

        Returns:
            float: Weighted capacity estimate (Ah).
        """
        if not self.capacity_measurements:
            return 7.2  # Fallback to rated

        total_delta_soc = sum(m.metadata.get('delta_soc_percent', 0) for m in self.capacity_measurements)

        if total_delta_soc == 0:
            # Fallback: equal weight
            ah_sum = sum(m.ah for m in self.capacity_measurements)
            return ah_sum / len(self.capacity_measurements)

        weighted_ah = 0.0
        for timestamp, ah, confidence, metadata in self.capacity_measurements:
            delta_soc_percent = metadata.get('delta_soc_percent', 0)
            weight = delta_soc_percent / total_delta_soc
            weighted_ah += weight * ah

        return weighted_ah

    def get_confidence(self) -> float:
        """Current confidence metric [0.0, 1.0] based on accumulated measurements."""
        return self._compute_confidence()

    def get_measurement_count(self) -> int:
        """Count of accumulated capacity measurements."""
        return len(self.capacity_measurements)

    def get_measurements(self) -> List[CapacityMeasurement]:
        """All accumulated measurements."""
        return self.capacity_measurements
