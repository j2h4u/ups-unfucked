"""Direct, best-effort writer for the eight-field telemetry stream."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.adapters.battery_history import (
    PRE_DISCHARGE_VOLTAGE_SLIDING_WINDOW_SECONDS,
    BatteryHistory,
    canonical_timestamp,
)
from src.adapters.jsonl_errors import EventCorruptionError
from src.adapters.minimal_event_file import MinimalEvent, TelemetrySample, append, sample
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
        self._path = Path(model_data_dir) / "telemetry.jsonl"
        self._history = BatteryHistory(self._path.with_name("history.jsonl"))
        self._episode_active = False
        self._episode_records: list[dict[str, object]] = []
        self._episode_kind: BlackoutKind = BlackoutKind.BLACKOUT_REAL
        self._completed_episode: tuple[dict[str, object], ...] | None = None
        self._recharging = False
        self._silent_window = (
            timedelta(seconds=silent_window_sec) if silent_window_sec is not None else None
        )
        self._recent_online: deque[PhysicalObservation] = deque(
            maxlen=PRE_DISCHARGE_VOLTAGE_SLIDING_WINDOW_SECONDS + 1
        )
        self._silent_observations: list[PhysicalObservation] = []
        self._post_full_until: datetime | None = None
        self._restore_active_episode()
        self._reconcile_closed_episodes()

    def _restore_active_episode(self) -> None:
        if not self._path.exists():
            return
        try:
            records = _read(self._path).records
        except EventCorruptionError:
            return
        if not records:
            return
        if _active_status(records[-1].get("status")):
            active_start = len(records) - 1
            while active_start > 0 and _active_status(records[active_start - 1].get("status")):
                active_start -= 1
            context_start = active_start
            while (
                context_start > 0
                and active_start - context_start < 5
                and _online_status(records[context_start - 1].get("status"))
            ):
                context_start -= 1
            context = records[context_start:active_start]
            self._restore_recent_online(context)
            self._episode_records = [dict(record) for record in records[context_start:]]
            self._episode_active = True
        elif _online_status(records[-1].get("status")):
            self._restore_recent_online(records)
        # Provenance is process-local; an active tail from a prior daemon is
        # never allowed to become a self-test after restart.
        self._episode_kind = BlackoutKind.BLACKOUT_REAL

    def _reconcile_closed_episodes(self) -> None:
        """Recover summaries whose raw terminal OL was durable before a restart."""
        if not self._path.exists():
            return
        try:
            records = _read(self._path).records
        except EventCorruptionError:
            return
        candidate = _latest_closed_episode(records)
        if candidate is None:
            return
        start, end = candidate
        start_at = _canonical_sample_time(records[start])
        episode_starts = {
            at
            for at, kind in self._history.event_kinds().items()
            if kind in {"blackout", "self_test"}
        }
        if start_at in episode_starts or any(at >= start_at for at in episode_starts):
            return
        context_start = max(0, start - PRE_DISCHARGE_VOLTAGE_SLIDING_WINDOW_SECONDS - 1)
        self._history.episode(
            [dict(row) for row in records[context_start : end + 1]],
            physical_kind=BlackoutKind.BLACKOUT_REAL,
        )

    def write(self, observation: PhysicalObservation, physical_kind: BlackoutKind) -> bool:
        """Append one eligible sample; return whether a line was written."""
        if physical_kind in {
            BlackoutKind.BLACKOUT_REAL,
            BlackoutKind.BLACKOUT_TEST,
        } and _active_status(observation.raw_status):
            if self._episode_active:
                row = _sample(observation)
                append(self._path, row)
                self._episode_records.append(row)
                return True
            context = self._event_context_rows()
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
            self._recharging = False
            self._episode_records = [*context, row]
            self._post_full_until = None
            return True
        if physical_kind == BlackoutKind.ONLINE:
            return self._write_online(observation)
        return False

    def _write_online(self, observation: PhysicalObservation) -> bool:
        if self._episode_active:
            row = _sample(observation)
            append(self._path, row)
            self._episode_records.append(row)
            self._episode_active = False
            self._recharging = _below_full(observation)
            self._post_full_until = _post_full_deadline(observation, self._silent_window)
            self._remember_recent_observation(observation, pending=False)
            records = self._episode_records
            self._episode_records = []
            self._history.episode(records, physical_kind=self._episode_kind)
            self._completed_episode = tuple(records)
            self._episode_kind = BlackoutKind.BLACKOUT_REAL
            return True
        if self._recharging:
            return self._write_recharge(observation)
        if self._post_full_until is not None:
            if _below_full(observation):
                self._recharging = True
                self._post_full_until = None
                return self._write_recharge(observation)
            if _observation_time(observation) <= self._post_full_until:
                append(self._path, _sample(observation))
                self._remember_recent_observation(observation, pending=False)
                return True
            self._post_full_until = None
        self._remember_silent_observation(observation)
        return False

    def _write_recharge(self, observation: PhysicalObservation) -> bool:
        row = _sample(observation)
        append(self._path, row)
        self._remember_recent_observation(observation, pending=False)
        if _below_full(observation):
            self._post_full_until = None
        else:
            self._recharging = False
            self._post_full_until = _post_full_deadline(observation, self._silent_window)
        return True

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
        self._remember_recent_observation(observation, pending=True)

    def _flush_silent_observations(self) -> None:
        for observation in sorted(self._silent_observations, key=_observation_time):
            append(self._path, _sample(observation))
            self._silent_observations.remove(observation)

    def _remember_recent_observation(
        self, observation: PhysicalObservation, *, pending: bool
    ) -> None:
        if not _online_status(observation.raw_status):
            self._recent_online.clear()
            self._silent_observations.clear()
            return
        if self._recent_online:
            previous = _observation_time(self._recent_online[-1])
            if _observation_time(observation) - previous != timedelta(seconds=1):
                self._recent_online.clear()
        self._recent_online.append(observation)
        if pending and self._silent_window is not None:
            self._silent_observations.append(observation)
            cutoff = _observation_time(observation) - self._silent_window
            self._silent_observations = [
                item for item in self._silent_observations if _observation_time(item) >= cutoff
            ]

    def _event_context_rows(self) -> list[dict[str, object]]:
        observations = {_observation_time(item): item for item in self._recent_online}
        observations.update({_observation_time(item): item for item in self._silent_observations})
        return [_sample(item) for item in sorted(observations.values(), key=_observation_time)]

    def _restore_recent_online(self, records: tuple[TelemetrySample, ...]) -> None:
        for row in records[-(PRE_DISCHARGE_VOLTAGE_SLIDING_WINDOW_SECONDS + 1) :]:
            if not _online_status(row.get("status")):
                self._recent_online.clear()
                continue
            try:
                at = _parse_observation_time(str(row["at"]))
            except (KeyError, ValueError):
                self._recent_online.clear()
                continue
            observation = PhysicalObservation(
                monotonic_ns=0,
                wall_time_utc=at,
                raw_status=str(row["status"]),
                battery_voltage_v=_optional_float(row.get("battery_v")),
                load_percent=_optional_float(row.get("load_pct")),
                input_voltage_v=_optional_float(row.get("input_v")),
                battery_pct=_optional_float(row.get("battery_pct")),
                runtime_s=_optional_float(row.get("runtime_s")),
                output_v=_optional_float(row.get("output_v")),
            )
            self._remember_recent_observation(observation, pending=False)


def _sample(observation: PhysicalObservation) -> dict[str, object]:
    at_text = _observation_time(observation).isoformat(timespec="seconds").replace("+00:00", "Z")
    return dict(
        sample(
            at_text,
            observation.battery_voltage_v,
            observation.battery_pct,
            observation.runtime_s,
            observation.load_percent,
            observation.input_voltage_v,
            observation.output_v,
            observation.raw_status,
        )
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


def _parse_observation_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("telemetry timestamp must include a timezone")
    return moment.astimezone(timezone.utc)


def _optional_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _canonical_sample_time(record: TelemetrySample) -> str:
    return canonical_timestamp(record["at"])


def _latest_closed_episode(
    records: tuple[TelemetrySample, ...],
) -> tuple[int, int] | None:
    for index in range(len(records) - 1, -1, -1):
        if not _active_status(records[index].get("status")):
            continue
        start = index
        while start > 0 and _active_status(records[start - 1].get("status")):
            start -= 1
        end = index + 1
        while end < len(records) and _active_status(records[end].get("status")):
            end += 1
        return (start, end) if end < len(records) else None
    return None


def _below_full(observation: PhysicalObservation) -> bool:
    return observation.battery_pct is not None and observation.battery_pct < 100.0


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
