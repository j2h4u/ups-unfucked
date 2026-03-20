"""Discharge lifecycle handler — SoH, capacity, Peukert calibration, alerts.

Contains the discharge event processing pipeline that runs on OB→OL transition.

Methods here run on OB→OL transition (discharge complete) and during
capacity estimation. Errors propagate to MonitorDaemon.run() except
sulfation scoring, which catches ValueError/TypeError to allow the
rest of the discharge pipeline to complete.
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
from src.battery_math.regression import linear_regression_slope
from src.battery_math.sulfation import compute_sulfation_score
from src.battery_math.cycle_roi import compute_cycle_roi
from src import soh_calculator, replacement_predictor, alerter
from src.monitor_config import DischargeBuffer, safe_save, MIN_DISCHARGE_DURATION_SEC

logger = logging.getLogger('ups-battery-monitor')

BLACKOUT_CREDIT_DAYS = 7


def _parse_iso_utc(s: str) -> datetime:
    """Parse ISO8601 timestamp, normalizing 'Z' suffix to '+00:00' for fromisoformat."""
    return datetime.fromisoformat(s.replace('Z', '+00:00'))
RATED_CYCLE_LIFE = 300  # CyberPower UT850EG datasheet: 300 cycles @ 100% DoD, 25°C


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
        """Initialize discharge handler.

        Args:
            battery_model: Persistent battery model for LUT, SoH history, and physics.
            config: monitor_config.Config — polling interval, runtime threshold, etc.
            capacity_estimator: CapacityEstimator instance for Ah measurement.
            rls_peukert: ScalarRLS estimator for online Peukert exponent calibration.
            reference_load_percent: Fallback load % when discharge buffer is empty.
            soh_threshold: Decimal fraction [0.0, 1.0] below which SoH alerts fire.
        """
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
        """Process completed discharge: SoH, Peukert, replacement prediction, alerts, sulfation.

        Returns early (skipping all steps) if discharge is too short (<300s)
        or has insufficient samples (<2 voltages). Only discharges that pass
        _compute_soh validation trigger the full pipeline.

        Discharge trigger classification uses a 60-second window: if the buffer
        starts within 60s of the last upscmd timestamp, the discharge is classified
        as 'test_initiated' rather than 'natural' (see _classify_discharge_trigger).
        """
        soh_before = self.battery_model.get_soh()
        soh_result = self._compute_soh(discharge_buffer)
        if soh_result is None:
            return

        soh_after, capacity_ah_ref = soh_result

        avg_load = self._avg_load(discharge_buffer)
        replacement_prediction = self._predict_replacement(soh_after, capacity_ah_ref)
        self._check_alerts(soh_after, replacement_prediction, discharge_buffer, avg_load)
        self._auto_calibrate_peukert(soh_after, discharge_buffer)

        discharge_trigger = self._classify_discharge_trigger(discharge_buffer)
        soh_delta = soh_after - soh_before
        self._score_and_persist_sulfation(soh_after, soh_delta, discharge_buffer, discharge_trigger, capacity_ah_ref)

        self._log_discharge_prediction(discharge_buffer)

        safe_save(self.battery_model)

    def _compute_soh(self, discharge_buffer: DischargeBuffer):
        """Calculate SoH from discharge data, persist history entry.

        Returns (soh_new, capacity_ah_ref) tuple, or None if discharge
        is too short, has insufficient samples, or SoH calculation fails.
        """
        if len(discharge_buffer.voltages) < 2:
            return None  # No discharge detected; skip SoH update

        # Skip SoH/Peukert update for micro-discharges (<5 min).
        # Short discharges have terrible signal-to-noise: 105s discharge
        # caused SoH to drop from 99.7% to 88.6% (incident 2026-03-16).
        # Cycle count and on-battery time are still tracked (in _track_discharge).
        discharge_duration = discharge_buffer.times[-1] - discharge_buffer.times[0]
        if discharge_duration < MIN_DISCHARGE_DURATION_SEC:
            logger.info(
                f"Discharge too short for model update ({discharge_duration:.0f}s < {MIN_DISCHARGE_DURATION_SEC}s); "
                f"skipping SoH/Peukert calibration",
                extra={'event_type': 'micro_discharge_skip', 'duration_sec': int(discharge_duration)}
            )
            return None

        avg_load = self._avg_load(discharge_buffer)

        soh_result = soh_calculator.calculate_soh_from_discharge(
            voltage_series=discharge_buffer.voltages,
            time_series=discharge_buffer.times,
            reference_soh=self.battery_model.get_soh(),
            battery_model=self.battery_model,
            load_percent=avg_load,
            nominal_power_watts=self.battery_model.get_nominal_power_watts(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
        )

        if soh_result is None:
            logger.info("SoH update returned None; skipping history entry")
            return None

        soh_after, capacity_ah_ref = soh_result

        today = datetime.now().strftime('%Y-%m-%d')
        self.battery_model.add_soh_history_entry(today, soh_after, capacity_ah_ref=capacity_ah_ref)

        logger.info(f"SoH calculated: {soh_after:.2%}", extra={
            'event_type': 'soh_calculation', 'soh': f'{soh_after:.4f}',
        })

        return (soh_after, capacity_ah_ref)

    def _predict_replacement(self, soh_new: float, capacity_ah_ref: float):
        """Check convergence and run linear regression for replacement prediction.

        Returns the regression result tuple (slope, intercept, r2, replacement_date)
        or None. Persists replacement_due date in the model.
        """
        convergence = self.battery_model.get_convergence_status()
        if convergence.get('converged', False):
            replacement_prediction = replacement_predictor.linear_regression_soh(
                soh_history=self.battery_model.get_soh_history(),
                threshold_soh=self.soh_threshold,
                capacity_ah_ref=capacity_ah_ref,
            )
        else:
            replacement_prediction = None

        if replacement_prediction:
            _, _, _, replacement_date = replacement_prediction
            self.battery_model.set_replacement_due(replacement_date)
        else:
            self.battery_model.set_replacement_due(None)

        return replacement_prediction

    def _check_alerts(self, soh_new: float, replacement_prediction, discharge_buffer: DischargeBuffer, avg_load: float) -> None:
        """SoH threshold check + runtime threshold check."""
        if soh_new < self.soh_threshold:
            days_to_replacement = None
            if replacement_prediction:
                *_, replacement_date = replacement_prediction
                if replacement_date and replacement_date != 'overdue':
                    try:
                        repl_dt = datetime.strptime(replacement_date, '%Y-%m-%d')
                        days_to_replacement = (repl_dt - datetime.now()).days
                    except ValueError as e:
                        logger.debug(f"Invalid replacement date format: {e}")

            alerter.alert_soh_below_threshold(
                soh_new,
                self.soh_threshold,
                days_to_replacement
            )

        runtime_at_full_charge_min = runtime_minutes(
            soc=1.0, load_percent=avg_load,
            capacity_ah=self.battery_model.get_capacity_ah(),
            soh=soh_new,
            peukert_exponent=self.battery_model.get_peukert_exponent(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            nominal_power_watts=self.battery_model.get_nominal_power_watts()
        )
        if runtime_at_full_charge_min < self.config.runtime_threshold_minutes:
            alerter.alert_runtime_below_threshold(
                runtime_at_full_charge_min,
                self.config.runtime_threshold_minutes
            )

    def _avg_load(self, discharge_buffer: DischargeBuffer) -> float:
        """Average load from buffer, falling back to reference_load_percent if empty."""
        if discharge_buffer.loads:
            return sum(discharge_buffer.loads) / len(discharge_buffer.loads)
        return self.reference_load_percent

    def _score_and_persist_sulfation(
        self,
        soh_new: float,
        soh_delta: float,
        discharge_buffer: DischargeBuffer,
        discharge_trigger: str,
        capacity_ah_ref: Optional[float] = None,
    ) -> None:
        """Compute sulfation score, persist sulfation/discharge history, grant blackout credit.

        Updates in-memory last_* state, appends sulfation_history and discharge_event
        entries, logs the discharge complete event, and grants blackout credit for
        natural deep discharges (>=90% DoD).
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        days_since_deep = self._calculate_days_since_deep()
        ir_trend_rate = self._estimate_ir_trend()
        depth_of_discharge = self._estimate_dod_from_buffer(discharge_buffer)
        cycle_budget = self._estimate_cycle_budget()

        try:
            sulfation_state = compute_sulfation_score(
                days_since_deep=days_since_deep if days_since_deep is not None else 0.0,
                ir_trend_rate=ir_trend_rate,
                recovery_delta=soh_delta,
                temperature_celsius=35.0,
            )

            roi = compute_cycle_roi(
                depth_of_discharge=depth_of_discharge,
                cycle_budget_remaining=cycle_budget,
                ir_trend_rate=ir_trend_rate,
                sulfation_score=sulfation_state.score,
            )
        except (ValueError, TypeError) as e:
            logger.warning(f"Sulfation scoring failed: {e}", exc_info=True,
                          extra={'event_type': 'sulfation_scoring_failed',
                                 'error_class': type(e).__name__})
            sulfation_state = None
            roi = None

        self.last_sulfation_score = sulfation_state.score if sulfation_state else None
        self.last_sulfation_confidence = self._assess_sulfation_confidence(days_since_deep, ir_trend_rate) if sulfation_state else None
        self.last_days_since_deep = days_since_deep
        self.last_ir_trend_rate = ir_trend_rate
        self.last_recovery_delta = soh_delta
        self.last_cycle_roi = roi
        self.last_cycle_budget_remaining = cycle_budget
        self.last_discharge_timestamp = now_iso

        # Pre-compute rounded values shared across persistence and logging
        sulfation_score_r = round(sulfation_state.score, 3) if sulfation_state else None
        days_since_deep_r = round(days_since_deep, 1) if days_since_deep is not None else None
        ir_trend_r = round(ir_trend_rate, 6)
        recovery_delta_r = round(soh_delta, 3)
        discharge_duration = discharge_buffer.times[-1] - discharge_buffer.times[0]
        dod_r = round(depth_of_discharge, 2)
        roi_r = round(roi, 3) if roi is not None else None

        self.battery_model.append_sulfation_history({
            'timestamp': now_iso,
            'event_type': discharge_trigger,
            'sulfation_score': sulfation_score_r,
            'days_since_deep': days_since_deep_r,
            'ir_trend_rate': ir_trend_r,
            'recovery_delta': recovery_delta_r,
            'temperature_celsius': 35.0,
            'temperature_source': 'assumed_constant',
            'confidence_level': self.last_sulfation_confidence or 'low'
        })

        self.battery_model.append_discharge_event({
            'timestamp': now_iso,
            'event_reason': discharge_trigger,
            'duration_seconds': discharge_duration,
            'depth_of_discharge': dod_r,
            'measured_capacity_ah': capacity_ah_ref,
            'cycle_roi': roi_r
        })

        logger.info('Discharge complete', extra={
            'event_type': 'discharge_complete',
            'discharge_trigger': discharge_trigger,
            'duration_seconds': int(discharge_duration),
            'depth_of_discharge': dod_r,
            'sulfation_score': sulfation_score_r,
            'sulfation_confidence': self.last_sulfation_confidence,
            'recovery_delta': recovery_delta_r,
            'cycle_roi': roi_r,
            'measured_capacity_ah': round(capacity_ah_ref, 2) if capacity_ah_ref is not None else None,
            'temperature_celsius': 35.0,
            'temperature_source': 'assumed_constant',
            'timestamp': now_iso,
        })

        self._grant_blackout_credit(discharge_trigger, depth_of_discharge)

    def _grant_blackout_credit(self, discharge_trigger: str, depth_of_discharge: float) -> None:
        """Grant blackout credit for natural deep discharges (>=90% DoD)."""
        if discharge_trigger != 'natural' or depth_of_discharge < 0.90:
            return

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

        avg_load = self._avg_load(discharge_buffer)
        if avg_load is None or avg_load <= 0 or avg_load > 100:
            logger.debug(f"Peukert calibration skipped: invalid load ({avg_load})")
            return

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
            # Skip RLS update if result hit clamp bounds — carries no information
            if new_exponent <= 1.0 or new_exponent >= 1.4:
                logger.debug(
                    f"Peukert calibration hit clamp bound ({new_exponent:.3f}); "
                    f"skipping RLS update"
                )
                return

            old_exponent = self.battery_model.get_peukert_exponent()
            smoothed, new_P = self.rls_peukert.update(new_exponent)
            smoothed = max(1.0, min(1.4, smoothed))
            self.battery_model.set_peukert_exponent(smoothed)
            self.battery_model.set_rls_state(
                'peukert', smoothed, new_P, self.rls_peukert.sample_count)
            logger.info(
                f"Peukert calibrated: {old_exponent:.3f} \u2192 {smoothed:.3f} "
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
            logger.warning("Peukert calibration returned None (unexpected \u2014 math undefined?)",
                          extra={'event_type': 'peukert_calibration_failed'})

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
        avg_load = self._avg_load(discharge_buffer)

        logger.info(
            f"Discharge prediction: predicted={self.discharge_predicted_runtime:.1f}min, "
            f"actual={actual_minutes:.1f}min, load_avg={avg_load:.1f}%",
            extra={
                'event_type': 'discharge_prediction',
                'predicted_minutes': f'{self.discharge_predicted_runtime:.1f}',
                'actual_minutes': f'{actual_minutes:.1f}',
                'avg_load_percent': f'{avg_load:.1f}',
                'start_soc': f'{current_soc:.3f}' if current_soc is not None else 'N/A',
            })

        self.discharge_predicted_runtime = None

    def handle_discharge_complete(self, discharge_data: dict) -> None:
        """Handle discharge completion: measure capacity via CapacityEstimator.

        Called from _update_battery_health after a discharge event is fully
        processed. Extracts discharge data, calls CapacityEstimator.estimate(),
        stores result in model.json, and checks for capacity convergence /
        new-battery detection.
        Args:
            discharge_data: Dict with keys:
                - voltage_series: List[float] voltage readings (V)
                - time_series: List[float] unix timestamps (sec)
                - load_series: List[float] load percent (%)
                - timestamp: str ISO8601 timestamp
        """
        voltage_series = discharge_data.get('voltage_series', [])
        time_series = discharge_data.get('time_series', [])
        load_series = discharge_data.get('load_series', [])
        timestamp = discharge_data.get('timestamp', datetime.now().isoformat())

        if len(voltage_series) < 2 or len(time_series) < 2 or len(load_series) < 2:
            logger.debug(f"Discharge data incomplete for capacity estimation: "
                        f"{len(voltage_series)} V, {len(time_series)} t, {len(load_series)} I")
            return

        capacity_estimate = self.capacity_estimator.estimate(
            voltage_series=voltage_series,
            time_series=time_series,
            load_series=load_series,
            lut=self.battery_model.data.get('lut', [])
        )

        if capacity_estimate is None:
            logger.debug("Discharge rejected by CapacityEstimator quality filter")
            return

        ah_estimate, confidence, metadata = capacity_estimate

        self.battery_model.add_capacity_estimate(
            ah_estimate=ah_estimate,
            confidence=confidence,
            metadata=metadata,
            timestamp=timestamp
        )
        safe_save(self.battery_model)

        convergence_status = self.battery_model.get_convergence_status()
        sample_count = convergence_status['sample_count']
        cov = convergence_status.get('cov', 0.0)
        mean_ah = convergence_status.get('mean_ah', 0.0)
        std_ah = cov * mean_ah

        confidence_pct = int(confidence * 100) if confidence else 0

        delta_soc_percent = metadata.get('delta_soc_percent', 0.0)
        duration_sec = metadata.get('duration_sec', 0)
        load_avg_percent = metadata.get('load_avg_percent', 0.0)

        logger.info(
            f"capacity_measurement: {ah_estimate:.2f}Ah (\u00b1{std_ah:.2f}), CoV={cov:.3f} "
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

        if self.capacity_estimator.has_converged():
            self._handle_capacity_convergence(convergence_status)

    def _handle_capacity_convergence(self, convergence_status: dict) -> None:
        """Check convergence state: lock baseline, detect new battery, persist."""
        self.battery_model.data['capacity_converged'] = True

        if not self.has_logged_baseline_lock:
            logger.info(
                f"baseline_lock: capacity converged at {convergence_status['latest_ah']:.2f}Ah after {convergence_status['sample_count']} deep discharges",
                extra={
                    'event_type': 'baseline_lock',
                    'capacity_ah': f'{convergence_status["latest_ah"]:.2f}',
                    'sample_count': str(convergence_status['sample_count']),
                }
            )
            self.has_logged_baseline_lock = True

        current_measured = convergence_status.get('latest_ah')
        stored_baseline = self.battery_model.data.get('capacity_ah_measured', None)

        if stored_baseline is not None:
            delta_ah = abs(current_measured - stored_baseline)
            delta_percent = (delta_ah / stored_baseline) * 100

            if delta_percent > 10.0:
                logger.warning(
                    f"New battery detection: measured capacity {current_measured:.2f}Ah "
                    f"differs from baseline {stored_baseline:.2f}Ah ({delta_percent:.1f}% > 10% threshold)",
                    extra={
                        'event_type': 'new_battery_detected',
                        'current_ah': f'{current_measured:.2f}',
                        'baseline_ah': f'{stored_baseline:.2f}',
                        'delta_percent': f'{delta_percent:.1f}',
                    }
                )

                self.battery_model.data['new_battery_detected'] = True
                self.battery_model.data['new_battery_detected_timestamp'] = datetime.now().isoformat()

                logger.info(
                    "New battery flag set; MOTD will show alert next shell session. "
                    "User can confirm with: ups-battery-monitor --new-battery"
                )
        else:
            self.battery_model.data['capacity_ah_measured'] = current_measured
            logger.info(f"Capacity baseline stored: {current_measured:.2f}Ah (first convergence)")

        safe_save(self.battery_model)

    def _calculate_days_since_deep(self) -> Optional[float]:
        """Calculate days since last deep discharge (>70% DoD).

        Returns None if no deep discharge in history.
        """
        discharge_events = self.battery_model.data.get('discharge_events', [])
        now = datetime.now(timezone.utc)

        for event in reversed(discharge_events):
            if event.get('depth_of_discharge', 0) <= 0.7:
                continue
            timestamp_str = event.get('timestamp')
            if not timestamp_str:
                continue
            try:
                event_time = _parse_iso_utc(timestamp_str)
                return (now - event_time).total_seconds() / 86400.0
            except (ValueError, TypeError) as e:
                logger.debug(f"Skipping malformed discharge event: {e}")
                continue

        return None

    def _parse_r_entry(self, entry: dict, now: datetime) -> tuple | None:
        """Parse r_internal_history entry into (days_ago, r_ohm), or None if invalid/old."""
        r_value = entry.get('r_ohm')
        if r_value is None or not isinstance(r_value, (int, float)):
            logger.warning("r_internal_history entry invalid 'r_ohm': %r (keys: %s)",
                          r_value, list(entry.keys()),
                          extra={'event_type': 'r_internal_invalid_entry'})
            return None
        date_str = entry.get('date', '')
        try:
            entry_time = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except (ValueError, TypeError) as e:
            logger.warning("Skipping r_internal entry with bad date: %r — %s",
                           date_str, e,
                           extra={'event_type': 'r_internal_invalid_entry'})
            return None
        days_ago = (now - entry_time).total_seconds() / 86400.0
        if days_ago > 30:
            return None
        return (days_ago, r_value)

    def _estimate_ir_trend(self) -> float:
        """Estimate IR trend rate (dR/dt) in ohms/day from last 30 days.

        Returns 0.0 if insufficient data (<2 recent entries).
        """
        r_history = self.battery_model.data.get('r_internal_history', [])
        if len(r_history) < 2:
            return 0.0

        now = datetime.now(timezone.utc)
        points = [p for entry in r_history if (p := self._parse_r_entry(entry, now)) is not None]

        if len(points) < 2:
            return 0.0
        x_values = [p[0] for p in points]
        y_values = [p[1] for p in points]
        slope = linear_regression_slope(x_values, y_values)
        return max(0.0, slope) if slope is not None else 0.0

    def _classify_discharge_trigger(self, discharge_buffer: Optional[DischargeBuffer] = None) -> str:
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
            seconds_since_upscmd = (discharge_start_dt - upscmd_dt).total_seconds()

            if 0 <= seconds_since_upscmd <= 60:
                logger.info(
                    f"Discharge classified as test-initiated: {seconds_since_upscmd:.1f}s after upscmd",
                    extra={'event_type': 'discharge_classification', 'reason': 'test_initiated'}
                )
                return 'test_initiated'
            else:
                return 'natural'
        except (ValueError, TypeError):
            buf_start = discharge_buffer.times[0] if discharge_buffer.times else None
            logger.warning(
                f"Invalid timestamps in discharge classification: upscmd={last_upscmd}, buf_start={buf_start}",
                exc_info=True,
                extra={'event_type': 'discharge_classification_error'}
            )
            return 'natural'

    def _estimate_dod_from_buffer(self, discharge_buffer: DischargeBuffer) -> float:
        """Estimate depth of discharge from voltage swing (heuristic, not true DoD).

        Uses (Vmax - Vmin) / (Vnominal - Vfloor) as a rough proxy.
        Not based on SoC lookup — requires battery model LUT for true DoD.

        Args:
            discharge_buffer: DischargeBuffer with voltages array

        Returns: float [0, 1] representing fraction of battery discharged
        """
        if not hasattr(discharge_buffer, 'voltages') or len(discharge_buffer.voltages) < 2:
            return 0.0

        # Voltage-swing heuristic: (Vmax - Vmin) / (Vnominal - Vfloor). Approximation only —
        # true DoD requires SoC lookup via LUT, which needs the battery model instance.
        v_min = min(discharge_buffer.voltages)
        v_max = max(discharge_buffer.voltages)

        # CyberPower UT850: nominal voltage 12V, min ~10.5V (fully discharged)
        v_nominal = 12.0
        v_floor = 10.5

        result = (v_max - v_min) / (v_nominal - v_floor) if (v_nominal - v_floor) > 0 else 0.0
        return min(1.0, max(0.0, result))

    def _estimate_cycle_budget(self) -> int:
        """Estimate remaining cycle budget: RATED_CYCLE_LIFE * current SoH."""
        soh = self.battery_model.data.get('soh', 1.0)
        return int(RATED_CYCLE_LIFE * soh)

    def _assess_sulfation_confidence(self, days_since_deep: Optional[float], ir_trend_rate: float) -> str:
        """Assess sulfation signal quality based on input data availability.

        Returns: 'high', 'medium', or 'low'
        """
        r_history = self.battery_model.data.get('r_internal_history', [])
        if days_since_deep is not None and len(r_history) >= 3:
            return 'high'
        elif days_since_deep is not None or len(r_history) >= 2:
            return 'medium'
        return 'low'
