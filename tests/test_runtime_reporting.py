"""Bounded exporter, reporting scheduler, health, and MOTD contracts."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from src.alerter import JournaldHealthAlertSink
from src.application.capture_writer import (
    CaptureCommand,
    CaptureCommandKind,
    CaptureWriter,
)
from src.application.model_port import ModelPolicyProjection
from src.application.reporting import ReportingSnapshot
from src.application.reporting_scheduler import (
    ReportingScheduler,
    ReportingSchedulerDependencies,
)
from src.application.safety import SafetyInputs, calculate_safety, make_safety_publication
from src.application.storage_values import CaptureQueueHealth, EpochHistoryScan, StorageHealth
from src.battery_math.lut import LutPoint
from src.domain.decline import (
    assess_firmware_lb_reserve,
    assess_load_sag_trend,
    assess_long_partial_curve,
    storage_corruption_status,
)
from src.domain.values import (
    DEFAULT_IR_LEARNING_POLICY,
    BlackoutKind,
    FrozenModelSnapshot,
    PhysicalObservation,
    PlainLanguageReport,
    TerminalDisposition,
)
from src.monitor_config import ConfigError, load_config
from src.motd_status import render_motd
from src.virtual_ups_exporter import PollPublicationContext, VirtualUpsExporter


def _snapshot() -> FrozenModelSnapshot:
    return FrozenModelSnapshot(
        "2",
        "1",
        "a" * 32,
        "b" * 64,
        7.2,
        12.0,
        510.0,
        1.0,
        1.2,
        0.012,
        0.0,
        (LutPoint(13.7, 1.0, "standard"), LutPoint(10.8, 0.0, "anchor")),
    )


def _observation(status: str = "OB DISCHRG") -> PhysicalObservation:
    return PhysicalObservation(
        "boot-a",
        1_000_000_000,
        datetime(2026, 8, 16, tzinfo=timezone.utc),
        status,
        "13.30",
        13.3,
        0.01,
        20.0,
        0.0,
    )


def _storage_health() -> StorageHealth:
    return StorageHealth(
        capture_available=True,
        active_phase=None,
        queued_observations=None,
        durability_lag_s=0.0,
        consumed_step_budget_remaining=None,
        event_count=0,
        total_bytes=0,
        free_bytes=1024,
        alarm=None,
        bounded_error=None,
    )


def _decline():
    return (
        assess_load_sag_trend(()),
        assess_firmware_lb_reserve(()),
        assess_long_partial_curve(()),
    )


def test_virtual_export_uses_snapshot_sag_fields_and_retires_internal_resistance(
    tmp_path: Path,
) -> None:
    exporter = VirtualUpsExporter(
        virtual_ups_path=tmp_path / "ups.dev",
        health_path=tmp_path / "health.json",
    )
    observation = _observation("OB DISCHRG LB")
    snapshot = _snapshot()
    calculation = calculate_safety(
        inputs=SafetyInputs(13.3, 20.0, BlackoutKind.BLACKOUT_REAL, 5),
        snapshot=snapshot,
    )
    publication = make_safety_publication(observation, calculation)
    exporter.stage(PollPublicationContext(observation, snapshot, calculation, 7, 1.5))

    exporter.publish(publication)

    payload = (tmp_path / "ups.dev").read_text()
    assert "battery.load_sag.coefficient_v_per_load_percent: 0.012" in payload
    assert "battery.load_sag.reference_load_percent: 0" in payload
    assert "battery.internal_resistance" not in payload
    assert "ups.raw.lb_observed: yes" in payload


def test_health_and_motd_expose_storage_budget_and_decline_statuses(tmp_path: Path) -> None:
    exporter = VirtualUpsExporter(
        virtual_ups_path=tmp_path / "ups.dev",
        health_path=tmp_path / "health.json",
    )
    observation = _observation()
    snapshot = _snapshot()
    calculation = calculate_safety(
        inputs=SafetyInputs(13.3, 20.0, BlackoutKind.BLACKOUT_REAL, 5),
        snapshot=snapshot,
    )
    exporter.stage(PollPublicationContext(observation, snapshot, calculation, 1, 2.0))
    exporter.publish(make_safety_publication(observation, calculation))
    health = replace(
        _storage_health(),
        queued_observations=4,
        consumed_step_budget_remaining=252,
    )
    reporting = ReportingSnapshot(
        health=health,
        events=(),
    )
    capture = CaptureQueueHealth(
        capture_available=True,
        lifecycle_queued=0,
        observations_queued=4,
        observation_overflow_count=0,
        lifecycle_overflow_count=0,
        discarded_command_count=0,
        bounded_error=None,
    )

    assert exporter.publish_health(
        reporting,
        capture,
        consecutive_errors=0,
        decline=_decline(),
    )
    rendered = render_motd(health_path=tmp_path / "health.json")

    assert "queued_observations=4" in rendered
    assert "consumed_evidence_budget_remaining=252" in rendered
    assert "decline_load_sag_trend=insufficient_comparable_evidence" in rendered


def test_health_before_first_poll_is_explicitly_empty_and_keeps_bounded_error(
    tmp_path: Path,
) -> None:
    exporter = VirtualUpsExporter(
        virtual_ups_path=tmp_path / "ups.dev",
        health_path=tmp_path / "health.json",
    )
    exporter.record_error(RuntimeError("telemetry unavailable\nretrying"))
    capture_health = replace(
        CaptureWriter().health(),
        capture_available=False,
        bounded_error="terminal_recovery_failed OSError: gap failed",
    )

    assert exporter.publish_health(
        ReportingSnapshot(_storage_health(), ()),
        capture_health,
        consecutive_errors=2,
        decline=_decline(),
    )
    health = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))

    assert health["last_poll_utc"] is None
    assert health["physical_status"] is None
    assert health["virtual_status"] is None
    assert health["model_scientific_fingerprint"] is None
    assert health["consecutive_errors"] == 2
    assert health["bounded_error"] == "RuntimeError: telemetry unavailable retrying"
    assert health["capture_queue"]["capture_available"] is False
    assert health["capture_queue"]["bounded_error"] == (
        "terminal_recovery_failed OSError: gap failed"
    )


def test_successful_poll_clears_only_poll_error_channel(tmp_path: Path) -> None:
    exporter = VirtualUpsExporter(
        virtual_ups_path=tmp_path / "ups.dev",
        health_path=tmp_path / "health.json",
    )
    for channel in ("poll", "capture", "storage", "report", "background"):
        exporter.record_channel_error(channel, RuntimeError(f"{channel} failed"))

    observation = _observation("OL")
    snapshot = _snapshot()
    calculation = calculate_safety(
        inputs=SafetyInputs(13.3, 20.0, BlackoutKind.ONLINE, 5),
        snapshot=snapshot,
    )
    exporter.stage(PollPublicationContext(observation, snapshot, calculation, 1, 2.0))
    exporter.publish(make_safety_publication(observation, calculation))
    degraded_storage = replace(
        _storage_health(),
        alarm="storage failure",
        bounded_error="storage failure",
    )
    degraded_capture = replace(
        CaptureWriter().health(),
        capture_available=False,
        bounded_error="capture failure",
    )
    assert exporter.publish_health(
        ReportingSnapshot(degraded_storage, ()),
        degraded_capture,
        consecutive_errors=0,
        decline=_decline(),
    )

    channels = json.loads((tmp_path / "health.json").read_text())["error_channels"]
    assert channels["poll"] is None
    assert channels["capture"] == "capture failure"
    assert channels["storage"] == "storage failure"
    assert channels["report"] == "RuntimeError: report failed"
    assert channels["background"] == "RuntimeError: background failed"


def test_ol_to_ob_transition_and_latest_report_are_bounded_health_diagnostics(
    tmp_path: Path,
) -> None:
    exporter = VirtualUpsExporter(
        virtual_ups_path=tmp_path / "ups.dev",
        health_path=tmp_path / "health.json",
    )
    snapshot = _snapshot()
    online = replace(
        _observation("OL"),
        battery_voltage_v=13.5,
        load_percent=10.0,
        input_voltage_v=230.0,
    )
    on_battery = replace(
        _observation("OB DISCHRG"),
        battery_voltage_v=13.1,
        load_percent=30.0,
    )
    for sequence, (observation, kind) in enumerate(
        ((online, BlackoutKind.ONLINE), (on_battery, BlackoutKind.BLACKOUT_REAL))
    ):
        assert observation.load_percent is not None
        calculation = calculate_safety(
            inputs=SafetyInputs(13.3, observation.load_percent, kind, 5),
            snapshot=snapshot,
        )
        exporter.stage(PollPublicationContext(observation, snapshot, calculation, sequence, 1.0))
        exporter.publish(make_safety_publication(observation, calculation))
    exporter.record_report(
        PlainLanguageReport(
            blackout_id="blackout-a",
            disposition=TerminalDisposition.RECORDED_ONLY,
            lines=("No model change",),
            generated_utc=datetime(2026, 8, 16, tzinfo=timezone.utc),
        )
    )

    assert exporter.publish_health(
        ReportingSnapshot(_storage_health(), ()),
        CaptureWriter().health(),
        consecutive_errors=0,
        decline=_decline(),
    )
    health = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))

    assert health["last_apparent_ol_to_ob_sag"] == {
        "load_delta_percent": 20.0,
        "observed_utc": on_battery.wall_time_utc.isoformat(),
        "voltage_drop_v": pytest.approx(0.4),
    }
    assert health["last_report"] == {
        "blackout_id": "blackout-a",
        "disposition": "recorded_only",
        "generated_utc": "2026-08-16T00:00:00+00:00",
        "lines": ["No model change"],
    }


class _MaintenanceStore:
    def __init__(self) -> None:
        self.calls = []

    def storage_health(self, **kwargs):
        self.calls.append(("health", kwargs))
        return replace(
            _storage_health(),
            queued_observations=kwargs.get("queued_observations"),
            consumed_step_budget_remaining=kwargs.get("consumed_step_budget_remaining"),
        )

    def history_tail(self, _limit):
        return ()

    def history_scan_for_epoch(self, _battery_epoch_id):
        self.calls.append(("history", None))
        return EpochHistoryScan((), True)


class _HealthyReportingStore(_MaintenanceStore):
    def __init__(self) -> None:
        super().__init__()
        self.decline_calls = []

    def storage_health(self, **kwargs):
        self.calls.append(("health", kwargs))
        return replace(
            _storage_health(),
            queued_observations=kwargs.get("queued_observations"),
            consumed_step_budget_remaining=kwargs.get("consumed_step_budget_remaining"),
        )

    def history_tail(self, _limit):
        return ()

    def history_tail_for_epoch(self, _battery_epoch_id, _limit):
        raise AssertionError("decline reporting must use the eligibility-filtered query")

    def history_scan_for_epoch(self, _battery_epoch_id):
        self.decline_calls.append(_battery_epoch_id)
        return EpochHistoryScan((), True)


class _PolicyModel:
    def policy_projection(self):
        return ModelPolicyProjection(_snapshot(), "c" * 64, 0.012, None, frozenset({"d" * 64}))


class _ProjectedBudgetModel:
    def policy_projection(self):
        return ModelPolicyProjection(
            _snapshot(),
            "c" * 64,
            0.012,
            None,
            frozenset({"d" * 64}),
            replace(DEFAULT_IR_LEARNING_POLICY, max_consumed_step_hashes=300),
        )


class _HealthPublisher:
    def __init__(self) -> None:
        self.calls = []

    def publish_health(self, reporting, capture, *, consecutive_errors, decline):
        self.calls.append((reporting, capture, consecutive_errors, decline))
        return True


def _raise_terminal_writer_failure() -> None:
    raise RuntimeError("event validation failed\nretry is disabled")


def _capture_health(
    *,
    observation_overflow_count: int = 0,
    lifecycle_overflow_count: int = 0,
    discarded_command_count: int = 0,
    bounded_error: str | None,
) -> CaptureQueueHealth:
    return CaptureQueueHealth(
        capture_available=False,
        lifecycle_queued=0,
        observations_queued=0,
        observation_overflow_count=observation_overflow_count,
        lifecycle_overflow_count=lifecycle_overflow_count,
        discarded_command_count=discarded_command_count,
        bounded_error=bounded_error,
    )


@pytest.mark.parametrize(
    ("health", "reason"),
    (
        (
            _capture_health(
                observation_overflow_count=1,
                bounded_error="observation_queue_overflow",
            ),
            "observation_queue_overflow",
        ),
        (
            _capture_health(
                lifecycle_overflow_count=1,
                bounded_error="lifecycle_queue_overflow",
            ),
            "lifecycle_queue_overflow",
        ),
        (
            _capture_health(
                discarded_command_count=3,
                bounded_error="capture_writer_stopped_without_drain",
            ),
            "capture_writer_stopped_without_drain",
        ),
        (_capture_health(bounded_error=None), "capture_writer_unavailable"),
    ),
)
def test_capture_queue_alert_reports_overflow_stop_and_bounded_fallback(
    caplog: pytest.LogCaptureFixture,
    health: CaptureQueueHealth,
    reason: str,
) -> None:
    with caplog.at_level(logging.WARNING, logger="ups-battery-monitor"):
        JournaldHealthAlertSink().publish(health, _storage_health(), ())

    alerts = [
        record
        for record in caplog.records
        if getattr(record, "event_type", None) == "capture_queue_health_alert"
    ]
    assert len(alerts) == 1
    assert getattr(alerts[0], "capture_queue_error") == reason


def test_decline_storage_corruption_emits_distinct_operator_alert(
    caplog: pytest.LogCaptureFixture,
) -> None:
    status = storage_corruption_status("firmware_lb_reserve_proxy")

    with caplog.at_level(logging.WARNING, logger="ups-battery-monitor"):
        JournaldHealthAlertSink().publish(
            _capture_health(bounded_error=None),
            _storage_health(),
            (status,),
        )

    alerts = [
        record
        for record in caplog.records
        if getattr(record, "event_type", None) == "decline_evidence_storage_corrupt"
    ]
    assert len(alerts) == 1
    assert getattr(alerts[0], "metric") == "firmware_lb_reserve_proxy"


def test_reporting_scheduler_alerts_terminal_writer_failure_each_reporting_tick(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _HealthyReportingStore()
    writer = CaptureWriter()
    assert writer.submit(
        CaptureCommand(
            kind=CaptureCommandKind.END,
            execute=_raise_terminal_writer_failure,
            scope_id="blackout-a",
        )
    )
    assert writer.drain_one()
    assert not writer.health().capture_available
    publisher = _HealthPublisher()
    scheduler = ReportingScheduler(
        ReportingSchedulerDependencies(
            store=cast(Any, store),
            model=cast(Any, _PolicyModel()),
            writer=writer,
            publisher=publisher,
            health_alerts=JournaldHealthAlertSink(),
        )
    )

    with caplog.at_level(logging.WARNING, logger="ups-battery-monitor"):
        scheduler.tick(consecutive_errors=0)
        scheduler.tick(consecutive_errors=0)

    alerts = [
        record
        for record in caplog.records
        if getattr(record, "event_type", None) == "capture_queue_health_alert"
    ]
    assert len(alerts) == 2
    assert all(
        getattr(record, "capture_queue_error")
        == "RuntimeError: event validation failed retry is disabled"
        for record in alerts
    )
    assert len(store.decline_calls) == 2
    assert not any(
        getattr(record, "event_type", None) == "storage_health_alert" for record in caplog.records
    )


def test_reporting_scheduler_composes_real_queue_budget_and_direct_history() -> None:
    store = _MaintenanceStore()
    writer = CaptureWriter()
    for _ in range(3):
        writer.submit(
            CaptureCommand(
                kind=CaptureCommandKind.OBSERVATION,
                execute=lambda: None,
            )
        )
    publisher = _HealthPublisher()
    scheduler = ReportingScheduler(
        ReportingSchedulerDependencies(
            store=cast(Any, store),
            model=cast(Any, _PolicyModel()),
            writer=writer,
            publisher=publisher,
            health_alerts=JournaldHealthAlertSink(),
        )
    )

    snapshot = scheduler.tick(consecutive_errors=2)

    assert snapshot.health.queued_observations == 3
    assert snapshot.health.consumed_step_budget_remaining == 255
    assert [call[0] for call in store.calls if call[0] != "health"] == ["history"]


def test_reporting_scheduler_uses_the_projected_persisted_evidence_budget() -> None:
    store = _HealthyReportingStore()
    scheduler = ReportingScheduler(
        ReportingSchedulerDependencies(
            store=cast(Any, store),
            model=cast(Any, _ProjectedBudgetModel()),
            writer=CaptureWriter(),
            publisher=_HealthPublisher(),
            health_alerts=JournaldHealthAlertSink(),
        )
    )

    snapshot = scheduler.tick(consecutive_errors=0)

    assert snapshot.health.consumed_step_budget_remaining == 299


def test_motd_rejects_oversized_health_file(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    path.write_bytes(b"{" + b"x" * (64 * 1024) + b"}")
    rendered = render_motd(health_path=path)
    assert "physical_status=" in rendered
    assert "model_scientific_fingerprint=" in rendered


def test_config_rejects_duplicate_reference_load_source(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("reference_load_percent = 20\n")
    with pytest.raises(ConfigError, match="model-owned"):
        load_config(paths=(path,))


def test_config_warns_for_unknown_keys(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('ups_name = "cyberpower"\nshutdown_minutes = 7\nunexpected_setting = true\n')
    caplog.set_level(logging.WARNING, logger="ups-battery-monitor")

    config = load_config(paths=(path,))

    assert config.polling_interval == 1
    assert config.shutdown_minutes == 7
    assert "Ignoring unknown configuration keys: unexpected_setting" in caplog.text
