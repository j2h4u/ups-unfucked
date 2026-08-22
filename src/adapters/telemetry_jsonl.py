"""Direct, best-effort writer for the eight-field telemetry stream."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.adapters.minimal_event_file import append, sample
from src.domain.values import BlackoutKind, PhysicalObservation


class TelemetryJsonlWriter:
    """Append only samples that carry physical outage/recharge evidence.

    The in-memory episode flag deliberately is not recovered from disk.  A
    daemon restart while charging writes the first below-full sample; a daemon
    starting on an ordinary full online UPS remains quiet until a new episode.
    """

    def __init__(
        self, model_data_dir: str | Path, *, silent_window_sec: float | None = None
    ) -> None:
        self._path = Path(model_data_dir) / "events" / "telemetry.jsonl"
        self._episode_active = False
        self._silent_window = (
            timedelta(seconds=silent_window_sec) if silent_window_sec is not None else None
        )
        self._silent_observations: list[PhysicalObservation] = []

    def write(self, observation: PhysicalObservation, physical_kind: BlackoutKind) -> bool:
        """Append one eligible sample; return whether a line was written."""
        if physical_kind in {BlackoutKind.BLACKOUT_REAL, BlackoutKind.BLACKOUT_TEST}:
            self._flush_silent_observations()
            append(self._path, _sample(observation))
            self._episode_active = True
            return True
        elif physical_kind == BlackoutKind.ONLINE:
            if observation.battery_pct is not None and observation.battery_pct < 100.0:
                append(self._path, _sample(observation))
                self._silent_observations.clear()
                self._episode_active = True
                return True
            elif not self._episode_active:
                if observation.battery_pct == 100.0:
                    self._remember_silent_observation(observation)
                return False
        else:
            return False

        append(self._path, _sample(observation))
        self._episode_active = False
        return True

    def _remember_silent_observation(self, observation: PhysicalObservation) -> None:
        if self._silent_window is None:
            return
        self._silent_observations.append(observation)
        cutoff = _observation_time(observation) - self._silent_window
        self._silent_observations = [
            item for item in self._silent_observations if _observation_time(item) >= cutoff
        ]

    def _flush_silent_observations(self) -> None:
        for observation in sorted(self._silent_observations, key=_observation_time):
            append(self._path, _sample(observation))
            self._silent_observations.remove(observation)


def _sample(observation: PhysicalObservation) -> dict[str, object]:
    at_text = _observation_time(observation).isoformat(timespec="seconds").replace("+00:00", "Z")
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


def _observation_time(observation: PhysicalObservation) -> datetime:
    at = observation.wall_time_utc
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)
