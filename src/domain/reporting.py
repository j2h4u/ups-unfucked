"""Pure bounded plain-language rendering for one terminal outcome."""

from dataclasses import dataclass
from datetime import datetime

from src.domain.learning import ObservedLoadSagIncrease
from src.domain.reasons import InfrastructureReason
from src.domain.values import (
    ComparisonMode,
    PlainLanguageReport,
    TerminalDisposition,
    TerminalOutcome,
)


@dataclass(frozen=True, slots=True)
class ReportEvidenceContext:
    """Operator-visible diagnostic facts outside the scientific outcome aggregate."""

    raw_lb_observed: bool
    observed_load_sag_increase: ObservedLoadSagIncrease | None


def build_plain_language_report(
    outcome: TerminalOutcome,
    *,
    blackout_id: str,
    generated_utc: datetime,
    evidence: ReportEvidenceContext,
    consumed_evidence_budget_remaining: int,
) -> PlainLanguageReport:
    """Describe evidence and decisions without claiming capacity, SoH, or causality."""
    lines = [
        f"Blackout {blackout_id}: {outcome.disposition.value.replace('_', ' ')}.",
        (
            f"Observed {outcome.assessment.duration_s:.0f} seconds in "
            f"{outcome.assessment.observation_count} raw physical readings; "
            "a grid-restored partial is censored evidence, not measured full runtime or SoH."
        ),
    ]
    if evidence.raw_lb_observed:
        lines.append(
            "The UPS firmware reported raw LB; it was retained as a diagnostic and did not command virtual LB or FSD."
        )
    if outcome.comparison is None or outcome.comparison.mode == ComparisonMode.NONE:
        lines.append("Forward comparison was not available for this observed interval.")
    else:
        label = (
            "short observed-window"
            if outcome.comparison.mode == ComparisonMode.SHORT_WINDOW
            else "full"
        )
        lines.append(
            f"The {label} frozen-model comparison ended with residual "
            f"{outcome.comparison.end_residual_v:.3f} V (observed minus predicted)."
        )
    if evidence.observed_load_sag_increase is not None:
        observation = evidence.observed_load_sag_increase
        lines.append(
            "The scientific model was left unchanged: upward load-sag estimate "
            f"{observation.value_before:.6f} to {observation.measured_estimate:.6f} "
            "V per load percentage point was not applied "
            "(unsafe_upward_ir_change_not_applied)."
        )
        hashes = ", ".join(observation.evidence_hashes)
        lines.append(
            f"Evidence set {observation.evidence_set_id} from step records {hashes}; "
            "this is a possible battery-degradation signal, not measured SoH."
        )
    elif outcome.commit_receipt is not None:
        lines.append(
            f"Load-sag coefficient changed conservatively from "
            f"{outcome.commit_receipt.value_before:.6f} to "
            f"{outcome.commit_receipt.value_after:.6f} V per load percentage point."
        )
        lines.append("The safety oracle proved shutdown timing unchanged or earlier.")
    else:
        reason_codes = tuple(reason.value for reason in outcome.reasons.values)
        reason_text = ", ".join(reason_codes) if reason_codes else "no eligible independent cohort"
        lines.append(f"The scientific model was left unchanged: {reason_text}.")
    lines.append(
        f"Independent consumed-evidence budget remaining: {consumed_evidence_budget_remaining}."
    )
    return PlainLanguageReport(
        blackout_id=blackout_id,
        disposition=outcome.disposition,
        lines=tuple(lines),
        generated_utc=generated_utc,
    )


def build_infrastructure_rejection_report(
    *,
    blackout_id: str,
    generated_utc: datetime,
    reasons: tuple[InfrastructureReason, ...],
    raw_lb_observed: bool,
    consumed_evidence_budget_remaining: int,
) -> PlainLanguageReport:
    """Explain a durably rejected event without inventing scientific measurements."""
    reason_text = ", ".join(reason.value for reason in reasons)
    lines = [
        f"Blackout {blackout_id}: rejected.",
        f"Scientific processing was rejected by infrastructure: {reason_text}.",
        "No missing interval was reconstructed and no scientific model parameter was changed.",
    ]
    if raw_lb_observed:
        lines.append("The UPS firmware reported raw LB; it was retained only as a diagnostic fact.")
    lines.append(
        f"Independent consumed-evidence budget remaining: {consumed_evidence_budget_remaining}."
    )
    return PlainLanguageReport(
        blackout_id=blackout_id,
        disposition=TerminalDisposition.REJECTED,
        lines=tuple(lines),
        generated_utc=generated_utc,
    )
