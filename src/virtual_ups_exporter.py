"""Export battery state to NUT virtual UPS device and health endpoint."""

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from src.event_classifier import EventType
from src.monitor_config import HealthSnapshot, _sanitize_journal_error, write_health_endpoint
from src.virtual_ups import write_virtual_ups_dev

logger = logging.getLogger("ups-battery-monitor")

JournalHealthProvider = Callable[[], Mapping[str, object]]


class VirtualUpsExporter:
    """Write battery state to NUT dummy-ups device file and health endpoint.

    Extracted from MonitorDaemon to own all output/export concerns:
    - Virtual UPS dev file (consumed by NUT dummy-ups driver → Grafana)
    - Health snapshot JSON (consumed by MOTD, Alloy, external scripts)
    """

    def __init__(
        self,
        battery_model,
        event_classifier,
        discharge_handler,
        scheduler_manager,
        journal_health_provider: JournalHealthProvider | None = None,
        *,
        virtual_ups_path: Path,
        health_path: Path,
    ):
        self.battery_model = battery_model
        self.event_classifier = event_classifier
        self.discharge_handler = discharge_handler
        self.scheduler_manager = scheduler_manager
        self.journal_health_provider = journal_health_provider
        self.virtual_ups_path = Path(virtual_ups_path)
        self.health_path = Path(health_path)

    @staticmethod
    def _has_usable_status(ups_data, current_metrics) -> bool:
        """Return whether a physical status is available for a virtual write."""
        if not isinstance(ups_data, Mapping):
            return False
        override = getattr(current_metrics, "ups_status_override", None)
        raw_status = ups_data.get("ups.status")
        return any(
            isinstance(status, str) and bool(status.strip()) for status in (override, raw_status)
        )

    def write_virtual_ups(self, ups_data, battery_charge, time_rem, current_metrics) -> bool:
        """Write computed metrics and return whether the write succeeded."""
        if not self._has_usable_status(ups_data, current_metrics):
            logger.warning(
                "Skipping virtual UPS write: physical UPS status is unavailable",
                extra={"event_type": "virtual_ups_write_skipped", "reason": "missing_ups_status"},
            )
            return False
        try:
            virtual_metrics = self._build_virtual_metrics(
                ups_data, battery_charge, time_rem, current_metrics
            )
            result = write_virtual_ups_dev(virtual_metrics, output_path=self.virtual_ups_path)
            # The concrete writer currently signals failure by raising, but
            # preserve an explicit False result if a different writer is
            # injected at this boundary.
            return result is not False
        except (OSError, IOError, ValueError) as e:
            logger.error(
                f"Failed to write virtual UPS metrics: {e}",
                exc_info=True,
                extra={"event_type": "virtual_ups_write_failed"},
            )
            return False

    def write_health_snapshot(self, poll_latency_ms, current_metrics, consecutive_errors) -> bool:
        """Construct health snapshot and return whether the endpoint write succeeded."""
        convergence_status = self.battery_model.get_convergence_status()
        dh = self.discharge_handler
        journal_fields = self._journal_health_fields()
        scheduler_mode = getattr(self.scheduler_manager, "scheduler_mode", "proposal_only")
        if scheduler_mode not in {"capture_only", "proposal_only", "execute"}:
            raise ValueError(f"unknown scheduler mode {scheduler_mode!r}")
        snapshot = HealthSnapshot(
            soc_percent=(current_metrics.soc or 0.0) * 100.0,
            is_online=(current_metrics.ups_status_override == "OL"),
            poll_latency_ms=poll_latency_ms,
            capacity_ah_measured=convergence_status.latest_ah,
            capacity_ah_rated=convergence_status.rated_ah,
            capacity_confidence=convergence_status.confidence_percent / 100.0,
            capacity_samples_count=convergence_status.sample_count,
            capacity_converged=convergence_status.converged,
            days_since_deep=dh.last_days_since_deep,
            ir_trend_rate=dh.last_ir_trend_rate,
            cycle_budget_remaining=dh.last_cycle_budget_remaining,
            scheduling_reason=self.scheduler_manager.last_scheduling_reason,
            next_test_timestamp=self.scheduler_manager.last_next_test_timestamp,
            eligible_for_operator_test_at=self.scheduler_manager.last_next_test_timestamp,
            last_discharge_timestamp=dh.last_discharge_timestamp,
            consecutive_errors=consecutive_errors,
            shutdown_imminent=current_metrics.shutdown_imminent,
            soh_alert_threshold=self.battery_model.soh_threshold,
            # Release A is explicitly capture-only.  Scheduler proposals are
            # read-only and must never be reported as automatic dispatch.
            model_update_mode="capture_only",
            automatic_dispatch=scheduler_mode == "execute",
            scheduler_mode=scheduler_mode,
            **journal_fields,
        )
        return bool(write_health_endpoint(snapshot, health_path=self.health_path))

    def _journal_health_fields(self) -> dict[str, Any]:
        """Read optional journal state without making export or LB depend on it."""
        defaults: dict[str, Any] = {
            "journal_healthy": True,
            "active_event_id": None,
            "journal_last_synced_seq": None,
            "journal_last_error": None,
            "pending_replay": False,
            "recovered_partial_events": 0,
            "cycle_count": None,
            "cumulative_on_battery_sec": None,
        }
        provider = self.journal_health_provider
        if provider is None:
            return defaults

        try:
            state = provider()
            healthy = state.get("journal_healthy")
            if isinstance(healthy, bool):
                defaults["journal_healthy"] = healthy
            event_id = state.get("active_event_id")
            if isinstance(event_id, str):
                defaults["active_event_id"] = event_id[:256] or None
            sequence = state.get("journal_last_synced_seq")
            if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 0:
                defaults["journal_last_synced_seq"] = sequence
            error = state.get("journal_last_error")
            if error is not None:
                defaults["journal_last_error"] = _sanitize_journal_error(error)
            replay = state.get("pending_replay")
            if isinstance(replay, bool):
                defaults["pending_replay"] = replay
            partial = state.get("recovered_partial_events")
            if isinstance(partial, int) and not isinstance(partial, bool) and partial >= 0:
                defaults["recovered_partial_events"] = partial
            cycle_count = state.get("cycle_count")
            if (
                isinstance(cycle_count, int)
                and not isinstance(cycle_count, bool)
                and cycle_count >= 0
            ):
                defaults["cycle_count"] = cycle_count
            cumulative = state.get("cumulative_on_battery_sec")
            if (
                isinstance(cumulative, (int, float))
                and not isinstance(cumulative, bool)
                and cumulative >= 0
            ):
                defaults["cumulative_on_battery_sec"] = float(cumulative)
            current_fingerprint = state.get("scientific_fingerprint")
            if isinstance(current_fingerprint, str):
                defaults["scientific_fingerprint"] = current_fingerprint[:128]
            baseline_fingerprint = state.get("baseline_scientific_fingerprint")
            if isinstance(baseline_fingerprint, str):
                defaults["baseline_scientific_fingerprint"] = baseline_fingerprint[:128]
            latched = state.get("scientific_change_latched")
            if isinstance(latched, bool):
                defaults["scientific_change_latched"] = latched
            startup_degraded = state.get("startup_degraded")
            if isinstance(startup_degraded, bool):
                defaults["startup_degraded"] = startup_degraded
            disposition = state.get("last_event_disposition")
            if isinstance(disposition, str) and disposition in {
                "applied",
                "recorded_only",
                "rejected",
            }:
                defaults["last_event_disposition"] = disposition
            sag = state.get("last_voltage_sag_observation")
            if isinstance(sag, Mapping):
                # Copy only JSON scalar values from the immutable observation;
                # this keeps the health endpoint bounded and serializable.
                defaults["last_voltage_sag_observation"] = {
                    str(key): value
                    for key, value in sag.items()
                    if isinstance(value, (str, int, float, bool)) or value is None
                }
            return defaults
        except Exception as exc:
            # Journal observability is deliberately best effort.  In particular,
            # this path must never prevent virtual UPS output or alter LB handling.
            return {
                **defaults,
                "journal_healthy": False,
                "journal_last_error": _sanitize_journal_error(exc),
            }

    def _build_virtual_metrics(self, ups_data, battery_charge, time_rem, current_metrics):
        """Assemble enterprise-equivalent metrics dict for the virtual UPS device."""
        if not self._has_usable_status(ups_data, current_metrics):
            raise ValueError("physical UPS status is unavailable")

        current_status = getattr(current_metrics, "ups_status_override", None)
        if not isinstance(current_status, str) or not current_status.strip():
            current_status = None
        raw_status = ups_data.get("ups.status")
        if not isinstance(raw_status, str):
            raw_status = ""
        ups_status_override = current_status or raw_status
        if self._should_passthrough_ob_status(raw_status):
            ups_status_override = raw_status

        soh = self.battery_model.get_soh()
        install_date = self.battery_model.get_battery_install_date() or ""
        cycle_count: int = self.battery_model.get_cycle_count()
        cumulative_sec: float = self.battery_model.get_cumulative_on_battery_sec()
        if self.journal_health_provider is not None:
            try:
                journal_state = self.journal_health_provider()
                journal_cycle = journal_state.get("cycle_count")
                if isinstance(journal_cycle, int):
                    cycle_count = journal_cycle
                journal_cumulative = journal_state.get("cumulative_on_battery_sec")
                if isinstance(journal_cumulative, (int, float)):
                    cumulative_sec = float(journal_cumulative)
            except Exception:
                pass
        replacement_due = self.battery_model.compute_replacement_due() or ""
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
        """Median of non-zero R_internal measurements in mΩ; requires ≥3 for noise rejection.

        NOT A BUG: returning 0 is the honest "insufficient calibration data" signal,
        not a missing value. Zeros are filtered out (a 0 means the UPS returned to OL
        before the sag developed — see SagTracker._record_voltage_sag), and we need
        ≥3 valid samples before publishing a median. With <3 valid samples the exporter
        intentionally reports 0 → battery.internal_resistance=0 in the .dev is expected
        until enough real sag measurements accumulate.
        """
        r_internal_history = self.battery_model.get_r_internal_history()
        valid = [e["r_ohm"] for e in r_internal_history if e["r_ohm"] > 0]
        if len(valid) >= 3:
            sorted_r = sorted(valid)
            return round(sorted_r[len(sorted_r) // 2] * 1000, 1)
        return 0
