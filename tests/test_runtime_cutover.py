import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

import src.monitor as monitor
from src.adapters.minimal_event_file import sample
from src.adapters.telemetry_jsonl import TelemetryJsonlWriter
from src.application.safety import SafetyPublication
from src.battery_math.lut import LutPoint
from src.domain.values import BlackoutKind, FrozenModelSnapshot, PhysicalObservation
from src.monitor import MonitorDaemon, RuntimeDependencies
from src.monitor_config import Config


class _Telemetry:
    def __init__(self, observations: tuple[PhysicalObservation, ...]) -> None:
        self._observations = iter(observations)

    def read(self) -> PhysicalObservation:
        return next(self._observations)


class _Model:
    def current_snapshot(self) -> FrozenModelSnapshot:
        return FrozenModelSnapshot(
            "1",
            "b" * 64,
            7.2,
            12.0,
            510.0,
            1.0,
            1.2,
            0.015,
            0.0,
            (LutPoint(13.7, 1.0, "standard"), LutPoint(10.8, 0.0, "anchor")),
        )

    def close(self) -> None:
        return None


class _Publisher:
    def __init__(self) -> None:
        self.publications: list[SafetyPublication] = []
        self.channels: list[tuple[str, BaseException | str]] = []

    def stage(self, _context: object) -> None:
        return None

    def publish(self, publication: SafetyPublication) -> None:
        self.publications.append(publication)

    def record_channel_error(self, channel: str, error: BaseException | str) -> None:
        self.channels.append((channel, error))

    def clear_channel_error(self, _channel: str) -> None:
        return None

    def invalidate_output(self) -> None:
        return None

    def handle_poll_failure(self, _error: BaseException, *, now: float | None = None) -> object:
        del now
        return None

    @property
    def watchdog_healthy(self) -> bool:
        return True


def _observation(status: str, battery_pct: float, *, monotonic_ns: int = 1) -> PhysicalObservation:
    return PhysicalObservation(
        "boot",
        monotonic_ns,
        datetime(2026, 8, 22, tzinfo=timezone.utc),
        status,
        "13.3",
        13.3,
        20.0,
        0.0 if status.startswith("OB") else 230.0,
        battery_pct=battery_pct,
        runtime_s=600.0,
        output_v=230.0,
    )


def _daemon(tmp_path: Path, observations: tuple[PhysicalObservation, ...], writer: Any):
    return MonitorDaemon(
        Config(model_dir=tmp_path),
        RuntimeDependencies(
            telemetry=_Telemetry(observations),
            model=_Model(),
            publisher=_Publisher(),
            telemetry_writer=writer,
        ),
    )


