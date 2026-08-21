"""Single-file telemetry adapter with in-memory lifecycle projections."""

from __future__ import annotations

import shutil
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.adapters.jsonl_errors import EventCorruptionError
from src.adapters.minimal_event_file import (
    TELEMETRY_FILENAME,
    EventKind,
    MinimalEvent,
    append,
    create_event,
    encode,
    end,
    event_filename,
    gap,
    header,
    open_event,
    parse_filename,
    read,
    sample,
    scan,
)
from src.application.storage_values import (
    CaptureCloseReconciliation,
    CaptureCloseState,
    EpochHistoryScan,
    EpochHistoryTail,
    EventHandle,
    EventProjection,
    EventRecord,
    EventRef,
    EventStart,
    EventSummary,
    ProcessingRef,
    ProjectedEventRecord,
    RecoveredCapture,
    RecoveredObservation,
    ReportNoticeIdentity,
    SealedEventRef,
    StorageHealth,
    TerminalOutcomeRecord,
    WorkRegistry,
)
from src.domain.reasons import InfrastructureReason

_SYNTHETIC_BOOT = "telemetry"
_ACTIVE = "active"
_BLACKOUT: EventKind = "blackout"
_RECHARGE: EventKind = "recharge"

__all__ = (
    "EventKind",
    "MinimalEvent",
    "MinimalJsonlEventStore",
    "MinimalReportOutbox",
    "append",
    "create_event",
    "encode",
    "end",
    "event_filename",
    "gap",
    "header",
    "open_event",
    "parse_filename",
    "read",
    "sample",
    "scan",
)


