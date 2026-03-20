"""Discharge sample collector — accumulation, cooldown, and calibration writes.

Extracted from MonitorDaemon to reduce its responsibility surface (ARCH-05).
DischargeCollector owns the discharge buffer lifecycle, cooldown state machine,
and calibration point writes — all stateful collaborator behavior that does not
belong inline in the daemon orchestration loop.
"""

import logging
from typing import Optional

from src.model import BatteryModel
from src.monitor_config import DischargeBuffer, DISCHARGE_BUFFER_MAX_SAMPLES, Config
from src.soc_predictor import soc_from_voltage
from src.event_classifier import EventType

logger = logging.getLogger('ups-battery-monitor')


class DischargeCollector:
    """Discharge buffer lifecycle, cooldown state machine, and calibration writes.

    Extracted from MonitorDaemon to own all discharge collection state:
    discharge_buffer, _discharge_start_time, _discharge_buffer_clear_countdown,
    and _calibration_last_written_index.

    Usage:
        collector = DischargeCollector(battery_model, config, discharge_handler, ema_filter)
        # Each poll:
        cooldown_expired = collector.track(voltage, timestamp, event_type, current_metrics)
        if cooldown_expired:
            _update_battery_health()
        # After _update_battery_health() processes the buffer:
        collector.reset_buffer()
        # On OB->OL finalization (non-cooldown path):
        collector.finalize(timestamp)
    """

    def __init__(
        self,
        battery_model: BatteryModel,
        config: Config,
        discharge_handler,   # DischargeHandler — for discharge_predicted_runtime handoff
        ema_filter,          # EMAFilter — for stabilized check and load reads
    ):
        """Initialize DischargeCollector.

        Args:
            battery_model: Persistent battery model — used for increment_cycle_count(),
                add_on_battery_time(), calibration_write(), calibration_batch_flush(),
                get_lut().
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

        self.discharge_buffer = DischargeBuffer()
        self._discharge_start_time: Optional[float] = None
        self._discharge_buffer_clear_countdown: Optional[int] = None
        self._calibration_last_written_index: int = 0

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

    def track(
        self,
        voltage: float,
        timestamp: float,
        event_type,
        current_metrics,
    ) -> bool:
        """Drive the discharge state machine for one poll tick.

        Handles: cooldown management, sample accumulation, calibration writes,
        and finalization when transitioning back to OL.

        Args:
            voltage: Current EMA voltage reading.
            timestamp: Unix timestamp for this poll.
            event_type: Classified event (EventType enum).
            current_metrics: CurrentMetrics with previous_event_type and time_rem_minutes.

        Returns:
            True if cooldown expired and caller must call _update_battery_health(),
            False otherwise.
        """
        previous_event_type = current_metrics.previous_event_type

        if self._handle_discharge_cooldown(event_type, previous_event_type):
            return True

        is_discharging = event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)

        if is_discharging:
            if not self.discharge_buffer.collecting:
                self._start_discharge_collection(timestamp, current_metrics)
            if voltage is not None:
                if len(self.discharge_buffer.voltages) >= DISCHARGE_BUFFER_MAX_SAMPLES:
                    logger.warning(
                        f"Discharge buffer capped at {DISCHARGE_BUFFER_MAX_SAMPLES} samples",
                        extra={
                            'event_type': 'discharge_buffer_capped',
                            'max_samples': DISCHARGE_BUFFER_MAX_SAMPLES,
                        },
                    )
                else:
                    self.discharge_buffer.voltages.append(voltage)
                    self.discharge_buffer.times.append(timestamp)
                    load = self.ema_filter.load if self.ema_filter.load is not None else 0.0
                    self.discharge_buffer.loads.append(load)
                self._write_calibration_points(event_type)
        else:
            if self.discharge_buffer.collecting:
                self.finalize(timestamp)

        return False

    def finalize(self, timestamp: float) -> None:
        """End discharge collection: record on-battery time and reset buffer state.

        Called when transitioning out of discharging state (non-cooldown path).

        Args:
            timestamp: Unix timestamp when collection ended.
        """
        if self._discharge_start_time is not None:
            on_battery_sec = timestamp - self._discharge_start_time
            self.battery_model.add_on_battery_time(on_battery_sec)
            self._discharge_start_time = None
        self.discharge_buffer.collecting = False
        self._calibration_last_written_index = 0

    def reset_buffer(self) -> None:
        """Replace discharge_buffer with a fresh DischargeBuffer.

        Called by MonitorDaemon after _update_battery_health() processes the buffer.
        """
        self.discharge_buffer = DischargeBuffer()

    # ------------------------------------------------------------------
    # Private methods
    # ------------------------------------------------------------------

    def _start_discharge_collection(self, timestamp: float, current_metrics) -> None:
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
        self._discharge_start_time = timestamp
        # cycle_count counts OL→OB transitions (including flicker),
        # matching enterprise "transfer count" metric (Eaton/APC). This is
        # NOT the same as discharge events (which require 300s+ duration).
        # Actual battery wear proxy = cumulative_on_battery_sec.
        self.battery_model.increment_cycle_count()
        # Snapshot predicted runtime at OB start for prediction error logging
        if self.ema_filter.stabilized and current_metrics.time_rem_minutes is not None:
            self.discharge_handler.discharge_predicted_runtime = current_metrics.time_rem_minutes
        else:
            self.discharge_handler.discharge_predicted_runtime = None
        logger.info(
            f"Starting discharge buffer collection ({event_type.name}), "
            f"cycle #{self.battery_model.get_cycle_count()}",
            extra={
                'event_type': 'discharge_start',
                'discharge_type': event_type.name,
                'cycle_count': self.battery_model.get_cycle_count(),
            },
        )

    def _handle_discharge_cooldown(self, event_type, previous_event_type) -> bool:
        """Manage 60s cooldown timer after OB→OL transition.

        OB→OL→OB within 60s is treated as a single discharge event (power flicker).
        Returns True if cooldown expired — caller must call _update_battery_health()
        and return (do NOT continue processing the current tick).

        Args:
            event_type: Current event type.
            previous_event_type: Event type from the previous poll.

        Returns:
            True if cooldown expired (buffer ready for processing), False otherwise.
        """
        is_discharging = event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST)

        if not is_discharging:
            if previous_event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST):
                logger.info(
                    "Power loss detected; starting 60s discharge cooldown",
                    extra={'event_type': 'discharge_cooldown_start'},
                )
                self._discharge_buffer_clear_countdown = 60

        if is_discharging and self._discharge_buffer_clear_countdown is not None:
            logger.info(
                "Power restored during cooldown; treating as discharge continuation",
                extra={'event_type': 'discharge_cooldown_cancelled'},
            )
            self._discharge_buffer_clear_countdown = None

        if self._discharge_buffer_clear_countdown is not None:
            self._discharge_buffer_clear_countdown -= self.config.polling_interval
            if self._discharge_buffer_clear_countdown <= 0:
                logger.info(
                    "Cooldown expired (60s OL confirmed); clearing discharge buffer and calling _update_battery_health",
                    extra={'event_type': 'discharge_cooldown_expired'},
                )
                return True

        return False

    def _write_calibration_points(self, event_type) -> None:
        """Flush accumulated discharge points to LUT every reporting_interval during any blackout.

        Args:
            event_type: Current event type (used for logging context).
        """
        reporting_interval_polls = self.config.reporting_interval // self.config.polling_interval
        if (len(self.discharge_buffer.voltages) - self._calibration_last_written_index
                < reporting_interval_polls):
            return

        for i in range(self._calibration_last_written_index, len(self.discharge_buffer.voltages)):
            try:
                v = self.discharge_buffer.voltages[i]
                t = self.discharge_buffer.times[i]
                soc_est = soc_from_voltage(v, self.battery_model.get_lut())
                self.battery_model.calibration_write(v, soc_est, t)
                self._calibration_last_written_index = i + 1
            except (KeyError, ValueError, OSError) as e:
                logger.error(
                    f"Calibration write failed at index {i}: {e}",
                    exc_info=True,
                )
                self._calibration_last_written_index = i + 1
                continue

        # Batch flush: persist all accumulated points once per reporting_interval
        points_written = self._calibration_last_written_index
        if points_written > 0:
            try:
                self.battery_model.calibration_batch_flush()
                logger.info(
                    f"Batch flushed {points_written} calibration points to disk",
                    extra={
                        'event_type': 'calibration_batch_flush',
                        'points_written': points_written,
                    },
                )
            except OSError as e:
                logger.error(
                    f"Calibration batch flush failed: {e}",
                    exc_info=True,
                    extra={'event_type': 'calibration_flush_failed'},
                )
