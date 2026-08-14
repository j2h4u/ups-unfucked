"""Discharge lifecycle handler — SoH, capacity, Peukert calibration, alerts.

Contains the discharge event processing pipeline that runs on OB→OL transition.

Methods here run on OB→OL transition (discharge complete) and during
capacity estimation. Errors propagate to MonitorDaemon.run().
"""

import copy
import logging
import math
from datetime import datetime, timezone
from typing import Optional

from src import alerter, replacement_predictor, soh_calculator
from src.battery_math import ScalarRLS, calibrate_peukert
from src.battery_math.regression import linear_regression_slope
from src.capacity_estimator import CapacityEstimator
from src.discharge_types import CompletedDischarge, ModelApplicationResult
from src.model import BatteryModel
from src.monitor_config import DischargeBuffer
from src.runtime_calculator import runtime_minutes
from src.soc_predictor import soc_from_voltage

logger = logging.getLogger("ups-battery-monitor")


def _log_post_commit_failure(
    message: str,
    exc: Exception,
    event_id: str | None,
    event_type: str = "post_commit_side_effect_failed",
) -> None:
    """Report a non-critical post-commit side-effect failure without masking commit."""
    try:
        logger.error(
            message,
            exc,
            exc_info=True,
            extra={"event_type": event_type, "event_id": event_id},
        )
    except (OSError, RuntimeError, ValueError):
        # Logging must not turn an already durable model commit into a retry.
        pass


RATED_CYCLE_LIFE = 300  # CyberPower UT850EG datasheet: 300 cycles @ 100% DoD, 25°C