class MinimalJsonlEventStore:
    """Keep the application storage ports while persisting only samples.

    The stream is replayed on every query.  The projection recognizes a
    blackout as contiguous ``OB*`` samples and a recharge as contiguous
    ``OL* CHRG`` samples.  The normal full-charge sample (``OL`` with 100%)
    remains in the stream and closes the recharge episode.  No lifecycle or
    learning outcome is written; completed episodes are conservatively
    projected as ``recorded_only``.
    """

    def __init__(self, model_data_dir: str | Path, *, writer_lock_fd: int | None = None) -> None:
        del writer_lock_fd
        self._events_path = Path(model_data_dir) / "events"
        self._events_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._telemetry_path = self._events_path / TELEMETRY_FILENAME
        self._closed = False

    def close(self) -> None:
        self._closed = True

    @property
    def report_outbox(self) -> "MinimalReportOutbox":
        return MinimalReportOutbox()

    def open(self, start: EventStart) -> EventHandle:
        path = create_event(self._events_path, start.event_kind, start.wall_time_utc)
        append(
            path, _sample_from_observation(start.payload.get("observation"), start.wall_time_utc)
        )
        episodes = _episodes(read(path).records)
        candidates = [item for item in episodes if item.kind == start.event_kind]
        if not candidates:
            # Older callers may omit the observation payload.  Keep their
            # capture command lossless with an explicit non-episode sample;
            # replay remains fail-closed until a real status sample arrives.
            return EventHandle(
                f"{start.event_kind}-unclassified",
                TELEMETRY_FILENAME,
                TELEMETRY_FILENAME,
                len(read(path).records),
                start.event_kind,
            )
        episode = candidates[-1]
        return EventHandle(
            episode.id,
            TELEMETRY_FILENAME,
            TELEMETRY_FILENAME,
            len(read(path).records),
            start.event_kind,
        )

    def append(self, handle: EventHandle, record: EventRecord) -> EventHandle:
        path = self._path_for(handle)
        if record.record_type == "observation":
            observation = record.payload.get("observation", record.payload)
            append(path, _sample_from_observation(observation, record.wall_time_utc))
        elif record.record_type == "end":
            # The terminal command carries the last physical observation.  It
            # is valuable telemetry and must survive even though the
            # termination label itself has no sample-only representation.
            append(
                path,
                _sample_from_observation(record.payload.get("observation"), record.wall_time_utc),
            )
        # gap/derived records have no representation in the sample-only wire
        # contract.  Their capture remains safe because observations are
        # appended before these best-effort lifecycle calls.
        count = len(read(path).records)
        return EventHandle(
            handle.blackout_id, TELEMETRY_FILENAME, TELEMETRY_FILENAME, count, handle.event_kind
        )

    def seal(self, handle: EventHandle, outcome: TerminalOutcomeRecord) -> SealedEventRef:
        del outcome
        self._path_for(handle)
        return SealedEventRef(handle.blackout_id, TELEMETRY_FILENAME)

    def recover_startup(self) -> RecoveredCapture | None:
        event = _read_if_present(self._telemetry_path)
        if event is None:
            return None
        episodes = _episodes(event.records)
        active = next((item for item in reversed(episodes) if item.active), None)
        if active is None or not active.samples:
            return None
        last = _recovered_observation(active.samples[-1])
        first = _recovered_observation(active.samples[0])
        handle = EventHandle(
            active.id, TELEMETRY_FILENAME, TELEMETRY_FILENAME, len(event.records), active.kind
        )
        return RecoveredCapture(handle, _SYNTHETIC_BOOT, last, first)

    def work_registry(self) -> WorkRegistry:
        event = _read_if_present(self._telemetry_path)
        if event is None:
            return WorkRegistry(None, ())
        episodes = _episodes(event.records)
        active = next((item for item in reversed(episodes) if item.active), None)
        capture = None
        if active is not None:
            from src.application.storage_values import CapturingEventRef

            capture = CapturingEventRef(
                active.id, TELEMETRY_FILENAME, TELEMETRY_FILENAME, active.kind
            )
        pending = tuple(
            ProcessingRef(item.id, TELEMETRY_FILENAME, "end_durable")
            for item in episodes
            if not item.active and item.kind == _BLACKOUT
        )
        return WorkRegistry(capture, pending)

    def checkpoint_processing(self, handle: EventHandle, frozen_stage: str) -> None:
        del handle, frozen_stage

    def reconcile_damaged_close(
        self, blackout_id: str, current_handle: EventHandle | None
    ) -> CaptureCloseReconciliation:
        event = _read_if_present(self._telemetry_path)
        if event is None:
            return CaptureCloseReconciliation(CaptureCloseState.UNKNOWN, current_handle)
        episodes = _episodes(event.records)
        item = _episode_for_handle(episodes, current_handle)
        if item is None:
            item = next((candidate for candidate in episodes if candidate.id == blackout_id), None)
        if item is None:
            return CaptureCloseReconciliation(CaptureCloseState.UNKNOWN, current_handle)
        handle = _handle_for(item, len(event.records))
        return CaptureCloseReconciliation(
            CaptureCloseState.ACTIVE if item.active else CaptureCloseState.OUTCOME,
            handle,
        )

    def acknowledge_capture_recovery(self) -> None:
        return None

    def project(self, ref: EventRef | EventHandle | SealedEventRef) -> EventProjection:
        event = _read_if_present(self._telemetry_path)
        if event is None:
            raise EventCorruptionError("telemetry stream is missing")
        episodes = _episodes(event.records)
        item = _episode_for_ref(episodes, ref)
        if item is None:
            raise EventCorruptionError("telemetry episode is not reconstructable")
        return _project_episode(item)

    def sealed_event_projections(
        self, start_utc: str, end_utc: str, *, event_kind: str = _BLACKOUT
    ) -> tuple[EventProjection, ...]:
        event = _read_if_present(self._telemetry_path)
        if event is None:
            return ()
        start, end_at = _parse_datetime(start_utc), _parse_datetime(end_utc)
        result = []
        for item in _episodes(event.records):
            if item.kind != event_kind or item.active:
                continue
            if start <= _parse_datetime(item.samples[0]["at"]) < end_at:
                result.append(_project_episode(item))
        return tuple(result)

    def sealed_recharge_projections_for_blackouts(
        self, blackout_ids: Collection[str]
    ) -> tuple[EventProjection, ...]:
        wanted = frozenset(blackout_ids)
        if not wanted:
            return ()
        event = _read_if_present(self._telemetry_path)
        if event is None:
            return ()
        episodes = _episodes(event.records)
        blackouts = [item for item in episodes if item.kind == _BLACKOUT and item.id in wanted]
        result = []
        for blackout in blackouts:
            next_recharge = next(
                (
                    item
                    for item in episodes
                    if item.kind == _RECHARGE and item.start_index > blackout.end_index
                ),
                None,
            )
            if next_recharge is not None and not next_recharge.active:
                result.append(_project_episode(next_recharge))
        return tuple(result)

    def history_tail(self, limit: int) -> tuple[EventSummary, ...]:
        event = _read_if_present(self._telemetry_path)
        if event is None:
            return ()
        values = [
            _summary(item)
            for item in _episodes(event.records)
            if item.kind == _BLACKOUT and not item.active
        ]
        values.sort(key=lambda item: item.started_utc, reverse=True)
        return tuple(values[:limit])

    def history_tail_for_epoch(self, battery_epoch_id: str, limit: int) -> EpochHistoryTail:
        del battery_epoch_id
        values = self.history_tail(limit)
        return EpochHistoryTail(values, 0, True)

    def history_scan_for_epoch(self, battery_epoch_id: str) -> EpochHistoryScan:
        del battery_epoch_id
        return EpochHistoryScan(self.history_tail(2**31 - 1), True)

    def storage_health(
        self,
        *,
        queued_observations: int | None = None,
        consumed_step_budget_remaining: int | None = None,
    ) -> StorageHealth:
        event = _read_if_present(self._telemetry_path)
        active = event is not None and any(item.active for item in _episodes(event.records))
        return StorageHealth(
            not self._closed,
            _ACTIVE if active else None,
            queued_observations,
            0.0,
            consumed_step_budget_remaining,
            1 if event is not None else 0,
            self._telemetry_path.stat().st_size if event is not None else 0,
            shutil.disk_usage(self._events_path).free,
            None,
            None,
        )

    def reject_processing(
        self, processing: ProcessingRef, reason: InfrastructureReason
    ) -> SealedEventRef:
        del reason
        return SealedEventRef(processing.blackout_id, TELEMETRY_FILENAME)

    def _path_for(self, handle: EventHandle) -> Path:
        if handle.path_token != TELEMETRY_FILENAME or not self._telemetry_path.is_file():
            raise EventCorruptionError("telemetry stream is missing")
        return self._telemetry_path


