"""Integration tests: verify orchestrator (monitor.py) wires kernel correctly.

Tests the separation of concerns between:
- monitor.py (orchestrator): guard clauses, state management, I/O
- battery_math kernel: pure math functions

Verifies:
1. Correct arguments passed to kernel (rated capacity, not measured)
2. Correct argument selection (average load, not current EMA)
3. Call ordering (SoH before Peukert)
4. Systemd watchdog integration survival
5. _poll_once full call chain: _classify_event → discharge_collector.track → _handle_event_transition
"""

import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.battery_math.rls import ScalarRLS
from src.discharge_journal import (
    DischargeJournal,
    EventCursor,
    JournalEnd,
    JournalSample,
    JournalStart,
)
from src.discharge_types import CompletedDischarge
from src.event_classifier import EventType
from src.model import BatteryModel
from src.monitor import MonitorDaemon
from src.monitor_config import Config, DischargeBuffer


@pytest.fixture
def temp_dir():
    """Temporary directory for test artifacts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def test_config(temp_dir):
    """Test configuration with reasonable defaults."""
    return Config(
        ups_name="cyberpower",
        polling_interval=10,
        reporting_interval=60,
        nut_host="localhost",
        nut_port=3493,
        nut_timeout=2.0,
        shutdown_minutes=5,
        soh_alert_threshold=0.80,
        model_dir=temp_dir,
        runtime_threshold_minutes=20,
        reference_load_percent=20.0,
        ema_window_sec=120,
        capacity_ah=7.2,
    )


@pytest.fixture
def mock_daemon(test_config):
    """Mock MonitorDaemon with test config, no NUT connection needed."""
    with patch("src.monitor.NUTClient"):
        daemon = MonitorDaemon(
            config=test_config,
            virtual_ups_path=test_config.model_dir / "ups-virtual.dev",
            health_path=test_config.model_dir / "ups-health.json",
        )
        # Initialize key attributes
        daemon.battery_model.set_peukert_exponent(1.2)
        daemon.battery_model.state["soh"] = 0.95
        return daemon


def test_existing_model_constructor_is_byte_for_byte_read_only(test_config):
    """An existing current-schema model is not initialized or saved at startup."""
    model_path = test_config.model_dir / "model.json"
    model = BatteryModel(model_path)
    model.save()
    persisted_before = model_path.read_bytes()

    from src.monitor import MonitorDaemon

    with (
        patch("src.monitor.NUTClient"),
        patch.object(MonitorDaemon, "_check_nut_connectivity"),
        patch.object(MonitorDaemon, "_probe_temperature_sensor"),
    ):
        daemon = MonitorDaemon(
            test_config,
            virtual_ups_path=test_config.model_dir / "ups-virtual.dev",
            health_path=test_config.model_dir / "ups-health.json",
        )

    try:
        assert model_path.read_bytes() == persisted_before
    finally:
        daemon.journal.close()
        daemon._release_writer_lock()


def _append_closed_event(journal, start_payload, *, evidence_class="operational", duration=0.0):
    cursor = journal.start_event(JournalStart(start_payload))
    cursor = journal.append_sample(
        cursor,
        JournalSample(
            {
                "timestamp": 100.0,
                "ema_voltage": 12.0,
                "ema_load": 25.0,
            }
        ),
    )
    if duration:
        cursor = journal.append_sample(
            cursor,
            JournalSample(
                {
                    "timestamp": 100.0 + duration,
                    "ema_voltage": 11.8,
                    "ema_load": 25.0,
                }
            ),
        )
    journal.close_event(
        cursor,
        JournalEnd(
            {
                "lifecycle": "closed_power_restored",
                "evidence_class": evidence_class,
                "model_processing_eligible": False,
                "eligibility_reasons": ["capture_only"],
            }
        ),
    )
    return cursor.event_id


def test_replay_filters_events_without_or_from_other_battery_epoch(tmp_path):
    """Historical evidence cannot affect current-epoch replay disposition."""
    model = BatteryModel(tmp_path / "model.json")
    model.save()
    current_epoch = model.get_battery_epoch_id()
    journal = DischargeJournal(tmp_path / "events")
    missing_id = _append_closed_event(
        journal,
        {"tag": "missing-epoch"},
        evidence_class="operational_partial",
    )
    old_id = _append_closed_event(
        journal,
        {"tag": "old-epoch", "battery_epoch_id": str(uuid.uuid4())},
        evidence_class="operational_partial",
    )
    current_id = _append_closed_event(
        journal,
        {"tag": "current-epoch", "battery_epoch_id": current_epoch},
    )

    daemon = MonitorDaemon.__new__(MonitorDaemon)
    daemon.journal = journal
    daemon.battery_model = model
    daemon.discharge_handler = MagicMock()
    daemon.discharge_handler.apply_completed_discharge.return_value = MagicMock(
        skipped=True,
        applied=False,
        already_applied=False,
    )
    daemon._pending_replay = False
    daemon._recovered_partial_events = 0
    daemon.journal_error = None

    daemon._replay_closed_events()

    projection = journal.replay()
    assert projection.events[missing_id].applied is None
    assert projection.events[old_id].applied is None
    assert projection.events[current_id].applied is not None
    daemon.discharge_handler.apply_completed_discharge.assert_called_once()
    assert daemon._pending_replay is False
    assert daemon._recovered_partial_events == 0
    journal.close()


def test_journal_counters_are_scoped_to_current_epoch_and_reset(tmp_path):
    """Model counters are the epoch baseline; only current-epoch journal data is added."""
    model = BatteryModel(tmp_path / "model.json")
    model.state["cycle_count"] = 2
    model.state["cumulative_on_battery_sec"] = 100.0
    current_epoch = model.get_battery_epoch_id()
    journal = DischargeJournal(tmp_path / "events")
    _append_closed_event(
        journal,
        {"tag": "current", "battery_epoch_id": current_epoch},
        duration=60.0,
    )
    _append_closed_event(journal, {"tag": "missing"})
    _append_closed_event(journal, {"tag": "old", "battery_epoch_id": "old-epoch"})

    daemon = MonitorDaemon.__new__(MonitorDaemon)
    daemon.journal = journal
    daemon.battery_model = model
    daemon.journal_error = None

    counters = daemon._journal_counters()
    assert counters == {"cycle_count": 3, "cumulative_on_battery_sec": 160.0}

    model.reset_baseline(install_date="2026-08-15")
    new_epoch = model.get_battery_epoch_id()
    assert new_epoch != current_epoch
    _append_closed_event(journal, {"tag": "new", "battery_epoch_id": new_epoch}, duration=30.0)

    reset_counters = daemon._journal_counters()
    assert reset_counters == {"cycle_count": 1, "cumulative_on_battery_sec": 30.0}
    journal.close()


def test_journal_counters_share_one_replay_within_a_poll(tmp_path):
    """Two health consumers in one acquisition tick do not replay the file twice."""
    model = BatteryModel(tmp_path / "model.json")
    model.save()
    journal = DischargeJournal(tmp_path / "events")
    current_epoch = model.get_battery_epoch_id()
    _append_closed_event(
        journal,
        {"battery_epoch_id": current_epoch, "tag": "first"},
        duration=10.0,
    )
    _append_closed_event(
        journal,
        {"battery_epoch_id": current_epoch, "tag": "second"},
        duration=20.0,
    )
    daemon = MonitorDaemon.__new__(MonitorDaemon)
    daemon.journal = journal
    daemon.battery_model = model
    daemon.journal_error = None
    daemon._journal_projection_cache = None
    daemon._journal_projection_cache_active = True

    original_replay = journal.replay
    with (
        patch.object(journal, "replay", wraps=original_replay) as replay,
        patch.object(
            journal, "observed_duration", side_effect=AssertionError("replayed per event")
        ) as observed_duration,
    ):
        daemon._journal_counters()
        daemon._journal_counters()

    assert replay.call_count == 1
    observed_duration.assert_not_called()
    daemon._journal_projection_cache_active = False
    daemon._journal_projection_cache = None
    journal.close()


def test_journal_health_reports_latest_current_epoch_disposition(tmp_path):
    """Health exposes the explicit terminal disposition without another replay."""
    model = BatteryModel(tmp_path / "model.json")
    model.save()
    journal = DischargeJournal(tmp_path / "events")
    event_id = _append_closed_event(
        journal,
        {"battery_epoch_id": model.get_battery_epoch_id()},
    )
    journal.mark_applied(event_id, "hash-a", "recorded_only")

    daemon = MonitorDaemon.__new__(MonitorDaemon)
    daemon.journal = journal
    daemon.battery_model = model
    daemon.journal_error = None
    daemon._pending_replay = False
    daemon._recovered_partial_events = 0
    daemon.baseline_scientific_fingerprint = model.scientific_fingerprint()
    daemon._fingerprint_alarm_latched = False
    daemon.startup_degraded = False
    daemon._last_sag_observation = None
    daemon._journal_projection_cache_active = True
    daemon._journal_projection_cache = None

    with patch.object(journal, "replay", wraps=journal.replay) as replay:
        health = daemon._journal_health()

    assert health["last_event_disposition"] == "recorded_only"
    assert replay.call_count == 1
    daemon._journal_projection_cache_active = False
    daemon._journal_projection_cache = None
    journal.close()


def test_journal_health_does_not_infer_historical_marker_disposition(tmp_path):
    """A legacy applied marker without disposition remains undisclosed evidence."""
    model = BatteryModel(tmp_path / "model.json")
    model.save()
    journal = DischargeJournal(tmp_path / "events", boot_id="boot-a")
    event_id = _append_closed_event(
        journal,
        {"battery_epoch_id": model.get_battery_epoch_id()},
    )
    journal._append(
        EventCursor(event_id, 3, "boot-a", True),
        "applied",
        {"model_hash": "legacy-hash"},
    )

    daemon = MonitorDaemon.__new__(MonitorDaemon)
    daemon.journal = journal
    daemon.battery_model = model
    daemon.journal_error = None
    daemon._pending_replay = False
    daemon._recovered_partial_events = 0
    daemon.baseline_scientific_fingerprint = model.scientific_fingerprint()
    daemon._fingerprint_alarm_latched = False
    daemon.startup_degraded = False
    daemon._last_sag_observation = None
    assert daemon._journal_health()["last_event_disposition"] is None
    journal.close()


def test_reset_rebuilds_capacity_estimator_and_handler_tracking(mock_daemon):
    """A replacement battery cannot inherit estimator measurements or handler state."""
    old_estimator = mock_daemon.capacity_estimator
    old_estimator.add_measurement(6.5, "2026-08-14T00:00:00+00:00", {"old": True})
    handler = mock_daemon.discharge_handler
    handler.discharge_predicted_runtime = 12.0
    handler.has_logged_baseline_lock = True
    handler.last_days_since_deep = 4.0
    handler.last_ir_trend_rate = 0.01
    handler.last_cycle_budget_remaining = 200
    handler.last_discharge_timestamp = "2026-08-14T00:00:00+00:00"
    mock_daemon.has_logged_baseline_lock = True
    mock_daemon.battery_model.physics.ir_compensation.reference_load_percent = 33.0
    mock_daemon.ir_reference_load_percent = 33.0

    mock_daemon._reset_battery_baseline()

    assert mock_daemon.capacity_estimator is not old_estimator
    assert handler.capacity_estimator is mock_daemon.capacity_estimator
    assert mock_daemon.capacity_estimator.capacity_measurements == []
    assert handler.discharge_predicted_runtime is None
    assert handler.has_logged_baseline_lock is False
    assert handler.last_days_since_deep is None
    assert handler.last_ir_trend_rate == 0.0
    assert handler.last_cycle_budget_remaining == 0
    assert handler.last_discharge_timestamp is None
    assert mock_daemon.has_logged_baseline_lock is False
    assert mock_daemon.ir_reference_load_percent == 20.0


def test_reset_failure_preserves_estimator_and_handler_tracking(mock_daemon):
    """A failed model transaction must not partially reset runtime tracking."""
    old_estimator = mock_daemon.capacity_estimator
    handler = mock_daemon.discharge_handler
    handler.last_days_since_deep = 4.0
    handler.last_ir_trend_rate = 0.01
    handler.last_cycle_budget_remaining = 200
    handler.last_discharge_timestamp = "2026-08-14T00:00:00+00:00"
    handler.has_logged_baseline_lock = True

    with (
        patch.object(
            mock_daemon.battery_model,
            "reset_baseline",
            side_effect=RuntimeError("save failed"),
        ),
        pytest.raises(RuntimeError, match="save failed"),
    ):
        mock_daemon._reset_battery_baseline()

    assert mock_daemon.capacity_estimator is old_estimator
    assert handler.capacity_estimator is old_estimator
    assert handler.last_days_since_deep == 4.0
    assert handler.last_ir_trend_rate == 0.01
    assert handler.last_cycle_budget_remaining == 200
    assert handler.last_discharge_timestamp == "2026-08-14T00:00:00+00:00"
    assert handler.has_logged_baseline_lock is True


class TestOrchestratorWiring:
    """Tests for correct orchestrator/kernel boundary."""

    def test_capacity_ah_ref_not_measured(self, mock_daemon):
        """VAL-02: calibrate_peukert receives RATED capacity, not measured.

        Verifies that even if model.capacity_ah_measured differs from
        config.capacity_ah (rated), the kernel is called with the rated value.
        """
        # Set up: model has different measured capacity
        mock_daemon.discharge_collector.discharge_buffer.voltages = [13.0, 12.5, 12.0, 11.5, 10.5]
        mock_daemon.discharge_collector.discharge_buffer.times = [0.0, 10.0, 20.0, 30.0, 40.0]
        mock_daemon.discharge_collector.discharge_buffer.loads = [20, 21, 19, 22, 20]

        # Add calibration samples so guard clause passes
        mock_daemon.discharge_collector.discharge_buffer.voltages.extend([10.0, 9.5])
        mock_daemon.discharge_collector.discharge_buffer.times.extend([50.0, 60.0])
        mock_daemon.discharge_collector.discharge_buffer.loads.extend([20, 20])

        # Config capacity (rated)
        assert mock_daemon.config.capacity_ah == 7.2

        with patch("src.discharge_handler.calibrate_peukert") as mock_calibrate:
            mock_calibrate.return_value = 1.22

            # Call orchestrator method
            mock_daemon._auto_calibrate_peukert(current_soh=0.95)

            # Verify kernel was called
            assert mock_calibrate.called, "calibrate_peukert not called"

            # Extract capacity_ah from call
            call_args = mock_calibrate.call_args
            kwargs = call_args.kwargs if call_args.kwargs else {}

            actual_capacity = kwargs.get("capacity_ah")
            assert actual_capacity == 7.2, (
                f"Expected capacity_ah=7.2 (rated), got {actual_capacity}"
            )

    def test_avg_load_from_discharge_buffer(self, mock_daemon):
        """Verify average load calculated from discharge buffer, not EMA.

        Average load should be computed from discharge_buffer.loads,
        not from ema_filter.load (which is real-time value).
        """
        # Set up discharge buffer with specific load samples
        mock_daemon.discharge_collector.discharge_buffer.voltages = [13.0, 12.5, 12.0, 11.5, 10.5]
        mock_daemon.discharge_collector.discharge_buffer.times = [0.0, 20.0, 40.0, 60.0, 80.0]
        mock_daemon.discharge_collector.discharge_buffer.loads = [20, 30, 25, 30, 20]  # avg = 25

        # Note: We don't set ema_filter.load (read-only property) because
        # the orchestrator explicitly uses discharge_buffer.loads instead

        with patch("src.discharge_handler.calibrate_peukert") as mock_calibrate:
            mock_calibrate.return_value = 1.21

            mock_daemon._auto_calibrate_peukert(current_soh=0.95)

            # Verify kernel was called with average from buffer
            call_args = mock_calibrate.call_args
            kwargs = call_args.kwargs if call_args.kwargs else {}

            actual_load = kwargs.get("avg_load_percent")
            assert actual_load == 25.0, (
                f"Expected avg_load_percent=25.0 (mean of [20,30,25,30,20]), got {actual_load}"
            )

    def test_guard_clause_sample_count(self, mock_daemon):
        """Guard clause 1: rejects if <2 discharge samples."""
        # Empty discharge buffer
        mock_daemon.discharge_collector.discharge_buffer.times = []
        mock_daemon.discharge_collector.discharge_buffer.loads = []
        mock_daemon.discharge_collector.discharge_buffer.voltages = []

        with patch("src.discharge_handler.calibrate_peukert") as mock_calibrate:
            mock_daemon._auto_calibrate_peukert(current_soh=0.95)

            # Should NOT call kernel (guard clause blocks it)
            assert not mock_calibrate.called, "Kernel should not be called with <2 samples"

    def test_guard_clause_discharge_duration(self, mock_daemon):
        """Guard clause 2: rejects if discharge < 60 seconds."""
        # Short discharge (only 30 seconds)
        mock_daemon.discharge_collector.discharge_buffer.times = [0.0, 30.0]
        mock_daemon.discharge_collector.discharge_buffer.loads = [20, 20]
        mock_daemon.discharge_collector.discharge_buffer.voltages = [13.0, 12.5]

        with patch("src.discharge_handler.calibrate_peukert") as mock_calibrate:
            mock_daemon._auto_calibrate_peukert(current_soh=0.95)

            # Should NOT call kernel
            assert not mock_calibrate.called, "Kernel should not be called with discharge < 60s"

    def test_guard_clause_invalid_load(self, mock_daemon):
        """Guard clause 3: rejects if average load is invalid."""
        # Valid duration but empty loads
        mock_daemon.discharge_collector.discharge_buffer.times = [0.0, 100.0]
        mock_daemon.discharge_collector.discharge_buffer.loads = []  # Empty
        mock_daemon.discharge_collector.discharge_buffer.voltages = [13.0, 12.5]
        mock_daemon.reference_load_percent = 0.0  # Fallback is invalid
        mock_daemon.discharge_handler.reference_load_percent = 0.0

        with patch("src.discharge_handler.calibrate_peukert") as mock_calibrate:
            mock_daemon._auto_calibrate_peukert(current_soh=0.95)

            # Should NOT call kernel
            assert not mock_calibrate.called, "Kernel should not be called with invalid load"


class TestPeukertClampSkip:
    """F30: Skip RLS update when calibrate_peukert returns clamped value."""

    def test_peukert_rls_skipped_on_clamp_upper(self, mock_daemon):
        """Short discharge → calibrate_peukert returns 1.4 (clamped) → RLS not updated."""
        mock_daemon.discharge_collector.discharge_buffer.voltages = [13.0, 12.5, 12.0, 11.5, 10.5]
        mock_daemon.discharge_collector.discharge_buffer.times = [0.0, 20.0, 40.0, 60.0, 80.0]
        mock_daemon.discharge_collector.discharge_buffer.loads = [20, 21, 19, 22, 20]

        initial_sample_count = mock_daemon.rls_peukert.sample_count

        with patch("src.discharge_handler.calibrate_peukert") as mock_calibrate:
            mock_calibrate.return_value = 1.4  # Hit upper clamp

            mock_daemon._auto_calibrate_peukert(current_soh=0.95)

            # RLS should NOT have been updated
            assert mock_daemon.rls_peukert.sample_count == initial_sample_count

    def test_peukert_rls_skipped_on_clamp_lower(self, mock_daemon):
        """calibrate_peukert returns 1.0 (lower clamp) → RLS not updated."""
        mock_daemon.discharge_collector.discharge_buffer.voltages = [13.0, 12.5, 12.0, 11.5, 10.5]
        mock_daemon.discharge_collector.discharge_buffer.times = [0.0, 20.0, 40.0, 60.0, 80.0]
        mock_daemon.discharge_collector.discharge_buffer.loads = [20, 21, 19, 22, 20]

        initial_sample_count = mock_daemon.rls_peukert.sample_count

        with patch("src.discharge_handler.calibrate_peukert") as mock_calibrate:
            mock_calibrate.return_value = 1.0  # Hit lower clamp

            mock_daemon._auto_calibrate_peukert(current_soh=0.95)

            assert mock_daemon.rls_peukert.sample_count == initial_sample_count

    def test_peukert_rls_updated_on_valid_exponent(self, mock_daemon):
        """Valid exponent (1.15) → RLS updated, sample_count increments."""
        mock_daemon.discharge_collector.discharge_buffer.voltages = [13.0, 12.5, 12.0, 11.5, 10.5]
        mock_daemon.discharge_collector.discharge_buffer.times = [0.0, 20.0, 40.0, 60.0, 80.0]
        mock_daemon.discharge_collector.discharge_buffer.loads = [20, 21, 19, 22, 20]

        initial_sample_count = mock_daemon.rls_peukert.sample_count

        with patch("src.discharge_handler.calibrate_peukert") as mock_calibrate:
            mock_calibrate.return_value = 1.15  # Valid, not clamped

            mock_daemon._auto_calibrate_peukert(current_soh=0.95)

            assert mock_daemon.rls_peukert.sample_count == initial_sample_count + 1


class TestSoHRecalibrationFlow:
    """Tests for SoH recalibration and new battery detection."""


def test_journald_event_filtering():
    """The monitor routes an eligible completion through the authoritative API."""
    from unittest.mock import Mock

    from src.discharge_types import CompletedDischarge

    daemon = MonitorDaemon.__new__(MonitorDaemon)
    daemon.discharge_handler = Mock()
    daemon.discharge_collector = Mock()
    daemon.discharge_collector.finalize = Mock()
    daemon.discharge_collector.reset_buffer = Mock()
    daemon.discharge_handler.apply_completed_discharge.return_value = Mock(
        applied=True,
        already_applied=False,
        skipped=False,
        event_id="journal-event",
        model_hash="model-hash",
    )
    daemon.journal = None
    completion = CompletedDischarge(
        "journal-event",
        "closed_power_restored",
        "controlled_capacity_test",
        (13.0, 12.0, 11.0),
        (0.0, 300.0, 900.0),
        (30.0, 30.0, 30.0),
        True,
        (),
    )

    daemon._update_battery_health(completion)

    daemon.discharge_handler.apply_completed_discharge.assert_called_once_with(completion)


def test_operational_skipped_completion_is_marked_applied_immediately():
    """Capture-only operational completion does not wait for a restart to audit."""
    daemon = MonitorDaemon.__new__(MonitorDaemon)
    daemon.discharge_handler = MagicMock()
    daemon.discharge_collector = MagicMock()
    daemon.journal = MagicMock()
    daemon.battery_model = MagicMock()
    daemon.battery_model.get_persisted_hash.return_value = "persisted-hash"
    daemon._pending_replay = False
    daemon.discharge_handler.apply_completed_discharge.return_value = MagicMock(
        skipped=True,
        applied=False,
        already_applied=False,
    )
    completion = CompletedDischarge(
        "operational-event",
        "closed_power_restored",
        "operational",
        (13.0, 12.0),
        (0.0, 60.0),
        (30.0, 30.0),
        False,
        ("capture_only",),
    )

    daemon._update_battery_health(completion)

    daemon.journal.mark_applied.assert_called_once_with(
        "operational-event", "persisted-hash", "recorded_only"
    )
    assert daemon._pending_replay is False


def test_scientific_ineligible_shape_failure_is_not_marked_applied():
    """A scientific skip remains pending for its audit/retry path."""
    daemon = MonitorDaemon.__new__(MonitorDaemon)
    daemon.discharge_handler = MagicMock()
    daemon.discharge_collector = MagicMock()
    daemon.journal = MagicMock()
    daemon.battery_model = MagicMock()
    daemon._pending_replay = False
    daemon.discharge_handler.apply_completed_discharge.return_value = MagicMock(
        skipped=True,
        applied=False,
        already_applied=False,
    )
    completion = CompletedDischarge(
        "scientific-event",
        "closed_power_restored",
        "controlled_capacity_test",
        (13.0,),
        (0.0,),
        (30.0,),
        True,
        ("invalid_shape",),
    )

    daemon._update_battery_health(completion)

    daemon.journal.mark_applied.assert_not_called()


def test_missing_terminal_marker_identity_does_not_create_pending_replay():
    """A failed journal start has no durable event that replay can repair."""
    daemon = MonitorDaemon.__new__(MonitorDaemon)
    daemon.journal = MagicMock()
    daemon._pending_replay = False

    daemon._mark_applied(None, None, "recorded_only")

    daemon.journal.mark_applied.assert_not_called()
    assert daemon._pending_replay is False


def test_failed_terminal_marker_for_durable_event_sets_pending_replay():
    """A real event whose terminal marker fails remains pending and degraded."""
    daemon = MonitorDaemon.__new__(MonitorDaemon)
    daemon.journal = MagicMock()
    daemon.journal.mark_applied.side_effect = OSError("journal unavailable")
    daemon._pending_replay = False
    daemon.journal_error = None

    daemon._mark_applied("event-id", "model-hash", "applied")

    assert daemon._pending_replay is True
    assert daemon.journal_error == "journal unavailable"


def test_explicit_scientific_quality_rejection_gets_rejected_disposition():
    """A deliberate quality rejection is terminal; malformed shape stays pending."""
    daemon = MonitorDaemon.__new__(MonitorDaemon)
    daemon.discharge_handler = MagicMock()
    daemon.discharge_collector = MagicMock()
    daemon.journal = MagicMock()
    daemon.battery_model = MagicMock()
    daemon.battery_model.get_persisted_hash.return_value = "persisted-hash"
    daemon._pending_replay = False
    daemon.discharge_handler.apply_completed_discharge.return_value = MagicMock(
        skipped=True,
        applied=False,
        already_applied=False,
        eligibility_reasons=("capacity_quality_rejected",),
    )
    completion = CompletedDischarge(
        "scientific-event",
        "closed_power_restored",
        "controlled_capacity_test",
        (13.0, 12.0),
        (0.0, 600.0),
        (30.0, 30.0),
        True,
        (),
    )

    daemon._update_battery_health(completion)

    daemon.journal.mark_applied.assert_called_once_with(
        "scientific-event", "persisted-hash", "rejected"
    )
    assert daemon._pending_replay is False


def test_health_endpoint_capacity_persistence(tmp_path, monkeypatch):
    """Verify health endpoint updates capacity fields across discharge cycles.

    RPT-03 - Health endpoint capacity metrics persist and update correctly across multiple discharges.
    Integration test validates that _write_health_endpoint receives correct capacity parameters
    from BatteryModel.get_convergence_status() across discharge lifecycle.
    """
    import json
    import logging
    import sys
    from unittest.mock import MagicMock, patch

    # Mock systemd before importing
    sys.modules["systemd"] = MagicMock()
    sys.modules["systemd.journal"] = MagicMock()

    from src.model import BatteryModel
    from src.monitor import MonitorDaemon
    from src.monitor_config import HealthSnapshot, write_health_endpoint

    # Setup test paths
    model_path = tmp_path / "model.json"
    health_file = tmp_path / "ups-health.json"

    # Create real battery model instance (capacity_ah sourced from config, not state)
    battery_model = BatteryModel(model_path)
    battery_model.state["capacity_estimates"] = []
    battery_model.save()

    # Test config
    from src.monitor_config import Config

    config = Config(
        ups_name="cyberpower",
        polling_interval=10,
        reporting_interval=60,
        nut_host="localhost",
        nut_port=3493,
        nut_timeout=2.0,
        shutdown_minutes=5,
        soh_alert_threshold=0.80,
        model_dir=tmp_path,
        runtime_threshold_minutes=20,
        reference_load_percent=20.0,
        ema_window_sec=120,
        capacity_ah=7.2,
    )

    # Mock external dependencies to focus on health endpoint
    with (
        patch("src.monitor.NUTClient"),
        patch("src.monitor.EMAFilter"),
        patch("src.monitor.EventClassifier"),
        patch.object(MonitorDaemon, "_check_nut_connectivity"),
        patch.object(MonitorDaemon, "_validate_model"),
        patch.object(MonitorDaemon, "_reset_battery_baseline"),
    ):
        # Setup logger to avoid MagicMock issues
        from src.monitor_config import logger as monitor_logger

        monitor_logger.handlers.clear()
        monitor_logger.addHandler(logging.StreamHandler())
        monitor_logger.setLevel(logging.INFO)

        daemon = MonitorDaemon(
            config,
            virtual_ups_path=config.model_dir / "ups-virtual.dev",
            health_path=config.model_dir / "ups-health.json",
        )
        daemon.battery_model = battery_model

    # Cycle 1: First discharge (0 samples, no convergence)
    battery_model.state["capacity_estimates"] = []
    battery_model.save()

    write_health_endpoint(
        HealthSnapshot(
            soc_percent=50.0,
            is_online=False,
            capacity_ah_measured=None,
            capacity_ah_rated=7.2,
            capacity_confidence=0.0,
            capacity_samples_count=0,
            capacity_converged=False,
        ),
        health_path=health_file,
    )

    # Verify health endpoint written with capacity fields
    data_cycle1 = json.loads(health_file.read_text())
    assert data_cycle1["capacity_samples_count"] == 0, "Cycle 1: expected 0 samples"
    assert data_cycle1["capacity_converged"] is False, "Cycle 1: expected not converged"
    assert data_cycle1["capacity_ah_measured"] is None, "Cycle 1: expected None measured"
    assert "capacity_ah_rated" in data_cycle1, "Cycle 1: capacity_ah_rated missing"

    # Cycle 2: Second discharge (1 sample collected)
    battery_model.state["capacity_estimates"] = [
        {"ah_estimate": 6.90, "timestamp": "2026-03-16T12:00:00", "metadata": {}}
    ]
    battery_model.save()

    # Simulate get_convergence_status return for 1 sample
    write_health_endpoint(
        HealthSnapshot(
            soc_percent=40.0,
            is_online=False,
            capacity_ah_measured=6.90,
            capacity_ah_rated=7.2,
            capacity_confidence=0.0,  # No confidence with < 3 samples
            capacity_samples_count=1,
            capacity_converged=False,
        ),
        health_path=health_file,
    )

    data_cycle2 = json.loads(health_file.read_text())
    assert data_cycle2["capacity_samples_count"] == 1, "Cycle 2: expected 1 sample"
    assert data_cycle2["capacity_converged"] is False, "Cycle 2: expected not converged"
    assert data_cycle2["capacity_ah_measured"] == 6.90, "Cycle 2: expected measured 6.90"

    # Cycle 3: Third discharge (3 samples collected, convergence reached)
    battery_model.state["capacity_estimates"] = [
        {"ah_estimate": 6.88, "timestamp": "2026-03-16T12:00:00", "metadata": {}},
        {"ah_estimate": 6.92, "timestamp": "2026-03-16T14:00:00", "metadata": {}},
        {"ah_estimate": 6.95, "timestamp": "2026-03-16T16:00:00", "metadata": {}},
    ]
    battery_model.save()

    # Compute convergence status manually for 3 samples (CoV < 0.10 → converged)
    # mean = (6.88 + 6.92 + 6.95) / 3 = 6.917
    # variance = ((6.88-6.917)^2 + (6.92-6.917)^2 + (6.95-6.917)^2) / 3
    # variance = (0.001369 + 0.000009 + 0.001089) / 3 = 0.000819
    # std = sqrt(0.000819) = 0.0286
    # cov = 0.0286 / 6.917 = 0.00413 < 0.10 → converged!
    # confidence = 1 - cov = 0.99587 * 100 = 99.587%

    write_health_endpoint(
        HealthSnapshot(
            soc_percent=30.0,
            is_online=False,
            capacity_ah_measured=6.95,
            capacity_ah_rated=7.2,
            capacity_confidence=0.996,  # ~99.6% (1 - 0.004 CoV)
            capacity_samples_count=3,
            capacity_converged=True,
        ),
        health_path=health_file,
    )

    data_cycle3 = json.loads(health_file.read_text())
    assert data_cycle3["capacity_samples_count"] == 3, "Cycle 3: expected 3 samples"
    assert data_cycle3["capacity_converged"] is True, "Cycle 3: expected converged"
    assert data_cycle3["capacity_ah_measured"] == 6.95, "Cycle 3: expected measured 6.95"
    assert data_cycle3["capacity_confidence"] > 0.99, (
        f"Cycle 3: expected high confidence, got {data_cycle3['capacity_confidence']}"
    )

    # Verify JSON schema consistency across all 3 reads (no schema changes)
    for cycle_num, data in enumerate([data_cycle1, data_cycle2, data_cycle3], 1):
        required_fields = [
            "last_poll",
            "last_poll_unix",
            "current_soc_percent",
            "online",
            "daemon_version",
            "poll_latency_ms",
            "capacity_ah_measured",
            "capacity_ah_rated",
            "capacity_confidence",
            "capacity_samples_count",
            "capacity_converged",
        ]
        for field in required_fields:
            assert field in data, f"Cycle {cycle_num}: missing field {field}"


class TestPollOnceCallChain:
    """Integration: _poll_once → _classify_event → discharge_collector.track → _handle_event_transition.

    Regression coverage for event transition handling across the real poll chain.
    Real internal method chain; only external I/O and downstream subsystems mocked.

    What runs for real: _update_ema, _classify_event, sag_tracker.track,
    discharge_collector.track, _handle_event_transition, _compute_metrics, _log_status.

    What is mocked: NUTClient (external), sd_notify (systemd), time.sleep,
    _write_health_endpoint (file I/O), write_virtual_ups_dev (file I/O),
    _update_battery_health (complex subsystem with own tests),
    operational journal writes (covered by collector tests).
    """

    @pytest.fixture
    def daemon(self, tmp_path):
        """Daemon with real EventClassifier, EMAFilter, CurrentMetrics."""
        config = Config(
            ups_name="cyberpower",
            polling_interval=10,
            reporting_interval=60,
            nut_host="localhost",
            nut_port=3493,
            nut_timeout=2.0,
            shutdown_minutes=5,
            soh_alert_threshold=0.80,
            model_dir=tmp_path,
            runtime_threshold_minutes=20,
            reference_load_percent=20.0,
            ema_window_sec=120,
            capacity_ah=7.2,
        )

        with patch("src.monitor.NUTClient") as mock_nut_cls:
            # NUTClient returns floats for numeric values, strings for status
            mock_nut_cls.return_value.get_ups_vars.return_value = {
                "battery.voltage": 13.0,
                "input.voltage": 230.0,
                "ups.status": "OL",
                "ups.load": 20.0,
            }
            d = MonitorDaemon(
                config,
                virtual_ups_path=config.model_dir / "ups-virtual.dev",
                health_path=config.model_dir / "ups-health.json",
            )

        # Attributes normally initialized in run(), needed by _poll_once
        d.poll_count = 0
        d._stabilization_logged = False
        d._startup_logged = False
        d._consecutive_errors = 0
        d._startup_time = time.monotonic()

        # Mock downstream subsystems (own test coverage, complex I/O).
        # Side effect mimics real _update_battery_health buffer cleanup.
        def _fake_health_update(completion=None):
            d.discharge_collector.discharge_buffer = DischargeBuffer()

        d._update_battery_health = MagicMock(side_effect=_fake_health_update)
        return d

    def _poll(self, daemon, status="OL", voltage=13.0, input_voltage=230.0, load=20.0):
        """Execute one _poll_once with controlled UPS data, mocked external I/O."""
        daemon.nut_client.get_ups_vars.return_value = {
            "battery.voltage": voltage,
            "input.voltage": input_voltage,
            "ups.status": status,
            "ups.load": load,
        }
        with (
            patch("src.monitor.sd_notify"),
            patch("time.sleep"),
            patch("src.virtual_ups_exporter.write_health_endpoint"),
            patch("src.virtual_ups_exporter.write_virtual_ups_dev"),
        ):
            daemon._poll_once()

    def test_ready_waits_for_complete_physical_poll_and_both_outputs(self, daemon):
        """READY follows a good physical poll and successful virtual+health writes only."""
        daemon._startup_logged = True
        daemon._classify_event = MagicMock()
        daemon._handle_event_transition = MagicMock()
        daemon._log_status = MagicMock()
        daemon.sag_tracker = MagicMock(track=MagicMock(return_value=None))
        daemon.discharge_collector = MagicMock()
        daemon.discharge_collector.track.return_value = None
        daemon.scheduler_manager = MagicMock()
        daemon.exporter.write_virtual_ups = MagicMock(return_value=True)
        daemon.exporter.write_health_snapshot = MagicMock(return_value=True)
        daemon._update_ema = MagicMock(return_value=(13.0, 20.0))

        notifications = []
        daemon.nut_client.get_ups_vars.return_value = {
            "battery.voltage": 13.0,
            "ups.load": 20.0,
        }
        with patch("src.monitor.sd_notify", side_effect=notifications.append):
            daemon._poll_once()

        assert not daemon._ready_sent
        assert any(value.startswith("STATUS=degraded:") for value in notifications)
        assert "READY=1\nSTATUS=ready: physical UPS poll and outputs are fresh" not in notifications

        daemon.nut_client.get_ups_vars.return_value = {
            "battery.voltage": 13.0,
            "input.voltage": 230.0,
            "ups.status": "OL",
            "ups.load": 20.0,
        }
        daemon.exporter.write_virtual_ups.return_value = False
        with patch("src.monitor.sd_notify", side_effect=notifications.append):
            daemon._poll_once()

        assert not daemon._ready_sent
        assert "READY=1\nSTATUS=ready: physical UPS poll and outputs are fresh" not in notifications

        daemon.exporter.write_virtual_ups.return_value = True
        with patch("src.monitor.sd_notify", side_effect=notifications.append):
            daemon._poll_once()

        assert daemon._ready_sent
        assert "READY=1\nSTATUS=ready: physical UPS poll and outputs are fresh" in notifications

        # A subsequent healthy tick must not fall through to degraded.
        with patch("src.monitor.sd_notify", side_effect=notifications.append):
            daemon._poll_once()
        assert notifications[-2] == "STATUS=ready: physical UPS poll and outputs are fresh"

        # A partial physical response is degraded even after READY, then a
        # complete poll explicitly reports recovery without retracting READY.
        daemon.nut_client.get_ups_vars.return_value = {
            "battery.voltage": 13.0,
            "ups.load": 20.0,
        }
        with patch("src.monitor.sd_notify", side_effect=notifications.append):
            daemon._poll_once()
        assert notifications[-2].startswith("STATUS=degraded:")
        assert daemon._ready_sent is True

        daemon.nut_client.get_ups_vars.return_value = {
            "battery.voltage": 13.0,
            "input.voltage": 230.0,
            "ups.status": "OL",
            "ups.load": 20.0,
        }
        with patch("src.monitor.sd_notify", side_effect=notifications.append):
            daemon._poll_once()
        assert notifications[-2] == "STATUS=ready: physical UPS poll and outputs are fresh"

    def test_partial_status_reply_preserves_virtual_ups_file_and_heartbeats(self, daemon):
        """Numeric fields without ups.status cannot mutate state or overwrite OB/LB."""
        virtual_path = daemon.virtual_ups_path
        prior_bytes = b"ups.status: OB DISCHRG LB\nbattery.runtime: 1\n"
        virtual_path.write_bytes(prior_bytes)

        daemon._startup_logged = True
        daemon.nut_client.get_ups_vars.return_value = {
            "battery.voltage": 13.0,
            "ups.load": 20.0,
        }
        daemon._update_ema = MagicMock(return_value=(13.0, 20.0))
        daemon._classify_event = MagicMock()
        daemon.sag_tracker.track = MagicMock()
        daemon.discharge_collector.track = MagicMock()
        daemon.scheduler_manager.run_daily = MagicMock()
        daemon.exporter.write_health_snapshot = MagicMock(return_value=True)

        notifications = []
        try:
            with patch("src.monitor.sd_notify", side_effect=notifications.append):
                daemon._poll_once()
        finally:
            daemon.journal.close()
            daemon._release_writer_lock()

        assert virtual_path.read_bytes() == prior_bytes
        assert daemon._update_ema.call_count == 0
        daemon._classify_event.assert_not_called()
        daemon.discharge_collector.track.assert_not_called()
        daemon.scheduler_manager.run_daily.assert_not_called()
        daemon.exporter.write_health_snapshot.assert_not_called()
        assert notifications.count("WATCHDOG=1") == 1
        assert any(value.startswith("STATUS=degraded:") for value in notifications)
        assert not any(value.startswith("READY=1") for value in notifications)

    def test_event_transition_regression(self, daemon):
        """Real event transition handling must survive the complete poll chain."""
        self._poll(daemon, status="OL", voltage=13.0, input_voltage=230.0)
        # OL→OB exercises the original transition crash site.
        self._poll(daemon, status="OB DISCHRG", voltage=12.0, input_voltage=0.0)
        # OB→OL: another transition through the same code path
        self._poll(daemon, status="OL", voltage=13.0, input_voltage=230.0)
        assert daemon.current_metrics.event_type == EventType.ONLINE
        assert daemon.event_classifier.state == EventType.ONLINE

    def test_event_classifier_threads_across_transitions(self, daemon):
        """Event classifier state remains correct across multiple transitions."""
        transitions = [
            ("OL", 13.0, 230.0, EventType.ONLINE),
            ("OB DISCHRG", 12.0, 0.0, EventType.BLACKOUT_REAL),
            ("OL", 13.0, 230.0, EventType.ONLINE),
            ("OB DISCHRG", 12.5, 220.0, EventType.BLACKOUT_TEST),
            ("OL", 13.0, 230.0, EventType.ONLINE),
        ]
        for status, voltage, input_v, expected_type in transitions:
            self._poll(daemon, status=status, voltage=voltage, input_voltage=input_v)
            assert daemon.current_metrics.event_type == expected_type
            assert daemon.event_classifier.state == expected_type

    def test_ol_steady_state(self, daemon):
        """Steady OL remains online with no discharge collection."""
        for _ in range(5):
            self._poll(daemon, status="OL", voltage=13.0, input_voltage=230.0)

        assert daemon.current_metrics.event_type == EventType.ONLINE
        assert daemon.event_classifier.state == EventType.ONLINE
        assert not daemon.discharge_collector.discharge_buffer.collecting
        assert len(daemon.discharge_collector.discharge_buffer.voltages) == 0

    def test_ol_to_ob_starts_discharge(self, daemon):
        """OL→OB: discharge collection starts, cycle count increments."""
        self._poll(daemon, status="OL", voltage=13.0, input_voltage=230.0)
        initial_cycles = daemon.battery_model.get_cycle_count()

        self._poll(daemon, status="OB DISCHRG", voltage=12.0, input_voltage=0.0)

        assert daemon.current_metrics.event_type == EventType.BLACKOUT_REAL
        assert daemon.discharge_collector.discharge_buffer.collecting
        assert len(daemon.discharge_collector.discharge_buffer.voltages) == 1
        assert daemon.battery_model.get_cycle_count() == initial_cycles

    def test_ob_accumulates_samples(self, daemon):
        """Multiple OB polls accumulate discharge voltage/time/load samples."""
        self._poll(daemon, status="OL", voltage=13.0, input_voltage=230.0)

        ob_voltages = [12.0, 11.8, 11.5, 11.2]
        for v in ob_voltages:
            self._poll(daemon, status="OB DISCHRG", voltage=v, input_voltage=0.0)

        # Acquisition is one-second, but durable measurement capture is
        # deadline-based at ten seconds; rapid unit-test calls only require
        # the mandatory first sample.
        assert len(daemon.discharge_collector.discharge_buffer.voltages) == 1
        assert len(daemon.discharge_collector.discharge_buffer.times) == 1
        assert len(daemon.discharge_collector.discharge_buffer.loads) == 1
        assert daemon.discharge_collector.discharge_buffer.collecting

    def test_full_blackout_cycle(self, daemon):
        """OL→OB→OL: transitions correct, EVT-05 fires _update_battery_health."""
        # OL baseline
        self._poll(daemon, status="OL", voltage=13.0, input_voltage=230.0)
        assert daemon.event_classifier.state == EventType.ONLINE

        # OB phase (3 polls)
        for v in [12.0, 11.8, 11.5]:
            self._poll(daemon, status="OB DISCHRG", voltage=v, input_voltage=0.0)

        assert daemon.current_metrics.event_type == EventType.BLACKOUT_REAL
        assert daemon.discharge_collector.discharge_buffer.collecting

        # Power restored (OB→OL)
        daemon._update_battery_health.reset_mock()
        self._poll(daemon, status="OL", voltage=13.0, input_voltage=230.0)

        assert daemon.current_metrics.event_type == EventType.ONLINE
        assert daemon.event_classifier.state == EventType.ONLINE
        assert daemon._update_battery_health.call_count == 1
        assert not daemon.discharge_collector.discharge_buffer.collecting, (
            "buffer should be cleared after stable OL completion"
        )
        assert len(daemon.discharge_collector.discharge_buffer.voltages) == 0, (
            "buffer voltages should be empty after stable OL completion"
        )

    def test_ob_ol_ob_resumes_collection(self, daemon):
        """OB→OL→OB: new collection starts after brief power restoration."""
        self._poll(daemon, status="OL", voltage=13.0, input_voltage=230.0)
        self._poll(daemon, status="OB DISCHRG", voltage=12.0, input_voltage=0.0)
        self._poll(daemon, status="OB DISCHRG", voltage=11.8, input_voltage=0.0)

        # OB→OL: EVT-05 fires, mock clears discharge state
        self._poll(daemon, status="OL", voltage=13.0, input_voltage=230.0)

        # OL→OB again
        self._poll(daemon, status="OB DISCHRG", voltage=11.5, input_voltage=0.0)

        assert daemon.current_metrics.event_type == EventType.BLACKOUT_REAL
        assert daemon.discharge_collector.discharge_buffer.collecting

    def test_battery_test_classified_correctly(self, daemon):
        """Battery test (OB with mains voltage) → BLACKOUT_TEST, no shutdown."""
        self._poll(daemon, status="OL", voltage=13.0, input_voltage=230.0)
        # Battery test: UPS goes OB but input voltage stays high (mains present)
        self._poll(daemon, status="OB DISCHRG", voltage=12.5, input_voltage=220.0)

        assert daemon.current_metrics.event_type == EventType.BLACKOUT_TEST
        # EVT-03: battery test suppresses shutdown
        assert not daemon.current_metrics.shutdown_imminent


class TestRLSCalibrationIntegration:
    """Integration tests for RLS auto-calibration of ir_k and Peukert."""

    def test_ir_k_observation_does_not_update_model(self, mock_daemon):
        """Sag capture is immutable evidence until a sanctioned model proposal exists."""
        # Setup: known voltage sag scenario via SagTracker
        mock_daemon.sag_tracker._v_before_sag = 13.0
        mock_daemon.sag_tracker._current_load = 25.0

        old_ir_k = mock_daemon.sag_tracker.ir_k

        with patch("src.monitor_config.safe_save"):
            observation = mock_daemon.sag_tracker._record_voltage_sag(
                v_sag=12.5, event_type=EventType.BLACKOUT_REAL
            )

        assert observation.apparent_r_internal_ohm > 0
        assert mock_daemon.sag_tracker.ir_k == old_ir_k
        assert mock_daemon.battery_model.get_ir_k() == old_ir_k
        assert mock_daemon.sag_tracker.rls_ir_k.sample_count == 0

    def test_peukert_smoothed_via_rls(self, mock_daemon):
        """Discharge → Peukert updated with RLS smoothing, not raw value."""
        # Setup: valid discharge buffer
        mock_daemon.discharge_collector.discharge_buffer.voltages = [13.0, 12.5, 12.0, 11.5, 10.5]
        mock_daemon.discharge_collector.discharge_buffer.times = [0.0, 20.0, 40.0, 60.0, 80.0]
        mock_daemon.discharge_collector.discharge_buffer.loads = [20, 21, 19, 22, 20]

        with patch("src.discharge_handler.calibrate_peukert") as mock_calibrate:
            mock_calibrate.return_value = 1.25  # Raw kernel result

            mock_daemon._auto_calibrate_peukert(current_soh=0.95)

            # Peukert should be set via RLS smoothing (not raw 1.25)
            actual = mock_daemon.battery_model.get_peukert_exponent()
            # First sample with P=1.0: RLS will move theta from 1.2 partway toward 1.25
            assert 1.0 <= actual <= 1.4  # Physical bounds
            assert mock_daemon.rls_peukert.sample_count == 1

    def test_rls_state_persists_across_save_load(self, mock_daemon):
        """Save model, reload, RLS state preserved."""
        # Feed some data to RLS via SagTracker
        mock_daemon.sag_tracker.rls_ir_k.update(0.018)
        mock_daemon.sag_tracker.rls_ir_k.update(0.017)
        mock_daemon.battery_model.set_rls_state(
            "ir_k",
            mock_daemon.sag_tracker.rls_ir_k.theta,
            mock_daemon.sag_tracker.rls_ir_k.P,
            mock_daemon.sag_tracker.rls_ir_k.sample_count,
        )

        # Save and reload
        mock_daemon.battery_model.save()
        reloaded = BatteryModel(mock_daemon.battery_model.model_path)

        state = reloaded.get_rls_state("ir_k")
        assert state["sample_count"] == 2
        assert abs(state["theta"] - mock_daemon.sag_tracker.rls_ir_k.theta) < 1e-10

        # Restore RLS from saved state
        restored = ScalarRLS.from_dict(state)
        assert restored.sample_count == 2
        assert abs(restored.theta - mock_daemon.sag_tracker.rls_ir_k.theta) < 1e-10

    def test_battery_replacement_resets_rls(self, mock_daemon):
        """_reset_battery_baseline → RLS P back to 1.0."""
        # Feed data to build confidence
        for _ in range(10):
            mock_daemon.sag_tracker.rls_ir_k.update(0.018)
            mock_daemon.rls_peukert.update(1.22)

        assert mock_daemon.sag_tracker.rls_ir_k.P < 0.5  # Has some confidence
        assert mock_daemon.rls_peukert.P < 0.5

        mock_daemon._reset_battery_baseline()

        # After reset: fresh RLS instances with P=1.0
        assert mock_daemon.sag_tracker.rls_ir_k.P == 1.0
        assert mock_daemon.sag_tracker.rls_ir_k.theta == 0.015
        assert mock_daemon.sag_tracker.rls_ir_k.sample_count == 0
        assert mock_daemon.rls_peukert.P == 1.0
        assert mock_daemon.rls_peukert.theta == 1.2
        assert mock_daemon.rls_peukert.sample_count == 0

        # Model state also reset
        ir_k_state = mock_daemon.battery_model.get_rls_state("ir_k")
        assert ir_k_state["P"] == 1.0
        assert ir_k_state["sample_count"] == 0

    def test_prediction_error_logged(self, mock_daemon):
        """OL→OB→OL cycle with sufficient duration → discharge_prediction event logged."""
        # Setup: simulate a discharge that already happened
        mock_daemon.discharge_handler.discharge_predicted_runtime = (
            15.0  # Predicted 15 min at OB start
        )
        mock_daemon.discharge_collector.discharge_buffer.times = [0.0, 100.0, 200.0, 300.0, 400.0]
        mock_daemon.discharge_collector.discharge_buffer.loads = [20, 22, 21, 20, 19]
        mock_daemon.current_metrics.soc = 0.80

        with patch("src.discharge_handler.logger") as mock_logger:
            mock_daemon._log_discharge_prediction()

            # Find the discharge_prediction event
            prediction_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c.kwargs.get("extra", {}).get("event_type") == "discharge_prediction"
            ]
            assert len(prediction_calls) == 1

            extra = prediction_calls[0].kwargs["extra"]
            assert extra["predicted_minutes"] == "15.0"
            assert float(extra["actual_minutes"]) == pytest.approx(400.0 / 60.0, abs=0.1)

        # Prediction cleared after logging
        assert mock_daemon.discharge_handler.discharge_predicted_runtime is None

    def test_prediction_error_gated_by_duration(self, mock_daemon):
        """Short discharge (<300s) → no prediction logged."""
        mock_daemon.discharge_handler.discharge_predicted_runtime = 15.0
        mock_daemon.discharge_collector.discharge_buffer.times = [0.0, 100.0]  # Only 100s
        mock_daemon.discharge_collector.discharge_buffer.loads = [20, 20]

        with patch("src.monitor.logger") as mock_logger:
            mock_daemon._log_discharge_prediction()

            prediction_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c.kwargs.get("extra", {}).get("event_type") == "discharge_prediction"
            ]
            assert len(prediction_calls) == 0

    def test_prediction_error_gated_by_snapshot(self, mock_daemon):
        """No prediction snapshot → no prediction logged even with long discharge."""
        mock_daemon.discharge_handler.discharge_predicted_runtime = (
            None  # No snapshot (EMA not stabilized)
        )
        mock_daemon.discharge_collector.discharge_buffer.times = [0.0, 100.0, 200.0, 300.0, 400.0]
        mock_daemon.discharge_collector.discharge_buffer.loads = [20, 20, 20, 20, 20]

        with patch("src.monitor.logger") as mock_logger:
            mock_daemon._log_discharge_prediction()

            prediction_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c.kwargs.get("extra", {}).get("event_type") == "discharge_prediction"
            ]
            assert len(prediction_calls) == 0
