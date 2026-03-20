"""Unit tests for DischargeCollector — accumulation, cooldown, calibration, finalize, properties.

Tests the DischargeCollector class directly without constructing MonitorDaemon.
BatteryModel, Config, DischargeHandler, and EMAFilter are mocked.
"""

import pytest
from unittest.mock import MagicMock, call, patch
from src.discharge_collector import DischargeCollector
from src.monitor_config import DischargeBuffer, DISCHARGE_BUFFER_MAX_SAMPLES, Config
from src.event_classifier import EventType


def make_collector(polling_interval=10, reporting_interval=60, reference_load_percent=20.0):
    """Build a DischargeCollector with mocked dependencies."""
    mock_model = MagicMock()
    mock_model.get_lut.return_value = [
        {"v": 10.5, "soc": 0.0, "source": "anchor"},
        {"v": 11.0, "soc": 0.25, "source": "standard"},
        {"v": 12.0, "soc": 0.5, "source": "standard"},
        {"v": 13.0, "soc": 1.0, "source": "standard"},
    ]
    mock_config = MagicMock(spec=Config)
    mock_config.polling_interval = polling_interval
    mock_config.reporting_interval = reporting_interval
    mock_config.reference_load_percent = reference_load_percent
    mock_handler = MagicMock()
    mock_ema = MagicMock()
    mock_ema.stabilized = True
    mock_ema.load = 25.0
    collector = DischargeCollector(
        battery_model=mock_model,
        config=mock_config,
        discharge_handler=mock_handler,
        ema_filter=mock_ema,
    )
    return collector, mock_model, mock_config, mock_handler, mock_ema


def make_metrics(event_type=EventType.BLACKOUT_REAL, previous_event_type=EventType.ONLINE,
                 time_rem_minutes=30.0):
    """Build a mock CurrentMetrics object."""
    m = MagicMock()
    m.event_type = event_type
    m.previous_event_type = previous_event_type
    m.time_rem_minutes = time_rem_minutes
    return m


# ------------------------------------------------------------------
# Properties
# ------------------------------------------------------------------

def test_is_collecting_initially_false():
    """is_collecting property reflects buffer.collecting initial state (False)."""
    collector, *_ = make_collector()
    assert collector.is_collecting is False


def test_buffer_property_returns_discharge_buffer():
    """buffer property returns the internal DischargeBuffer instance."""
    collector, *_ = make_collector()
    buf = collector.buffer
    assert isinstance(buf, DischargeBuffer)
    assert buf.collecting is False


def test_reset_buffer_replaces_with_fresh():
    """reset_buffer() replaces discharge_buffer with a fresh DischargeBuffer."""
    collector, *_ = make_collector()
    # Dirty the buffer
    collector.discharge_buffer.collecting = True
    collector.discharge_buffer.voltages = [12.0, 11.5]
    collector.reset_buffer()
    assert collector.discharge_buffer.collecting is False
    assert collector.discharge_buffer.voltages == []
    assert collector.is_collecting is False


# ------------------------------------------------------------------
# track() — accumulation
# ------------------------------------------------------------------

def test_track_ob_starts_collection():
    """track() on OB event with non-collecting buffer starts collection (collecting=True)."""
    collector, mock_model, *_ = make_collector()
    metrics = make_metrics(event_type=EventType.BLACKOUT_REAL)
    collector.track(12.0, 1000.0, EventType.BLACKOUT_REAL, metrics)
    assert collector.is_collecting is True


def test_track_ob_appends_voltage_timestamp_load():
    """track() on OB event appends voltage, timestamp, and load to buffer arrays."""
    collector, *_ = make_collector()
    metrics = make_metrics(event_type=EventType.BLACKOUT_REAL)
    collector.track(12.0, 1000.0, EventType.BLACKOUT_REAL, metrics)
    assert 12.0 in collector.buffer.voltages
    assert 1000.0 in collector.buffer.times
    assert len(collector.buffer.loads) == 1


