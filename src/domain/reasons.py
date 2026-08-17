"""Typed, ordered reason-code namespaces for scientific decisions."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class ReadinessReason(StrEnum):
    INSUFFICIENT_CONTINUOUS_ONLINE_TIME = "insufficient_continuous_online_time"
    BOOT_CHANGED = "boot_changed"
    NOT_ONLINE = "not_online"
    CALIBRATION_ACTIVE = "calibration_active"
    VOLTAGE_UNAVAILABLE = "voltage_unavailable"
    VOLTAGE_OUT_OF_RANGE = "readiness_voltage_out_of_range"
    ACQUISITION_GAP = "readiness_acquisition_gap"
    VOLTAGE_SPAN_TOO_WIDE = "readiness_voltage_span_too_wide"


class EvidenceReason(StrEnum):
    EVENT_NOT_NATURALLY_COMPLETED = "event_not_naturally_completed"
    NOT_NATURAL_PHYSICAL_BLACKOUT = "not_natural_physical_blackout"
    CALIBRATION_OBSERVED = "calibration_observed"
    UNSUPPORTED_FROZEN_SNAPSHOT = "unsupported_frozen_snapshot"
    BATTERY_EPOCH_MISMATCH = "battery_epoch_mismatch"
    INSUFFICIENT_COVERAGE = "insufficient_coverage"
    RAW_GAP_TOO_LARGE = "raw_gap_too_large"
    REBOOT_GAP = "reboot_gap"
    INVALID_BATTERY_VOLTAGE = "invalid_battery_voltage"
    INVALID_LOAD_PERCENT = "invalid_load_percent"
    CAPTURE_DAMAGED = "capture_damaged"
    SNAPSHOT_BUDGET_EXCEEDED = "snapshot_budget_exceeded"
    INPUT_VOLTAGE_UNAVAILABLE = "input_voltage_unavailable"


class ComparisonReason(StrEnum):
    COMPARISON_NOT_ATTEMPTED = "comparison_not_attempted"
    NO_STABLE_POST_TRANSFER_ORIGIN = "no_stable_post_transfer_origin"
    INSUFFICIENT_EVALUATED_DURATION = "insufficient_evaluated_duration"
    INSUFFICIENT_NORMALIZED_MOVEMENT = "insufficient_normalized_movement"
    NO_QUALIFYING_SHORT_WINDOW = "no_qualifying_short_window"
    SHORT_WINDOW_COMPARISON = "short_window_comparison"
    INVALID_EFFECTIVE_RUNTIME = "invalid_effective_runtime"
    WITHIN_KNOWN_LUT_FRAME_UNCERTAINTY = "within_known_lut_frame_uncertainty"
    BEYOND_KNOWN_LUT_FRAME_UNCERTAINTY = "beyond_known_lut_frame_uncertainty"


class IdentificationReason(StrEnum):
    STEP_STATUS_NOT_ELIGIBLE = "step_status_not_eligible"
    STEP_CROSSES_BOOT = "step_crosses_boot"
    STEP_GAP_TOO_LARGE = "step_gap_too_large"
    LOAD_OUT_OF_RANGE = "step_load_out_of_range"
    LOAD_PLATEAU_UNSTABLE = "load_plateau_unstable"
    VOLTAGE_PLATEAU_SLOPE_TOO_LARGE = "voltage_plateau_slope_too_large"
    DISCHARGE_DRIFT_TOO_LARGE = "discharge_drift_too_large"
    VOLTAGE_MOVEMENT_TOO_SMALL = "voltage_movement_too_small"
    VOLTAGE_LOAD_DIRECTION_MISMATCH = "voltage_load_direction_mismatch"
    INVALID_STEP_VOLTAGE = "invalid_step_voltage"
    IR_ESTIMATE_OUT_OF_RANGE = "ir_estimate_out_of_range"
    SAG_NOT_SETTLED = "sag_not_settled"
    INSUFFICIENT_UNCONSUMED_STEPS = "insufficient_unconsumed_steps"
    CURRENT_BLACKOUT_STEP_REQUIRED = "current_blackout_step_required"
    INSUFFICIENT_BLACKOUT_DIVERSITY = "insufficient_blackout_diversity"
    BOTH_STEP_DIRECTIONS_REQUIRED = "both_step_directions_required"
    MIXED_BATTERY_EPOCH = "mixed_battery_epoch"
    MIXED_EVALUATION_REVISION = "mixed_evaluation_revision"
    MIXED_LEARNING_POLICY = "mixed_learning_policy"
    HIGH_COHORT_DISPERSION = "high_cohort_dispersion"
    COHORT_PROJECTION_UNAVAILABLE = "cohort_projection_unavailable"
    CANDIDATE_EVENT_OVERFLOW = "candidate_event_overflow"


class LearningReason(StrEnum):
    COHORT_NOT_ELIGIBLE = "cohort_not_eligible"
    IR_REFERENCE_FRAME_NOT_TRANSFORMED = "ir_reference_frame_not_transformed"
    UNSAFE_UPWARD_IR_CHANGE_NOT_APPLIED = "unsafe_upward_ir_change_not_applied"
    IR_CHANGE_BELOW_NOISE_FLOOR = "ir_change_below_noise_floor"
    COMMIT_RATE_LIMITED = "commit_rate_limited"
    COMMIT_RATE_WINDOW_INDETERMINATE = "commit_rate_window_indeterminate"
    EPOCH_CUMULATIVE_DECREASE_LIMIT = "epoch_cumulative_decrease_limit"
    CONSUMED_EVIDENCE_BUDGET_EXHAUSTED = "consumed_evidence_budget_exhausted"
    EVIDENCE_ALREADY_CONSUMED = "evidence_already_consumed"
    UNSUPPORTED_MODEL_PARAMETER = "unsupported_model_parameter"
    MISSING_COMMIT_EVIDENCE = "missing_commit_evidence"
    IR_COMMIT_NOT_CANONICAL = "ir_commit_not_canonical"
    IR_BOUND_FLAG_MISMATCH = "ir_bound_flag_mismatch"
    INVALID_EVIDENCE_HASH = "invalid_evidence_hash"
    DUPLICATE_EVIDENCE_HASH = "duplicate_evidence_hash"
    INVALID_MODEL_CHANGE_VALUE = "invalid_model_change_value"
    INVALID_COMMIT_TIME = "invalid_commit_time"
    SAFETY_ORACLE_FAILED = "safety_oracle_failed"
    LEARNING_POLICY_MISMATCH = "learning_policy_mismatch"
    MODEL_STATE_CONFLICT = "model_state_conflict"


class DeclineReason(StrEnum):
    INSUFFICIENT_COMPARABLE_EVIDENCE = "insufficient_comparable_evidence"
    EVIDENCE_STORAGE_CORRUPT = "evidence_storage_corrupt"
    POSSIBLE_LOAD_SAG_DEGRADATION = "possible_load_sag_degradation"
    POSSIBLE_RESERVE_DECLINE = "possible_reserve_decline"
    STABLE_WITHIN_OBSERVED_EVIDENCE = "stable_within_observed_evidence"


class InfrastructureReason(StrEnum):
    CAPTURE_DAMAGED = "capture_damaged"
    PROCESSING_BACKLOG_FULL = "processing_backlog_full"


type ReasonCode = (
    ReadinessReason
    | EvidenceReason
    | ComparisonReason
    | IdentificationReason
    | LearningReason
    | DeclineReason
    | InfrastructureReason
)

MAX_REASONS: Final = 8
_REASON_TYPES: Final = (
    ReadinessReason,
    EvidenceReason,
    ComparisonReason,
    IdentificationReason,
    LearningReason,
    DeclineReason,
    InfrastructureReason,
)


def _build_reason_order() -> dict[ReasonCode, int]:
    order: dict[ReasonCode, int] = {}
    for position, reason in enumerate(
        reason for reason_type in _REASON_TYPES for reason in reason_type
    ):
        # StrEnum members from different namespaces compare by their string value.
        # Preserve the first, more specific scientific priority for duplicate codes.
        order.setdefault(reason, position)
    return order


_REASON_ORDER: Final = _build_reason_order()


@dataclass(frozen=True, slots=True)
class OrderedReasons:
    """A canonical bounded reason tuple with explicit overflow accounting."""

    values: tuple[ReasonCode, ...]
    overflow_count: int


def order_reasons(reasons: tuple[ReasonCode, ...]) -> OrderedReasons:
    """Deduplicate, canonically order, and bound known typed reasons."""
    for reason in reasons:
        if not isinstance(reason, _REASON_TYPES):
            raise TypeError(f"unknown reason code: {reason!r}")
    unique = tuple(sorted(set(reasons), key=_REASON_ORDER.__getitem__))
    return OrderedReasons(unique[:MAX_REASONS], max(0, len(unique) - MAX_REASONS))
