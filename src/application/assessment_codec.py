"""Strict, bounded codecs at the sealed-assessment application boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Any

from src.application.model_port import PreparedModelCommit
from src.application.storage_values import EventProjection
from src.battery_math.lut import LutPoint
from src.domain.reasons import (
    ComparisonReason,
    EvidenceReason,
    IdentificationReason,
    LearningReason,
    OrderedReasons,
)
from src.domain.values import (
    ComparisonMode,
    EvidenceAssessment,
    EvidenceClass,
    ForwardComparison,
    FrozenModelSnapshot,
    IrCohortEstimate,
    IrLearningPolicy,
    LearningDecision,
    LoadStepEstimate,
    ModelChange,
    ModelCommitReceipt,
    NumericSummary,
    PhysicalObservation,
    ensure_supported_ir_learning_policy,
)

_OBSERVATION_FIELDS = frozenset(
    {
        "boot_id",
        "monotonic_ns",
        "wall_time_utc",
        "raw_status",
        "battery_voltage_raw",
        "battery_voltage_v",
        "voltage_token_quantum_v",
        "load_percent",
        "input_voltage_v",
    }
)
_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_revision",
        "evaluation_revision",
        "battery_epoch_id",
        "scientific_fingerprint",
        "rated_capacity_ah",
        "nominal_voltage_v",
        "nominal_power_watts",
        "soh",
        "peukert_exponent",
        "ir_k_v_per_pp",
        "ir_reference_load_percent",
        "lut",
        "learning_policy",
    }
)


class ProjectionInputError(ValueError):
    """A projected raw or derived record failed strict boundary decoding."""


def json_value(value: object) -> Any:
    """Convert immutable domain values to the canonical JSON-compatible shape."""
    if isinstance(value, datetime):
        converted: Any = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    elif isinstance(value, Enum):
        converted = value.value
    elif isinstance(value, OrderedReasons):
        converted = {
            "reason_codes": [reason.value for reason in value.values],
            "reason_overflow": value.overflow_count,
        }
    elif is_dataclass(value) and not isinstance(value, type):
        converted = {field.name: json_value(getattr(value, field.name)) for field in fields(value)}
        reasons = getattr(value, "reasons", None)
        if isinstance(reasons, OrderedReasons):
            converted["reason_codes"] = [reason.value for reason in reasons.values]
            converted["reason_overflow"] = reasons.overflow_count
            converted.pop("reasons", None)
    elif isinstance(value, Mapping):
        converted = {str(key): json_value(item) for key, item in value.items()}
    elif isinstance(value, (tuple, list, frozenset)):
        converted = [json_value(item) for item in value]
    else:
        converted = value
    return converted


def project_observations(projection: EventProjection) -> tuple[PhysicalObservation, ...]:
    """Decode the start observation and bounded physical records in a projection."""
    if projection.start is None:
        raise ProjectionInputError("missing_start")
    start_payload = mapping(projection.start.payload, "start.payload")
    raw_start = mapping(start_payload.get("observation"), "start.observation")
    raw_records = (raw_start, *(record.payload for record in projection.observations))
    return tuple(parse_observation(raw, index) for index, raw in enumerate(raw_records))


def project_snapshot(projection: EventProjection) -> FrozenModelSnapshot:
    """Decode the frozen model snapshot captured at event start."""
    if projection.start is None:
        raise ProjectionInputError("missing_start")
    start_payload = mapping(projection.start.payload, "start.payload")
    raw = mapping(start_payload.get("frozen_model"), "start.frozen_model")
    if frozenset(raw) != _SNAPSHOT_FIELDS:
        raise ProjectionInputError("frozen snapshot has invalid fields")
    lut_raw = raw["lut"]
    if not isinstance(lut_raw, list) or not lut_raw:
        raise ProjectionInputError("frozen snapshot LUT is invalid")
    lut = []
    for entry in lut_raw:
        point = mapping(entry, "frozen snapshot LUT point")
        if frozenset(point) != {"voltage_v", "soc", "source"}:
            raise ProjectionInputError("frozen snapshot LUT point has invalid fields")
        lut.append(
            LutPoint(
                required_number(point["voltage_v"], "lut.voltage_v"),
                required_number(point["soc"], "lut.soc"),
                required_string(point["source"], "lut.source"),
            )
        )
    try:
        return FrozenModelSnapshot(
            schema_revision=required_string(raw["schema_revision"], "schema_revision"),
            evaluation_revision=required_string(raw["evaluation_revision"], "evaluation_revision"),
            battery_epoch_id=required_string(raw["battery_epoch_id"], "battery_epoch_id"),
            scientific_fingerprint=required_string(
                raw["scientific_fingerprint"], "scientific_fingerprint"
            ),
            rated_capacity_ah=required_number(raw["rated_capacity_ah"], "rated_capacity_ah"),
            nominal_voltage_v=required_number(raw["nominal_voltage_v"], "nominal_voltage_v"),
            nominal_power_watts=required_number(raw["nominal_power_watts"], "nominal_power_watts"),
            soh=required_number(raw["soh"], "soh"),
            peukert_exponent=required_number(raw["peukert_exponent"], "peukert_exponent"),
            ir_k_v_per_pp=required_number(raw["ir_k_v_per_pp"], "ir_k_v_per_pp"),
            ir_reference_load_percent=required_number(
                raw["ir_reference_load_percent"], "ir_reference_load_percent"
            ),
            lut=tuple(lut),
            learning_policy=policy_from_json(raw.get("learning_policy")),
        )
    except (TypeError, ValueError) as exc:
        raise ProjectionInputError("frozen snapshot is invalid") from exc


def parse_observation(raw: object, index: int) -> PhysicalObservation:
    value = mapping(raw, f"observation[{index}]")
    if frozenset(value) != _OBSERVATION_FIELDS:
        raise ProjectionInputError(f"observation[{index}] has invalid fields")
    try:
        return PhysicalObservation(
            boot_id=required_string(value["boot_id"], "boot_id"),
            monotonic_ns=required_int(value["monotonic_ns"], "monotonic_ns"),
            wall_time_utc=parse_utc(value["wall_time_utc"]),
            raw_status=required_string(value["raw_status"], "raw_status"),
            battery_voltage_raw=optional_string(value["battery_voltage_raw"]),
            battery_voltage_v=optional_float(value["battery_voltage_v"]),
            voltage_token_quantum_v=optional_float(value["voltage_token_quantum_v"]),
            load_percent=optional_float(value["load_percent"]),
            input_voltage_v=optional_float(value["input_voltage_v"]),
        )
    except (TypeError, ValueError) as exc:
        raise ProjectionInputError(f"observation[{index}] is invalid") from exc


def step_hash(estimate: LoadStepEstimate) -> str:
    encoded = json.dumps(
        json_value(estimate),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def reasons_from_json(value: object) -> OrderedReasons:
    raw = mapping(value, "reasons")
    parsed = []
    for item in raw.get("reason_codes", []):
        for enum_type in (EvidenceReason, ComparisonReason, IdentificationReason, LearningReason):
            try:
                parsed.append(enum_type(str(item)))
                break
            except ValueError:
                continue
        else:
            raise ProjectionInputError(f"unknown reason: {item}")
    overflow = required_int(raw.get("reason_overflow", 0), "reason_overflow")
    return OrderedReasons(tuple(parsed), overflow)


def numeric_from_json(value: object) -> NumericSummary:
    raw = mapping(value, "numeric summary")
    return NumericSummary(
        optional_float(raw.get("minimum")),
        optional_float(raw.get("maximum")),
        optional_float(raw.get("mean")),
        optional_float(raw.get("population_stddev")),
    )


def assessment_from_json(value: object) -> EvidenceAssessment:
    raw = mapping(value, "assessment")
    return EvidenceAssessment(
        EvidenceClass(required_string(raw.get("evidence_class"), "evidence_class")),
        required_number(raw.get("duration_s"), "duration_s"),
        required_int(raw.get("observation_count"), "observation_count"),
        required_number(raw.get("coverage_ratio"), "coverage_ratio"),
        required_number(raw.get("max_gap_s"), "max_gap_s"),
        numeric_from_json(raw.get("voltage_summary")),
        numeric_from_json(raw.get("load_summary")),
        reasons_from_json(raw),
    )


def comparison_from_json(value: object) -> ForwardComparison:
    raw = mapping(value, "comparison")
    origin = raw.get("evaluation_origin_monotonic_ns")
    return ForwardComparison(
        mode=ComparisonMode(required_string(raw.get("mode"), "mode")),
        evaluation_origin_monotonic_ns=(
            None if origin is None else required_int(origin, "evaluation_origin_monotonic_ns")
        ),
        evaluated_duration_s=required_number(
            raw.get("evaluated_duration_s"), "evaluated_duration_s"
        ),
        point_count=required_int(raw.get("point_count"), "point_count"),
        start_residual_v=optional_float(raw.get("start_residual_v")),
        end_residual_v=optional_float(raw.get("end_residual_v")),
        mean_residual_v=optional_float(raw.get("mean_residual_v")),
        rmse_v=optional_float(raw.get("rmse_v")),
        observed_slope_v_per_s=optional_float(raw.get("observed_slope_v_per_s")),
        predicted_slope_v_per_s=optional_float(raw.get("predicted_slope_v_per_s")),
        delivered_ah_proxy=optional_float(raw.get("delivered_ah_proxy")),
        reasons=reasons_from_json(raw),
    )


def cohort_from_json(value: object) -> IrCohortEstimate:
    raw = mapping(value, "cohort")
    return IrCohortEstimate(
        battery_epoch_id=required_string(raw.get("battery_epoch_id"), "battery_epoch_id"),
        blackout_ids=required_strings(raw.get("blackout_ids"), "blackout_ids"),
        step_count=required_int(raw.get("step_count"), "step_count"),
        up_step_count=required_int(raw.get("up_step_count"), "up_step_count"),
        down_step_count=required_int(raw.get("down_step_count"), "down_step_count"),
        median_k_v_per_pp=optional_float(raw.get("median_k_v_per_pp")),
        mad_ratio=optional_float(raw.get("mad_ratio")),
        reasons=reasons_from_json(raw),
    )


def decision_from_json(value: object) -> LearningDecision:
    raw = mapping(value, "decision")
    return LearningDecision(
        required_bool(raw.get("record_history"), "record_history"),
        required_bool(raw.get("compare_forward_model"), "compare_forward_model"),
        required_bool(raw.get("record_decline_evidence"), "record_decline_evidence"),
        required_bool(raw.get("commit_ir_k"), "commit_ir_k"),
    )


def change_from_json(value: object) -> ModelChange:
    raw = mapping(value, "model change")
    return ModelChange(
        required_string(raw.get("parameter"), "parameter"),
        required_number(raw.get("value_before"), "value_before"),
        required_number(raw.get("measured_estimate"), "measured_estimate"),
        required_number(raw.get("value_after"), "value_after"),
        required_strings(raw.get("evidence_hashes"), "evidence_hashes"),
        required_bool(raw.get("bound_applied"), "bound_applied"),
    )


def policy_from_json(value: object) -> IrLearningPolicy:
    raw = mapping(value, "learning policy")
    try:
        return ensure_supported_ir_learning_policy(IrLearningPolicy.from_mapping(raw))
    except (TypeError, ValueError) as exc:
        raise ProjectionInputError(f"invalid learning policy: {exc}") from exc


def prepared_from_json(value: object) -> PreparedModelCommit:
    raw = mapping(value, "prepared commit")
    return PreparedModelCommit(
        blackout_id=required_string(raw.get("blackout_id"), "blackout_id"),
        change=change_from_json(raw.get("change")),
        committed_at=parse_utc(raw.get("committed_at")),
        model_hash_before=required_string(raw.get("model_hash_before"), "model_hash_before"),
        expected_model_hash_after=required_string(
            raw.get("expected_model_hash_after"), "expected_model_hash_after"
        ),
        expected_scientific_fingerprint_after=required_string(
            raw.get("expected_scientific_fingerprint_after"),
            "expected_scientific_fingerprint_after",
        ),
        learning_policy=policy_from_json(raw.get("learning_policy")),
    )


def receipt_from_json(value: object) -> ModelCommitReceipt:
    raw = mapping(value, "model commit receipt")
    return ModelCommitReceipt(
        blackout_id=required_string(raw.get("blackout_id"), "blackout_id"),
        parameter=required_string(raw.get("parameter"), "parameter"),
        value_before=required_number(raw.get("value_before"), "value_before"),
        measured_estimate=required_number(raw.get("measured_estimate"), "measured_estimate"),
        value_after=required_number(raw.get("value_after"), "value_after"),
        model_hash_before=required_string(raw.get("model_hash_before"), "model_hash_before"),
        model_hash_after=required_string(raw.get("model_hash_after"), "model_hash_after"),
        scientific_fingerprint_before=required_string(
            raw.get("scientific_fingerprint_before"), "scientific_fingerprint_before"
        ),
        scientific_fingerprint_after=required_string(
            raw.get("scientific_fingerprint_after"), "scientific_fingerprint_after"
        ),
        evidence_set_id=required_string(raw.get("evidence_set_id"), "evidence_set_id"),
        consumed_step_hashes=required_strings(
            raw.get("consumed_step_hashes"), "consumed_step_hashes"
        ),
        reference_reparameterization=required_bool(
            raw.get("reference_reparameterization"), "reference_reparameterization"
        ),
        safety_oracle=required_string(raw.get("safety_oracle"), "safety_oracle"),
    )


def mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProjectionInputError(f"{name} must be an object")
    return value


def parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProjectionInputError("timestamp is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ProjectionInputError("timestamp is invalid") from exc
    return parsed.astimezone(timezone.utc)


def optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectionInputError("numeric projection field is invalid")
    return float(value)


def optional_string(value: object) -> str | None:
    if value is None:
        return None
    return required_string(value, "optional string")


def required_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ProjectionInputError(f"{name} must be a string")
    return value


def required_sha256(value: object, name: str) -> str:
    text = required_string(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ProjectionInputError(f"{name} must be lowercase SHA-256")
    return text


def required_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectionInputError(f"{name} must be an integer")
    return value


def required_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectionInputError(f"{name} must be numeric")
    return float(value)


def required_float(value: float | None, name: str) -> float:
    if value is None:
        raise ProjectionInputError(f"{name} is required")
    return value


def required_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ProjectionInputError(f"{name} must be a boolean")
    return value


def required_strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProjectionInputError(f"{name} must be a list")
    return tuple(required_string(item, f"{name}[{index}]") for index, item in enumerate(value))