def test_track_caps_buffer_at_max_samples():
    """track() caps buffer at DISCHARGE_BUFFER_MAX_SAMPLES and logs warning."""
    collector, *_ = make_collector()
    # Pre-fill buffer to cap
    collector.discharge_buffer.collecting = True
    collector.discharge_buffer.voltages = [12.0] * DISCHARGE_BUFFER_MAX_SAMPLES
    collector.discharge_buffer.times = [float(i) for i in range(DISCHARGE_BUFFER_MAX_SAMPLES)]
    collector.discharge_buffer.loads = [25.0] * DISCHARGE_BUFFER_MAX_SAMPLES
    metrics = make_metrics(event_type=EventType.BLACKOUT_REAL)
    # Should not raise; buffer stays at cap
    collector.track(11.5, 99999.0, EventType.BLACKOUT_REAL, metrics)
    assert len(collector.buffer.voltages) == DISCHARGE_BUFFER_MAX_SAMPLES


def test_track_returns_false_during_normal_accumulation():
    """track() returns False during normal OB accumulation (no cooldown expiry)."""
    collector, *_ = make_collector()
    metrics = make_metrics(event_type=EventType.BLACKOUT_REAL)
    result = collector.track(12.0, 1000.0, EventType.BLACKOUT_REAL, metrics)
    assert result is False


# ------------------------------------------------------------------
# track() — cooldown state machine
# ------------------------------------------------------------------

def test_track_ob_to_ol_starts_cooldown():
    """track() on OB->OL transition starts 60s cooldown (set to 60, then decremented once)."""
    collector, *_ = make_collector(polling_interval=10)
    # Start collecting
    collector.discharge_buffer.collecting = True
    metrics = make_metrics(
        event_type=EventType.ONLINE,
        previous_event_type=EventType.BLACKOUT_REAL,
    )
    collector.track(13.0, 2000.0, EventType.ONLINE, metrics)
    # Countdown starts at 60 then is decremented by polling_interval=10 on the same tick
    assert collector._discharge_buffer_clear_countdown is not None
    assert collector._discharge_buffer_clear_countdown == 50


def test_track_ol_ob_during_cooldown_cancels_it():
    """track() on OL->OB during active cooldown cancels cooldown (continuation)."""
    collector, *_ = make_collector(polling_interval=10)
    collector.discharge_buffer.collecting = True
    collector._discharge_buffer_clear_countdown = 50  # Active cooldown

    metrics = make_metrics(
        event_type=EventType.BLACKOUT_REAL,
        previous_event_type=EventType.ONLINE,
    )
    collector.track(11.5, 3000.0, EventType.BLACKOUT_REAL, metrics)
    assert collector._discharge_buffer_clear_countdown is None


def test_track_returns_true_when_cooldown_expires():
    """track() returns True when cooldown timer expires (60s of OL confirmed)."""
    collector, *_ = make_collector(polling_interval=10)
    collector.discharge_buffer.collecting = True
    # Set countdown to just above 0 so one decrement expires it
    collector._discharge_buffer_clear_countdown = 10  # Will become 0 after polling_interval=10

    metrics = make_metrics(
        event_type=EventType.ONLINE,
        previous_event_type=EventType.ONLINE,  # Already in OL, not a new transition
    )
    result = collector.track(13.0, 4000.0, EventType.ONLINE, metrics)
    assert result is True


def test_track_returns_false_during_active_cooldown():
    """track() returns False while cooldown is counting down but not yet expired."""
    collector, *_ = make_collector(polling_interval=10)
    collector.discharge_buffer.collecting = True
    collector._discharge_buffer_clear_countdown = 30  # Will become 20, not expired

    metrics = make_metrics(
        event_type=EventType.ONLINE,
        previous_event_type=EventType.ONLINE,
    )
    result = collector.track(13.0, 4000.0, EventType.ONLINE, metrics)
    assert result is False


# ------------------------------------------------------------------
# _start_discharge_collection — cycle count and snapshot
# ------------------------------------------------------------------

def test_start_discharge_collection_increments_cycle_count():
    """_start_discharge_collection increments cycle_count via battery_model."""
    collector, mock_model, *_ = make_collector()
    metrics = make_metrics(event_type=EventType.BLACKOUT_REAL, time_rem_minutes=25.0)
    # Trigger start via track()
    collector.track(12.0, 1000.0, EventType.BLACKOUT_REAL, metrics)
    mock_model.increment_cycle_count.assert_called_once()


