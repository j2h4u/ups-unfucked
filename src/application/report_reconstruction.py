"""Decode the durable terminal outcome into the latest operator report."""

from collections.abc import Mapping
from datetime import datetime, timezone

from src.application.assessment_codec import (
    ProjectionInputError,
    assessment_from_json,
    cohort_from_json,
    comparison_from_json,
    decision_from_json,
    mapping,
    reasons_from_json,
    receipt_from_json,
)
from src.application.assessment_replay import observed_load_sag_increase_from_json
from src.application.ports import ReportingEventStorePort
from src.application.storage_values import EventProjection, EventRef
from src.domain.learning import (
    ObservedLoadSagIncrease,
    validate_observed_load_sag_outcome,
)
from src.domain.reasons import InfrastructureReason
from src.domain.reporting import (
    ReportEvidenceContext,
    build_infrastructure_rejection_report,
    build_plain_language_report,
)
from src.domain.values import (
    PlainLanguageReport,
    TerminalDisposition,
    TerminalOutcome,
)


class ReportReconstructionError(ValueError):
    """The sealed outcome cannot be safely turned into a bounded report."""


REPORT_MAX_LINES = 8
REPORT_MAX_LINE_CHARS = 512


def canonical_report_bytes(report: PlainLanguageReport) -> bytes:
    """Render a bounded report deterministically as UTF-8 bytes.

    The renderer is an application boundary for restart comparisons.  It does
    not add metadata or timestamps; those are already part of the immutable
    ``PlainLanguageReport`` value.  Whitespace normalization keeps equivalent
    operator lines byte-stable while the line and character limits preserve a
    bounded report even when a malformed durable value is supplied.
    """
    lines = tuple(
        " ".join(line.split())[:REPORT_MAX_LINE_CHARS] for line in report.lines[:REPORT_MAX_LINES]
    )
    return "\n".join(lines).encode("utf-8")


def reconstruct_latest_report(
    store: ReportingEventStorePort,
    *,
    consumed_evidence_budget_remaining: int,
) -> PlainLanguageReport | None:
    """Rebuild one report from the newest sealed event and its terminal outcome."""
    if consumed_evidence_budget_remaining < 0:
        raise ValueError("consumed evidence budget cannot be negative")
    try:
        summaries = store.index_tail(1)
        if not summaries:
            return None
        summary = summaries[-1]
        return reconstruct_report_for_event(
            store,
            blackout_id=summary.blackout_id,
            segment_filename=summary.segment_filename,
            consumed_evidence_budget_remaining=consumed_evidence_budget_remaining,
        )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ReportReconstructionError("durable terminal outcome is not reportable") from exc


def reconstruct_report_for_event(
    store: ReportingEventStorePort,
    *,
    blackout_id: str,
    segment_filename: str,
    consumed_evidence_budget_remaining: int,
) -> PlainLanguageReport | None:
    """Rebuild one report for a sealed event identified by its index summary."""
    if consumed_evidence_budget_remaining < 0:
        raise ValueError("consumed evidence budget cannot be negative")
    projection = store.project(EventRef(blackout_id, segment_filename))
    if projection.outcome is None:
        return None
    generated = _parse_utc(projection.outcome.wall_time_utc)
    raw_lb = _raw_lb_observed(projection)
    if "terminal_outcome" not in projection.outcome.payload:
        return build_infrastructure_rejection_report(
            blackout_id=blackout_id,
            generated_utc=generated,
            reasons=decode_infrastructure_rejection(projection.outcome.payload),
            raw_lb_observed=raw_lb,
            consumed_evidence_budget_remaining=consumed_evidence_budget_remaining,
        )
    outcome = decode_terminal_outcome(projection.outcome.payload)
    observed_increase = _decode_observed_increase(projection.outcome.payload, outcome)
    return build_plain_language_report(
        outcome,
        blackout_id=blackout_id,
        generated_utc=generated,
        evidence=ReportEvidenceContext(raw_lb, observed_increase),
        consumed_evidence_budget_remaining=consumed_evidence_budget_remaining,
    )


