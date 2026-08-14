"""Unit tests for DischargeCollector — accumulation, cooldown, calibration, finalize, properties.

Tests the DischargeCollector class directly without constructing MonitorDaemon.
BatteryModel, Config, DischargeHandler, and EMAFilter are mocked.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.discharge_collector import DischargeCollector
from src.discharge_journal import DischargeJournal, JournalError, JournalSample, JournalStart
from src.event_classifier import EventType
from src.model import BatteryModel
from src.monitor_config import DISCHARGE_BUFFER_MAX_SAMPLES, Config, DischargeBuffer


def make_collector(
    polling_interval=10, reporting_interval=60, reference_load_percent=20.0, journal=None
):
    """Build a DischargeCollector with mocked dependencies."""
    mock_model = MagicMock()
    mock_model.get_battery_epoch_id.return_value = "epoch-test"
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
    mock_config.shutdown_minutes = 5
    mock_handler = MagicMock()
    mock_ema = MagicMock()
    mock_ema.stabilized = True
    mock_ema.load = 25.0
    collector = DischargeCollector(
        battery_model=mock_model,
        config=mock_config,
        discharge_handler=mock_handler,
        ema_filter=mock_ema,
        journal=journal,
    )
    return collector, mock_model, mock_config, mock_handler, mock_ema


def make_metrics(event_type=EventType.BLACKOUT_REAL, time_rem_minutes=30.0):
    """Build a mock CurrentMetrics object."""
    m = MagicMock()
    m.event_type = event_type
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


def test_track_returns_none_during_normal_accumulation():
    """track() returns no completion during normal OB accumulation."""
    collector, *_ = make_collector()
    metrics = make_metrics(event_type=EventType.BLACKOUT_REAL)
    result = collector.track(12.0, 1000.0, EventType.BLACKOUT_REAL, metrics)
    assert result is None


# ------------------------------------------------------------------
# track() — immediate lifecycle and elapsed journal cadence
# ------------------------------------------------------------------


def test_first_visible_online_closes_immediately(tmp_path):
    """OB→OL closes on the first visible OL with no cooldown."""
    journal = DischargeJournal(tmp_path / "events", boot_id="test-boot")
    collector, *_ = make_collector(journal=journal)
    collector.track(
        12.0,
        100.0,
        EventType.BLACKOUT_REAL,
        make_metrics(),
        raw_ups_data={"ups.status": "OB"},
        monotonic_timestamp=10.0,
    )
    completion = collector.track(
        13.0, 102.0, EventType.ONLINE, make_metrics(EventType.ONLINE), monotonic_timestamp=12.0
    )
    assert completion is not None
    assert completion.evidence_class == "operational"
    event = next(iter(journal.replay().events.values()))
    assert event.end.payload["observed_recovery_timestamp"] == 102.0
    assert event.end.payload["observed_duration_sec"] == 2.0


def test_open_event_from_other_epoch_is_not_recovered(tmp_path):
    """An old open event closes as raw history without entering the new epoch."""
    journal = DischargeJournal(tmp_path / "events", boot_id="test-boot")
    cursor = journal.start_event(JournalStart({"battery_epoch_id": "old-epoch"}))
    cursor = journal.append_sample(cursor, JournalSample({"timestamp": 90.0}))
    collector, *_ = make_collector(journal=journal)

    collector.track(
        13.0,
        100.0,
        EventType.ONLINE,
        make_metrics(EventType.ONLINE),
        monotonic_timestamp=10.0,
    )

    event = next(iter(journal.replay().events.values()))
    assert event.end is not None
    assert event.end.payload["lifecycle"] == "closed_epoch_mismatch"
    assert event.end.payload["disposition"] == "history_only"
    assert event.end.payload["reason"] == "battery_epoch_mismatch"
    assert event.end.payload["model_processing_eligible"] is False
    assert event.end.payload["last_confirmed_timestamp"] == 90.0
    assert event.reboot_gaps == ()
    assert journal.replay().open_event_id is None


def test_open_event_without_epoch_closes_as_raw_history(tmp_path):
    """An open legacy event without an epoch is terminal but never current-epoch data."""
    journal = DischargeJournal(tmp_path / "events", boot_id="test-boot")
    journal.start_event(JournalStart({"event_classification": "BLACKOUT_REAL"}))
    collector, *_ = make_collector(journal=journal)

    collector.track(
        13.0,
        100.0,
        EventType.ONLINE,
        make_metrics(EventType.ONLINE),
        monotonic_timestamp=10.0,
    )

    event = next(iter(journal.replay().events.values()))
    assert event.end is not None
    assert event.end.payload["lifecycle"] == "closed_epoch_mismatch"
    assert event.end.payload["disposition"] == "history_only"
    assert event.end.payload["reason"] == "missing_battery_epoch_id"
    assert event.end.payload["model_processing_eligible"] is False
    assert journal.replay().open_event_id is None


def test_epoch_mismatch_recovery_is_idempotent_and_allows_new_event(tmp_path):
    """A recovered foreign event gets one end and does not wedge the next start."""
    journal = DischargeJournal(tmp_path / "events", boot_id="test-boot")
    journal.start_event(JournalStart({"battery_epoch_id": "old-epoch"}))

    collector, *_ = make_collector(journal=journal)
    collector.track(
        13.0,
        100.0,
        EventType.ONLINE,
        make_metrics(EventType.ONLINE),
        monotonic_timestamp=10.0,
    )
    first_projection = journal.replay()
    assert first_projection.open_event_id is None
    assert len(first_projection.events) == 1

    restarted, *_ = make_collector(journal=journal)
    restarted.track(
        12.0,
        200.0,
        EventType.BLACKOUT_REAL,
        make_metrics(EventType.BLACKOUT_REAL),
        raw_ups_data={"ups.status": "OB"},
        monotonic_timestamp=20.0,
    )

    projection = journal.replay()
    assert len(projection.events) == 2
    old_event = next(
        event
        for event in projection.events.values()
        if event.start.payload.get("battery_epoch_id") == "old-epoch"
    )
    assert sum(record.record_type == "end" for record in old_event.records) == 1
    new_event = next(event for event in projection.events.values() if event is not old_event)
    assert new_event.start.payload["battery_epoch_id"] == "epoch-test"


def test_same_epoch_open_event_resumes_and_restart_is_idempotent(tmp_path):
    """A current-epoch open event resumes once, then a later restart does not duplicate records."""
    journal = DischargeJournal(tmp_path / "events", boot_id="test-boot")
    cursor = journal.start_event(
        JournalStart({"battery_epoch_id": "epoch-test", "start_timestamp": 90.0})
    )
    journal.append_sample(
        cursor, JournalSample({"timestamp": 90.0, "ema_voltage": 12.2, "ema_load": 25.0})
    )

    collector, *_ = make_collector(journal=journal)
    collector.track(
        12.1,
        100.0,
        EventType.BLACKOUT_REAL,
        make_metrics(EventType.BLACKOUT_REAL),
        raw_ups_data={"ups.status": "OB"},
        monotonic_timestamp=10.0,
    )
    resumed_projection = journal.replay()
    resumed_event = next(iter(resumed_projection.events.values()))
    assert resumed_projection.open_event_id == resumed_event.event_id
    assert len(resumed_event.reboot_gaps) == 1

    collector.track(
        13.0,
        102.0,
        EventType.ONLINE,
        make_metrics(EventType.ONLINE),
        monotonic_timestamp=12.0,
    )
    closed_projection = journal.replay()
    assert closed_projection.open_event_id is None
    assert (
        sum(
            record.record_type == "end"
            for record in next(iter(closed_projection.events.values())).records
        )
        == 1
    )

    restarted, *_ = make_collector(journal=journal)
    restarted.track(
        13.0,
        103.0,
        EventType.ONLINE,
        make_metrics(EventType.ONLINE),
        monotonic_timestamp=13.0,
    )
    final_event = next(iter(journal.replay().events.values()))
    assert sum(record.record_type == "end" for record in final_event.records) == 1


def test_new_event_start_records_current_battery_epoch_after_baseline_reset(tmp_path):
    """A sanctioned baseline reset rotates the epoch used by later journal starts."""
    model = BatteryModel(tmp_path / "model.json")
    journal = DischargeJournal(tmp_path / "events", boot_id="test-boot")
    config = SimpleNamespace(
        polling_interval=1,
        reporting_interval=60,
        reference_load_percent=20.0,
        shutdown_minutes=5,
    )
    handler = MagicMock()
    ema = SimpleNamespace(stabilized=True, load=25.0)
    collector = DischargeCollector(model, config, handler, ema, journal=journal)

    collector.track(
        12.0,
        100.0,
        EventType.BLACKOUT_REAL,
        make_metrics(),
        raw_ups_data={"ups.status": "OB"},
        monotonic_timestamp=10.0,
    )
    first = collector.track(
        13.0,
        102.0,
        EventType.ONLINE,
        make_metrics(EventType.ONLINE),
        monotonic_timestamp=12.0,
    )
    assert first is not None
    collector.finalize(102.0)
    collector.reset_buffer()
    old_epoch = journal.replay().events[first.event_id].start.payload["battery_epoch_id"]

    model.reset_baseline(install_date="2026-08-15", event_open=False)
    new_epoch = model.get_battery_epoch_id()
    assert new_epoch != old_epoch

    collector.track(
        12.0,
        200.0,
        EventType.BLACKOUT_REAL,
        make_metrics(),
        raw_ups_data={"ups.status": "OB"},
        monotonic_timestamp=20.0,
    )
    second_event_id = collector.event_id
    assert second_event_id is not None
    second = journal.replay().events[second_event_id]
    assert second.start.payload["battery_epoch_id"] == new_epoch


def test_ob_ol_ob_creates_distinct_event_ids(tmp_path):
    journal = DischargeJournal(tmp_path / "events", boot_id="test-boot")
    collector, *_ = make_collector(journal=journal)
    for start, stop in ((100.0, 102.0), (103.0, 105.0)):
        collector.track(
            12.0,
            start,
            EventType.BLACKOUT_REAL,
            make_metrics(),
            raw_ups_data={"ups.status": "OB"},
            monotonic_timestamp=start,
        )
        collector.track(
            13.0, stop, EventType.ONLINE, make_metrics(EventType.ONLINE), monotonic_timestamp=stop
        )
        collector.finalize(stop)
        collector.reset_buffer()
    projection = journal.replay()
    assert len(projection.events) == 2
    assert len({event.event_id for event in projection.events.values()}) == 2


def test_journal_keeps_raw_and_ema_fields_and_excludes_cooldown(tmp_path):
    """Capture stores selected raw NUT fields and ends at the last OB sample."""
    model_dir = tmp_path / "model"
    model_dir.mkdir(mode=0o700)
    journal = DischargeJournal(model_dir / "discharge-events-v1.jsonl", boot_id="test-boot")
    collector, *_ = make_collector(journal=journal)
    raw = {
        "battery.voltage": "12.4",
        "ups.load": "31",
        "input.voltage": "0",
        "ups.status": "OB DISCHRG",
        "unrelated": "must-not-be-stored",
    }

    first_metrics = make_metrics(event_type=EventType.BLACKOUT_REAL)
    first_metrics.soc = 42.0
    collector.track(
        12.2,
        100.0,
        EventType.BLACKOUT_REAL,
        first_metrics,
        raw_ups_data=raw,
        monotonic_timestamp=0.0,
    )
    collector.track(
        12.0,
        110.0,
        EventType.BLACKOUT_REAL,
        make_metrics(event_type=EventType.BLACKOUT_REAL),
        raw_ups_data=raw,
        monotonic_timestamp=10.0,
    )
    collector.track(
        11.9,
        119.0,
        EventType.BLACKOUT_REAL,
        make_metrics(event_type=EventType.BLACKOUT_REAL),
        raw_ups_data=raw,
        monotonic_timestamp=19.0,
    )
    completion = collector.track(
        13.1,
        120.0,
        EventType.ONLINE,
        make_metrics(event_type=EventType.ONLINE),
        monotonic_timestamp=20.0,
    )

    assert completion is not None
    projection = journal.replay()
    event = next(iter(projection.events.values()))
    sample = event.samples[0].payload
    assert sample["raw_nut"] == {key: raw[key] for key in raw if key != "unrelated"}
    assert sample["raw_voltage"] == "12.4"
    assert sample["raw_load"] == "31"
    assert sample["ema_voltage"] == 12.2
    assert sample["ema_load"] == 25.0
    assert sample["model_input_soc"] == 42.0
    assert collector.buffer.raw_voltages[:3] == ["12.4", "12.4", "12.4"]
    assert collector.buffer.raw_loads[:3] == ["31", "31", "31"]
    assert event.end is not None
    assert event.end.payload["observed_duration_sec"] == 20.0
    assert event.end.payload["evidence_class"] == "operational"
    assert [sample.payload["timestamp"] for sample in event.samples] == [100.0, 110.0, 119.0]


def test_cal_and_real_blackouts_never_mutate_model(tmp_path):
    """CAL provenance is sticky metadata; operational events never mutate LUT."""
    journal = DischargeJournal(tmp_path / "discharge-events-v1.jsonl", boot_id="test-boot")
    collector, mock_model, *_ = make_collector(journal=journal)
    cal_raw = {
        "battery.voltage": "12.4",
        "ups.load": "31",
        "input.voltage": "220",
        "ups.status": "CAL DISCHRG",
    }
    for index in range(12):
        collector.track(
            12.2 - index * 0.05,
            100.0 + index * 10,
            EventType.BLACKOUT_TEST,
            make_metrics(event_type=EventType.BLACKOUT_TEST, time_rem_minutes=1.0),
            raw_ups_data=cal_raw,
            monotonic_timestamp=float(index),
        )

    collector.track(
        13.0, 112.0, EventType.ONLINE, make_metrics(EventType.ONLINE), monotonic_timestamp=12.0
    )
    collector.finalize(112.0)
    collector.reset_buffer()
    collector.track(
        12.0,
        120.0,
        EventType.BLACKOUT_REAL,
        make_metrics(EventType.BLACKOUT_REAL),
        raw_ups_data={"ups.status": "OB"},
        monotonic_timestamp=20.0,
    )

    projection = journal.replay()
    events = list(projection.events.values())
    event = events[0]
    assert len(event.samples) == 3
    assert event.start.payload["cal_provenance"] is True
    assert event.end.payload["cal_provenance"] is True
    assert mock_model.calibration_write.call_count == 0
    mock_model.calibration_batch_flush.assert_not_called()
    mock_model.increment_cycle_count.assert_not_called()
    mock_model.add_on_battery_time.assert_not_called()
    assert events[1].end is None
    assert mock_model.calibration_write.call_count == 0
    mock_model.calibration_batch_flush.assert_not_called()


def test_shutdown_flushes_cached_observation_before_end(tmp_path):
    """Shutdown journals the final accepted sample before its terminal end marker."""
    journal = DischargeJournal(tmp_path / "events", boot_id="test-boot")
    collector, *_ = make_collector(journal=journal)

    collector.track(
        12.2,
        100.0,
        EventType.BLACKOUT_REAL,
        make_metrics(EventType.BLACKOUT_REAL),
        raw_ups_data={"ups.status": "OB"},
        monotonic_timestamp=0.0,
    )
    collector.track(
        12.0,
        105.0,
        EventType.BLACKOUT_REAL,
        make_metrics(EventType.BLACKOUT_REAL),
        raw_ups_data={"ups.status": "OB"},
        monotonic_timestamp=1.0,
    )

    collector.shutdown(timestamp=106.0)

    event = next(iter(journal.replay().events.values()))
    assert [sample.payload["timestamp"] for sample in event.samples] == [100.0, 105.0]
    assert event.end is not None
    assert event.end.payload["last_confirmed_timestamp"] == 105.0
    assert event.end.payload["last_accepted_seq"] == 2


def test_shutdown_does_not_claim_failed_cached_observation_as_durable(tmp_path, monkeypatch):
    """A failed final sample leaves the event open rather than writing a false end timestamp."""
    journal = DischargeJournal(tmp_path / "events", boot_id="test-boot")
    collector, *_ = make_collector(journal=journal)
    collector.track(
        12.2,
        100.0,
        EventType.BLACKOUT_REAL,
        make_metrics(EventType.BLACKOUT_REAL),
        raw_ups_data={"ups.status": "OB"},
        monotonic_timestamp=0.0,
    )
    collector.track(
        12.0,
        105.0,
        EventType.BLACKOUT_REAL,
        make_metrics(EventType.BLACKOUT_REAL),
        raw_ups_data={"ups.status": "OB"},
        monotonic_timestamp=1.0,
    )

    def fail_append(*_args, **_kwargs):
        raise JournalError("simulated sample append failure")

    monkeypatch.setattr(journal, "append_sample", fail_append)
    collector.shutdown(timestamp=106.0)

    event = next(iter(journal.replay().events.values()))
    assert event.end is None
    assert [sample.payload["timestamp"] for sample in event.samples] == [100.0]
    assert journal.health.healthy is False


# ------------------------------------------------------------------
# _start_discharge_collection — cycle count and snapshot
# ------------------------------------------------------------------


def test_start_discharge_collection_does_not_mutate_model_counters():
    """Capture owns lifecycle data; model counters are journal projections."""
    collector, mock_model, *_ = make_collector()
    metrics = make_metrics(event_type=EventType.BLACKOUT_REAL, time_rem_minutes=25.0)
    # Trigger start via track()
    collector.track(12.0, 1000.0, EventType.BLACKOUT_REAL, metrics)
    mock_model.increment_cycle_count.assert_not_called()


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


def test_finalize_does_not_mutate_model_on_battery_time():
    """finalize() leaves durable counter projection to the journal."""
    collector, mock_model, *_ = make_collector()
    collector._discharge_start_time = 1000.0
    collector.discharge_buffer.collecting = True
    collector.finalize(1300.0)
    mock_model.add_on_battery_time.assert_not_called()


def test_finalize_resets_buffer_collecting():
    """finalize() resets buffer.collecting to False."""
    collector, *_ = make_collector()
    collector.discharge_buffer.collecting = True
    collector._discharge_start_time = 1000.0
    collector.finalize(1200.0)
    assert collector.discharge_buffer.collecting is False


def test_finalize_resets_event_sampling_state():
    collector, *_ = make_collector()
    collector._discharge_start_time = 1000.0
    collector._discharge_start_monotonic = 10.0
    collector._next_sample_deadline = 20.0
    collector.finalize(1200.0)
    assert collector._next_sample_deadline is None


def test_finalize_handles_no_start_time():
    """finalize() does not crash when _discharge_start_time is None."""
    collector, mock_model, *_ = make_collector()
    collector._discharge_start_time = None
    collector.discharge_buffer.collecting = True
    collector.finalize(1200.0)  # Should not raise
    mock_model.add_on_battery_time.assert_not_called()
    assert collector.discharge_buffer.collecting is False