def test_start_discharge_collection_snapshots_predicted_runtime_when_stabilized():
    """_start_discharge_collection snapshots predicted_runtime when ema_filter.stabilized."""
    collector, mock_model, mock_config, mock_handler, mock_ema = make_collector()
    mock_ema.stabilized = True
    metrics = make_metrics(event_type=EventType.BLACKOUT_REAL, time_rem_minutes=35.0)
    collector.track(12.0, 1000.0, EventType.BLACKOUT_REAL, metrics)
    assert mock_handler.discharge_predicted_runtime == 35.0


def test_start_discharge_collection_no_snapshot_when_not_stabilized():
    """_start_discharge_collection sets discharge_predicted_runtime=None when not stabilized."""
    collector, mock_model, mock_config, mock_handler, mock_ema = make_collector()
    mock_ema.stabilized = False
    metrics = make_metrics(event_type=EventType.BLACKOUT_REAL, time_rem_minutes=35.0)
    collector.track(12.0, 1000.0, EventType.BLACKOUT_REAL, metrics)
    assert mock_handler.discharge_predicted_runtime is None


# ------------------------------------------------------------------
# finalize()
# ------------------------------------------------------------------

def test_finalize_records_on_battery_time():
    """finalize() records on-battery time via battery_model.add_on_battery_time()."""
    collector, mock_model, *_ = make_collector()
    collector._discharge_start_time = 1000.0
    collector.discharge_buffer.collecting = True
    collector.finalize(1300.0)
    mock_model.add_on_battery_time.assert_called_once_with(300.0)


def test_finalize_resets_buffer_collecting():
    """finalize() resets buffer.collecting to False."""
    collector, *_ = make_collector()
    collector.discharge_buffer.collecting = True
    collector._discharge_start_time = 1000.0
    collector.finalize(1200.0)
    assert collector.discharge_buffer.collecting is False


def test_finalize_resets_calibration_last_written_index():
    """finalize() resets _calibration_last_written_index to 0."""
    collector, *_ = make_collector()
    collector._calibration_last_written_index = 5
    collector._discharge_start_time = 1000.0
    collector.finalize(1200.0)
    assert collector._calibration_last_written_index == 0


def test_finalize_handles_no_start_time():
    """finalize() does not crash when _discharge_start_time is None."""
    collector, mock_model, *_ = make_collector()
    collector._discharge_start_time = None
    collector.discharge_buffer.collecting = True
    collector.finalize(1200.0)  # Should not raise
    mock_model.add_on_battery_time.assert_not_called()
    assert collector.discharge_buffer.collecting is False


# ------------------------------------------------------------------
# _write_calibration_points
# ------------------------------------------------------------------

def test_write_calibration_points_every_reporting_interval():
    """track() writes calibration points every reporting_interval polls via battery_model."""
    # polling_interval=10, reporting_interval=60 → every 6 polls
    collector, mock_model, *_ = make_collector(polling_interval=10, reporting_interval=60)
    mock_model.get_lut.return_value = [
        {"v": 10.5, "soc": 0.0, "source": "anchor"},
        {"v": 13.0, "soc": 1.0, "source": "standard"},
    ]
    collector.discharge_buffer.collecting = True

    metrics = make_metrics(event_type=EventType.BLACKOUT_REAL)
    # Add 6 samples (enough to trigger one calibration flush: 6 >= 60//10)
    for i in range(6):
        collector.discharge_buffer.voltages.append(12.0 - i * 0.1)
        collector.discharge_buffer.times.append(1000.0 + i * 10)
        collector.discharge_buffer.loads.append(25.0)

    # Force calibration by setting index at start (already have 6, index=0 → 6-0=6 >= 6)
    collector._calibration_last_written_index = 0
    collector._write_calibration_points(EventType.BLACKOUT_REAL)

    # Should have called calibration_write and calibration_batch_flush
    assert mock_model.calibration_write.call_count == 6
    mock_model.calibration_batch_flush.assert_called_once()