def decode_terminal_outcome(payload: Mapping[str, object]) -> TerminalOutcome:
    """Decode the complete ``terminal_outcome`` payload without re-assessment."""
    raw = mapping(payload.get("terminal_outcome"), "terminal_outcome")
    comparison_raw = raw.get("comparison")
    cohort_raw = raw.get("cohort_estimate")
    receipt_raw = raw.get("commit_receipt")
    comparison = None if comparison_raw is None else comparison_from_json(comparison_raw)
    cohort = None if cohort_raw is None else cohort_from_json(cohort_raw)
    receipt = None if receipt_raw is None else receipt_from_json(receipt_raw)
    return TerminalOutcome(
        disposition=TerminalDisposition(str(raw["disposition"])),
        assessment=assessment_from_json(raw["assessment"]),
        comparison=comparison,
        cohort_estimate=cohort,
        learning_decision=decision_from_json(raw["learning_decision"]),
        commit_receipt=receipt,
        reasons=reasons_from_json(raw),
    )


def decode_infrastructure_rejection(
    payload: Mapping[str, object],
) -> tuple[InfrastructureReason, ...]:
    """Decode the small explicit grammar used when science never ran."""
    if (
        payload.get("disposition") != TerminalDisposition.REJECTED.value
        or payload.get("evidence_class") != "rejected"
        or payload.get("comparison_available") is not False
        or payload.get("comparison_mode") != "none"
        or payload.get("ir_estimate_available") is not False
    ):
        raise ReportReconstructionError("infrastructure rejection fields are invalid")
    raw_reasons = payload.get("reasons")
    if not isinstance(raw_reasons, list) or len(raw_reasons) != 1:
        raise ReportReconstructionError("infrastructure rejection reason is invalid")
    try:
        reason = InfrastructureReason(str(raw_reasons[0]))
    except ValueError as exc:
        raise ReportReconstructionError("unknown infrastructure rejection reason") from exc
    if reason not in {
        InfrastructureReason.CAPTURE_DAMAGED,
        InfrastructureReason.PROCESSING_BACKLOG_FULL,
    }:
        raise ReportReconstructionError("unsupported infrastructure rejection reason")
    return (reason,)


def _decode_observed_increase(
    payload: Mapping[str, object],
    outcome: TerminalOutcome,
) -> ObservedLoadSagIncrease | None:
    if "observed_load_sag_increase" not in payload:
        raise ReportReconstructionError("upward load-sag observation field is missing")
    try:
        observation = observed_load_sag_increase_from_json(payload["observed_load_sag_increase"])
    except ProjectionInputError as exc:
        raise ReportReconstructionError("upward load-sag observation is invalid") from exc
    try:
        validate_observed_load_sag_outcome(observation, outcome)
    except ValueError as exc:
        raise ReportReconstructionError(str(exc)) from exc
    if observation is None:
        return None
    identifiers = mapping(payload.get("evidence_identifiers"), "evidence_identifiers")
    expected_identifiers = {
        "evidence_set_id": observation.evidence_set_id,
        "commit_receipt_id": None,
        "consumed_step_hashes": [],
        "observed_step_hashes": list(observation.evidence_hashes),
    }
    if identifiers != expected_identifiers:
        raise ReportReconstructionError("upward load-sag evidence identifiers are inconsistent")
    return observation


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ReportReconstructionError("outcome timestamp is not canonical UTC")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
    except ValueError as exc:
        raise ReportReconstructionError("outcome timestamp is invalid") from exc


def _raw_lb_observed(projection: EventProjection) -> bool:
    for record in projection.records:
        nested = record.payload.get("observation")
        status = (
            nested.get("raw_status")
            if isinstance(nested, Mapping)
            else record.payload.get("raw_status")
        )
        if isinstance(status, str) and "LB" in status.split():
            return True
    return False
