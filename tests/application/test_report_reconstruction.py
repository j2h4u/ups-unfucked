"""Restart reconstruction uses the sealed terminal outcome as its source."""

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, cast

import pytest

from src.application.assessment_codec import json_value
from src.application.ports import ReportingEventStorePort
from src.application.report_reconstruction import (
    canonical_report_bytes,
    decode_infrastructure_rejection,
    reconstruct_latest_report,
    reconstruct_report_for_event,
)
from src.application.storage_values import (
    EventProjection,
    EventSummary,
    ProjectedEventRecord,
)
from src.domain.learning import ObservedLoadSagIncrease, evidence_set_id
from src.domain.reasons import LearningReason, order_reasons
from src.domain.values import (
    ComparisonMode,
    EvidenceAssessment,
    EvidenceClass,
    ForwardComparison,
    IrCohortEstimate,
    LearningDecision,
    NumericSummary,
    PlainLanguageReport,
    TerminalDisposition,
    TerminalOutcome,
)

BLACKOUT_ID = "a" * 32
SEGMENT = f"{BLACKOUT_ID}.0.jsonl"
WHEN = "2026-08-16T10:00:00Z"


def _outcome_payload(
    observed_increase: ObservedLoadSagIncrease | None = None,
) -> dict[str, Any]:
    reasons = order_reasons(
        () if observed_increase is None else (LearningReason.UNSAFE_UPWARD_IR_CHANGE_NOT_APPLIED,)
    )
    assessment = EvidenceAssessment(
        EvidenceClass.OPERATIONAL_ONLY,
        10.0,
        2,
        1.0,
        1.0,
        NumericSummary(12.0, 13.0, 12.5, 0.1),
        NumericSummary(20.0, 30.0, 25.0, 5.0),
        reasons,
    )
    comparison = ForwardComparison(
        ComparisonMode.NONE,
        None,
        0.0,
        0,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        reasons,
    )
    cohort = IrCohortEstimate(
        "e" * 32,
        (BLACKOUT_ID,),
        0,
        0,
        0,
        None if observed_increase is None else observed_increase.measured_estimate,
        None,
        reasons,
    )
    outcome = TerminalOutcome(
        TerminalDisposition.RECORDED_ONLY,
        assessment,
        comparison,
        cohort,
        LearningDecision(True, False, False, False),
        None,
        reasons,
    )
    return {
        "observed_load_sag_increase": json_value(observed_increase),
        "evidence_identifiers": {
            "evidence_set_id": (
                None if observed_increase is None else observed_increase.evidence_set_id
            ),
            "commit_receipt_id": None,
            "consumed_step_hashes": [],
            "observed_step_hashes": (
                [] if observed_increase is None else list(observed_increase.evidence_hashes)
            ),
        },
        "terminal_outcome": json_value(outcome),
    }


def _projection(
    observed_increase: ObservedLoadSagIncrease | None = None,
) -> EventProjection:
    outcome = ProjectedEventRecord(
        2,
        "outcome",
        "derived",
        BLACKOUT_ID,
        "b" * 32,
        2,
        "boot-a",
        WHEN,
        2,
        "c" * 64,
        _outcome_payload(observed_increase),
        "d" * 64,
    )
    start = ProjectedEventRecord(
        2,
        "start",
        "physical",
        BLACKOUT_ID,
        "b" * 32,
        0,
        "boot-a",
        WHEN,
        0,
        None,
        {"observation": {"raw_status": "OB DISCHRG LB"}},
        "e" * 64,
    )
    return EventProjection(
        start, (), (), None, (), outcome, ((start, outcome),), (), 0, (start, outcome)
    )


def _infrastructure_projection(reason: str) -> EventProjection:
    projection = _projection()
    assert projection.start is not None
    assert projection.outcome is not None
    outcome = replace(
        projection.outcome,
        payload={
            "disposition": "rejected",
            "evidence_class": "rejected",
            "comparison_available": False,
            "comparison_mode": "none",
            "ir_estimate_available": False,
            "reasons": [reason],
        },
    )
    return replace(
        projection,
        outcome=outcome,
        trusted_prefixes=((projection.start, outcome),),
        records=(projection.start, outcome),
    )


class _Store:
    def index_tail(self, limit: int) -> tuple[EventSummary, ...]:
        assert limit == 1
        return (
            EventSummary(
                2,
                BLACKOUT_ID,
                SEGMENT,
                WHEN,
                WHEN,
                "power_restored",
                "operational_only",
                "recorded_only",
                10.0,
                2,
                "e" * 32,
                False,
                "none",
                False,
                None,
                (),
                0,
                "f" * 64,
                "1" * 64,
            ),
        )

    def project(self, _ref: object) -> EventProjection:
        return _projection()