def _parse_iso_utc(s: str) -> datetime:
    """Parse ISO8601 timestamp, normalizing 'Z' suffix to '+00:00' for fromisoformat."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


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

        self.last_days_since_deep: Optional[float] = None
        self.last_ir_trend_rate: float = 0.0
        self.last_cycle_budget_remaining: int = 0
        self.last_discharge_timestamp: Optional[str] = None

    def apply_completed_discharge(self, completion: CompletedDischarge) -> ModelApplicationResult:
        """Apply one scientifically eligible event with exactly one model save.

        This is the authoritative Phase 2 boundary.  All calculations run against
        isolated candidate state first.  The live model, estimator, RLS tracker, and
        handler tracking fields are changed only after the candidate model has been
        atomically persisted successfully.
        """
        reasons = self._validate_application_input(completion)
        event_id = completion.event_id
        if event_id is not None and self.battery_model.has_discharge_event(event_id):
            return ModelApplicationResult(event_id, "already_applied", None, ())
        if reasons:
            return ModelApplicationResult(event_id, "skipped", None, tuple(reasons))

        original_state = copy.deepcopy(self.battery_model.state)
        original_physics = copy.deepcopy(self.battery_model.physics)
        original_rls = self.rls_peukert
        original_capacity_estimator = self.capacity_estimator
        original_tracking = (
            self.last_days_since_deep,
            self.last_ir_trend_rate,
            self.last_cycle_budget_remaining,
            self.last_discharge_timestamp,
            self.has_logged_baseline_lock,
        )

        candidate_model = copy.copy(self.battery_model)
        candidate_model.state = copy.deepcopy(original_state)
        candidate_model.physics = copy.deepcopy(original_physics)
        candidate_estimator = copy.deepcopy(self.capacity_estimator)
        candidate_rls = copy.deepcopy(self.rls_peukert)
        event_buffer = DischargeBuffer(
            voltages=list(completion.voltages),
            times=list(completion.times),
            loads=list(completion.loads),
            collecting=False,
        )
        committed = False

        try:
            avg_load = self._avg_load(event_buffer)
            soh_result = soh_calculator.calculate_soh_from_discharge(
                voltage_series=list(completion.voltages),
                time_series=list(completion.times),
                reference_soh=candidate_model.get_soh(),
                battery_model=candidate_model,
                load_percent=avg_load,
                nominal_power_watts=candidate_model.get_nominal_power_watts(),
                nominal_voltage=candidate_model.get_nominal_voltage(),
            )
            if soh_result is None:
                return ModelApplicationResult(
                    event_id,
                    "skipped",
                    None,
                    ("soh_quality_rejected",),
                )
            soh_after, capacity_ah_ref = soh_result

            capacity_result = candidate_estimator.estimate(
                voltage_series=list(completion.voltages),
                time_series=list(completion.times),
                load_series=list(completion.loads),
                lut=candidate_model.state.get("lut", []),
            )
            if capacity_result is None:
                return ModelApplicationResult(
                    event_id,
                    "skipped",
                    None,
                    ("capacity_quality_rejected",),
                )
            ah_estimate, confidence, metadata = capacity_result
            event_timestamp = datetime.now(timezone.utc).isoformat()
            metadata = {
                **metadata,
                "event_id": event_id,
                "evidence_class": completion.evidence_class,
                "lifecycle": completion.lifecycle,
            }

            candidate_model.add_soh_history_entry(
                event_timestamp[:10], soh_after, capacity_ah_ref=capacity_ah_ref
            )
            candidate_model.append_capacity_estimate(
                ah_estimate=ah_estimate,
                confidence=confidence,
                metadata=metadata,
                timestamp=event_timestamp,
            )
            candidate_estimator.add_measurement(ah_estimate, event_timestamp, metadata)

            peukert_result = self._calculate_peukert_candidate(
                current_soh=soh_after,
                times=completion.times,
                loads=completion.loads,
                candidate_rls=candidate_rls,
                current_exponent=self.battery_model.get_peukert_exponent(),
            )
            if peukert_result is not None:
                smoothed, new_p, sample_count = peukert_result
                candidate_model.set_peukert_exponent(smoothed)
                candidate_model.set_rls_state("peukert", smoothed, new_p, sample_count)

            depth_of_discharge = self._estimate_dod_from_buffer(event_buffer)
            candidate_model.append_discharge_event(
                {
                    "event_id": event_id,
                    "timestamp": event_timestamp,
                    "event_reason": self._classify_discharge_trigger(event_buffer),
                    "lifecycle": completion.lifecycle,
                    "evidence_class": completion.evidence_class,
                    "model_processing_eligible": completion.model_processing_eligible,
                    "duration_seconds": completion.times[-1] - completion.times[0],
                    "depth_of_discharge": round(depth_of_discharge, 2),
                    "measured_capacity_ah": capacity_ah_ref,
                    "capacity_estimate_ah": ah_estimate,
                }
            )

            candidate_convergence = candidate_model.get_convergence_status()
            baseline_locked = False
            if candidate_estimator.has_converged() and candidate_convergence.latest_ah is not None:
                stored_baseline = candidate_model.state.get("capacity_ah_measured")
                if stored_baseline is None:
                    candidate_model.state["capacity_ah_measured"] = candidate_convergence.latest_ah
                    baseline_locked = True
                elif (
                    abs(candidate_convergence.latest_ah - stored_baseline) / stored_baseline > 0.10
                ):
                    candidate_model.state["new_battery_detected"] = True
                    candidate_model.state["new_battery_detected_timestamp"] = event_timestamp

            self.battery_model.state = candidate_model.state
            self.battery_model.physics = candidate_model.physics
            try:
                model_hash = self.battery_model.save()
            except Exception:
                self.battery_model.state = original_state
                self.battery_model.physics = original_physics
                self.rls_peukert = original_rls
                self.capacity_estimator = original_capacity_estimator
                (
                    self.last_days_since_deep,
                    self.last_ir_trend_rate,
                    self.last_cycle_budget_remaining,
                    self.last_discharge_timestamp,
                    self.has_logged_baseline_lock,
                ) = original_tracking
                raise
            committed = True

            self.rls_peukert = candidate_rls
            self.capacity_estimator = candidate_estimator
            self.last_days_since_deep = self._calculate_days_since_deep()
            self.last_ir_trend_rate = self._estimate_ir_trend()
            self.last_cycle_budget_remaining = self._estimate_cycle_budget()
            self.last_discharge_timestamp = event_timestamp
            if baseline_locked and not self.has_logged_baseline_lock:
                try:
                    logger.info(
                        "baseline_lock: capacity converged at %.2fAh",
                        candidate_convergence.latest_ah,
                        extra={"event_type": "baseline_lock", "event_id": event_id},
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    _log_post_commit_failure(
                        "Post-commit baseline-lock log failed; model application remains applied: %s",
                        exc,
                        event_id,
                        "baseline_lock_log_failed",
                    )
                self.has_logged_baseline_lock = True

            replacement_prediction = self._predict_replacement(soh_after, capacity_ah_ref)
            try:
                self._check_alerts(soh_after, replacement_prediction, event_buffer, avg_load)
            except (OSError, RuntimeError, ValueError) as exc:
                _log_post_commit_failure(
                    "Post-commit discharge alert failed; model application remains applied: %s",
                    exc,
                    event_id,
                    "post_commit_alert_failed",
                )
            try:
                logger.info(
                    "Discharge complete",
                    extra={
                        "event_type": "discharge_complete",
                        "event_id": event_id,
                        "evidence_class": completion.evidence_class,
                        "lifecycle": completion.lifecycle,
                        "duration_seconds": int(completion.times[-1] - completion.times[0]),
                        "measured_capacity_ah": round(capacity_ah_ref, 2),
                    },
                )
            except (OSError, RuntimeError, ValueError) as exc:
                _log_post_commit_failure(
                    "Post-commit discharge log failed; model application remains applied: %s",
                    exc,
                    event_id,
                    "post_commit_discharge_log_failed",
                )
            return ModelApplicationResult(event_id, "applied", model_hash, ())
        except Exception:
            if not committed:
                self.battery_model.state = original_state
                self.battery_model.physics = original_physics
                self.rls_peukert = original_rls
                self.capacity_estimator = original_capacity_estimator
                (
                    self.last_days_since_deep,
                    self.last_ir_trend_rate,
                    self.last_cycle_budget_remaining,
                    self.last_discharge_timestamp,
                    self.has_logged_baseline_lock,
                ) = original_tracking
            raise

    @staticmethod
    def _validate_application_input(completion: CompletedDischarge) -> list[str]:
        """Return explicit reasons why an event cannot enter authoritative state."""
        reasons = list(completion.eligibility_reasons)
        if not completion.event_id:
            reasons.append("missing_event_id")
        if not completion.model_processing_eligible:
            reasons.append("model_processing_not_eligible")
        if completion.evidence_class != "controlled_capacity_test":
            reasons.append("evidence_class_not_authoritative")
        if len(completion.voltages) < 2:
            reasons.append("insufficient_voltage_samples")
        if len(completion.times) != len(completion.voltages):
            reasons.append("voltage_time_length_mismatch")
        if len(completion.loads) != len(completion.voltages):
            reasons.append("voltage_load_length_mismatch")
        finite_time_series = True
        for field_name, values in (
            ("voltage", completion.voltages),
            ("time", completion.times),
            ("load", completion.loads),
        ):
            field_is_finite = not any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in values
            )
            if not field_is_finite:
                reasons.append(f"non_finite_{field_name}_sample")
            if field_name == "time":
                finite_time_series = field_is_finite
        if finite_time_series and any(
            current <= previous for previous, current in zip(completion.times, completion.times[1:])
        ):
            reasons.append("non_increasing_time_series")
        return list(dict.fromkeys(reasons))

    def _calculate_peukert_candidate(
        self,
        *,
        current_soh: float,
        times: tuple[float, ...],
        loads: tuple[float, ...],
        candidate_rls: ScalarRLS,
        current_exponent: float,
    ) -> tuple[float, float, int] | None:
        """Calculate Peukert/RLS changes without mutating the live RLS object."""
        if len(times) < 2 or len(loads) < 2:
            return None
        duration = times[-1] - times[0]
        avg_load = sum(loads) / len(loads)
        if duration < 60 or avg_load <= 0 or avg_load > 100:
            return None
        new_exponent = calibrate_peukert(
            actual_duration_sec=duration,
            avg_load_percent=avg_load,
            current_soh=current_soh,
            capacity_ah=self.config.capacity_ah,
            current_exponent=current_exponent,
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            nominal_power_watts=self.battery_model.get_nominal_power_watts(),
        )
        if new_exponent is None or new_exponent <= 1.0 or new_exponent >= 1.4:
            return None
        smoothed, new_p = candidate_rls.update(new_exponent)
        return max(1.0, min(1.4, smoothed)), new_p, candidate_rls.sample_count

    def _predict_replacement(self, soh_new: float, capacity_ah_ref: float):
        """Check convergence and run linear regression for replacement prediction.

        Returns the regression result tuple (slope, intercept, r2, replacement_date)
        or None for _check_alerts. replacement_due is computed live at read time
        (model.compute_replacement_due) using the configured soh_threshold and
        latest-entry capacity_ah_ref — not persisted here.
        """
        convergence = self.battery_model.get_convergence_status()
        if convergence.converged:
            replacement_prediction = replacement_predictor.linear_regression_soh(
                soh_history=self.battery_model.get_soh_history(),
                threshold_soh=self.soh_threshold,
                capacity_ah_ref=capacity_ah_ref,
            )
        else:
            replacement_prediction = None

        return replacement_prediction

    def _check_alerts(
        self,
        soh_new: float,
        replacement_prediction,
        discharge_buffer: DischargeBuffer,
        avg_load: float,
    ) -> None:
        """SoH threshold check + runtime threshold check."""
        if soh_new < self.soh_threshold:
            days_to_replacement = None
            if replacement_prediction:
                *_, replacement_date = replacement_prediction
                if replacement_date and replacement_date != "overdue":
                    try:
                        repl_dt = datetime.strptime(replacement_date, "%Y-%m-%d")
                        days_to_replacement = (repl_dt - datetime.now()).days
                    except ValueError as e:
                        logger.debug(f"Invalid replacement date format: {e}")

            alerter.alert_soh_below_threshold(soh_new, self.soh_threshold, days_to_replacement)

        runtime_at_full_charge_min = runtime_minutes(
            soc=1.0,
            load_percent=avg_load,
            capacity_ah=self.battery_model.get_capacity_ah(),
            soh=soh_new,
            peukert_exponent=self.battery_model.get_peukert_exponent(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            nominal_power_watts=self.battery_model.get_nominal_power_watts(),
        )
        if runtime_at_full_charge_min < self.config.runtime_threshold_minutes:
            alerter.alert_runtime_below_threshold(
                runtime_at_full_charge_min, self.config.runtime_threshold_minutes
            )

    def _avg_load(self, discharge_buffer: DischargeBuffer) -> float:
        """Average load from buffer, falling back to reference_load_percent if empty."""
        if discharge_buffer.loads:
            return sum(discharge_buffer.loads) / len(discharge_buffer.loads)
        return self.reference_load_percent

    def _auto_calibrate_peukert(
        self, current_soh: float, discharge_buffer: DischargeBuffer
    ) -> None:
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
            logger.debug(
                f"Peukert calibration skipped: discharge too short ({actual_duration_sec:.0f}s < 60s)"
            )
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
            nominal_power_watts=self.battery_model.get_nominal_power_watts(),
        )

        # Handle kernel result: RLS smoothing instead of direct set
        if new_exponent is not None:
            # Skip RLS update if result hit clamp bounds — carries no information
            if new_exponent <= 1.0 or new_exponent >= 1.4:
                logger.debug(
                    f"Peukert calibration hit clamp bound ({new_exponent:.3f}); skipping RLS update"
                )
                return

            old_exponent = self.battery_model.get_peukert_exponent()
            smoothed, new_P = self.rls_peukert.update(new_exponent)
            smoothed = max(1.0, min(1.4, smoothed))
            self.battery_model.set_peukert_exponent(smoothed)
            self.battery_model.set_rls_state(
                "peukert", smoothed, new_P, self.rls_peukert.sample_count
            )
            logger.info(
                f"Peukert calibrated: {old_exponent:.3f} → {smoothed:.3f} "
                f"(single-point={new_exponent:.3f}), "
                f"confidence={self.rls_peukert.confidence:.0%}",
                extra={
                    "event_type": "peukert_calibration",
                    "peukert_old": f"{old_exponent:.3f}",
                    "peukert_new": f"{smoothed:.3f}",
                    "peukert_raw": f"{new_exponent:.3f}",
                    "rls_p": f"{new_P:.4f}",
                    "rls_confidence": f"{self.rls_peukert.confidence:.3f}",
                    "sample_count": str(self.rls_peukert.sample_count),
                },
            )
        else:
            logger.warning(
                "Peukert calibration returned None (unexpected — math undefined?)",
                extra={"event_type": "peukert_calibration_failed"},
            )

    def _log_discharge_prediction(
        self, discharge_buffer: DischargeBuffer, current_soc: float | None = None
    ) -> None:
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
                "event_type": "discharge_prediction",
                "predicted_minutes": f"{self.discharge_predicted_runtime:.1f}",
                "actual_minutes": f"{actual_minutes:.1f}",
                "avg_load_percent": f"{avg_load:.1f}",
                "start_soc": f"{current_soc:.3f}" if current_soc is not None else "N/A",
            },
        )

        self.discharge_predicted_runtime = None

    def _calculate_days_since_deep(self) -> Optional[float]:
        """Calculate days since last deep discharge (>70% DoD).

        Returns None if no deep discharge in history.
        """
        discharge_events = self.battery_model.state.get("discharge_events", [])
        now = datetime.now(timezone.utc)

        for event in reversed(discharge_events):
            if event.get("depth_of_discharge", 0) <= 0.7:
                continue
            timestamp_str = event.get("timestamp")
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
        r_value = entry.get("r_ohm")
        if r_value is None or not isinstance(r_value, (int, float)):
            logger.warning(
                "r_internal_history entry invalid 'r_ohm': %r (keys: %s)",
                r_value,
                list(entry.keys()),
                extra={"event_type": "r_internal_invalid_entry"},
            )
            return None
        date_str = entry.get("date", "")
        try:
            entry_time = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError) as e:
            logger.warning(
                "Skipping r_internal entry with bad date: %r — %s",
                date_str,
                e,
                extra={"event_type": "r_internal_invalid_entry"},
            )
            return None
        days_ago = (now - entry_time).total_seconds() / 86400.0
        if days_ago > 30:
            return None
        return (days_ago, r_value)

    def _estimate_ir_trend(self) -> float:
        """Estimate IR trend rate (dR/dt) in ohms/day from last 30 days.

        Returns 0.0 if insufficient data (<2 recent entries).
        """
        r_history = self.battery_model.state.get("r_internal_history", [])
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

    def _classify_discharge_trigger(
        self, discharge_buffer: Optional[DischargeBuffer] = None
    ) -> str:
        """Classify discharge as natural or test-initiated.

        Compare discharge_buffer start time to last upscmd timestamp.
        If discharge started within 60 seconds of upscmd, it's test-initiated.
        Otherwise, natural.

        Returns: 'natural' | 'test_initiated'
        """
        last_upscmd = self.battery_model.get_last_upscmd_timestamp()
        if not last_upscmd or not discharge_buffer:
            return "natural"

        if not hasattr(discharge_buffer, "times") or not discharge_buffer.times:
            return "natural"

        discharge_start_dt = datetime.fromtimestamp(discharge_buffer.times[0], tz=timezone.utc)

        try:
            upscmd_dt = datetime.fromisoformat(last_upscmd)
            seconds_since_upscmd = (discharge_start_dt - upscmd_dt).total_seconds()

            if 0 <= seconds_since_upscmd <= 60:
                logger.info(
                    f"Discharge classified as test-initiated: {seconds_since_upscmd:.1f}s after upscmd",
                    extra={"event_type": "discharge_classification", "reason": "test_initiated"},
                )
                return "test_initiated"
            else:
                return "natural"
        except (ValueError, TypeError):
            buf_start = discharge_buffer.times[0] if discharge_buffer.times else None
            logger.warning(
                f"Invalid timestamps in discharge classification: upscmd={last_upscmd}, buf_start={buf_start}",
                exc_info=True,
                extra={"event_type": "discharge_classification_error"},
            )
            return "natural"

    def _estimate_dod_from_buffer(self, discharge_buffer: DischargeBuffer) -> float:
        """Estimate depth of discharge as the SoC span covered during the event.

        DoD = SoC(V_max) - SoC(V_min), reading both off the battery model's LUT so the
        non-linear voltage→SoC curve is honored. This replaces the old
        (V_max - V_min) / (V_nominal - V_floor) voltage-swing proxy, which under-reported
        DoD whenever a discharge began already sagged (a lower V_max shrank the swing even
        though the battery was driven just as deep). DoD feeds the days_since_deep metric.

        Args:
            discharge_buffer: DischargeBuffer with voltages array

        Returns: float [0, 1] representing fraction of battery discharged
        """
        if not hasattr(discharge_buffer, "voltages") or len(discharge_buffer.voltages) < 2:
            return 0.0

        lut = self.battery_model.get_lut()
        v_min = min(discharge_buffer.voltages)
        v_max = max(discharge_buffer.voltages)
        # SoC is higher at V_max (start of discharge) than at V_min (deepest point), so the
        # difference is the fraction of charge drawn out over the event.
        dod = soc_from_voltage(v_max, lut) - soc_from_voltage(v_min, lut)
        return min(1.0, max(0.0, dod))

    def _estimate_cycle_budget(self) -> int:
        """Estimate remaining cycle budget: RATED_CYCLE_LIFE * current SoH."""
        soh = self.battery_model.state.get("soh", 1.0)
        return int(RATED_CYCLE_LIFE * soh)