class MinimalReportOutbox:
    def report_outbox_pending(self, limit: int) -> tuple[ReportNoticeIdentity, ...]:
        del limit
        return ()

    def acknowledge_report_notice(self, notice: ReportNoticeIdentity) -> None:
        del notice


@dataclass(slots=True)
class _Episode:
    kind: EventKind
    samples: list[dict[str, Any]]
    start_index: int
    end_index: int
    active: bool
    id: str
    termination: str | None = None


def _episodes(records: tuple[dict[str, Any], ...]) -> tuple[_Episode, ...]:
    result: list[_Episode] = []
    current: _Episode | None = None
    ordinals = {_BLACKOUT: 0, _RECHARGE: 0}
    for index, record in enumerate(records):
        status = str(record["status"])
        kind = _next_kind(current, record, status)
        if current is not None and kind != current.kind:
            current.end_index = index - 1
            current.active = False
            current.termination = _termination_for_transition(current, status, record)
            current = None
        if current is not None and current.kind == _RECHARGE:
            current.samples.append(record)
            current.end_index = index
            if status.startswith("OL") and "CHRG" not in status and record["battery_pct"] == 100:
                current.active = False
                current.termination = "charge_complete"
                current = None
            continue
        if current is not None and current.kind == _BLACKOUT:
            current.samples.append(record)
            current.end_index = index
            continue
        if kind is None:
            continue
        current = _Episode(kind, [record], index, index, True, f"{kind}-{ordinals[kind]}")
        ordinals[kind] += 1
        result.append(current)
    return tuple(result)


def _kind(status: str, record: Mapping[str, Any] | None = None) -> EventKind | None:
    if status.startswith("OB"):
        return _BLACKOUT
    if status.startswith("OL") and _is_recharge_sample(status, record):
        return _RECHARGE
    return None


def _next_kind(
    current: _Episode | None, record: Mapping[str, Any], status: str
) -> EventKind | None:
    kind: EventKind | None = None
    if current is None:
        kind = _kind(status, record)
    elif current.kind == _BLACKOUT:
        if status.startswith("OB"):
            kind = _BLACKOUT
        elif status.startswith("OL") and _is_recharge_sample(status, record):
            kind = _RECHARGE
    elif status.startswith("OL"):
        kind = _RECHARGE
    elif status.startswith("OB"):
        kind = _BLACKOUT
    return kind


def _is_recharge_sample(status: str, record: Mapping[str, Any] | None) -> bool:
    if "CHRG" in status:
        return True
    return record is not None and record.get("battery_pct") != 100


def _termination_for_transition(current: _Episode, status: str, record: Mapping[str, Any]) -> str:
    if current.kind == _RECHARGE and status.startswith("OB"):
        return "superseded_by_blackout"
    if current.kind == _RECHARGE and status.startswith("OL") and record.get("battery_pct") == 100:
        return "charge_complete"
    if current.kind == _BLACKOUT and status.startswith("OL"):
        return "power_restored"
    return "unknown"


def _read_if_present(path: Path) -> MinimalEvent | None:
    return read(path) if path.exists() else None


