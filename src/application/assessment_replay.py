"""Durable derived-stage grammar and deterministic assessment record creation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.application.assessment_codec import (
    ProjectionInputError,
    assessment_from_json,
    cohort_from_json,
    comparison_from_json,
    decision_from_json,
    json_value,
    mapping,
    policy_from_json,
    prepared_from_json,
    reasons_from_json,
    receipt_from_json,
    required_number,
    required_sha256,
    required_string,
    required_strings,
)
from src.application.model_port import PreparedModelCommit
from src.application.storage_values import EventProjection, EventRecord
from src.domain.ir_identification import CohortStep, IrCohortSelection
from src.domain.learning import ObservedLoadSagIncrease
from src.domain.reasons import OrderedReasons
from src.domain.values import (
    EvidenceAssessment,
    ForwardComparison,
    IrCohortEstimate,
    IrLearningPolicy,
    LearningDecision,
    ModelCommitReceipt,
)


@dataclass(frozen=True, slots=True)
class DecisionBasis:
    assessment: EvidenceAssessment
    comparison: ForwardComparison
    steps: tuple[CohortStep, ...]
    cohort: IrCohortEstimate
    cohort_step_hashes: tuple[str, ...]
    decision: LearningDecision
    reasons: OrderedReasons
    learning_reasons: OrderedReasons
    observed_load_sag_increase: ObservedLoadSagIncrease | None
    prepared: PreparedModelCommit | None
    learning_policy: IrLearningPolicy


@dataclass(frozen=True, slots=True)
class DurableDerivedStages:
    assessment: EvidenceAssessment | None
    comparison: ForwardComparison | None
    cohort: IrCohortSelection | None
    learning_policy: IrLearningPolicy | None = None


@dataclass(frozen=True, slots=True)
class DurableClose:
    assessment: EvidenceAssessment
    comparison: ForwardComparison
    cohort: IrCohortEstimate
    decision: LearningDecision
    reasons: OrderedReasons
    observed_load_sag_increase: ObservedLoadSagIncrease | None
    prepared: PreparedModelCommit | None
    receipt: ModelCommitReceipt | None
    learning_policy: IrLearningPolicy


def durable_derived_stages(projection: EventProjection) -> DurableDerivedStages:
    """Read only a valid prefix of the assessment derived-record grammar."""
    records = projection.derived_records
    if not records:
        return DurableDerivedStages(None, None, None)
    first = records[0]
    if first.record_type != "assessment":
        raise ProjectionInputError("durable derived sequence must begin with assessment")
    assessment = assessment_from_json(first.payload)
    assessment_payload = mapping(first.payload, "durable assessment")
    raw_policy = assessment_payload.get("learning_policy")
    if raw_policy is None:
        raise ProjectionInputError("durable assessment learning policy is missing")
    learning_policy = policy_from_json(raw_policy)
    if len(records) == 1:
        return DurableDerivedStages(assessment, None, None, learning_policy)
    second = records[1]
    if second.record_type != "comparison":
        raise ProjectionInputError("durable comparison must follow assessment")
    comparison = comparison_from_json(second.payload)
    cohort = _read_cohort_prefix(records[2:])
    return DurableDerivedStages(assessment, comparison, cohort, learning_policy)


def durable_close(projection: EventProjection) -> DurableClose | None:
    """Decode a completed durable decision without recomputing scientific values."""
    decision_record = next(
        (
            record
            for record in projection.derived_records
            if record.record_type == "learning_decision"
        ),
        None,
    )
    if decision_record is None:
        return None
    payload = mapping(decision_record.payload, "learning_decision")
    basis = mapping(payload.get("outcome_basis"), "outcome_basis")
    assessment = assessment_from_json(basis.get("assessment"))
    comparison = comparison_from_json(basis.get("comparison"))
    cohort = cohort_from_json(basis.get("cohort_estimate"))
    decision = decision_from_json(payload.get("learning_decision"))
    reasons = reasons_from_json(basis)
    if "observed_load_sag_increase" not in payload:
        raise ProjectionInputError("durable upward load-sag observation is missing")
    observed_load_sag_increase = observed_load_sag_increase_from_json(
        payload["observed_load_sag_increase"]
    )
    prepared_raw = payload.get("prepared_commit")
    prepared = None if prepared_raw is None else prepared_from_json(prepared_raw)
    learning_policy = _durable_learning_policy(projection, basis, prepared)
    receipt_record = next(
        (record for record in projection.derived_records if record.record_type == "model_commit"),
        None,
    )
    receipt = None if receipt_record is None else receipt_from_json(receipt_record.payload)
    return DurableClose(
        assessment,
        comparison,
        cohort,
        decision,
        reasons,
        observed_load_sag_increase,
        prepared,
        receipt,
        learning_policy,
    )


def observed_load_sag_increase_from_json(value: object) -> ObservedLoadSagIncrease | None:
    """Decode the exact upward observation stored in the durable decision record."""
    if value is None:
        return None
    raw = mapping(value, "observed_load_sag_increase")
    expected = {
        "parameter",
        "value_before",
        "measured_estimate",
        "evidence_set_id",
        "evidence_hashes",
    }
    if set(raw) != expected:
        raise ProjectionInputError("observed load-sag increase fields are invalid")
    try:
        return ObservedLoadSagIncrease(
            parameter=required_string(raw["parameter"], "upward parameter"),
            value_before=required_number(raw["value_before"], "upward value_before"),
            measured_estimate=required_number(raw["measured_estimate"], "upward measured_estimate"),
            evidence_set_id=required_sha256(raw["evidence_set_id"], "upward evidence_set_id"),
            evidence_hashes=required_strings(raw["evidence_hashes"], "upward evidence_hashes"),
        )
    except ValueError as exc:
        raise ProjectionInputError(f"invalid observed load-sag increase: {exc}") from exc


def _durable_learning_policy(
    projection: EventProjection,
    basis: Mapping[str, Any],
    prepared: PreparedModelCommit | None,
) -> IrLearningPolicy:
    policy_raw = basis.get("learning_policy")
    learning_policy = (
        policy_from_json(policy_raw)
        if policy_raw is not None
        else (prepared.learning_policy if prepared is not None else None)
    )
    if learning_policy is None:
        raise ProjectionInputError("durable learning policy is missing")
    assessment_record = next(
        (record for record in projection.derived_records if record.record_type == "assessment"),
        None,
    )
    if assessment_record is not None:
        assessment_payload = mapping(assessment_record.payload, "assessment")
        assessment_policy = policy_from_json(assessment_payload.get("learning_policy"))
        if assessment_policy != learning_policy:
            raise ProjectionInputError("assessment learning policy mismatch")
    if prepared is not None and prepared.learning_policy != learning_policy:
        raise ProjectionInputError("prepared commit learning policy mismatch")
    return learning_policy


def _read_cohort_prefix(records: tuple[Any, ...]) -> IrCohortSelection | None:
    cohort: IrCohortSelection | None = None
    for record in records:
        if record.record_type != "ir_estimate":
            raise ProjectionInputError("unexpected durable record before learning decision")
        payload = mapping(record.payload, "durable ir_estimate")
        kind = payload.get("kind")
        if not isinstance(kind, str):
            raise ProjectionInputError("durable ir_estimate kind must be a string")
        if kind == "step":
            if cohort is not None:
                raise ProjectionInputError("durable step follows cohort estimate")
            _validate_step_record(payload)
            continue
        if kind != "cohort" or cohort is not None:
            raise ProjectionInputError("durable ir_estimate kind is invalid")
        cohort = _decode_cohort(payload)
    return cohort


def _validate_step_record(payload: Mapping[str, Any]) -> None:
    required_sha256(payload.get("step_record_hash"), "durable step hash")
    if "estimate" not in payload:
        raise ProjectionInputError("durable step estimate is missing")


def _decode_cohort(payload: Mapping[str, Any]) -> IrCohortSelection:
    raw_hashes = payload.get("consumed_step_hashes")
    if not isinstance(raw_hashes, list):
        raise ProjectionInputError("durable cohort hashes must be a list")
    hashes = tuple(
        required_sha256(value, f"durable cohort hash[{index}]")
        for index, value in enumerate(raw_hashes)
    )
    if hashes != tuple(sorted(set(hashes))):
        raise ProjectionInputError("durable cohort hashes must be sorted and unique")
    return IrCohortSelection(cohort_from_json(payload.get("estimate")), hashes)


def new_derived_records(
    projection: EventProjection,
    basis: DecisionBasis,
) -> tuple[EventRecord, ...]:
    """Create the canonical suffix after a valid durable prefix."""
    assessment_payload = json_value(basis.assessment)
    if not isinstance(assessment_payload, dict):
        raise ProjectionInputError("assessment serialization is not an object")
    assessment_payload["learning_policy"] = json_value(basis.learning_policy)
    records = [
        _derived_record(projection, "assessment", assessment_payload),
        _derived_record(projection, "comparison", json_value(basis.comparison)),
    ]
    records.extend(
        _derived_record(
            projection,
            "ir_estimate",
            {
                "kind": "step",
                "step_record_hash": item.step_record_hash,
                "estimate": json_value(item.estimate),
            },
        )
        for item in basis.steps
    )
    records.append(
        _derived_record(
            projection,
            "ir_estimate",
            {
                "kind": "cohort",
                "estimate": json_value(basis.cohort),
                "consumed_step_hashes": list(basis.cohort_step_hashes),
            },
        )
    )
    records.append(
        _derived_record(
            projection,
            "learning_decision",
            {
                "learning_decision": json_value(basis.decision),
                "learning_reasons": json_value(basis.learning_reasons),
                "observed_load_sag_increase": json_value(basis.observed_load_sag_increase),
                "prepared_commit": json_value(basis.prepared),
                "outcome_basis": {
                    "assessment": json_value(basis.assessment),
                    "comparison": json_value(basis.comparison),
                    "cohort_estimate": json_value(basis.cohort),
                    "learning_policy": json_value(basis.learning_policy),
                    "reason_codes": [reason.value for reason in basis.reasons.values],
                    "reason_overflow": basis.reasons.overflow_count,
                },
            },
        )
    )
    expected = tuple(records)
    durable = projection.derived_records
    if len(durable) > len(expected):
        raise ProjectionInputError("durable derived record count exceeds frozen close plan")
    for index, durable_record in enumerate(durable):
        expected_record = expected[index]
        if (
            durable_record.provenance != "derived"
            or durable_record.record_type != expected_record.record_type
            or durable_record.payload != expected_record.payload
        ):
            raise ProjectionInputError(
                f"durable derived stage {index} conflicts with frozen close plan"
            )
    return expected[len(durable) :]


def _derived_record(
    projection: EventProjection,
    record_type: str,
    payload: Mapping[str, Any],
) -> EventRecord:
    final = _event_anchor(projection)
    if final is None:
        raise ProjectionInputError("event has no physical records")
    return EventRecord(
        record_type=record_type,
        boot_id=final.boot_id,
        wall_time_utc=final.wall_time_utc,
        monotonic_ns=final.monotonic_ns,
        payload=payload,
        provenance="derived",
    )


def _event_anchor(projection: EventProjection):
    if projection.end is not None:
        return projection.end
    if projection.start is not None:
        return projection.start
    return projection.records[-1] if projection.records else None
