"""Direct, best-effort writer for the eight-field telemetry stream."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.adapters.battery_history import BatteryHistory
from src.adapters.jsonl_errors import EventCorruptionError
from src.adapters.minimal_event_file import MinimalEvent, append, sample
from src.adapters.minimal_event_file import read as _read
from src.domain.values import BlackoutKind, PhysicalObservation


class TelemetryJsonlWriter:
    """Append only samples that carry physical outage/recharge evidence.

    An unfinished OB/CAL tail is recovered from the append-only stream so a
    daemon restart can close it on the first OL observation.  Ordinary OL
    tails remain silent until a new episode starts.
    """

    def __init__(
        self, model_data_dir: str | Path, *, silent_window_sec: float | None = None
    ) -> None:
        self._path = Path(model_data_dir) / "events" / "telemetry.jsonl"
        self._history = BatteryHistory(self._path.with_name("history.jsonl"))
        self._episode_active = False
        self._episode_records: list[dict[str, object]] = []
        self._episode_kind: BlackoutKind = BlackoutKind.BLACKOUT_REAL
        self._completed_episode: tuple[dict[str, object], ...] | None = None
        self._restore_active_episode()
        self._silent_window = (
            timedelta(seconds=silent_window_sec) if silent_window_sec is not None else None
        )
        self._silent_observations: list[PhysicalObservation] = []
        self._post_full_until: datetime | None = None

    def _restore_active_episode(self) -> None:
        if not self._path.exists():
            return
        try:
            records = _read(self._path).records
        except EventCorruptionError:
            return
        if not records or not _active_status(records[-1].get("status")):
            return
        start = len(records) - 1
        while start > 0 and not _online_status(records[start - 1].get("status")):
            start -= 1
        self._episode_records = [dict(record) for record in records[start:]]
        self._episode_active = True
        # Provenance is process-local; an active tail from a prior daemon is
        # never allowed to become a self-test after restart.
        self._episode_kind = BlackoutKind.BLACKOUT_REAL

    def write(self, observation: PhysicalObservation, physical_kind: BlackoutKind) -> bool:
        """Append one eligible sample; return whether a line was written."""
        if physical_kind in {BlackoutKind.BLACKOUT_REAL, BlackoutKind.BLACKOUT_TEST}:
            context = [_sample(item) for item in self._silent_observations]
            self._flush_silent_observations()
            row = _sample(observation)
            append(self._path, row)
            if not self._episode_active:
                self._episode_kind = (
                    physical_kind
                    if physical_kind == BlackoutKind.BLACKOUT_TEST
                    else BlackoutKind.BLACKOUT_REAL
                )
            self._episode_active = True
            self._episode_records.extend((*context, row))
            self._post_full_until = None
            return True
        elif physical_kind == BlackoutKind.ONLINE:
            return self._write_online(observation)
        return False

    def _write_online(self, observation: PhysicalObservation) -> bool:
        if observation.battery_pct is not None and observation.battery_pct < 100.0:
            self._flush_silent_observations()
            append(self._path, _sample(observation))
            self._silent_observations.clear()
            if not self._episode_active:
                self._episode_kind = BlackoutKind.BLACKOUT_REAL
            self._episode_active = True
            self._episode_records.append(_sample(observation))
            self._post_full_until = None
            return True
        if self._episode_active:
            row = _sample(observation)
            append(self._path, row)
            self._episode_records.append(row)
            self._episode_active = False
            self._post_full_until = _post_full_deadline(observation, self._silent_window)
            records = self._episode_records
            self._episode_records = []
            self._history.episode(records, physical_kind=self._episode_kind)
            self._completed_episode = tuple(records)
            self._episode_kind = BlackoutKind.BLACKOUT_REAL
            return True
        if observation.battery_pct != 100.0:
            return False
        if self._post_full_until is not None:
            if _observation_time(observation) <= self._post_full_until:
                append(self._path, _sample(observation))
                return True
            self._post_full_until = None
        self._remember_silent_observation(observation)
        return False

    def take_completed_episode(self) -> tuple[dict[str, object], ...] | None:
        """Return a newly closed episode once, after its telemetry is durable."""
        completed = self._completed_episode
        self._completed_episode = None
        return completed

    def event_kinds(self) -> dict[str, str]:
        """Expose the small history index needed to avoid repeat feedback."""
        return self._history.event_kinds()

    def record_ir_observation(self, observation: Mapping[str, Any] | object) -> bool:
        """Persist an extracted IR observation in the existing history file."""
        values = _observation_values(observation)
        return self._history.ir_observation(**values)

    def ir_observations(self) -> list[dict[str, Any]]:
        """Expose persisted, not-yet-consumed IR observations."""
        return self._history.ir_observations()

    def upsert_model_update(self, receipt: Mapping[str, Any]) -> bool:
        """Recover a model receipt into history idempotently."""
        return self._history.upsert_model_update(receipt)

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


def _observation_values(observation: Mapping[str, Any] | object) -> dict[str, Any]:
    if isinstance(observation, Mapping):

        def get(key: str) -> Any:
            return observation[key]
    else:

        def get(key: str) -> Any:
            return getattr(observation, key)

    return {
        "event_at": str(get("event_at")),
        "estimate": float(get("estimate")),
        "evidence_at": str(get("evidence_at")),
        "uncertainty": float(get("uncertainty")),
        "reason": str(get("reason")),
    }


def _observation_time(observation: PhysicalObservation) -> datetime:
    at = observation.wall_time_utc
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)


def _post_full_deadline(
    observation: PhysicalObservation, window: timedelta | None
) -> datetime | None:
    if observation.battery_pct != 100.0 or window is None:
        return None
    return _observation_time(observation) + window


def _active_status(status: object) -> bool:
    flags = str(status).split()
    return "OB" in flags or "CAL" in flags


def _online_status(status: object) -> bool:
    return "OL" in str(status).split()


def read(path: Path) -> MinimalEvent:
    """Read the canonical strict telemetry stream for composition callers."""
    return _read(path)