def test_safety_publication_is_followed_by_minimal_telemetry(tmp_path: Path) -> None:
    blackout = _observation("OB DISCHRG", 40.0)
    charging = _observation("OL CHRG", 80.0)
    full = _observation("OL", 100.0)
    writer = TelemetryJsonlWriter(tmp_path)
    daemon = _daemon(tmp_path, (blackout, blackout, charging, full, full), writer)

    for _ in range(5):
        daemon.poll_once()

    rows = [
        json.loads(line)
        for line in (tmp_path / "events" / "telemetry.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 4
    assert len(rows[0]) == 8


def test_telemetry_storage_error_is_health_only(tmp_path: Path) -> None:
    class FailingWriter:
        def write(self, _observation: PhysicalObservation, _kind: BlackoutKind) -> bool:
            raise OSError("read-only events directory")

    publisher = _Publisher()
    daemon = MonitorDaemon(
        Config(model_dir=tmp_path),
        RuntimeDependencies(
            telemetry=_Telemetry((_observation("OB DISCHRG", 40.0),)),
            model=_Model(),
            publisher=publisher,
            telemetry_writer=FailingWriter(),  # type: ignore[arg-type]
        ),
    )

    result = daemon.poll_once()

    assert result.publication is publisher.publications[0]
    assert publisher.channels and publisher.channels[0][0] == "storage"


def _write_event(tmp_path: Path, status: str, at: str) -> None:
    event_path = tmp_path / "events" / "telemetry.jsonl"
    event_path.parent.mkdir()
    event_path.write_text(
        json.dumps(sample(at, 13.3, 80.0, 600.0, 20.0, 230.0, 230.0, status)) + "\n"
    )


def _command_config(tmp_path: Path) -> Path:
    path = tmp_path / "upsmon.conf"
    path.write_text("MONITOR cyberpower-virtual@localhost 1 test-user [REDACTED_SECRET] primary\n")
    return path


def test_quick_self_test_waits_for_ema_horizon_then_runs_once(tmp_path: Path, monkeypatch) -> None:
    command_config = _command_config(tmp_path)
    monkeypatch.setattr(monitor, "NUT_COMMAND_CONFIG_PATH", command_config)
    command = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(monitor.subprocess, "run", command)
    daemon = _daemon(
        tmp_path,
        (
            _observation("OL", 100.0, monotonic_ns=1_000_000_000),
            _observation("OL", 100.0, monotonic_ns=60_000_000_000),
            _observation("OL", 100.0, monotonic_ns=121_000_000_000),
            _observation("OL", 100.0, monotonic_ns=122_000_000_000),
        ),
        TelemetryJsonlWriter(tmp_path),
    )

    daemon.poll_once()
    daemon.poll_once()
    command.assert_not_called()
    daemon.poll_once()
    daemon.poll_once()

    command.assert_called_once()
    assert command.call_args.args[0][-2:] == ["cyberpower@localhost", "test.battery.start.quick"]


@pytest.mark.parametrize("status", ["OB DISCHRG", "CAL"])
def test_quick_self_test_respects_recent_natural_blackout_and_self_test(
    tmp_path: Path, monkeypatch, status: str
) -> None:
    command_config = _command_config(tmp_path)
    monkeypatch.setattr(monitor, "NUT_COMMAND_CONFIG_PATH", command_config)
    command = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(monitor.subprocess, "run", command)
    _write_event(tmp_path, status, "2026-08-21T00:00:00Z")
    daemon = _daemon(
        tmp_path,
        (
            _observation("OL", 100.0, monotonic_ns=1_000_000_000),
            _observation("OL", 100.0, monotonic_ns=121_000_000_000),
        ),
        TelemetryJsonlWriter(tmp_path),
    )

    daemon.poll_once()
    daemon.poll_once()

    command.assert_not_called()


@pytest.mark.parametrize("status, battery_pct", [("OL", 99.0), ("OB DISCHRG", 100.0)])
def test_quick_self_test_requires_full_online_ups(
    tmp_path: Path, monkeypatch, status: str, battery_pct: float
) -> None:
    command_config = _command_config(tmp_path)
    monkeypatch.setattr(monitor, "NUT_COMMAND_CONFIG_PATH", command_config)
    command = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(monitor.subprocess, "run", command)
    daemon = _daemon(
        tmp_path,
        (
            _observation("OL", 100.0, monotonic_ns=1_000_000_000),
            _observation(status, battery_pct, monotonic_ns=121_000_000_000),
        ),
        TelemetryJsonlWriter(tmp_path),
    )

    daemon.poll_once()
    daemon.poll_once()

    command.assert_not_called()


def test_quick_self_test_skips_malformed_telemetry_without_affecting_publication(
    tmp_path: Path, monkeypatch
) -> None:
    command_config = _command_config(tmp_path)
    monkeypatch.setattr(monitor, "NUT_COMMAND_CONFIG_PATH", command_config)
    command = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(monitor.subprocess, "run", command)
    event_path = tmp_path / "events" / "telemetry.jsonl"
    event_path.parent.mkdir()
    event_path.write_text("not-json\n")
    daemon = _daemon(
        tmp_path,
        (
            _observation("OL", 100.0, monotonic_ns=1_000_000_000),
            _observation("OL", 100.0, monotonic_ns=121_000_000_000),
        ),
        TelemetryJsonlWriter(tmp_path),
    )

    result = daemon.poll_once()
    result = daemon.poll_once()

    command.assert_not_called()
    assert result.publication


def test_quick_self_test_failure_is_health_only_and_not_retried_same_day(
    tmp_path: Path, monkeypatch
) -> None:
    command_config = _command_config(tmp_path)
    monkeypatch.setattr(monitor, "NUT_COMMAND_CONFIG_PATH", command_config)
    command = Mock(return_value=Mock(returncode=1))
    monkeypatch.setattr(monitor.subprocess, "run", command)
    daemon = _daemon(
        tmp_path,
        (
            _observation("OL", 100.0, monotonic_ns=1_000_000_000),
            _observation("OL", 100.0, monotonic_ns=121_000_000_000),
        ),
        TelemetryJsonlWriter(tmp_path),
    )

    first = daemon.poll_once()
    second = daemon.poll_once()

    command.assert_called_once()
    assert first.publication and second.publication
