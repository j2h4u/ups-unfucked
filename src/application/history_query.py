"""Bounded human-history query over sealed blackout and recharge authorities."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol

from src.application.storage_values import EventProjection


class HistoryReadPort(Protocol):
    """Direct sealed-authority reads needed by the human history use case."""

    def sealed_event_projections(
        self,
        start_utc: str,
        end_utc: str,
        *,
        event_kind: str = "blackout",
    ) -> tuple[EventProjection, ...]: ...

    def sealed_recharge_projections_for_blackouts(
        self,
        blackout_ids: Collection[str],
    ) -> tuple[EventProjection, ...]: ...


@dataclass(frozen=True, slots=True)
class HistoryRange:
    """A non-empty UTC half-open interval."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = _utc(self.start, "range start")
        end = _utc(self.end, "range end")
        if start >= end:
            raise ValueError("history range must be non-empty and ordered")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def start_utc(self) -> str:
        return _format_utc(self.start)

    @property
    def end_utc(self) -> str:
        return _format_utc(self.end)


@dataclass(frozen=True, slots=True)
class LearningHistory:
    status: Literal["used", "refused"]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RechargeHistory:
    episode_id: str
    ended_utc: str | None
    outcome: str
    reason: str


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    blackout_id: str
    loss_utc: str
    restoration_utc: str | None
    termination: str | None
    disposition: str
    learning: LearningHistory
    recharge: RechargeHistory | None
    evidence_damage: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HistoryResult:
    period: HistoryRange
    entries: tuple[HistoryEntry, ...]


def query_history(store: HistoryReadPort, period: HistoryRange) -> HistoryResult:
    """Return oldest-first entries for sealed events beginning in ``period``."""
    events = store.sealed_event_projections(
        period.start_utc,
        period.end_utc,
        event_kind="blackout",
    )
    recharge = _recharge_by_blackout(
        store.sealed_recharge_projections_for_blackouts(
            tuple(
                projection.start.blackout_id
                for projection in events
                if projection.start is not None
            )
        )
    )
    entries = tuple(
        sorted(
            (_entry(projection, recharge) for projection in events),
            key=lambda item: (item.loss_utc, item.blackout_id),
        )
    )
    return HistoryResult(period, entries)


def utc_day(year: int, month: int, day: int) -> HistoryRange:
    """Build one UTC calendar day range."""
    start = datetime(year, month, day, tzinfo=timezone.utc)
    return HistoryRange(start, start + timedelta(days=1))


def utc_month(year: int, month: int) -> HistoryRange:
    """Build one UTC calendar month range."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1
    return HistoryRange(start, datetime(next_year, next_month, 1, tzinfo=timezone.utc))


def utc_year(year: int) -> HistoryRange:
    """Build one UTC calendar year range."""
    return HistoryRange(
        datetime(year, 1, 1, tzinfo=timezone.utc),
        datetime(year + 1, 1, 1, tzinfo=timezone.utc),
    )


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and normalize it to UTC."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid UTC timestamp: {value}") from exc
    return _utc(parsed, "timestamp")


def _recharge_by_blackout(
    projections: tuple[EventProjection, ...],
) -> dict[str, EventProjection]:
    linked: dict[str, EventProjection] = {}
    for projection in projections:
        start = projection.start
        if start is None:
            raise ValueError("sealed recharge projection has no start")
        blackout_id = _string(start.payload.get("preceding_blackout_id"))
        if blackout_id is None:
            continue
        if blackout_id in linked:
            raise ValueError(f"multiple sealed recharge episodes link blackout {blackout_id}")
        linked[blackout_id] = projection
    return linked


def _entry(
    projection: EventProjection,
    recharge: Mapping[str, EventProjection],
) -> HistoryEntry:
    start = projection.start
    if start is None:
        raise ValueError("sealed blackout projection has no start")
    end = projection.end
    termination = _string(end.payload.get("termination")) if end is not None else None
    restoration = end.wall_time_utc if termination == "power_restored" and end is not None else None
    outcome = projection.outcome
    disposition, learning = _learning(outcome.payload if outcome is not None else {})
    return HistoryEntry(
        start.blackout_id,
        start.wall_time_utc,
        restoration,
        termination,
        disposition,
        learning,
        _recharge_entry(recharge.get(start.blackout_id)),
        _evidence_damage(projection),
    )


def _learning(payload: Mapping[str, object]) -> tuple[str, LearningHistory]:
    terminal = payload.get("terminal_outcome")
    raw = terminal if isinstance(terminal, Mapping) else payload
    reasons = _learning_reasons(payload, raw)
    disposition = (
        _string(payload.get("disposition")) or _string(raw.get("disposition")) or "unknown"
    )
    if raw.get("disposition") == "rejected":
        if not reasons:
            reasons = ("scientific processing was refused",)
        return disposition, LearningHistory("refused", reasons)
    if _has_durable_model_change(payload, raw, disposition):
        return disposition, LearningHistory("used", reasons or ("ir k model update committed",))
    return disposition, LearningHistory(
        "refused", reasons or ("learning gates did not approve a model update",)
    )


def _learning_reasons(
    payload: Mapping[str, object], terminal: Mapping[str, object]
) -> tuple[str, ...]:
    for candidate in (payload, terminal):
        reasons = _reasons(candidate.get("reason_codes", candidate.get("reasons")))
        if reasons:
            return reasons
        reasons = _reasons(candidate.get("ordered_reasons"))
        if reasons:
            return reasons
    return ()


def _has_durable_model_change(
    payload: Mapping[str, object], terminal: Mapping[str, object], disposition: str
) -> bool:
    receipt_id = _string(payload.get("commit_receipt_id"))
    model_change = payload.get("model_change")
    terminal_receipt = terminal.get("commit_receipt")
    has_receipt_or_change = (
        receipt_id is not None
        or isinstance(model_change, Mapping)
        or isinstance(terminal_receipt, Mapping)
    )
    return disposition == "learned" and has_receipt_or_change


def _recharge_entry(projection: EventProjection | None) -> RechargeHistory | None:
    if projection is None:
        return None
    start = projection.start
    if start is None:
        raise ValueError("recharge projection has no start")
    episode_id = start.blackout_id
    end = projection.end
    if end is None:
        return RechargeHistory(episode_id, None, "incomplete", "recharge is still open")
    assessment = end.payload.get("assessment")
    assessment_map = assessment if isinstance(assessment, Mapping) else {}
    outcome = _string(assessment_map.get("kind")) or "unknown"
    reason = _string(assessment_map.get("reason")) or _string(end.payload.get("reason"))
    return RechargeHistory(
        episode_id,
        end.wall_time_utc,
        outcome or "unknown",
        reason or "recharge terminal reason was not recorded",
    )


def _evidence_damage(projection: EventProjection) -> tuple[str, ...]:
    reasons = [
        f"gap: {_string(record.payload.get('reason')) or 'unspecified'}"
        for record in projection.gaps
    ]
    if projection.damaged_segment_hashes:
        reasons.append(
            f"capture damage: {len(projection.damaged_segment_hashes)} damaged segment(s)"
        )
    if projection.damaged_segment_overflow:
        reasons.append(f"capture damage overflow: {projection.damaged_segment_overflow}")
    return tuple(reasons)


def _reasons(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result = []
    for item in value:
        if isinstance(item, str) and item:
            result.append(item.replace("_", " "))
    return tuple(result)


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
