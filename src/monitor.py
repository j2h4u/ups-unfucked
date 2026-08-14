"""UPS Battery Monitor daemon — pipeline orchestrator.

Polls NUT upsd, applies EMA smoothing, classifies events, tracks discharge/sag,
computes metrics, and exports to virtual UPS + health endpoint.

Config/dataclasses extracted to monitor_config.py,
discharge lifecycle extracted to discharge_handler.py.
"""

import argparse
import errno
import fcntl
import math
import os
import signal
import socket
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from systemd.daemon import notify as sd_notify  # pyright: ignore[reportMissingImports]
except ImportError:
    # python-systemd not installed (dev/test environment).
    # Only affects watchdog heartbeats and READY=1 signal — daemon logic is unaffected.

    def sd_notify(status):
        pass


from src.battery_math import ScalarRLS
from src.capacity_estimator import CapacityEstimator
from src.discharge_collector import DischargeCollector
from src.discharge_handler import DischargeHandler
from src.discharge_journal import (
    AppliedDisposition,
    DischargeJournal,
    JournalError,
    JournalProjection,
)
from src.discharge_types import CompletedDischarge
from src.ema_filter import EMAFilter, ir_compensate
from src.event_classifier import EventClassifier, EventType
from src.model import DEFAULT_PEUKERT_EXPONENT, BatteryModel
from src.monitor_config import (
    DAEMON_VERSION,
    ERROR_LOG_BURST,
    Config,
    CurrentMetrics,
    SchedulingConfig,
    load_config,
    logger,
)
from src.nut_client import NUTClient
from src.runtime_calculator import runtime_minutes
from src.sag_tracker import SagTracker
from src.scheduler_manager import SchedulerManager
from src.soc_predictor import charge_percentage, soc_from_voltage
from src.virtual_ups import compute_ups_status_override
from src.virtual_ups_exporter import VirtualUpsExporter

_JOURNAL_PROJECTION_UNSET = object()


