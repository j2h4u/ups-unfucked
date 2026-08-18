"""Bounded v3 terminal reporting facts, independent of consumer admission."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from src.domain.blackout_terminal import BlackoutTermination
from src.domain.reasons import InfrastructureReason

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ID_BYTES = 128
_MAX_RAW_RECORD_COUNT = 1_000_000
_MAX_RAW_SAMPLE_COUNT = 3_170


class TerminalOutcomeKind(StrEnum):
    """Closed reporting states for a completed v3 aggregate."""

    ASSESSED = "assessed"
    RECORDED_ONLY = "recorded_only"
    INFRASTRUCTURE_REFUSED = "infrastructure_refused"


@dataclass(frozen=True, slots=True)
class TerminalOutcome:
    """A restart-reconstructable outcome containing facts, not global admission."""

    outcome_id: str
    blackout_id: str
    physical_episode_id: str
    battery_epoch_id: str
    kind: TerminalOutcomeKind
    termination: BlackoutTermination
    ended_at_utc: datetime
    raw_record_count: int
    raw_sample_count: int
    blackout_end_hash: str
    consumer_summary_hashes: tuple[str, ...]
    decision_record_hash: str | None
    receipt_record_hash: str | None
    infrastructure_reasons: tuple[InfrastructureReason, ...] = ()
    segment_id: str = ""

    def __post_init__(self) -> None:
        for value, name in (
            (self.outcome_id, "outcome ID"),
            (self.blackout_id, "blackout ID"),
            (self.physical_episode_id, "physical episode ID"),
            (self.battery_epoch_id, "battery epoch ID"),
            (self.segment_id, "segment ID"),
        ):
            _validate_id(value, name)
        if not isinstance(self.kind, TerminalOutcomeKind):
            raise TypeError("outcome kind must be TerminalOutcomeKind")
        if not isinstance(self.termination, BlackoutTermination):
            raise TypeError("outcome termination must be BlackoutTermination")
        _validate_utc(self.ended_at_utc)
        _validate_count(self.raw_record_count, "raw record count", _MAX_RAW_RECORD_COUNT)
        _validate_count(self.raw_sample_count, "raw sample count", _MAX_RAW_SAMPLE_COUNT)
        _validate_hash(self.blackout_end_hash, "blackout end hash")
        _validate_hashes(self.consumer_summary_hashes, "consumer summary hashes")
        if self.decision_record_hash is not None:
            _validate_hash(self.decision_record_hash, "decision record hash")
        if self.receipt_record_hash is not None:
            _validate_hash(self.receipt_record_hash, "receipt record hash")
        if any(not isinstance(item, InfrastructureReason) for item in self.infrastructure_reasons):
            raise TypeError("infrastructure reasons must be InfrastructureReason values")
        _validate_outcome_shape(self)


def _validate_outcome_shape(value: TerminalOutcome) -> None:
    assessed = value.kind is TerminalOutcomeKind.ASSESSED
    if assessed and len(value.consumer_summary_hashes) != 3:
        raise ValueError("assessed outcome requires exactly three consumer summaries")
    if assessed and value.decision_record_hash is None:
        raise ValueError("assessed outcome requires a learning-decision record hash")
    if value.kind is TerminalOutcomeKind.INFRASTRUCTURE_REFUSED:
        if not value.infrastructure_reasons:
            raise ValueError("infrastructure refusal requires an infrastructure reason")
        if value.consumer_summary_hashes or value.decision_record_hash or value.receipt_record_hash:
            raise ValueError("infrastructure refusal cannot invent scientific results")
    if value.kind is not TerminalOutcomeKind.ASSESSED and value.receipt_record_hash is not None:
        raise ValueError("receipt record hash requires an assessed outcome")


def _validate_id(value: object, field: str) -> None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > _MAX_ID_BYTES:
        raise ValueError(f"{field} must be a non-empty UTF-8 ID of at most 128 bytes")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} must not contain control characters")


def _validate_utc(value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("outcome time must be timezone-aware UTC")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("outcome time must be UTC")


def _validate_count(value: object, field: str, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{field} must be a bounded nonnegative integer")


def _validate_hash(value: object, field: str) -> None:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 hash")


def _validate_hashes(values: tuple[str, ...], field: str) -> None:
    if (
        not isinstance(values, tuple)
        or len(values) > 3
        or any(_HASH_RE.fullmatch(item) is None for item in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError(f"{field} must contain lowercase SHA-256 hashes")
