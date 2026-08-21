"""Durable event-record commands used by the blackout capture lane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.application.assessment_codec import json_value
from src.application.capture_writer import CaptureCommandKind
from src.application.ports import CaptureEventStorePort
from src.application.storage_values import (
    EventHandle,
    EventRecord,
    EventStart,
    RecoveredObservation,
)
from src.domain.values import ChargeReadiness, FrozenModelSnapshot, PhysicalObservation


@dataclass(frozen=True, slots=True)
class CaptureStart:
    blackout_id: str
    segment_id: str
    observation: PhysicalObservation
    snapshot: FrozenModelSnapshot
    readiness: ChargeReadiness


@dataclass(frozen=True, slots=True)
class GapAppend:
    observation: PhysicalObservation | RecoveredObservation
    reason: str
    failed_command: CaptureCommandKind | None = None
    error_type: str | None = None
    overflow_count: int | None = None
    overflow_first_boot_id: str | None = None
    overflow_first_monotonic_ns: int | None = None
    overflow_last_boot_id: str | None = None
    overflow_last_monotonic_ns: int | None = None


def start_capture(
    store: CaptureEventStorePort,
    start: CaptureStart,
) -> EventHandle:
    observation = start.observation
    return store.open(
        EventStart(
            blackout_id=start.blackout_id,
            segment_id=start.segment_id,
            boot_id=observation.boot_id,
            wall_time_utc=_utc_text(observation.wall_time_utc),
            monotonic_ns=observation.monotonic_ns,
            payload={
                "observation": json_value(observation),
                "frozen_model": json_value(start.snapshot),
                "charge_readiness": json_value(start.readiness),
                "battery_epoch_id": start.snapshot.battery_epoch_id,
                "evaluation_revision": start.snapshot.evaluation_revision,
            },
        )
    )


def append_observation(
    store: CaptureEventStorePort,
    handle: EventHandle,
    observation: PhysicalObservation,
) -> EventHandle:
    return store.append(
        handle,
        EventRecord(
            record_type="observation",
            boot_id=observation.boot_id,
            wall_time_utc=_utc_text(observation.wall_time_utc),
            monotonic_ns=observation.monotonic_ns,
            payload=json_value(observation),
            provenance="physical",
        ),
    )


def append_gap(
    store: CaptureEventStorePort,
    handle: EventHandle,
    gap: GapAppend,
) -> EventHandle:
    payload: dict[str, str] = {"reason": gap.reason}
    if gap.failed_command is not None:
        payload["failed_command"] = gap.failed_command.value
    if gap.error_type is not None:
        payload["error_type"] = gap.error_type
    if gap.overflow_count is not None:
        payload.update(
            {
                "overflow_count": str(gap.overflow_count),
                "overflow_first_boot_id": gap.overflow_first_boot_id or "",
                "overflow_first_monotonic_ns": str(gap.overflow_first_monotonic_ns),
                "overflow_last_boot_id": gap.overflow_last_boot_id or "",
                "overflow_last_monotonic_ns": str(gap.overflow_last_monotonic_ns),
            }
        )
    return store.append(
        handle,
        EventRecord(
            record_type="gap",
            boot_id=gap.observation.boot_id,
            wall_time_utc=_utc_text(gap.observation.wall_time_utc),
            monotonic_ns=gap.observation.monotonic_ns,
            payload=payload,
            provenance="system",
        ),
    )


def append_end(
    store: CaptureEventStorePort,
    handle: EventHandle,
    observation: PhysicalObservation | RecoveredObservation,
    termination: str,
) -> EventHandle:
    return store.append(
        handle,
        EventRecord(
            record_type="end",
            boot_id=observation.boot_id,
            wall_time_utc=_utc_text(observation.wall_time_utc),
            monotonic_ns=observation.monotonic_ns,
            payload={"termination": termination, "observation": json_value(observation)},
            provenance="physical",
        ),
    )


def append_recovered_end(
    store: CaptureEventStorePort,
    handle: EventHandle,
    observation: RecoveredObservation,
) -> EventHandle:
    return append_end(store, handle, observation, "closed_restart_gap")


def _utc_text(value: datetime | str) -> str:
    if isinstance(value, str):
        return value
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
