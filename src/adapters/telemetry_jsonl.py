"""Direct, best-effort writer for the eight-field telemetry stream."""

from __future__ import annotations

from datetime import timezone
from pathlib import Path

from src.adapters.minimal_event_file import append, sample
from src.domain.values import BlackoutKind, PhysicalObservation


class TelemetryJsonlWriter:
    """Append only samples that carry physical outage/recharge evidence.

    The in-memory episode flag deliberately is not recovered from disk.  A
    daemon restart while charging writes the first below-full sample; a daemon
    starting on an ordinary full online UPS remains quiet until a new episode.
    """

    def __init__(self, model_data_dir: str | Path) -> None:
        self._path = Path(model_data_dir) / "events" / "telemetry.jsonl"
        self._episode_active = False

    def write(self, observation: PhysicalObservation, physical_kind: BlackoutKind) -> bool:
        """Append one eligible sample; return whether a line was written."""
        if physical_kind in {BlackoutKind.BLACKOUT_REAL, BlackoutKind.BLACKOUT_TEST}:
            append(self._path, _sample(observation))
            self._episode_active = True
            return True
        elif physical_kind == BlackoutKind.ONLINE:
            if observation.battery_pct is not None and observation.battery_pct < 100.0:
                append(self._path, _sample(observation))
                self._episode_active = True
                return True
            elif not self._episode_active:
                return False
        else:
            return False

        append(self._path, _sample(observation))
        self._episode_active = False
        return True


def _sample(observation: PhysicalObservation) -> dict[str, object]:
    at = observation.wall_time_utc
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    at_text = at.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return sample(
        at_text,
        observation.battery_voltage_v,
        observation.battery_pct,
        observation.runtime_s,
        observation.load_percent,
        observation.input_voltage_v,
        observation.output_v,
        observation.raw_status,
    )
