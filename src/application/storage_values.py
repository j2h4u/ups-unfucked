"""Immutable storage-boundary values shared by ports and concrete adapters.

These are persistence commands/references/projections, not scientific domain
values.  Domain objects are serialized into record payloads by application use
cases and remain owned by :mod:`src.domain`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class EventStart:
    blackout_id: str
    segment_id: str
    boot_id: str
    wall_time_utc: str
    monotonic_ns: int
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EventRecord:
    record_type: str
    boot_id: str
    wall_time_utc: str
    monotonic_ns: int
    payload: Mapping[str, Any]
    provenance: Literal["physical", "system", "derived"]


@dataclass(frozen=True, slots=True)
class TerminalOutcomeRecord:
    boot_id: str
    wall_time_utc: str
    monotonic_ns: int
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EventHandle:
    blackout_id: str
    segment_id: str
    path_token: str
    next_seq: int
    last_record_sha256: str


class CaptureCloseState(StrEnum):
    """Durable tail state observed while retrying a capture close."""

    UNKNOWN = "unknown"
    ACTIVE = "active"
    END = "end"
    OUTCOME = "outcome"


@dataclass(frozen=True, slots=True)
class CaptureCloseReconciliation:
    state: CaptureCloseState
    handle: EventHandle | None


@dataclass(frozen=True, slots=True)
class RecoveredObservation:
    boot_id: str
    wall_time_utc: str
    monotonic_ns: int
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RecoveredCapture:
    """Bounded active-tail metadata needed to continue capture after restart."""

    handle: EventHandle
    last_boot_id: str
    last_observation: RecoveredObservation

    @property
    def blackout_id(self) -> str:
        return self.handle.blackout_id

    @property
    def segment_id(self) -> str:
        return self.handle.segment_id

    @property
    def path_token(self) -> str:
        return self.handle.path_token

    @property
    def next_seq(self) -> int:
        return self.handle.next_seq

    @property
    def last_record_sha256(self) -> str:
        return self.handle.last_record_sha256


@dataclass(frozen=True, slots=True)
class PreparingCaptureRef:
    blackout_id: str
    segment_id: str
    path_token: str
    canonical_start_record_utf8: str
    tag: Literal["preparing"] = "preparing"


@dataclass(frozen=True, slots=True)
class CapturingEventRef:
    blackout_id: str
    segment_id: str
    path_token: str
    tag: Literal["capturing"] = "capturing"


type CaptureRef = PreparingCaptureRef | CapturingEventRef


@dataclass(frozen=True, slots=True)
class ProcessingRef:
    blackout_id: str
    segment_ids: tuple[str, ...]
    final_path_token: str
    frozen_stage: str
    last_record_hash: str
    tag: Literal["processing"] = "processing"


@dataclass(frozen=True, slots=True)
class WorkRegistry:
    capture: CaptureRef | None
    pending_processing: tuple[ProcessingRef, ...]


@dataclass(frozen=True, slots=True)
class EventRef:
    blackout_id: str
    path_token: str


@dataclass(frozen=True, slots=True)
class ProjectedEventRecord:
    schema_version: int
    record_type: str
    provenance: str
    blackout_id: str
    segment_id: str
    seq: int
    boot_id: str
    wall_time_utc: str
    monotonic_ns: int
    prev_record_sha256: str | None
    payload: Mapping[str, Any]
    record_sha256: str


@dataclass(frozen=True, slots=True)
class EventProjection:
    start: ProjectedEventRecord | None
    observations: tuple[ProjectedEventRecord, ...]
    gaps: tuple[ProjectedEventRecord, ...]
    end: ProjectedEventRecord | None
    derived_records: tuple[ProjectedEventRecord, ...]
    outcome: ProjectedEventRecord | None
    trusted_prefixes: tuple[tuple[ProjectedEventRecord, ...], ...]
    damaged_segment_hashes: tuple[str, ...]
    damaged_segment_overflow: int
    records: tuple[ProjectedEventRecord, ...]


@dataclass(frozen=True, slots=True)
class EventSummary:
    schema_version: int
    blackout_id: str
    segment_filename: str
    started_utc: str
    ended_utc: str | None
    termination: str | None
    evidence_class: str | None
    disposition: str
    duration_s: float | None
    observation_count: int
    battery_epoch_id: str | None
    comparison_available: bool
    comparison_mode: Literal["full", "short_window", "none"]
    ir_estimate_available: bool
    commit_receipt_id: str | None
    damaged_segment_hashes: tuple[str, ...]
    damaged_segment_overflow: int
    outcome_record_sha256: str
    event_file_sha256: str


@dataclass(frozen=True, slots=True)
class EpochHistoryTail:
    """Newest-first bounded history query with explicit older-match overflow."""

    summaries: tuple[EventSummary, ...]
    overflow_count: int
    scan_complete: bool

    def __post_init__(self) -> None:
        if (
            isinstance(self.overflow_count, bool)
            or not isinstance(self.overflow_count, int)
            or self.overflow_count < 0
        ):
            raise ValueError("epoch history overflow_count must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class EpochHistoryScan:
    """Newest-first same-epoch history scan with explicit completeness."""

    summaries: tuple[EventSummary, ...]
    scan_complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.scan_complete, bool):
            raise ValueError("epoch history scan completeness must be boolean")


@dataclass(frozen=True, slots=True)
class SealedEventRef:
    blackout_id: str
    segment_ids: tuple[str, ...]
    final_path_token: str
    outcome_record_sha256: str


@dataclass(frozen=True, slots=True)
class StorageHealth:
    capture_available: bool
    active_phase: str | None
    queued_observations: int | None
    durability_lag_s: float | None
    consumed_step_budget_remaining: int | None
    event_count: int
    total_bytes: int
    free_bytes: int
    alarm: str | None
    bounded_error: str | None


@dataclass(frozen=True, slots=True)
class ReportNoticeIdentity:
    """Stable report identity independent of report delivery attempts."""

    blackout_id: str
    segment_filename: str
    summary_sha256: str


@dataclass(frozen=True, slots=True)
class CaptureQueueHealth:
    """Bounded health projection for the sole application writer lane."""

    capture_available: bool
    lifecycle_queued: int
    observations_queued: int
    observation_overflow_count: int
    lifecycle_overflow_count: int
    discarded_command_count: int
    bounded_error: str | None
    maintenance_queued: int = 0
    max_busy_time_s: float = 0.0
    oldest_queue_age_s: float = 0.0
    last_failure_kind: str | None = None
