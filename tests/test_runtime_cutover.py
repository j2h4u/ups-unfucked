import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
            "2",
            "1",
            "a" * 32,
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


def _observation(status: str, battery_pct: float) -> PhysicalObservation:
    return PhysicalObservation(
        "boot",
        1,
        datetime(2026, 8, 22, tzinfo=timezone.utc),
        status,
        "13.3",
        13.3,
        0.01,
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
