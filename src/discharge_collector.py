"""Discharge sample collector — operational event capture and journaling.

Extracted from MonitorDaemon to reduce its responsibility surface (ARCH-05).
DischargeCollector owns the discharge buffer lifecycle and durable operational
journal writes.  It deliberately does not mutate the learned battery model.
"""

import logging
import time
from typing import Optional

from src.discharge_journal import (
    DischargeJournal,
    JournalEnd,
    JournalError,
    JournalSample,
    JournalStart,
)
from src.discharge_types import CompletedDischarge
from src.event_classifier import EventType
from src.model import BatteryModel
from src.monitor_config import (
    DISCHARGE_BUFFER_MAX_SAMPLES,
    DISCHARGE_SAMPLE_INTERVAL_SEC,
    Config,
    DischargeBuffer,
)

logger = logging.getLogger("ups-battery-monitor")


class DischargeCollector:
    """Capture the visible lifecycle of operational battery events.

    Extracted from MonitorDaemon to own all discharge collection state:
    discharge_buffer and event/journal state.  Operational events are durable
    evidence only; they must never mutate the learned LUT in this collector.

    Usage:
        collector = DischargeCollector(battery_model, config, discharge_handler, ema_filter)
        # Each poll:
        completion = collector.track(voltage, timestamp, event_type, current_metrics)
        if completion is not None:
            _update_battery_health(completion)
        # After _update_battery_health() processes the buffer:
        collector.reset_buffer()
        # On the first visible OB->OL transition:
        collector.finalize(timestamp)
    """

    def __init__(
        self,
        battery_model: BatteryModel,
        config: Config,
        discharge_handler,  # DischargeHandler — for discharge_predicted_runtime handoff
        ema_filter,  # EMAFilter — for stabilized check and load reads
        journal: Optional[DischargeJournal] = None,
    ):
        """Initialize DischargeCollector.

        Args:
            battery_model: Persistent battery model — used for the current battery
                epoch ID and read-only lifecycle context.
            config: Frozen Config dataclass — provides polling_interval, reporting_interval,
                reference_load_percent.
            discharge_handler: DischargeHandler — discharge_predicted_runtime is set here
                at OB start for prediction error logging after discharge completes.
            ema_filter: EMAFilter — stabilized flag and current load value.
        """
        self.battery_model = battery_model
        self.config = config
        self.discharge_handler = discharge_handler
        self.ema_filter = ema_filter
        self.journal = journal

        self.discharge_buffer = DischargeBuffer()
        self._discharge_start_monotonic: Optional[float] = None
        self._event_id: Optional[str] = None
        self._journal_cursor = None
        self._boot_recovery_checked = False
        self._last_durable_sample_timestamp: Optional[float] = None
        self._next_sample_deadline: Optional[float] = None
        self._last_journal_sample_timestamp: Optional[float] = None
        self._cached_observation: Optional[dict] = None
        self._cal_provenance = False
        self._sag_observation_payload: Optional[dict] = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def is_collecting(self) -> bool:
        """True while discharge buffer is actively collecting samples."""
        return self.discharge_buffer.collecting

    @property
    def buffer(self) -> DischargeBuffer:
        """Current discharge buffer (read by MonitorDaemon._update_battery_health)."""
        return self.discharge_buffer

    @property
    def event_id(self) -> Optional[str]:
        return self._event_id

    def track(
        self,
        voltage: float,
        timestamp: float,
        event_type,
        current_metrics,
        raw_ups_data=None,
        monotonic_timestamp: Optional[float] = None,
        sag_observation=None,
    ) -> Optional[CompletedDischarge]:
        """Drive the discharge state machine for one poll tick.

        Every visible OB starts/continues an event.  The first OB is always
        journaled; subsequent measurement samples use a monotonic deadline and
        are journaled every ten seconds.  The first visible OL closes the event
        immediately after flushing the cached last OB observation.

        Args:
            voltage: Current EMA voltage reading.
            timestamp: Unix timestamp for this poll.
            event_type: Classified event (EventType enum).
            current_metrics: CurrentMetrics with time_rem_minutes.
            sag_observation: Optional immutable VoltageSagObservation produced by
                SagTracker. It is embedded in the existing sample payload.

        Returns:
            An immutable completion request on the first visible OL, otherwise
            ``None``.
        """
        monotonic_timestamp = (
            monotonic_timestamp
            if isinstance(monotonic_timestamp, (int, float))
            and not isinstance(monotonic_timestamp, bool)
            else time.monotonic()
        )
        if self._is_raw_calibration_status(raw_ups_data):
            # CAL provenance is sticky metadata for the event, not a mutation
            # guard.  All operational events are ineligible for model writes.
            self._cal_provenance = True

        if not self._boot_recovery_checked:
            self._recover_open_event(event_type, timestamp)
            self._boot_recovery_checked = True

        is_discharging = event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)

        if is_discharging:
            if not self.discharge_buffer.collecting:
                # A caller may consume the completion asynchronously; do not
                # carry CAL provenance from a previously closed event into a
                # new OB event.
                if self._event_id is not None and not self._is_raw_calibration_status(raw_ups_data):
                    self._cal_provenance = False
                self._start_discharge_collection(
                    timestamp, current_metrics, raw_ups_data, monotonic_timestamp
                )
            if sag_observation is not None:
                self._sag_observation_payload = {
                    "observed_at": sag_observation.observed_at.isoformat(),
                    "event_type": str(sag_observation.event_type),
                    "voltage_before": float(sag_observation.voltage_before),
                    "voltage_sag": float(sag_observation.voltage_sag),
                    "load_percent": float(sag_observation.load_percent),
                    "apparent_r_internal_ohm": float(sag_observation.apparent_r_internal_ohm),
                }
            if voltage is not None:
                self._observe_ob(
                    event_type,
                    voltage,
                    load=self.ema_filter.load if self.ema_filter.load is not None else 0.0,
                    current_metrics=current_metrics,
                    raw_ups_data=raw_ups_data,
                    timestamp=timestamp,
                    monotonic_timestamp=monotonic_timestamp,
                )
            return None

        # There is deliberately no cooldown or event coalescing here.  A
        # visible OL is the recovery observation for this event, and a later
        # OB opens a new event ID (OB -> OL -> OB is two events).
        if self.discharge_buffer.collecting:
            return self._close_completed_event(timestamp, monotonic_timestamp)

        return None

    def finalize(self, timestamp: float) -> None:
        """End discharge collection: record on-battery time and reset buffer state.

        Called after the operational completion has been handed to the handler.

        Args:
            timestamp: Unix timestamp when collection ended.
        """
        self._discharge_start_monotonic = None
        self.discharge_buffer.collecting = False
        self._event_id = None
        self._journal_cursor = None
        self._cached_observation = None
        self._last_journal_sample_timestamp = None
        self._last_durable_sample_timestamp = None
        self._next_sample_deadline = None
        self._cal_provenance = False
        self._sag_observation_payload = None

    def reset_buffer(self) -> None:
        """Replace discharge_buffer with a fresh DischargeBuffer.

        Called by MonitorDaemon after _update_battery_health() processes the buffer.
        """
        self.discharge_buffer = DischargeBuffer()

    def shutdown(self, timestamp: Optional[float] = None) -> None:
        """Best-effort partial end marker; scientific processing is never run here."""
        if self.journal is None or self._journal_cursor is None:
            return
        try:
            self._flush_cached_observation()
            self.journal.close_event(
                self._journal_cursor,
                JournalEnd(
                    {
                        "lifecycle": "closed_shutdown_requested",
                        "last_confirmed_timestamp": self._last_durable_sample_timestamp,
                        "last_accepted_seq": self._journal_cursor.seq,
                        "evidence_class": "operational_partial",
                        "model_processing_eligible": False,
                        "eligibility_reasons": ["shutdown_requested", "operational_partial"],
                    }
                ),
            )
            self._journal_cursor = None
        except (JournalError, OSError) as exc:
            self._journal_failure("shutdown", exc)

    # ------------------------------------------------------------------
    # Private methods
    # ------------------------------------------------------------------

    def _start_discharge_collection(
        self,
        timestamp: float,
        current_metrics,
        raw_ups_data=None,
        monotonic_timestamp: Optional[float] = None,
    ) -> None:
        """Initialize discharge buffer for a new OL→OB event.

        Clears buffers, increments cycle count, snapshots predicted runtime.

        Args:
            timestamp: Unix timestamp when OB transition occurred.
            current_metrics: CurrentMetrics — used for time_rem_minutes snapshot.
        """
        event_type = current_metrics.event_type
        if event_type is None:
            return
        if self.discharge_buffer.collecting:
            return

        self.discharge_buffer.collecting = True
        self.discharge_buffer.voltages = []
        self.discharge_buffer.times = []
        self.discharge_buffer.loads = []
        self.discharge_buffer.raw_voltages = []
        self.discharge_buffer.raw_loads = []
        self._discharge_start_monotonic = monotonic_timestamp
        self._event_id = None
        self._journal_cursor = None
        self._cached_observation = None
        self._last_journal_sample_timestamp = None
        self._last_durable_sample_timestamp = None
        self._next_sample_deadline = None
        self._sag_observation_payload = None
        if self.journal is not None:
            try:
                self._journal_cursor = self.journal.start_event(
                    JournalStart(
                        {
                            "event_classification": event_type.name,
                            "classifications": [event_type.name],
                            "start_timestamp": timestamp,
                            "raw_nut": self._raw_nut(raw_ups_data),
                            "raw_voltage": (raw_ups_data or {}).get("battery.voltage"),
                            "raw_load": (raw_ups_data or {}).get("ups.load"),
                            "raw_input_voltage": (raw_ups_data or {}).get("input.voltage"),
                            "raw_status": (raw_ups_data or {}).get("ups.status"),
                            "predicted_runtime_minutes": (
                                current_metrics.time_rem_minutes
                                if self.ema_filter.stabilized
                                else None
                            ),
                            "shutdown_threshold_minutes": self.config.shutdown_minutes,
                            "cal_provenance": self._cal_provenance,
                            "model_processing_eligible": False,
                            "evidence_class": "operational",
                            "battery_epoch_id": self.battery_model.get_battery_epoch_id(),
                        }
                    )
                )
                self._event_id = self._journal_cursor.event_id
            except (JournalError, OSError) as exc:
                self._journal_failure("start", exc)
        # Snapshot predicted runtime at OB start for prediction error logging
        if self.ema_filter.stabilized and current_metrics.time_rem_minutes is not None:
            self.discharge_handler.discharge_predicted_runtime = current_metrics.time_rem_minutes
        else:
            self.discharge_handler.discharge_predicted_runtime = None
        logger.info(
            f"Starting discharge buffer collection ({event_type.name}), "
            f"cycle #{self.battery_model.get_cycle_count()}",
            extra={
                "event_type": "discharge_start",
                "discharge_type": event_type.name,
                "cycle_count": self.battery_model.get_cycle_count(),
            },
        )

    @staticmethod
    def _raw_nut(raw_ups_data) -> dict:
        """Keep only the UPS fields used by capture and classification."""
        raw = raw_ups_data or {}
        return {
            key: raw[key]
            for key in ("battery.voltage", "ups.load", "input.voltage", "ups.status")
            if key in raw
        }

    def _observe_ob(
        self,
        event_type,
        voltage: float,
        load: float,
        current_metrics,
        raw_ups_data,
        timestamp: float,
        monotonic_timestamp: float,
    ) -> None:
        """Cache each acquisition and persist on a monotonic ten-second deadline."""
        raw = self._raw_nut(raw_ups_data)
        payload = {
            "timestamp": timestamp,
            "event_classification": event_type.name,
            "raw_nut": raw,
            "raw_voltage": raw.get("battery.voltage"),
            "raw_load": raw.get("ups.load"),
            "raw_input_voltage": raw.get("input.voltage"),
            "raw_status": raw.get("ups.status"),
            "ema_voltage": voltage,
            "ema_load": load,
            "model_input_soc": (
                current_metrics.soc if isinstance(current_metrics.soc, (int, float)) else None
            ),
            "model_input_runtime_minutes": current_metrics.time_rem_minutes,
            "model_input_event": event_type.name,
            "cal_provenance": self._cal_provenance,
        }
        if self._sag_observation_payload is not None:
            # Keep this observation in the established sample payload. It is
            # diagnostic evidence only and never grants model/RLS write rights.
            payload["voltage_sag_observation"] = dict(self._sag_observation_payload)
        self._cached_observation = payload
        should_persist = (
            self._last_journal_sample_timestamp is None
            or self._next_sample_deadline is None
            or monotonic_timestamp >= self._next_sample_deadline
        )
        if should_persist:
            self._persist_observation(payload, monotonic_timestamp)

    def _persist_observation(self, payload: dict, monotonic_timestamp: float) -> None:
        """Persist one selected observation and mirror it into the bounded buffer."""
        persisted = self._append_journal_sample_payload(payload)
        if persisted:
            self._last_journal_sample_timestamp = payload.get("timestamp")
            self._last_durable_sample_timestamp = payload.get("timestamp")
            self._next_sample_deadline = monotonic_timestamp + 10.0
        if len(self.discharge_buffer.voltages) >= DISCHARGE_BUFFER_MAX_SAMPLES:
            logger.warning(
                "Discharge buffer capped at %s samples",
                DISCHARGE_BUFFER_MAX_SAMPLES,
                extra={
                    "event_type": "discharge_buffer_capped",
                    "max_samples": DISCHARGE_BUFFER_MAX_SAMPLES,
                },
            )
            return
        self.discharge_buffer.voltages.append(payload["ema_voltage"])
        self.discharge_buffer.times.append(payload["timestamp"])
        self.discharge_buffer.loads.append(payload["ema_load"])
        self.discharge_buffer.raw_voltages.append(payload.get("raw_voltage"))
        self.discharge_buffer.raw_loads.append(payload.get("raw_load"))

    @staticmethod
    def _is_raw_calibration_status(raw_ups_data) -> bool:
        """Return whether the raw UPS status identifies a CAL self-test."""
        status = (raw_ups_data or {}).get("ups.status")
        return DischargeCollector._status_has_calibration_flag(status)

    @staticmethod
    def _status_has_calibration_flag(status) -> bool:
        return isinstance(status, str) and "CAL" in status.split()

    def _recover_open_event(self, event_type, timestamp: float) -> None:
        """Reattach or close an event left open by an earlier boot."""
        if self.journal is None or self.journal.health.open_event_id is None:
            return
        event_id = self.journal.health.open_event_id
        try:
            projection = self.journal.replay()
            event = projection.events[event_id]
            if (
                event.start is None
                or event.start.payload.get("battery_epoch_id")
                != self.battery_model.get_battery_epoch_id()
            ):
                # Keep an unknown/previous-battery event as raw history-only
                # evidence, but close it so the journal can accept new events.
                cursor = self.journal.resume_event(event_id)
                start_payload = event.start.payload if event.start is not None else {}
                last_sample = event.samples[-1].payload if event.samples else {}
                epoch_reason = (
                    "missing_battery_epoch_id"
                    if "battery_epoch_id" not in start_payload
                    else "battery_epoch_mismatch"
                )
                last_sample_timestamp = last_sample.get("timestamp")
                logger.warning(
                    "Closing open discharge event %s as history-only: %s",
                    event_id,
                    epoch_reason,
                )
                self.journal.close_event(
                    cursor,
                    JournalEnd(
                        {
                            "lifecycle": "closed_epoch_mismatch",
                            "disposition": "history_only",
                            "reason": epoch_reason,
                            "last_confirmed_timestamp": last_sample_timestamp,
                            "last_confirmed_ob_timestamp": last_sample_timestamp,
                            "observed_recovery_timestamp": timestamp,
                            "observed_duration_sec": None,
                            "last_accepted_seq": cursor.seq,
                            "evidence_class": "operational_partial",
                            "model_processing_eligible": False,
                            "eligibility_reasons": [epoch_reason, "history_only"],
                        }
                    ),
                )
                return
            if event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST):
                self._cal_provenance = bool(
                    event.start and event.start.payload.get("cal_provenance")
                ) or any(
                    bool(sample.payload.get("cal_provenance"))
                    or self._status_has_calibration_flag(sample.payload.get("raw_status"))
                    for sample in event.samples
                    if not sample.payload.get("reboot_gap")
                )
                self._event_id = event_id
                self._journal_cursor = self.journal.resume_event(event_id)
                self._journal_cursor = self.journal.append_reboot_gap(
                    self._journal_cursor, payload={"reboot_timestamp": timestamp}
                )
                self.discharge_buffer.collecting = True
                start_payload = event.start.payload if event.start is not None else {}
                # A reboot creates a non-integrable gap.  Start a fresh
                # monotonic segment for any duration observed after resume.
                self._discharge_start_monotonic = time.monotonic()
                for sample in event.samples:
                    if sample.payload.get("reboot_gap"):
                        continue
                    voltage = sample.payload.get("ema_voltage")
                    sample_time = sample.payload.get("timestamp")
                    if isinstance(voltage, (int, float)) and isinstance(sample_time, (int, float)):
                        self.discharge_buffer.voltages.append(float(voltage))
                        self.discharge_buffer.times.append(float(sample_time))
                        self.discharge_buffer.loads.append(
                            float(sample.payload.get("ema_load", 0.0))
                        )
                        self.discharge_buffer.raw_voltages.append(sample.payload.get("raw_voltage"))
                        self.discharge_buffer.raw_loads.append(sample.payload.get("raw_load"))
                        self._cached_observation = dict(sample.payload)
                        self._last_journal_sample_timestamp = float(sample_time)
                        self._last_durable_sample_timestamp = float(sample_time)
                self._next_sample_deadline = time.monotonic() + DISCHARGE_SAMPLE_INTERVAL_SEC
                logger.warning("Resumed open discharge event %s after reboot", event_id)
            else:
                cursor = self.journal.resume_event(event_id)
                start_payload = event.start.payload if event.start is not None else {}
                last_sample = event.samples[-1].payload if event.samples else start_payload
                self.journal.close_event(
                    cursor,
                    JournalEnd(
                        {
                            "lifecycle": "closed_restart_recovered",
                            "last_confirmed_timestamp": last_sample.get(
                                "timestamp", last_sample.get("start_timestamp")
                            ),
                            "observed_recovery_timestamp": timestamp,
                            "last_confirmed_ob_timestamp": last_sample.get(
                                "timestamp", last_sample.get("start_timestamp")
                            ),
                            "observed_duration_sec": None,
                            "last_accepted_seq": cursor.seq,
                            "evidence_class": "operational_gapped",
                            "model_processing_eligible": False,
                            "eligibility_reasons": ["restart_recovered_gap", "operational"],
                        }
                    ),
                )
                logger.warning("Closed open discharge event %s during boot recovery", event_id)
        except (JournalError, OSError, KeyError, TypeError, ValueError) as exc:
            self._journal_failure("boot recovery", exc)

    def _append_journal_sample_payload(self, payload: dict) -> bool:
        if self.journal is None or self._journal_cursor is None:
            return False
        try:
            self._journal_cursor = self.journal.append_sample(
                self._journal_cursor, JournalSample(payload)
            )
            return True
        except (JournalError, OSError, KeyError, TypeError, ValueError) as exc:
            self._journal_failure("sample", exc)
            return False

    def _close_completed_event(
        self, timestamp: float, monotonic_timestamp: Optional[float] = None
    ) -> Optional[CompletedDischarge]:
        if not self.discharge_buffer.collecting:
            return None
        event_id = self._event_id
        self._flush_cached_observation(monotonic_timestamp)
        last_ob = self._last_durable_sample_timestamp
        mono_start = self._discharge_start_monotonic
        mono_end = monotonic_timestamp if monotonic_timestamp is not None else time.monotonic()
        duration = max(0.0, mono_end - mono_start) if mono_start is not None else None
        if self.journal is not None and self._journal_cursor is not None:
            try:
                self.journal.close_event(
                    self._journal_cursor,
                    JournalEnd(
                        {
                            "lifecycle": "closed_power_restored",
                            "observed_recovery_timestamp": timestamp,
                            "last_confirmed_ob_timestamp": last_ob,
                            "last_confirmed_timestamp": last_ob,
                            "last_accepted_seq": self._journal_cursor.seq,
                            "observed_duration_sec": duration,
                            "duration_sec": duration,
                            "evidence_class": "operational",
                            "model_processing_eligible": False,
                            "eligibility_reasons": [
                                "operational_event",
                                "not_a_controlled_capacity_test",
                            ],
                            "cal_provenance": self._cal_provenance,
                        }
                    ),
                )
            except (JournalError, OSError) as exc:
                self._journal_failure("end", exc)
        # Closing is a lifecycle transition, not a delayed completion.  The
        # monitor may process the returned completion immediately, while this
        # guard also prevents repeated OL polls from appending duplicate ends.
        self.discharge_buffer.collecting = False
        self._journal_cursor = None
        return CompletedDischarge(
            event_id,
            "closed_power_restored",
            "operational",
            tuple(self.discharge_buffer.voltages),
            tuple(self.discharge_buffer.times),
            tuple(self.discharge_buffer.loads),
            False,
            ("operational_event", "not_a_controlled_capacity_test"),
        )

    def _flush_cached_observation(self, monotonic_timestamp: Optional[float] = None) -> bool:
        """Persist the latest accepted observation before writing an event end."""
        if self._cached_observation is None:
            return True
        if self._last_journal_sample_timestamp == self._cached_observation.get("timestamp"):
            return True
        self._persist_observation(
            self._cached_observation,
            monotonic_timestamp if monotonic_timestamp is not None else time.monotonic(),
        )
        return self._last_journal_sample_timestamp == self._cached_observation.get("timestamp")

    def _journal_failure(self, operation: str, exc: BaseException) -> None:
        if self.journal is not None:
            self.journal.mark_degraded(f"{operation}: {exc}")
        logger.error(
            "Discharge journal %s failed; continuing degraded: %s",
            operation,
            exc,
            exc_info=True,
        )