class _InfrastructureStore(_Store):
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def project(self, _ref: object) -> EventProjection:
        return _infrastructure_projection(self._reason)


class _UpwardStore(_Store):
    def __init__(self, observation: ObservedLoadSagIncrease) -> None:
        self._observation = observation

    def project(self, _ref: object) -> EventProjection:
        return _projection(self._observation)


def _store() -> ReportingEventStorePort:
    return cast(ReportingEventStorePort, _Store())


def test_reconstruct_latest_report_is_stable_after_restart() -> None:
    first = reconstruct_latest_report(_store(), consumed_evidence_budget_remaining=256)
    second = reconstruct_latest_report(_store(), consumed_evidence_budget_remaining=256)

    assert first is not None
    assert second == first
    assert first.blackout_id == BLACKOUT_ID
    assert first.generated_utc == datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)
    assert any("raw LB" in line for line in first.lines)


def test_terminal_outcome_reconstructs_after_crash_before_in_memory_notice() -> None:
    durable_store = _store()
    report_before_crash = reconstruct_latest_report(
        durable_store,
        consumed_evidence_budget_remaining=256,
    )
    assert report_before_crash is not None

    # A new store instance stands in for a process restart.  Only the sealed
    # projection is available; no in-memory report notice is carried across.
    report_after_restart = reconstruct_latest_report(
        _store(),
        consumed_evidence_budget_remaining=256,
    )

    assert report_after_restart is not None
    assert canonical_report_bytes(report_after_restart) == canonical_report_bytes(
        report_before_crash
    )


def test_upward_ir_observation_reconstructs_exact_report_after_restart() -> None:
    hashes = tuple(character * 64 for character in "1234")
    observation = ObservedLoadSagIncrease(
        parameter="ir_k_v_per_pp",
        value_before=0.009,
        measured_estimate=0.010,
        evidence_set_id=evidence_set_id(hashes),
        evidence_hashes=hashes,
    )
    first = reconstruct_latest_report(
        cast(ReportingEventStorePort, _UpwardStore(observation)),
        consumed_evidence_budget_remaining=256,
    )
    restarted = reconstruct_latest_report(
        cast(ReportingEventStorePort, _UpwardStore(observation)),
        consumed_evidence_budget_remaining=256,
    )

    assert first is not None
    assert restarted is not None
    assert canonical_report_bytes(restarted) == canonical_report_bytes(first)
    rendered = canonical_report_bytes(restarted).decode("utf-8")
    assert "0.009000 to 0.010000" in rendered
    assert "was not applied" in rendered
    assert observation.evidence_set_id in rendered
    assert all(evidence_hash in rendered for evidence_hash in hashes)
    assert "possible battery-degradation signal, not measured SoH" in rendered


def test_index_summary_reconstructs_after_crash_before_report_publication() -> None:
    durable_store = _Store()
    summary = durable_store.index_tail(1)[-1]

    # The reporter crashed after the summary became durable but before the
    # publication call.  Restart uses the durable summary and event projection.
    report = reconstruct_report_for_event(
        _store(),
        blackout_id=summary.blackout_id,
        segment_filename=summary.segment_filename,
        consumed_evidence_budget_remaining=256,
    )
    assert report is not None
    assert canonical_report_bytes(report).decode("utf-8").startswith("Blackout ")


@pytest.mark.parametrize("reason", ("capture_damaged", "processing_backlog_full"))
def test_infrastructure_rejection_reconstructs_after_restart(reason: str) -> None:
    store = cast(ReportingEventStorePort, _InfrastructureStore(reason))

    report = reconstruct_latest_report(store, consumed_evidence_budget_remaining=256)

    assert report is not None
    assert report.disposition == TerminalDisposition.REJECTED
    rendered = canonical_report_bytes(report).decode("utf-8")
    assert reason in rendered
    assert "No missing interval was reconstructed" in rendered
    assert "model parameter was changed" in rendered


def test_unknown_infrastructure_rejection_reason_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown infrastructure rejection reason"):
        decode_infrastructure_rejection(
            {
                "disposition": "rejected",
                "evidence_class": "rejected",
                "comparison_available": False,
                "comparison_mode": "none",
                "ir_estimate_available": False,
                "reasons": ["invented_reason"],
            }
        )


def test_canonical_report_bytes_are_bounded_and_utf8() -> None:
    report = PlainLanguageReport(
        blackout_id="blackout-b",
        disposition=TerminalDisposition.RECORDED_ONLY,
        lines=("  батарея   ",) * 9 + ("x" * 600,),
        generated_utc=datetime(2026, 8, 16, tzinfo=timezone.utc),
    )

    rendered = canonical_report_bytes(report).decode("utf-8")
    lines = rendered.split("\n")

    assert len(lines) == 8
    assert all(len(line) <= 512 for line in lines)
    assert lines[0] == "батарея"
