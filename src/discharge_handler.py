"""Discharge lifecycle handler — SoH, capacity, Peukert calibration, alerts.

Extracted from MonitorDaemon (F58) to contain the discharge event processing
pipeline that accretes 10-20 lines per feature.

Methods here run on OB→OL transition (discharge complete) and during
capacity estimation. No exception handling — all errors propagate to
MonitorDaemon.run() per SRE requirement.
"""

import math
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.model import BatteryModel
from src.capacity_estimator import CapacityEstimator
from src.runtime_calculator import runtime_minutes
from src.soc_predictor import soc_from_voltage
from src.battery_math import calibrate_peukert, ScalarRLS
from src.battery_math.sulfation import compute_sulfation_score
from src.battery_math.cycle_roi import compute_cycle_roi
from src import soh_calculator, replacement_predictor, alerter
from src.monitor_config import DischargeBuffer, safe_save

logger = logging.getLogger('ups-battery-monitor')


class DischargeHandler:
    """Processes completed discharge events: SoH update, capacity estimation,
    Peukert calibration, replacement prediction, and alerts.

    Stateless regarding per-event data — discharge_buffer and current metrics
    are passed as method parameters. Owns rls_peukert estimator state and
    capacity estimation tracking flags.
    """

    def __init__(
        self,
        battery_model: BatteryModel,
        config,  # monitor_config.Config (avoid circular import)
        capacity_estimator: CapacityEstimator,
        rls_peukert: ScalarRLS,
        reference_load_percent: float,
        soh_threshold: float,
    ):
        self.battery_model = battery_model
        self.config = config
        self.capacity_estimator = capacity_estimator
        self.rls_peukert = rls_peukert
        self.reference_load_percent = reference_load_percent
        self.soh_threshold = soh_threshold

        # Per-discharge state
        self.discharge_predicted_runtime: Optional[float] = None

        self.has_logged_baseline_lock = False

        self.last_sulfation_score: Optional[float] = None
        self.last_sulfation_confidence: Optional[str] = None
        self.last_days_since_deep: Optional[float] = None
        self.last_ir_trend_rate: float = 0.0
        self.last_recovery_delta: float = 0.0
        self.last_cycle_roi: float = 0.0
        self.last_cycle_budget_remaining: int = 0
        self.last_discharge_timestamp: Optional[str] = None

    def update_battery_health(self, discharge_buffer: DischargeBuffer) -> None:
        """Process completed discharge event: SoH, Peukert, replacement prediction, alerts.

        Called when discharge event completes (OB→OL transition or cooldown expiry).
        Caller is responsible for clearing discharge_buffer after this returns.

        Args:
            discharge_buffer: Completed discharge data (voltages, times, loads).
        """
        if len(discharge_buffer.voltages) < 2:
            return  # No discharge detected; skip SoH update

        # Skip SoH/Peukert update for micro-discharges (<5 min).
        # Short discharges have terrible signal-to-noise: 105s discharge
        # caused SoH to drop from 99.7% to 88.6% (incident 2026-03-16).
        # Cycle count and on-battery time are still tracked (in _track_discharge).
        discharge_duration = discharge_buffer.times[-1] - discharge_buffer.times[0]
        if discharge_duration < 300:
            logger.info(f"Discharge too short for model update ({discharge_duration:.0f}s < 300s); "
                        f"skipping SoH/Peukert calibration")
            return

        avg_load = (sum(discharge_buffer.loads) / len(discharge_buffer.loads)
                   if discharge_buffer.loads else self.reference_load_percent)

        soh_result = soh_calculator.calculate_soh_from_discharge(
            discharge_voltage_series=discharge_buffer.voltages,
            discharge_time_series=discharge_buffer.times,
            reference_soh=self.battery_model.get_soh(),
            battery_model=self.battery_model,
            load_percent=avg_load,
            nominal_power_watts=self.battery_model.get_nominal_power_watts(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            peukert_exponent=self.battery_model.get_peukert_exponent()
        )

        if soh_result is None:
            logger.info("SoH update returned None; skipping history entry")
            return

        soh_new, capacity_ah_used = soh_result

        today = datetime.now().strftime('%Y-%m-%d')
        self.battery_model.add_soh_history_entry(today, soh_new, capacity_ah_ref=capacity_ah_used)

        logger.info(f"SoH calculated: {soh_new:.2%}")

        convergence = self.battery_model.get_convergence_status()
        if convergence.get('converged', False):
            soh_regression = replacement_predictor.linear_regression_soh(
                soh_history=self.battery_model.get_soh_history(),
                threshold_soh=self.soh_threshold,
                capacity_ah_ref=capacity_ah_used,
            )
        else:
            soh_regression = None

        if soh_regression:
            _, _, _, replacement_date = soh_regression
            self.battery_model.set_replacement_due(replacement_date)
        else:
            self.battery_model.set_replacement_due(None)

        if soh_new < self.soh_threshold:
            days_to_replacement = None
            if soh_regression:
                slope, intercept, r2, replacement_date = soh_regression
                if replacement_date and replacement_date != 'overdue':
                    try:
                        repl_dt = datetime.strptime(replacement_date, '%Y-%m-%d')
                        days_to_replacement = (repl_dt - datetime.now()).days
                    except ValueError:
                        pass

            alerter.alert_soh_below_threshold(
                logger,
                soh_new,
                self.soh_threshold,
                days_to_replacement
            )

        time_rem_at_100pct = runtime_minutes(
            soc=1.0, load_percent=avg_load,
            capacity_ah=self.battery_model.get_capacity_ah(),
            soh=soh_new,
            peukert_exponent=self.battery_model.get_peukert_exponent(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            nominal_power_watts=self.battery_model.get_nominal_power_watts()
        )
        if time_rem_at_100pct < self.config.runtime_threshold_minutes:
            alerter.alert_runtime_below_threshold(
                logger,
                time_rem_at_100pct,
                self.config.runtime_threshold_minutes
            )

        # Auto-calibrate Peukert exponent from measured discharge
        self._auto_calibrate_peukert(soh_new, discharge_buffer)

        days_since_deep = self._calculate_days_since_deep()
        ir_trend_rate = self._estimate_ir_trend()
        depth_of_discharge = self._estimate_dod_from_buffer(discharge_buffer)
        cycle_budget = self._estimate_cycle_budget()

        try:
            sulfation_state = compute_sulfation_score(
                days_since_deep=days_since_deep if days_since_deep is not None else 0.0,
                ir_trend_rate=ir_trend_rate,
                recovery_delta=soh_new - self.battery_model.get_soh() if soh_result else 0.0,
                temperature_celsius=35.0,
            )

            roi = compute_cycle_roi(
                days_since_deep=days_since_deep if days_since_deep is not None else 0.0,
                depth_of_discharge=depth_of_discharge,
                cycle_budget_remaining=cycle_budget,
                ir_trend_rate=ir_trend_rate,
                sulfation_score=sulfation_state.score,
            )
        except (ValueError, TypeError) as e:
            logger.warning(f"Sulfation scoring failed: {e}", exc_info=True)
            sulfation_state = None
            roi = 0.0

        self.last_sulfation_score = sulfation_state.score if sulfation_state else None
        self.last_sulfation_confidence = 'high' if sulfation_state else None
        self.last_days_since_deep = days_since_deep
        self.last_ir_trend_rate = ir_trend_rate
        self.last_recovery_delta = soh_new - self.battery_model.get_soh() if soh_result else 0.0
        self.last_cycle_roi = roi
        self.last_cycle_budget_remaining = cycle_budget
        self.last_discharge_timestamp = datetime.now(timezone.utc).isoformat()

        # Classify once (deterministic for a given buffer)
        event_reason = self._classify_discharge_trigger(discharge_buffer)

        # Persist to model.json
        self.battery_model.append_sulfation_history({
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'event_type': event_reason,
            'sulfation_score': round(sulfation_state.score, 3) if sulfation_state else None,
            'days_since_deep': round(days_since_deep, 1) if days_since_deep is not None else None,
            'ir_trend_rate': round(ir_trend_rate, 6),
            'recovery_delta': round(self.last_recovery_delta, 3),
            'temperature_celsius': 35.0,
            'confidence_level': 'high'
        })

        discharge_duration = discharge_buffer.times[-1] - discharge_buffer.times[0]
        self.battery_model.append_discharge_event({
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'event_reason': event_reason,
            'duration_seconds': discharge_duration,
            'depth_of_discharge': round(depth_of_discharge, 2),
            'measured_capacity_ah': capacity_ah_used if soh_result else None,
            'cycle_roi': round(roi, 3)
        })

        logger.info('Discharge complete', extra={
            'event_type': 'discharge_complete',
            'event_reason': event_reason,
            'duration_seconds': int(discharge_duration),
            'depth_of_discharge': round(depth_of_discharge, 2),
            'sulfation_score': round(sulfation_state.score, 3) if sulfation_state else None,
            'sulfation_confidence': 'high' if sulfation_state else None,
            'recovery_delta': round(self.last_recovery_delta, 3),
            'cycle_roi': round(roi, 3),
            'measured_capacity_ah': round(capacity_ah_used, 2) if capacity_ah_used else None,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })

        # Grant blackout credit for natural deep discharges (≥90% DoD)
        BLACKOUT_CREDIT_DAYS = 7
        if event_reason == 'natural' and depth_of_discharge >= 0.90:
            credit_expires = datetime.now(timezone.utc) + timedelta(days=BLACKOUT_CREDIT_DAYS)
            logger.info(
                f"Natural blackout desulfation credit: DoD={depth_of_discharge:.0%}",
                extra={
                    'event_type': 'blackout_credit_granted',
                    'dod': round(depth_of_discharge, 2),
                    'credit_expires': credit_expires.isoformat(),
                }
            )

            self.battery_model.set_blackout_credit({
                'active': True,
                'credited_event_timestamp': datetime.now(timezone.utc).isoformat(),
                'credit_expires': credit_expires.isoformat(),
                'desulfation_credit': 0.15,  # Approximate desulfation benefit for ~90% DoD
            })

        self._log_discharge_prediction(discharge_buffer)

        # Single save at end after all mutations
        safe_save(self.battery_model)

    def _auto_calibrate_peukert(self, current_soh: float, discharge_buffer: DischargeBuffer) -> None:
        """Auto-calibrate Peukert exponent from actual discharge duration.

        Guard clauses (sample count, duration, load validity) stay here in orchestrator.
        Pure math is delegated to kernel function.
        """
        times = discharge_buffer.times
        if len(times) < 2:
            logger.debug("Peukert calibration skipped: <2 discharge samples")
            return

        actual_duration_sec = times[-1] - times[0]
        if actual_duration_sec < 60:
            logger.debug(f"Peukert calibration skipped: discharge too short ({actual_duration_sec:.0f}s < 60s)")
            return

        avg_load = (sum(discharge_buffer.loads) / len(discharge_buffer.loads)
                   if discharge_buffer.loads else self.reference_load_percent)
        if avg_load is None or avg_load <= 0 or avg_load > 100:
            logger.debug(f"Peukert calibration skipped: invalid load ({avg_load})")
            return

        # Data validated; call pure kernel function
        # Use RATED capacity (self.config.capacity_ah), not measured (VAL-02)
        new_exponent = calibrate_peukert(
            actual_duration_sec=actual_duration_sec,
            avg_load_percent=avg_load,
            current_soh=current_soh,
            capacity_ah=self.config.capacity_ah,
            current_exponent=self.battery_model.get_peukert_exponent(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            nominal_power_watts=self.battery_model.get_nominal_power_watts()
        )

        # Handle kernel result: RLS smoothing instead of direct set
        if new_exponent is not None:
            # F30: Skip RLS update if result hit clamp bounds — carries no information
            if new_exponent <= 1.0 or new_exponent >= 1.4:
                logger.debug(
                    f"Peukert calibration hit clamp bound ({new_exponent:.3f}); "
                    f"skipping RLS update"
                )
                return

            old_exponent = self.battery_model.get_peukert_exponent()
            smoothed, new_P = self.rls_peukert.update(new_exponent)
            smoothed = max(1.0, min(1.4, smoothed))  # physical bounds
            self.battery_model.set_peukert_exponent(smoothed)
            self.battery_model.set_rls_state(
                'peukert', smoothed, new_P, self.rls_peukert.sample_count)
            logger.info(
                f"Peukert calibrated: {old_exponent:.3f} → {smoothed:.3f} "
                f"(single-point={new_exponent:.3f}), "
                f"confidence={self.rls_peukert.confidence:.0%}",
                extra={
                    'event_type': 'peukert_calibration',
                    'peukert_old': f'{old_exponent:.3f}',
                    'peukert_new': f'{smoothed:.3f}',
                    'peukert_raw': f'{new_exponent:.3f}',
                    'rls_p': f'{new_P:.4f}',
                    'rls_confidence': f'{self.rls_peukert.confidence:.3f}',
                    'sample_count': str(self.rls_peukert.sample_count),
                })
        else:
            logger.error("Peukert calibration returned None (unexpected — math undefined?)")

    def _log_discharge_prediction(self, discharge_buffer: DischargeBuffer, current_soc: float = 0.0) -> None:
        """Log prediction vs actual runtime for model accuracy tracking.

        Gate: predicted runtime must exist AND discharge >= 300s.
        Logs raw data only (predicted, actual, load, start SoC) — no error % in daemon.
        """
        if self.discharge_predicted_runtime is None:
            return

        times = discharge_buffer.times
        if len(times) < 2:
            self.discharge_predicted_runtime = None
            return

        discharge_duration_sec = times[-1] - times[0]
        if discharge_duration_sec < 300:
            self.discharge_predicted_runtime = None
            return

        actual_minutes = discharge_duration_sec / 60.0
        avg_load = (sum(discharge_buffer.loads) / len(discharge_buffer.loads)
                    if discharge_buffer.loads else 0.0)

        logger.info(
            f"Discharge prediction: predicted={self.discharge_predicted_runtime:.1f}min, "
            f"actual={actual_minutes:.1f}min, load_avg={avg_load:.1f}%",
            extra={
                'event_type': 'discharge_prediction',
                'predicted_minutes': f'{self.discharge_predicted_runtime:.1f}',
                'actual_minutes': f'{actual_minutes:.1f}',
                'avg_load_percent': f'{avg_load:.1f}',
                'start_soc': f'{current_soc:.3f}',
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })

        self.discharge_predicted_runtime = None

    def handle_discharge_complete(self, discharge_data: dict) -> None:
        """Handle discharge completion: measure capacity via CapacityEstimator.

        Called when OB→OL transition detected. Extracts discharge data,
        calls CapacityEstimator.estimate(), and stores result in model.json.
        Implements CAP-01 and CAP-05.

        Args:
            discharge_data: Dict with keys:
                - voltage_series: List[float] voltage readings (V)
                - time_series: List[float] unix timestamps (sec)
                - load_series: List[float] load percent (%)
                - timestamp: str ISO8601 timestamp

        NOTE: Measured capacity lives only in capacity_estimates[] array.
        Replacement of rated→measured happens on convergence.
        """
        voltage_series = discharge_data.get('voltage_series', [])
        time_series = discharge_data.get('time_series', [])
        load_series = discharge_data.get('load_series', discharge_data.get('current_series', []))
        timestamp = discharge_data.get('timestamp', datetime.now().isoformat())

        # Guard: need at least 2 samples
        if len(voltage_series) < 2 or len(time_series) < 2 or len(load_series) < 2:
            logger.debug(f"Discharge data incomplete for capacity estimation: "
                        f"{len(voltage_series)} V, {len(time_series)} t, {len(load_series)} I")
            return

        # Call CapacityEstimator
        result = self.capacity_estimator.estimate(
            voltage_series=voltage_series,
            time_series=time_series,
            load_series=load_series,
            lut=self.battery_model.data.get('lut', [])
        )

        # Quality filter rejection (VAL-01: micro/shallow discharges rejected)
        if result is None:
            logger.debug("Discharge rejected by CapacityEstimator quality filter")
            return

        # Success: unpack estimate
        ah_estimate, confidence, metadata = result

        # Store in model
        self.battery_model.add_capacity_estimate(
            ah_estimate=ah_estimate,
            confidence=confidence,
            metadata=metadata,
            timestamp=timestamp
        )

        # Structured journald logging
        convergence_status = self.battery_model.get_convergence_status()
        sample_count = convergence_status['sample_count']

        # Compute CoV for human-readable message
        estimates = self.battery_model.data.get('capacity_estimates', [])
        ah_values = [e['ah_estimate'] for e in estimates]
        if len(ah_values) >= 2:
            mean_ah = sum(ah_values) / len(ah_values)
            std_ah = (sum((x - mean_ah) ** 2 for x in ah_values) / len(ah_values)) ** 0.5
            cov = std_ah / mean_ah if mean_ah > 0 else 0.0
        else:
            std_ah = 0.0
            cov = 0.0

        confidence_pct = int(confidence * 100) if confidence else 0

        # Extract metadata fields with safe defaults
        delta_soc_percent = metadata.get('delta_soc_percent', 0.0)
        duration_sec = metadata.get('duration_sec', 0)
        load_avg_percent = metadata.get('load_avg_percent', 0.0)

        logger.info(
            f"capacity_measurement: {ah_estimate:.2f}Ah (±{std_ah:.2f}), CoV={cov:.3f} "
            f"({sample_count} samples, {confidence_pct}% confidence)",
            extra={
                'event_type': 'capacity_measurement',
                'capacity_ah': f'{ah_estimate:.2f}',
                'confidence_percent': str(confidence_pct),
                'sample_count': str(sample_count),
                'delta_soc_percent': f'{delta_soc_percent:.1f}',
                'duration_sec': str(int(duration_sec)),
                'load_avg_percent': f'{load_avg_percent:.1f}',
            }
        )

        # Log baseline_lock when convergence detected
        if self.capacity_estimator.has_converged():
            self.battery_model.data['capacity_converged'] = True

            # Log baseline_lock event only once per convergence
            if not self.has_logged_baseline_lock:
                logger.info(
                    f"baseline_lock: capacity converged at {convergence_status['latest_ah']:.2f}Ah after {convergence_status['sample_count']} deep discharges",
                    extra={
                        'event_type': 'baseline_lock',
                        'capacity_ah': f'{convergence_status["latest_ah"]:.2f}',
                        'sample_count': str(convergence_status['sample_count']),
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                    }
                )
                self.has_logged_baseline_lock = True

            safe_save(self.battery_model)

        # New battery detection (post-discharge)
        convergence = self.battery_model.get_convergence_status()

        if convergence.get('converged', False):
            current_measured = convergence.get('latest_ah')
            stored_baseline = self.battery_model.data.get('capacity_ah_measured', None)

            if stored_baseline is not None:
                delta_ah = abs(current_measured - stored_baseline)
                delta_percent = (delta_ah / stored_baseline) * 100

                if delta_percent > 10.0:  # >10% threshold
                    logger.warning(
                        f"New battery detection: measured capacity {current_measured:.2f}Ah "
                        f"differs from baseline {stored_baseline:.2f}Ah ({delta_percent:.1f}% > 10% threshold)"
                    )

                    self.battery_model.data['new_battery_detected'] = True
                    self.battery_model.data['new_battery_detected_timestamp'] = datetime.now().isoformat()
                    safe_save(self.battery_model)

                    logger.info(
                        "New battery flag set; MOTD will show alert next shell session. "
                        "User can confirm with: ups-battery-monitor --new-battery"
                    )
            else:
                # First time convergence; store as baseline
                self.battery_model.data['capacity_ah_measured'] = current_measured
                safe_save(self.battery_model)
                logger.info(f"Capacity baseline stored: {current_measured:.2f}Ah (first convergence)")

    def _calculate_days_since_deep(self) -> Optional[float]:
        """Calculate days since last deep discharge (>70% DoD).

        Returns None if no deep discharge in history.
        Returns float days since last deep discharge otherwise.
        """
        discharge_events = self.battery_model.data.get('discharge_events', [])

        # Find most recent deep discharge (DoD > 0.7)
        for event in reversed(discharge_events):
            if event.get('depth_of_discharge', 0) > 0.7:
                # Parse timestamp (ISO8601) and calculate days ago
                try:
                    timestamp_str = event['timestamp']
                    event_time = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    now = datetime.now(timezone.utc)
                    days_ago = (now - event_time).total_seconds() / 86400.0
                    return days_ago
                except (ValueError, KeyError):
                    continue

        return None

    def _estimate_ir_trend(self) -> float:
        """Estimate IR trend rate (dR/dt) in ohms/day.

        Queries r_internal_history (existing v2.0 field), calculates slope.
        Returns 0.0 if insufficient data.
        """
        r_history = self.battery_model.data.get('r_internal_history', [])

        if len(r_history) < 2:
            return 0.0

        # Keep only last 30 days of data
        now = datetime.now(timezone.utc)
        recent_entries = []
        for entry in r_history:
            try:
                entry_time = datetime.fromisoformat(entry.get('date', '').replace('Z', '+00:00'))
                days_ago = (now - entry_time).total_seconds() / 86400.0
                if days_ago <= 30:
                    r_value = entry.get('r_ohm')
                    if r_value is None:
                        logger.warning(f"r_internal_history entry missing 'r_ohm' key: {list(entry.keys())}")
                        continue
                    recent_entries.append({
                        'days_ago': days_ago,
                        'r_ohm': r_value,
                    })
            except (ValueError, KeyError):
                continue

        if len(recent_entries) < 2:
            return 0.0

        n = len(recent_entries)
        sum_x = sum(e['days_ago'] for e in recent_entries)
        sum_y = sum(e['r_ohm'] for e in recent_entries)
        sum_xy = sum(e['days_ago'] * e['r_ohm'] for e in recent_entries)
        sum_x2 = sum(e['days_ago'] ** 2 for e in recent_entries)

        denominator = n * sum_x2 - sum_x ** 2
        if abs(denominator) < 1e-10:
            return 0.0

        slope = (n * sum_xy - sum_x * sum_y) / denominator
        return max(0.0, slope)  # Clip to non-negative

    def _classify_discharge_trigger(self, discharge_buffer: Optional[object] = None) -> str:
        """Classify discharge as natural or test-initiated.

        Compare discharge_buffer start time to last upscmd timestamp.
        If discharge started within 60 seconds of upscmd, it's test-initiated.
        Otherwise, natural.

        Returns: 'natural' | 'test_initiated'
        """
        last_upscmd = self.battery_model.get_last_upscmd_timestamp()
        if not last_upscmd or not discharge_buffer:
            return 'natural'

        # Use buffer start time (Unix float) instead of wall clock
        if not hasattr(discharge_buffer, 'times') or not discharge_buffer.times:
            return 'natural'

        discharge_start_dt = datetime.fromtimestamp(discharge_buffer.times[0], tz=timezone.utc)

        try:
            upscmd_dt = datetime.fromisoformat(last_upscmd)
            time_delta = (discharge_start_dt - upscmd_dt).total_seconds()

            if 0 <= time_delta <= 60:
                logger.info(
                    f"Discharge classified as test-initiated: {time_delta:.1f}s after upscmd",
                    extra={'event_type': 'discharge_classification', 'reason': 'test_initiated'}
                )
                return 'test_initiated'
            else:
                return 'natural'
        except (ValueError, TypeError):
            logger.warning(f"Invalid timestamps in discharge classification: upscmd={last_upscmd}")
            return 'natural'

    def _estimate_dod_from_buffer(self, discharge_buffer: object) -> float:
        """Estimate depth of discharge from voltage samples.

        Args:
            discharge_buffer: DischargeBuffer with voltages array

        Returns: float [0, 1] representing fraction of battery discharged
        """
        if not hasattr(discharge_buffer, 'voltages') or len(discharge_buffer.voltages) < 2:
            return 0.0

        # TODO: integrate with LUT-based SoC predictor for higher accuracy
        v_min = min(discharge_buffer.voltages)
        v_max = max(discharge_buffer.voltages)

        # CyberPower UT850: nominal voltage 12V, min ~10.5V (fully discharged)
        v_nominal = 12.0
        v_floor = 10.5

        result = (v_max - v_min) / (v_nominal - v_floor) if (v_nominal - v_floor) > 0 else 0.0
        return min(1.0, max(0.0, result))

    def _estimate_cycle_budget(self) -> int:
        """Estimate remaining cycle budget based on SoH.

        CyberPower UT850 rated at 300 cycles (per datasheet).
        Estimate remaining = 300 * SoH
        """
        soh = self.battery_model.data.get('soh', 1.0)
        rated_cycles = 300
        return int(rated_cycles * soh)
