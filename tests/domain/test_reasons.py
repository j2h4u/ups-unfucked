"""Focused reason ordering regressions for bounded terminal outcomes."""

from src.domain.reasons import EvidenceReason, IdentificationReason, order_reasons


def test_evidence_capture_damaged_keeps_priority_over_later_diagnostics():
    ordered = order_reasons(
        (
            EvidenceReason.INSUFFICIENT_COVERAGE,
            EvidenceReason.RAW_GAP_TOO_LARGE,
            EvidenceReason.INPUT_VOLTAGE_UNAVAILABLE,
            EvidenceReason.CAPTURE_DAMAGED,
            IdentificationReason.INSUFFICIENT_UNCONSUMED_STEPS,
            IdentificationReason.CURRENT_BLACKOUT_STEP_REQUIRED,
            IdentificationReason.INSUFFICIENT_BLACKOUT_DIVERSITY,
            IdentificationReason.BOTH_STEP_DIRECTIONS_REQUIRED,
            IdentificationReason.MIXED_BATTERY_EPOCH,
            IdentificationReason.MIXED_EVALUATION_REVISION,
        )
    )

    assert EvidenceReason.CAPTURE_DAMAGED in ordered.values
    assert ordered.overflow_count == 2