class MonitorDaemon:
    """
    Main daemon for UPS battery monitoring.

    Polls NUT upsd, applies EMA smoothing, tracks battery state.
    """

    def __init__(
        self,
        config: Config,
        journal: Optional[DischargeJournal] = None,
        *,
        virtual_ups_path: Path,
        health_path: Path,
    ):
        """Initialize daemon with provided configuration.

        Args:
            config: Config dataclass instance with all daemon parameters.
        """
        self.config = config
        self.virtual_ups_path = Path(virtual_ups_path)
        self.health_path = Path(health_path)
        self.shutdown_threshold_minutes = config.shutdown_minutes

        # Only create a new state directory with the private mode required for
        # the model/journal.  An existing directory may be managed by the
        # caller and must not be chmod'ed as a startup side effect.
        try:
            config.model_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError:
            if not config.model_dir.is_dir():
                raise RuntimeError(f"model_dir is not a directory: {config.model_dir}")
        self._writer_lock_fd = self._acquire_writer_lock(config.model_dir)
        self._ready_sent = False
        self.startup_degraded = False
        self._last_sag_observation = None
        self.journal: Optional[DischargeJournal] = journal
        self.journal_error: Optional[str] = None
        if self.journal is None:
            try:
                self.journal = DischargeJournal(config.model_dir / "discharge-events-v1.jsonl")
            except (JournalError, OSError, ValueError) as exc:
                # Persistence is fail-open: NUT, LB and shutdown keep running.
                self.journal_error = str(exc)[:512]
                logger.error(
                    "Discharge journal unavailable; continuing degraded: %s",
                    exc,
                    exc_info=True,
                )
        self.running = True

        try:
            self.nut_client = NUTClient(
                host=config.nut_host,
                port=config.nut_port,
                timeout=config.nut_timeout,
                ups_name=config.ups_name,
            )

            self.ema_filter = EMAFilter(
                window_sec=config.ema_window_sec, poll_interval_sec=config.polling_interval
            )
        except BaseException:
            self._release_writer_lock()
            raise

        try:
            self._init_battery_model_and_estimators(config)
        except BaseException:
            # A constructor failure must not leave the process-wide writer
            # lock held until garbage collection.  This is especially
            # important when model/journal composition fails during startup.
            self._release_writer_lock()
            raise
        # Replay only after initialization has persisted the current model
        # state; otherwise the final init save could overwrite an applied
        # event committed by the authoritative handler.
        self._pending_replay = False
        self._recovered_partial_events = 0
        self._replay_closed_events()
        # The repaired/replayed state is the startup baseline.  From this point
        # Release A is capture-only: scientific writes are observed and alarmed,
        # never silently accepted as learning.
        self.capture_only = True
        try:
            self.baseline_scientific_fingerprint = self.battery_model.scientific_fingerprint()
        except BaseException:
            self._release_writer_lock()
            raise
        self._fingerprint_alarm_latched = False
        self._journal_projection_cache = None
        self._journal_projection_cache_active = False

        self.current_metrics = CurrentMetrics()
        self._last_logged_soc = None
        self._last_logged_time_rem = None

        self.soh_threshold = config.soh_alert_threshold
        self.runtime_threshold_minutes = config.runtime_threshold_minutes

        self.reference_load_percent = config.reference_load_percent

        # SagTracker, SchedulerManager, and DischargeCollector constructed in _init_battery_model_and_estimators()

        self.has_logged_baseline_lock = False

        # Runtime fields (initialized here, populated in run())
        self.poll_count = 0
        self._stabilization_logged = False
        self._startup_logged = False
        self._consecutive_errors = 0
        self._startup_time: Optional[float] = None
        self._next_reporting_deadline: Optional[float] = None

        try:
            signal.signal(signal.SIGTERM, self._signal_handler)
            signal.signal(signal.SIGINT, self._signal_handler)
        except BaseException:
            self._release_writer_lock()
            raise

        logger.info(
            f"Daemon initialized: shutdown_threshold={self.shutdown_threshold_minutes}min, "
            f"poll={config.polling_interval}s, model={self.battery_model.model_path}, nut={config.nut_host}:{config.nut_port}",
            extra={
                "event_type": "daemon_init",
                "shutdown_threshold_minutes": self.shutdown_threshold_minutes,
                "poll_interval_sec": config.polling_interval,
                "model_path": str(self.battery_model.model_path),
                "nut_host": config.nut_host,
                "nut_port": config.nut_port,
            },
        )

        # Fail fast on misconfigured NUT rather than silently looping
        try:
            self._check_nut_connectivity()
            self._probe_temperature_sensor()
        except BaseException:
            self._release_writer_lock()
            raise

    def _init_battery_model_and_estimators(self, config: Config):
        """Initialize battery model, capacity estimator, RLS filters, and discharge handler."""
        model_path = config.model_dir / "model.json"
        new_model = not model_path.exists()
        self.battery_model = BatteryModel(
            model_path,
            capacity_ah=config.capacity_ah,
            soh_threshold=config.soh_alert_threshold,
        )
        self._validate_model()

        if new_model:
            if self.battery_model.get_battery_install_date() is None:
                self.battery_model.set_battery_install_date(datetime.now().strftime("%Y-%m-%d"))
            self.battery_model.save()  # Write defaults so tools (battery-health.py, MOTD) can read
        self.event_classifier = EventClassifier()

        self.capacity_estimator = CapacityEstimator(
            peukert_exponent=self.battery_model.get_peukert_exponent(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            nominal_power_watts=self.battery_model.get_nominal_power_watts(),
            capacity_ah=self.battery_model.get_capacity_ah(),
        )

        # Replay historical estimates so has_converged() survives restarts
        for estimate in self.battery_model.get_capacity_estimates():
            self.capacity_estimator.add_measurement(
                ah=estimate["ah_estimate"],
                timestamp=estimate["timestamp"],
                metadata=estimate["metadata"],
            )

        self.ir_reference_load_percent = self.battery_model.get_ir_reference_load()

        self.sag_tracker = SagTracker(
            battery_model=self.battery_model,
            rls_ir_k=ScalarRLS.from_dict(
                self.battery_model.get_rls_state("ir_k"), forgetting_factor=0.97
            ),
            ir_k=self.battery_model.get_ir_k(),
        )

        self.rls_peukert = ScalarRLS.from_dict(
            self.battery_model.get_rls_state("peukert"), forgetting_factor=0.97
        )

        self.discharge_handler = DischargeHandler(
            battery_model=self.battery_model,
            config=config,
            capacity_estimator=self.capacity_estimator,
            rls_peukert=self.rls_peukert,
            reference_load_percent=config.reference_load_percent,
            soh_threshold=config.soh_alert_threshold,
        )
        self.discharge_collector = DischargeCollector(
            battery_model=self.battery_model,
            config=config,
            discharge_handler=self.discharge_handler,
            ema_filter=self.ema_filter,
            journal=self.journal,
        )
        self.scheduler_manager = SchedulerManager(
            battery_model=self.battery_model,
            nut_client=self.nut_client,
            scheduling_config=config.scheduling or SchedulingConfig(),
            discharge_handler=self.discharge_handler,
            operational_journal_provider=lambda: self.journal,
        )

        self.exporter = VirtualUpsExporter(
            battery_model=self.battery_model,
            event_classifier=self.event_classifier,
            discharge_handler=self.discharge_handler,
            scheduler_manager=self.scheduler_manager,
            journal_health_provider=self._journal_health,
            virtual_ups_path=self.virtual_ups_path,
            health_path=self.health_path,
        )

    @staticmethod
    def _acquire_writer_lock(model_dir):
        """Take the process-wide writer lock before opening mutable state/output."""
        lock_path = model_dir / "monitor.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            if "fd" in locals():
                os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                raise RuntimeError(f"another monitor writer already owns {lock_path}") from exc
            raise RuntimeError(f"cannot acquire monitor writer lock {lock_path}: {exc}") from exc

    def _release_writer_lock(self) -> None:
        fd = getattr(self, "_writer_lock_fd", None)
        if fd is None:
            return
        self._writer_lock_fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __del__(self):
        # Covers constructor failures and test-created daemons that are not run.
        try:
            self._release_writer_lock()
        except Exception:
            pass

    def _journal_health(self) -> dict:
        """Expose bounded journal health to the optional health exporter."""
        if self.journal is None:
            return {
                "journal_healthy": False,
                "active_event_id": None,
                "journal_last_synced_seq": None,
                "journal_last_error": self.journal_error,
                "pending_replay": getattr(self, "_pending_replay", False),
                "recovered_partial_events": 0,
                "cycle_count": self.battery_model.get_cycle_count(),
                "cumulative_on_battery_sec": self.battery_model.get_cumulative_on_battery_sec(),
                "scientific_fingerprint": self.battery_model.scientific_fingerprint(),
                "baseline_scientific_fingerprint": self.baseline_scientific_fingerprint,
                "scientific_change_latched": self._fingerprint_alarm_latched,
                "startup_degraded": self.startup_degraded,
                "last_voltage_sag_observation": self._last_sag_observation,
                "last_event_disposition": None,
            }
        try:
            projection = self._journal_projection_for_poll()
        except (JournalError, OSError, TypeError, ValueError) as exc:
            self._journal_degraded("health", exc)
            projection = None
        health = self.journal.health
        counters = self._journal_counters(projection=projection)
        current_epoch = self.battery_model.get_battery_epoch_id()
        return {
            "journal_healthy": health.healthy,
            "active_event_id": health.open_event_id,
            "journal_last_synced_seq": health.last_synced_seq,
            "journal_last_error": health.last_error,
            "pending_replay": self._pending_replay,
            "recovered_partial_events": self._recovered_partial_events,
            "scientific_fingerprint": self.battery_model.scientific_fingerprint(),
            "baseline_scientific_fingerprint": self.baseline_scientific_fingerprint,
            "scientific_change_latched": self._fingerprint_alarm_latched,
            "startup_degraded": self.startup_degraded,
            "last_voltage_sag_observation": self._last_sag_observation,
            "last_event_disposition": self._last_event_disposition_from_projection(
                projection, current_epoch
            ),
            **counters,
        }

    @staticmethod
    def _last_event_disposition_from_projection(projection, current_epoch: str) -> Optional[str]:
        """Return only an explicit disposition from the latest current-epoch marker."""
        if projection is None:
            return None
        terminal_events = [
            event
            for event in projection.events.values()
            if event.start is not None
            and event.start.payload.get("battery_epoch_id") == current_epoch
            and event.end is not None
        ]
        if not terminal_events:
            return None
        record_positions = {id(record): index for index, record in enumerate(projection.records)}
        latest = max(
            terminal_events,
            key=lambda event: record_positions.get(
                id(event.applied if event.applied is not None else event.end), -1
            ),
        )
        if latest.applied is None:
            return None
        disposition = latest.applied.payload.get("disposition")
        return disposition if disposition in {"recorded_only", "applied", "rejected"} else None

    def _check_capture_only_fingerprint(self) -> None:
        """Alarm if scientific state changes while Release A is capture-only."""
        if not self.capture_only:
            return
        current = self.battery_model.scientific_fingerprint()
        if current != self.baseline_scientific_fingerprint:
            if self._fingerprint_alarm_latched:
                return
            self._fingerprint_alarm_latched = True
            logger.error(
                "Scientific model changed while capture_only is armed",
                extra={
                    "event_type": "capture_only_scientific_change",
                    "baseline_fingerprint": self.baseline_scientific_fingerprint,
                    "current_fingerprint": current,
                },
            )

    def _replay_closed_events(self) -> None:
        """Apply eligible closed events and repair exactly-once audit markers."""
        if self.journal is None:
            return
        try:
            projection = self.journal.replay()
            current_epoch = self.battery_model.get_battery_epoch_id()
            for event in projection.events.values():
                start = event.start
                if start is None or start.payload.get("battery_epoch_id") != current_epoch:
                    # Journal evidence is retained verbatim, but an event from
                    # another/unknown battery epoch cannot affect this model's
                    # replay, markers, pending state, or recovery counters.
                    continue
                if event.end is None:
                    continue
                end_payload = event.end.payload
                evidence = end_payload.get("evidence_class", "operational_partial")
                if (
                    evidence in {"operational_partial", "operational_gapped"}
                    or end_payload.get("lifecycle") == "closed_restart_recovered"
                ):
                    self._recovered_partial_events += 1
                    # Capture-only operational events are a terminal decision.
                    # The marker records that the event was retained without a
                    # scientific model application.
                    if event.applied is None:
                        self._mark_applied(
                            event.event_id,
                            self.battery_model.get_persisted_hash(),
                            "recorded_only",
                        )
                    continue
                if event.applied is not None:
                    continue
                completion = self._completion_from_projection(event)
                if completion is None:
                    self._pending_replay = True
                    continue
                result = self.discharge_handler.apply_completed_discharge(completion)
                if result.applied:
                    self._mark_applied(result.event_id, result.model_hash, "applied")
                elif result.already_applied:
                    self._mark_applied(
                        result.event_id,
                        self.battery_model.get_persisted_hash(),
                        "applied",
                    )
                elif result.skipped:
                    # A normal policy rejection is final for this event.  Only
                    # persistence/shape failures remain pending for operator repair.
                    if completion.evidence_class.startswith("operational"):
                        self._mark_applied(
                            completion.event_id,
                            self.battery_model.get_persisted_hash(),
                            "recorded_only",
                        )
                    else:
                        result_reasons = getattr(result, "eligibility_reasons", ())
                        if not isinstance(result_reasons, (tuple, list)):
                            result_reasons = ()
                        quality_rejected = any(
                            reason in {"soh_quality_rejected", "capacity_quality_rejected"}
                            for reason in result_reasons
                        )
                        if quality_rejected:
                            self._mark_applied(
                                completion.event_id,
                                self.battery_model.get_persisted_hash(),
                                "rejected",
                            )
                        else:
                            self._pending_replay = True
        except Exception as exc:
            self._pending_replay = True
            self._journal_degraded("replay", exc)

    def _completion_from_projection(self, event) -> Optional[CompletedDischarge]:
        end = event.end
        if end is None:
            return None
        voltages: list[float] = []
        times: list[float] = []
        loads: list[float] = []
        for record in event.samples:
            if record.payload.get("reboot_gap"):
                continue
            voltage = record.payload.get("ema_voltage")
            timestamp = record.payload.get("timestamp")
            load = record.payload.get("ema_load")
            if not all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in (voltage, timestamp, load)
            ):
                return None
            voltages.append(float(voltage))
            times.append(float(timestamp))
            loads.append(float(load))
        reasons = tuple(end.payload.get("eligibility_reasons", ()))
        return CompletedDischarge(
            event.event_id,
            str(end.payload.get("lifecycle", "closed_power_restored")),
            str(end.payload.get("evidence_class", "operational_partial")),
            tuple(voltages),
            tuple(times),
            tuple(loads),
            bool(end.payload.get("model_processing_eligible", False)),
            reasons,
        )

    def _mark_applied(
        self,
        event_id: Optional[str],
        model_hash: Optional[str],
        disposition: AppliedDisposition,
    ) -> None:
        # A missing identity means there is no durable event whose terminal
        # marker can be repaired.  Do not turn a journal-start failure into a
        # misleading pending-replay alarm.
        if not event_id or not model_hash:
            return
        if self.journal is None:
            self._pending_replay = True
            return
        try:
            self.journal.mark_applied(event_id, model_hash, disposition)
        except (JournalError, OSError) as exc:
            self._pending_replay = True
            self._journal_degraded("mark_applied", exc)

    def _journal_degraded(self, operation: str, exc: BaseException) -> None:
        self.journal_error = str(exc)[:512]
        if self.journal is not None:
            self.journal.mark_degraded(f"{operation}: {exc}")
        logger.error(
            "Discharge journal %s failed; continuing degraded: %s", operation, exc, exc_info=True
        )

    def _journal_counters(
        self, *, projection: JournalProjection | None | object = _JOURNAL_PROJECTION_UNSET
    ) -> dict[str, object]:
        if self.journal is None:
            return {
                "cycle_count": self.battery_model.get_cycle_count(),
                "cumulative_on_battery_sec": self.battery_model.get_cumulative_on_battery_sec(),
            }
        try:
            resolved_projection = projection
            if resolved_projection is _JOURNAL_PROJECTION_UNSET:
                resolved_projection = self._journal_projection_for_poll()
            if resolved_projection is None or not isinstance(
                resolved_projection, JournalProjection
            ):
                raise JournalError("journal projection unavailable")
            current_epoch = self.battery_model.get_battery_epoch_id()
            current_events = [
                event
                for event in resolved_projection.events.values()
                if event.start is not None
                and event.start.payload.get("battery_epoch_id") == current_epoch
            ]
            base_cycles = self.battery_model.get_cycle_count()
            base_seconds = self.battery_model.get_cumulative_on_battery_sec()
            if not isinstance(base_cycles, (int, float)) or not isinstance(
                base_seconds, (int, float)
            ):
                raise ValueError("invalid model counter baseline")
            duration = sum(
                self._observed_duration_from_projection(event) or 0.0 for event in current_events
            )
            return {
                "cycle_count": int(base_cycles) + len(current_events),
                "cumulative_on_battery_sec": float(base_seconds) + duration,
            }
        except (JournalError, OSError, TypeError, ValueError) as exc:
            self._journal_degraded("counters", exc)
            return {
                "cycle_count": self.battery_model.get_cycle_count(),
                "cumulative_on_battery_sec": self.battery_model.get_cumulative_on_battery_sec(),
            }

    def _journal_projection_for_poll(self) -> Optional[JournalProjection]:
        """Return one journal replay for the current poll's health exports.

        The journal remains the source of truth.  This small cache only shares
        the immutable projection between the virtual UPS and health writers in
        one acquisition tick; calls outside a poll still read a fresh replay.
        """
        if self.journal is None:
            return None
        if getattr(self, "_journal_projection_cache_active", False):
            projection = getattr(self, "_journal_projection_cache", None)
            if projection is None:
                projection = self.journal.replay()
                self._journal_projection_cache = projection
            return projection
        return self.journal.replay()

    @staticmethod
    def _observed_duration_from_projection(event) -> Optional[float]:
        """Calculate event duration without replaying the journal per event."""
        has_reboot_gap = any(record.payload.get("reboot_gap") for record in event.samples)
        if event.end is not None and not has_reboot_gap:
            observed = event.end.payload.get("observed_duration_sec")
            if (
                isinstance(observed, (int, float))
                and not isinstance(observed, bool)
                and math.isfinite(float(observed))
                and observed >= 0
            ):
                return float(observed)
        by_boot: dict[str, list[float]] = {}
        for record in event.samples:
            if record.payload.get("reboot_gap"):
                continue
            timestamp = record.payload.get("timestamp")
            if (
                isinstance(timestamp, (int, float))
                and not isinstance(timestamp, bool)
                and math.isfinite(float(timestamp))
            ):
                by_boot.setdefault(record.boot_id, []).append(float(timestamp))
        return sum(max(values) - min(values) for values in by_boot.values()) if by_boot else None

    def _validate_model(self):
        """Validate battery model state without mutating or repairing it."""
        lut = self.battery_model.get_lut()
        if len(lut) < 2:
            logger.warning(
                f"Model LUT has only {len(lut)} point(s); predictions will be inaccurate until calibration"
            )

        if self.battery_model.get_anchor_voltage() is None:
            logger.warning(
                "Model LUT missing anchor entry (soc=0.0, source='anchor'); SoH calculation may fail"
            )

        soh = self.battery_model.get_soh()
        if not (0.0 < soh <= 1.0):
            raise ValueError(
                f"Model SoH={soh!r} is invalid; refusing startup self-heal. "
                "Repair the scientific state through the sanctioned baseline reset."
            )

        capacity = self.battery_model.get_capacity_ah()
        if capacity <= 0:
            raise ValueError(f"Model capacity_ah={capacity} invalid; cannot compute runtime")

    def _check_nut_connectivity(self):
        """Verify NUT upsd is reachable before entering main loop."""
        try:
            _ = self.nut_client.get_ups_vars()
            logger.info(
                "NUT upsd reachable, polling started", extra={"event_type": "nut_reachable"}
            )
        except (socket.error, OSError, ConnectionError, TimeoutError, ValueError):
            logger.warning(
                f"NUT upsd unreachable at {self.config.nut_host}:{self.config.nut_port}, "
                f"will retry every {self.config.polling_interval}s",
                exc_info=True,
                extra={"event_type": "nut_unreachable"},
            )

    def _probe_temperature_sensor(self):
        """Check NUT for temperature variable at startup (one-time probe).

        Checks for standard NUT temperature variable names. If found, logs
        the sensor value. If absent, logs that thermal compensation is skipped
        (35°C assumed constant per v3.0 design).

        Does not raise — temperature is informational, not safety-critical.
        """
        _TEMPERATURE_VARS = ("ups.temperature", "battery.temperature", "ambient.temperature")
        try:
            ups_vars = self.nut_client.get_ups_vars()
        except (socket.error, OSError, ConnectionError, TimeoutError, ValueError):
            # NUT unreachable — _check_nut_connectivity already logged this
            return

        for var_name in _TEMPERATURE_VARS:
            if var_name in ups_vars:
                logger.info(
                    "Temperature sensor found: %s=%s°C",
                    var_name,
                    ups_vars[var_name],
                    extra={
                        "event_type": "temperature_sensor_found",
                        "temperature_var": var_name,
                        "temperature_celsius": ups_vars[var_name],
                    },
                )
                return

        logger.info(
            "Temperature sensor unavailable, skipping thermal compensation",
            extra={"event_type": "temperature_sensor_unavailable"},
        )

    def _handle_event_transition(self):
        """Execute actions based on event transitions.

        Mutates: current_metrics.shutdown_imminent, current_metrics.ups_status_override.
        On OB→OL transition: triggers _update_battery_health (SoH, LUT, capacity).
        """
        event_type = self.current_metrics.event_type

        if event_type == EventType.BLACKOUT_REAL:
            time_rem = self.current_metrics.time_rem_minutes
            if time_rem is not None and time_rem < self.shutdown_threshold_minutes:
                logger.warning(
                    f"Real blackout: time_rem={time_rem:.1f}min < threshold {self.shutdown_threshold_minutes}min; "
                    f"prepare LB flag",
                    extra={
                        "event_type": "shutdown_imminent",
                        "time_rem_minutes": f"{time_rem:.1f}",
                        "threshold_minutes": self.shutdown_threshold_minutes,
                    },
                )
                self.current_metrics.shutdown_imminent = True
            else:
                self.current_metrics.shutdown_imminent = False

        if event_type == EventType.BLACKOUT_TEST:
            logger.info(
                "Battery test detected; collecting calibration data, no shutdown",
                extra={"event_type": "battery_test_detected"},
            )
            self.current_metrics.shutdown_imminent = False

        if event_type is not None:
            self.current_metrics.ups_status_override = compute_ups_status_override(
                event_type,
                self.current_metrics.time_rem_minutes or 0,
                self.shutdown_threshold_minutes,
            )

    def _update_battery_health(self, completion: Optional[CompletedDischarge] = None):
        """Apply one completion through the authoritative Phase 2 boundary."""
        if completion is None:
            return
        try:
            result = self.discharge_handler.apply_completed_discharge(completion)
            if result.applied:
                self._mark_applied(result.event_id, result.model_hash, "applied")
            elif result.already_applied:
                self._mark_applied(
                    result.event_id,
                    self.battery_model.get_persisted_hash(),
                    "applied",
                )
            elif (
                result.skipped
                and not completion.model_processing_eligible
                and completion.evidence_class.startswith("operational")
            ):
                # Ordinary operational capture is complete as soon as the
                # durable end marker exists.  Scientific ineligibility must
                # not leave the event pending until the next restart, but a
                # scientific calculation/shape failure remains unmarked for
                # its retry/audit path.
                self._mark_applied(
                    completion.event_id,
                    self.battery_model.get_persisted_hash(),
                    "recorded_only",
                )
            elif result.skipped:
                result_reasons = getattr(result, "eligibility_reasons", ())
                if not isinstance(result_reasons, (tuple, list)):
                    result_reasons = ()
                if any(
                    reason in {"soh_quality_rejected", "capacity_quality_rejected"}
                    for reason in result_reasons
                ):
                    self._mark_applied(
                        completion.event_id,
                        self.battery_model.get_persisted_hash(),
                        "rejected",
                    )
                else:
                    self._pending_replay = True
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            logger.error(
                "Discharge application failed; capture remains durable: %s", exc, exc_info=True
            )
        finally:
            self.discharge_collector.finalize(
                completion.times[-1] if completion.times else time.time()
            )
            self.discharge_collector.reset_buffer()

    def _auto_calibrate_peukert(self, current_soh: float):
        """Delegate to DischargeHandler."""
        self.discharge_handler._auto_calibrate_peukert(current_soh, self.discharge_collector.buffer)

    def _log_discharge_prediction(self):
        """Delegate to DischargeHandler."""
        self.discharge_handler._log_discharge_prediction(
            self.discharge_collector.buffer, self.current_metrics.soc
        )

    # --- Battery baseline reset ---

    def _reset_battery_baseline(self):
        """Apply the sanctioned operator baseline reset transaction."""
        if self.journal is None:
            raise RuntimeError("cannot reset battery baseline: discharge journal unavailable")
        journal_health = self.journal.health
        if not journal_health.healthy:
            raise RuntimeError("cannot reset battery baseline: discharge journal unhealthy")
        journal_event_open = isinstance(journal_health.open_event_id, str) and bool(
            journal_health.open_event_id
        )
        collector_event_open = bool(
            self.discharge_collector.is_collecting or self.discharge_collector.event_id
        )
        if journal_event_open or collector_event_open:
            raise RuntimeError("cannot reset battery baseline while a discharge event is open")

        # Construct every replacement runtime object before touching the
        # persisted model or live handler.  If the model transaction rejects
        # the reset, the old estimator/RLS/tracking remain intact.
        fresh_capacity_estimator = CapacityEstimator(
            peukert_exponent=DEFAULT_PEUKERT_EXPONENT,
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            nominal_power_watts=self.battery_model.get_nominal_power_watts(),
            capacity_ah=self.battery_model.get_capacity_ah(),
        )
        fresh_rls_peukert = ScalarRLS(theta=DEFAULT_PEUKERT_EXPONENT, P=1.0)

        self.battery_model.reset_baseline(event_open=False)

        # The model reset is the commit boundary.  From here on all runtime
        # scientific state must describe only the replacement battery.
        self.capacity_estimator = fresh_capacity_estimator
        self.discharge_handler.capacity_estimator = fresh_capacity_estimator
        self.rls_peukert = fresh_rls_peukert
        self.discharge_handler.rls_peukert = fresh_rls_peukert
        self.ir_reference_load_percent = self.battery_model.get_ir_reference_load()
        self.discharge_handler.discharge_predicted_runtime = None
        self.discharge_handler.has_logged_baseline_lock = False
        self.discharge_handler.last_days_since_deep = None
        self.discharge_handler.last_ir_trend_rate = 0.0
        self.discharge_handler.last_cycle_budget_remaining = 0
        self.discharge_handler.last_discharge_timestamp = None
        self.has_logged_baseline_lock = False
        self.baseline_scientific_fingerprint = self.battery_model.scientific_fingerprint()
        self._fingerprint_alarm_latched = False
        self.sag_tracker.reset_rls(theta=0.015, P=1.0)

    def _signal_handler(self, signum, frame):
        """Record a stop request only; persistence belongs to normal shutdown."""
        logger.info(
            f"Received signal {signum}; shutting down",
            extra={"event_type": "shutdown", "signal": signum},
        )
        self.running = False

    # --- Pipeline stages ---

    @staticmethod
    def _physical_reply_is_valid(ups_data) -> bool:
        """Return whether a NUT reply is complete enough for any pipeline work.

        A numeric voltage/load pair without a status is not a usable physical
        sample: retaining the previous status in that case could overwrite a
        real OB/LB safety state with a synthetic online value.  Accept numeric
        strings here because test doubles and some NUT adapters expose parsed
        values that way; the downstream EMA still performs its own strict
        numeric check before mutating its filter.
        """
        if not isinstance(ups_data, Mapping):
            return False
        status = ups_data.get("ups.status")
        if not isinstance(status, str) or not status.strip():
            return False

        numeric_values = {}
        for field in ("battery.voltage", "ups.load"):
            value = ups_data.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                return False
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                return False
            if not math.isfinite(numeric_value):
                return False
            numeric_values[field] = numeric_value

        return (
            8.0 <= numeric_values["battery.voltage"] <= 15.0
            and 0.0 <= numeric_values["ups.load"] <= 100.0
        )

    def _update_ema(self, ups_data):
        """Feed voltage/load into EMA filter, log stabilization event."""
        voltage = ups_data.get("battery.voltage")
        load = ups_data.get("ups.load")
        if (
            not isinstance(voltage, (int, float))
            or isinstance(voltage, bool)
            or not isinstance(load, (int, float))
            or isinstance(load, bool)
            or not math.isfinite(float(voltage))
            or not math.isfinite(float(load))
        ):
            return None, None

        # Voltage bounds check (8.0-15.0V) and load bounds check (0-100%)
        if not (8.0 <= float(voltage) <= 15.0):
            logger.warning(
                f"Voltage {voltage:.2f}V out of bounds [8.0-15.0V]; skipping sample",
                extra={
                    "event_type": "sensor_out_of_bounds",
                    "field": "voltage",
                    "value": f"{voltage:.2f}",
                },
            )
            return None, None
        if not (0 <= float(load) <= 100):
            logger.warning(
                f"Load {load:.1f}% out of bounds [0-100%]; skipping sample",
                extra={
                    "event_type": "sensor_out_of_bounds",
                    "field": "load",
                    "value": f"{load:.1f}",
                },
            )
            return None, None

        self.ema_filter.add_sample(voltage, load)
        self.poll_count += 1
        if self.ema_filter.stabilized and not self._stabilization_logged:
            logger.info(
                f"EMA buffer stabilized after {self.poll_count} samples, IR compensation active",
                extra={"event_type": "ema_stabilized", "poll_count": self.poll_count},
            )
            self._stabilization_logged = True
        return voltage, load

    def _classify_event(self, ups_data):
        """Classify UPS event and log transitions."""
        ups_status = ups_data.get("ups.status")
        input_voltage = ups_data.get("input.voltage")
        if ups_status is None or input_voltage is None:
            logger.debug(
                f"Missing NUT fields: ups.status={ups_status}, input.voltage={input_voltage}"
            )
            return
        event_type = self.event_classifier.classify(ups_status, input_voltage)
        self.current_metrics.event_type = event_type
        self.current_metrics.transition_occurred = self.event_classifier.transition_occurred

    def _log_soc_change(self, soc, soc_prev):
        """Log SoC when it changes by more than 5% or on first reading."""
        if soc_prev is not None and abs(soc - soc_prev) <= 0.05:
            return
        if soc_prev is not None:
            logger.info(
                f"SoC updated: {soc_prev * 100:.0f}% \u2192 {soc * 100:.0f}%",
                extra={
                    "event_type": "soc_change",
                    "soc_old": f"{soc_prev * 100:.0f}",
                    "soc_new": f"{soc * 100:.0f}",
                },
            )
        else:
            logger.info(
                f"SoC initial: {soc * 100:.0f}%",
                extra={"event_type": "soc_initial", "soc": f"{soc * 100:.0f}"},
            )
        self._last_logged_soc = soc

    def _compute_metrics(self):
        """Calculate SoC, charge%, and runtime from EMA values. Returns (battery_charge, time_rem)."""
        v_ema = self.ema_filter.voltage
        l_ema = self.ema_filter.load
        if not self.ema_filter.stabilized:
            return None, None
        assert l_ema is not None

        v_norm = ir_compensate(v_ema, l_ema, self.ir_reference_load_percent, self.sag_tracker.ir_k)
        if v_norm is None:
            return None, None
        self._last_v_norm = v_norm

        soc = soc_from_voltage(v_norm, self.battery_model.get_lut())
        battery_charge = charge_percentage(soc)
        time_rem = runtime_minutes(
            soc,
            l_ema,
            self.battery_model.get_capacity_ah(),
            self.battery_model.get_soh(),
            peukert_exponent=self.battery_model.get_peukert_exponent(),
            nominal_voltage=self.battery_model.get_nominal_voltage(),
            nominal_power_watts=self.battery_model.get_nominal_power_watts(),
        )

        self.current_metrics.soc = soc
        self.current_metrics.battery_charge = battery_charge
        self.current_metrics.time_rem_minutes = time_rem
        self.current_metrics.timestamp = datetime.now(timezone.utc)

        self._log_soc_change(soc, self._last_logged_soc)
        if self._last_logged_time_rem is None or abs(time_rem - self._last_logged_time_rem) > 1.0:
            logger.debug(
                f"Remaining runtime: {time_rem:.1f} minutes",
                extra={"event_type": "runtime_change", "time_rem_minutes": f"{time_rem:.1f}"},
            )
            self._last_logged_time_rem = time_rem

        return battery_charge, time_rem

    def _log_status(self, battery_charge, time_rem, poll_latency_ms=None):
        """Log periodic status line with all key metrics."""
        v_ema = self.ema_filter.voltage
        l_ema = self.ema_filter.load
        v_norm = getattr(self, "_last_v_norm", None)

        v_norm_str = f"{v_norm:.2f}V" if v_norm is not None else "N/A"
        charge_str = f"{battery_charge}%" if battery_charge is not None else "N/A"
        time_rem_str = f"{time_rem:.1f}min" if time_rem is not None else "N/A"
        event_type = self.current_metrics.event_type
        event_str = event_type.name if event_type else "N/A"
        latency_str = f"{poll_latency_ms:.0f}ms" if poll_latency_ms is not None else "N/A"
        logger.debug(
            f"Poll {self.poll_count}: V_ema={v_ema:.2f}V, L_ema={l_ema:.1f}%, "
            f"V_norm={v_norm_str}, charge={charge_str}, time_rem={time_rem_str}, "
            f"event={event_str}, stabilized={self.ema_filter.stabilized}, "
            f"nut_latency={latency_str}, discharge_buf={len(self.discharge_collector.buffer.voltages)}",
            extra={
                "event_type": "poll_status",
                "poll_count": str(self.poll_count),
                "v_ema": f"{v_ema:.2f}" if v_ema is not None else "N/A",
                "v_norm": f"{v_norm:.2f}" if v_norm is not None else "N/A",
                "load_pct": f"{l_ema:.1f}" if l_ema is not None else "N/A",
                "charge_pct": charge_str,
                "time_rem": time_rem_str,
                "soh": f"{self.battery_model.get_soh():.4f}",
                "event": event_str,
                "nut_latency_ms": f"{poll_latency_ms:.0f}"
                if poll_latency_ms is not None
                else "N/A",
            },
        )

    # --- Main loop ---

    def _reporting_due(self, monotonic_timestamp: float) -> bool:
        """Return true on the elapsed-time status deadline, including first poll."""
        interval = max(1.0, float(self.config.reporting_interval))
        if self._next_reporting_deadline is None:
            self._next_reporting_deadline = monotonic_timestamp + interval
            return True
        if monotonic_timestamp < self._next_reporting_deadline:
            return False
        # Preserve a stable cadence while recovering from a slow poll; do not
        # use poll-count modulo because NUT latency and errors are variable.
        while self._next_reporting_deadline <= monotonic_timestamp:
            self._next_reporting_deadline += interval
        return True

    def _poll_once(self) -> None:
        """Execute one acquisition tick and always emit one watchdog heartbeat."""
        # Keep this cadence guarantee outside the data-validation branches:
        # NUT outages and partial replies must not create a systemd restart
        # loop.  The cache is scoped to this same tick so both exporters share
        # one immutable journal projection.
        self._journal_projection_cache_active = True
        self._journal_projection_cache = None
        try:
            self._poll_once_inner()
        finally:
            self._journal_projection_cache_active = False
            self._journal_projection_cache = None
            sd_notify("WATCHDOG=1")

    def _poll_once_inner(self) -> None:
        """Execute a single poll cycle: fetch UPS data, update metrics, write outputs."""
        poll_started_monotonic = time.monotonic()
        ups_data = self.nut_client.get_ups_vars()
        # The observation clocks belong to the successful NUT response, not
        # the beginning of a potentially slow socket read.
        poll_monotonic = time.monotonic()
        timestamp = time.time()
        poll_latency_ms = (poll_monotonic - poll_started_monotonic) * 1000

        if not self._startup_logged:
            assert self._startup_time is not None
            startup_delta_ms = (time.monotonic() - self._startup_time) * 1000
            logger.info(
                f"First successful poll completed: startup took {startup_delta_ms:.0f}ms",
                extra={"event_type": "startup_complete", "startup_ms": f"{startup_delta_ms:.0f}"},
            )
            self._startup_logged = True

        if not self._physical_reply_is_valid(ups_data):
            logger.warning(
                f"Poll {self.poll_count}: physical UPS response is incomplete or invalid",
                extra={
                    "event_type": "invalid_physical_reply",
                    "poll_count": self.poll_count,
                },
            )
            self.startup_degraded = True
            sd_notify("STATUS=degraded: physical UPS response incomplete; outputs are not fresh")
            return

        voltage, load = self._update_ema(ups_data)
        if voltage is None or load is None:
            logger.warning(
                f"Poll {self.poll_count}: Missing voltage or load data after validation",
                extra={"event_type": "missing_poll_data", "poll_count": self.poll_count},
            )
            self.startup_degraded = True
            sd_notify("STATUS=degraded: physical UPS response incomplete; outputs are not fresh")
            return

        physical_poll_valid = True

        self._consecutive_errors = 0  # Reset after validated poll data

        self._classify_event(ups_data)
        event_type = self.current_metrics.event_type
        computed_metrics = None
        if event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST):
            computed_metrics = self._compute_metrics()
        sag_observation = self.sag_tracker.track(
            voltage,
            event_type=self.current_metrics.event_type,
            transition_occurred=self.event_classifier.transition_occurred,
            current_load=self.ema_filter.load,
        )
        if sag_observation is not None:
            # Keep the immutable observation on the existing health provider
            # path.  It is deliberately not promoted to a model write or a new
            # journal record type in capture-only Release A.
            self._last_sag_observation = {
                "observed_at": sag_observation.observed_at.isoformat(),
                "event_type": sag_observation.event_type,
                "voltage_before": sag_observation.voltage_before,
                "voltage_sag": sag_observation.voltage_sag,
                "load_percent": sag_observation.load_percent,
                "apparent_r_internal_ohm": sag_observation.apparent_r_internal_ohm,
            }
        completion = self.discharge_collector.track(
            voltage,
            timestamp,
            self.current_metrics.event_type,
            self.current_metrics,
            raw_ups_data=ups_data,
            monotonic_timestamp=poll_monotonic,
            sag_observation=sag_observation,
        )
        if completion is not None:
            self._update_battery_health(completion)

        event_type = self.current_metrics.event_type
        is_discharging = event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)

        # Event transition handling runs EVERY poll (not gated)
        self._handle_event_transition()
        if self._reporting_due(poll_monotonic):
            logger.debug("Metrics gate: status/reporting deadline reached")
            if computed_metrics is None and is_discharging:
                battery_charge, time_rem = self._compute_metrics()
            elif computed_metrics is not None:
                battery_charge, time_rem = computed_metrics
            else:
                battery_charge = self.current_metrics.battery_charge
                time_rem = self.current_metrics.time_rem_minutes
            self._log_status(battery_charge, time_rem, poll_latency_ms)
        else:
            battery_charge = self.current_metrics.battery_charge
            time_rem = self.current_metrics.time_rem_minutes

        # Virtual UPS and health are safety/operational surfaces, so they are
        # refreshed on every one-second acquisition tick independently of the
        # human-readable 60-second status cadence.
        self._check_capture_only_fingerprint()
        virtual_ups_ok = self.exporter.write_virtual_ups(
            ups_data, battery_charge, time_rem, self.current_metrics
        )

        health_ok = self.exporter.write_health_snapshot(
            poll_latency_ms, self.current_metrics, self._consecutive_errors
        )
        self.scheduler_manager.run_daily(datetime.now(timezone.utc), self.current_metrics)

        outputs_fresh = physical_poll_valid and bool(virtual_ups_ok) and bool(health_ok)
        if outputs_fresh:
            self.startup_degraded = False
            if self._ready_sent:
                sd_notify("STATUS=ready: physical UPS poll and outputs are fresh")
            else:
                # READY is a postcondition of the first complete physical poll and
                # both output writes, never a promise that NUT is reachable.
                sd_notify("READY=1\nSTATUS=ready: physical UPS poll and outputs are fresh")
                self._ready_sent = True
        else:
            self.startup_degraded = True
            sd_notify("STATUS=degraded: physical UPS status or output is not fresh")

    def run(self):
        """Run the poll loop and perform durable, non-scientific shutdown."""
        try:
            self._run_loop()
        finally:
            try:
                self.discharge_collector.shutdown(time.time())
            except (OSError, TypeError, ValueError) as exc:
                logger.error(
                    "Failed to close capture during normal shutdown: %s", exc, exc_info=True
                )
            finally:
                if self.journal is not None:
                    self.journal.close()
                self._release_writer_lock()

    def _run_loop(self):
        """
        Main polling loop.

        Polls UPS every POLL_INTERVAL seconds, processes data through the
        pipeline: EMA → event classification → sag/discharge tracking →
        metrics → virtual UPS output. Runs until SIGTERM/SIGINT.
        """
        logger.info("ups-battery-monitor %s starting", DAEMON_VERSION)
        self.poll_count = 0
        self._stabilization_logged = False
        self._startup_logged = False
        self._consecutive_errors = 0
        self._startup_time = time.monotonic()
        self._next_reporting_deadline = None
        next_poll_deadline = self._startup_time

        while self.running:
            now = time.monotonic()
            if now < next_poll_deadline:
                time.sleep(next_poll_deadline - now)
                if not self.running:
                    break
            next_poll_deadline += max(0.1, float(self.config.polling_interval))
            if next_poll_deadline <= time.monotonic():
                next_poll_deadline = time.monotonic() + max(
                    0.1, float(self.config.polling_interval)
                )
            try:
                self._poll_once()
            except KeyboardInterrupt:
                logger.info("Interrupted by user")
                break
            except (socket.error, OSError, ConnectionError, TimeoutError) as e:
                self._consecutive_errors += 1
                self.startup_degraded = True
                # READY is deliberately not retracted.  STATUS still exposes
                # the post-READY outage so operators see the degraded state.
                sd_notify("STATUS=degraded: physical UPS unavailable; outputs are not fresh")
                self.sag_tracker.reset_idle()
                error_type = type(e).__name__
                error_type_changed = error_type != getattr(self, "_last_error_type", None)
                self._last_error_type = error_type
                reporting_interval_polls = (
                    self.config.reporting_interval // self.config.polling_interval
                )
                should_log = (
                    self._consecutive_errors <= ERROR_LOG_BURST
                    or error_type_changed
                    or self._consecutive_errors % reporting_interval_polls == 0
                )
                if should_log:
                    logger.error(
                        f"Transient error in polling loop ({self._consecutive_errors} consecutive): {e}",
                        exc_info=(
                            self._consecutive_errors <= ERROR_LOG_BURST or error_type_changed
                        ),
                        extra={
                            "event_type": "poll_error",
                            "consecutive_errors": self._consecutive_errors,
                            "error_class": error_type,
                        },
                    )
            except Exception as e:
                # Non-transient error (AttributeError, TypeError, KeyError, etc.)
                # indicates a bug — fail fast rather than silently retrying forever
                logger.critical(
                    f"Bug in polling loop: {e}",
                    exc_info=True,
                    extra={
                        "event_type": "poll_bug",
                        "error_class": type(e).__name__,
                    },
                )
                raise

        logger.info("Polling loop ended; daemon shutting down")


def parse_args(args=None):
    """Parse command-line arguments.

    Args:
        args: List of arguments to parse (defaults to sys.argv[1:] if None)
              Used by tests to inject specific argument sequences.

    Returns:
        Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        description="UPS Battery Monitor Daemon", prog="ups-battery-monitor"
    )
    parser.add_argument(
        "--new-battery",
        action="store_true",
        help="Signal that a new battery has been installed; daemon will use this for next discharge measurement",
    )
    return parser.parse_args(args)


def main():
    """Entry point for daemon."""
    args = parse_args()

    try:
        config = load_config()
        daemon = MonitorDaemon(
            config,
            virtual_ups_path=Path("/run/ups-battery-monitor/ups-virtual.dev"),
            health_path=Path("/run/ups-battery-monitor/ups-health.json"),
        )
        if args.new_battery:
            daemon._reset_battery_baseline()
        daemon.run()
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
