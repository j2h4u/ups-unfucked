"""Export battery state to NUT virtual UPS device and health endpoint."""

import logging

from src.event_classifier import EventType
from src.monitor_config import RATED_CAPACITY_AH, HealthSnapshot, write_health_endpoint
from src.virtual_ups import write_virtual_ups_dev

logger = logging.getLogger("ups-battery-monitor")


class VirtualUpsExporter:
    """Write battery state to NUT dummy-ups device file and health endpoint.

    Extracted from MonitorDaemon to own all output/export concerns:
    - Virtual UPS dev file (consumed by NUT dummy-ups driver → Grafana)
    - Health snapshot JSON (consumed by MOTD, Alloy, external scripts)
    """

    def __init__(self, battery_model, event_classifier, discharge_handler, scheduler_manager):
        self.battery_model = battery_model
        self.event_classifier = event_classifier
        self.discharge_handler = discharge_handler
        self.scheduler_manager = scheduler_manager

    def write_virtual_ups(self, ups_data, battery_charge, time_rem, current_metrics):
        """Write computed metrics to tmpfs for NUT dummy-ups driver."""
        try:
            virtual_metrics = self._build_virtual_metrics(
                ups_data, battery_charge, time_rem, current_metrics
            )
            write_virtual_ups_dev(virtual_metrics)
        except (OSError, IOError) as e:
            logger.error(
                f"Failed to write virtual UPS metrics: {e}",
                exc_info=True,
                extra={"event_type": "virtual_ups_write_failed"},
            )

    def write_health_snapshot(self, poll_latency_ms, current_metrics, consecutive_errors):
        """Construct health snapshot from current state and write to endpoint."""
        convergence_status = self.battery_model.get_convergence_status()
        dh = self.discharge_handler
        snapshot = HealthSnapshot(
            soc_percent=(current_metrics.soc or 0.0) * 100.0,
            is_online=(current_metrics.ups_status_override == "OL"),
            poll_latency_ms=poll_latency_ms,
            capacity_ah_measured=convergence_status.latest_ah,
            capacity_ah_rated=convergence_status.rated_ah,
            capacity_confidence=convergence_status.confidence_percent / 100.0,
            capacity_samples_count=convergence_status.sample_count,
            capacity_converged=convergence_status.converged,
            sulfation_score=dh.last_sulfation_score,
            sulfation_confidence=dh.last_sulfation_confidence,
            days_since_deep=dh.last_days_since_deep,
            ir_trend_rate=dh.last_ir_trend_rate,
            recovery_delta=dh.last_recovery_delta,
            cycle_roi=dh.last_cycle_roi,
            cycle_budget_remaining=dh.last_cycle_budget_remaining,
            scheduling_reason=self.scheduler_manager.last_scheduling_reason,
            next_test_timestamp=self.scheduler_manager.last_next_test_timestamp,
            last_discharge_timestamp=dh.last_discharge_timestamp,
            consecutive_errors=consecutive_errors,
        )
        write_health_endpoint(snapshot)

    def _build_virtual_metrics(self, ups_data, battery_charge, time_rem, current_metrics):
        """Assemble enterprise-equivalent metrics dict for the virtual UPS device."""
        ups_status_override = current_metrics.ups_status_override or ups_data.get(
            "ups.status", "OL"
        )
        raw_status = ups_data.get("ups.status", "")
        if self._should_passthrough_ob_status(raw_status):
            ups_status_override = raw_status

        soh = self.battery_model.get_soh()
        install_date = self.battery_model.get_battery_install_date() or ""
        cycle_count = self.battery_model.get_cycle_count()
        cumulative_sec = self.battery_model.get_cumulative_on_battery_sec()
        replacement_due = self.battery_model.get_replacement_due() or ""
        r_internal_mohm = self._compute_median_r_internal_mohm()

        return {
            "battery.runtime": int(time_rem * 60)
            if time_rem is not None
            else int(float(ups_data.get("battery.runtime", 0))),
            "battery.charge": int(battery_charge)
            if battery_charge is not None
            else int(float(ups_data.get("battery.charge", 0))),
            "ups.status": ups_status_override,
            "battery.health": round(soh * 100),
            "battery.date": install_date,
            "battery.cycle.count": cycle_count,
            "battery.cumulative.runtime": int(cumulative_sec),
            "battery.replacement.due": replacement_due,
            "battery.internal_resistance": r_internal_mohm,
            **{
                k: v
                for k, v in ups_data.items()
                if k not in ["battery.runtime", "battery.charge", "ups.status"]
            },
        }

    def _should_passthrough_ob_status(self, raw_status: str) -> bool:
        """True when classifier kept previous state but raw NUT status contains OB.

        Guards against the classifier swallowing an OB status as unknown — pass
        through the original so downstream consumers (upsmon, Grafana) see it.
        """
        return (
            not self.event_classifier.transition_occurred
            and "OB" in raw_status.split()
            and self.event_classifier.state == EventType.ONLINE
        )

    def _compute_median_r_internal_mohm(self) -> float:
        """Median of non-zero R_internal measurements in mΩ; requires ≥3 for noise rejection."""
        r_internal_history = self.battery_model.get_r_internal_history()
        valid = [e["r_ohm"] for e in r_internal_history if e["r_ohm"] > 0]
        if len(valid) >= 3:
            sorted_r = sorted(valid)
            return round(sorted_r[len(sorted_r) // 2] * 1000, 1)
        return 0