def _sample_from_observation(value: Any, fallback_at: str) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    return sample(
        str(raw.get("wall_time_utc", fallback_at)),
        _number(raw.get("battery_voltage_v")),
        _number(raw.get("battery_pct", raw.get("battery_percent", raw.get("battery_charge_pct")))),
        _number(raw.get("runtime_s", raw.get("battery_runtime_s", raw.get("runtime")))),
        _number(raw.get("load_percent")),
        _number(raw.get("input_voltage_v")),
        _number(raw.get("output_v", raw.get("output_voltage_v"))),
        str(raw.get("raw_status", raw.get("status", "UNKNOWN"))) or "UNKNOWN",
    )


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def _recovered_observation(value: Mapping[str, Any]) -> RecoveredObservation:
    payload = {
        "boot_id": _SYNTHETIC_BOOT,
        "monotonic_ns": _monotonic(str(value["at"])),
        "wall_time_utc": value["at"],
        "raw_status": value["status"],
        "battery_voltage_raw": None if value["battery_v"] is None else str(value["battery_v"]),
        "battery_voltage_v": value["battery_v"],
        "voltage_token_quantum_v": 0.1 if value["battery_v"] is not None else None,
        "load_percent": value["load_pct"],
        "input_voltage_v": value["input_v"],
        "battery_pct": value["battery_pct"],
        "runtime_s": value["runtime_s"],
        "output_v": value["output_v"],
    }
    return RecoveredObservation(_SYNTHETIC_BOOT, value["at"], _monotonic(str(value["at"])), payload)


def _project_episode(item: _Episode) -> EventProjection:
    records: list[ProjectedEventRecord] = []
    start_sample = item.samples[0]
    start = _projected(
        "start", item, start_sample, {"observation": _observation_payload(start_sample)}, 0
    )
    records.append(start)
    observations = tuple(
        _projected("observation", item, value, _observation_payload(value), index + 1)
        for index, value in enumerate(item.samples)
    )
    records.extend(observations)
    end = None
    outcome = None
    if not item.active:
        terminal = item.samples[-1]
        end = _projected(
            "end",
            item,
            terminal,
            {
                "termination": item.termination
                or ("power_restored" if item.kind == _BLACKOUT else "unknown")
            },
            len(records),
        )
        records.append(end)
        outcome = _projected(
            "outcome",
            item,
            terminal,
            {
                "disposition": "recorded_only",
                "evidence_class": "operational_only",
                "reasons": ["terminal_outcome_unavailable"],
            },
            len(records),
        )
        records.append(outcome)
    return EventProjection(
        start, observations, (), end, (), outcome, (tuple(records),), tuple(records)
    )


def _projected(
    record_type: str, item: _Episode, value: Mapping[str, Any], payload: Mapping[str, Any], seq: int
) -> ProjectedEventRecord:
    at = str(value["at"])
    return ProjectedEventRecord(
        record_type,
        "physical",
        item.id,
        TELEMETRY_FILENAME,
        seq,
        _SYNTHETIC_BOOT,
        at,
        _monotonic(at),
        dict(payload),
        item.kind,
    )


def _observation_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "boot_id": _SYNTHETIC_BOOT,
        "monotonic_ns": _monotonic(str(value["at"])),
        "wall_time_utc": value["at"],
        "raw_status": value["status"],
        "battery_voltage_raw": None if value["battery_v"] is None else str(value["battery_v"]),
        "battery_voltage_v": value["battery_v"],
        "voltage_token_quantum_v": 0.1 if value["battery_v"] is not None else None,
        "load_percent": value["load_pct"],
        "input_voltage_v": value["input_v"],
        "battery_pct": value["battery_pct"],
        "runtime_s": value["runtime_s"],
        "output_v": value["output_v"],
    }


def _handle_for(item: _Episode, count: int) -> EventHandle:
    return EventHandle(item.id, TELEMETRY_FILENAME, TELEMETRY_FILENAME, count, item.kind)


def _episode_for_handle(
    episodes: tuple[_Episode, ...], handle: EventHandle | None
) -> _Episode | None:
    if handle is None:
        return None
    return next((item for item in episodes if item.id == handle.blackout_id), None)


def _episode_for_ref(
    episodes: tuple[_Episode, ...], ref: EventRef | EventHandle | SealedEventRef
) -> _Episode | None:
    identifier = ref.blackout_id
    return next((item for item in episodes if item.id == identifier), None)


def _summary(item: _Episode) -> EventSummary:
    start = item.samples[0]["at"]
    end = item.samples[-1]["at"]
    return EventSummary(
        item.id,
        TELEMETRY_FILENAME,
        start,
        end,
        item.termination,
        "operational_only",
        "recorded_only",
        _parse_datetime(end).timestamp() - _parse_datetime(start).timestamp(),
        len(item.samples),
        None,
        False,
        False,
        None,
    )


def _monotonic(value: str) -> int:
    return int(_parse_datetime(value).timestamp() * 1_000_000_000)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
