"""Evidence and frozen forward-comparison threshold tests."""

import pytest

from src.domain.evidence import EvidenceContext, assess_evidence
from src.domain.forward_comparison import compare_forward_model
from src.domain.reasons import ComparisonReason, EvidenceReason
from src.domain.values import BlackoutKind, ComparisonMode, EvidenceClass


def _assessment(observations):
    return assess_evidence(
        observations,
        EvidenceContext(
            blackout_kind=BlackoutKind.BLACKOUT_REAL,
            frozen_snapshot_supported=True,
            current_battery_epoch_id="epoch-a",
            frozen_battery_epoch_id="epoch-a",
            capture_damaged=False,
            snapshot_within_budget=True,
        ),
    )


def test_input_voltage_missing_is_diagnostic_not_science_refusal(observation_factory):
    observations = tuple(observation_factory(second, input_voltage_v=None) for second in range(10))
    assessment = _assessment(observations)
    assert assessment.evidence_class == EvidenceClass.QUALIFYING
    assert EvidenceReason.INPUT_VOLTAGE_UNAVAILABLE in assessment.reasons.values


def test_cal_and_raw_gap_fail_evidence_closed(observation_factory):
    observations = (
        observation_factory(0),
        observation_factory(1, raw_status="OB CAL"),
        observation_factory(7),
    )
    assessment = _assessment(observations)
    assert assessment.evidence_class == EvidenceClass.OPERATIONAL_ONLY
    assert EvidenceReason.CALIBRATION_OBSERVED in assessment.reasons.values
    assert EvidenceReason.RAW_GAP_TOO_LARGE in assessment.reasons.values


def test_unknown_status_inside_active_capture_cannot_authorize_science(
    observation_factory,
):
    observations = tuple(
        observation_factory(second, raw_status="UNKNOWN" if second == 5 else "OB DISCHRG")
        for second in range(10)
    )

    assessment = _assessment(observations)

    assert assessment.evidence_class == EvidenceClass.OPERATIONAL_ONLY
    assert EvidenceReason.NOT_NATURAL_PHYSICAL_BLACKOUT in assessment.reasons.values


@pytest.mark.parametrize("replacement_second", (99.0, 98.0))
def test_non_increasing_monotonic_edge_cannot_authorize_science(
    observation_factory,
    replacement_second,
):
    observations = tuple(
        observation_factory(replacement_second if second == 100 else second)
        for second in range(201)
    )
    assessment = _assessment(observations)

    assert assessment.evidence_class == EvidenceClass.OPERATIONAL_ONLY
    assert EvidenceReason.INSUFFICIENT_COVERAGE in assessment.reasons.values


@pytest.mark.parametrize(
    ("gap_s", "qualifies"),
    ((4.999, True), (5.0, True), (5.001, False)),
)
def test_scientific_edge_gap_boundary(observation_factory, gap_s, qualifies):
    observations = tuple(
        observation_factory(second if second < 100 else second - 1 + gap_s) for second in range(201)
    )
    assessment = _assessment(observations)

    assert (assessment.evidence_class == EvidenceClass.QUALIFYING) is qualifies


@pytest.mark.parametrize(
    ("evaluated_duration_s", "expected_mode"),
    (
        (179, ComparisonMode.NONE),
        (180, ComparisonMode.SHORT_WINDOW),
        (181, ComparisonMode.SHORT_WINDOW),
    ),
)
def test_short_window_boundaries(
    observation_factory,
    frozen_snapshot,
    evaluated_duration_s,
    expected_mode,
):
    physical_end = 75 + evaluated_duration_s
    observations = tuple(observation_factory(second) for second in range(physical_end + 1))
    result = compare_forward_model(observations, frozen_snapshot, _assessment(observations))
    assert result.mode == expected_mode
    if expected_mode == ComparisonMode.NONE:
        assert result.reasons.values == (ComparisonReason.COMPARISON_NOT_ATTEMPTED,)
    else:
        assert ComparisonReason.SHORT_WINDOW_COMPARISON in result.reasons.values


@pytest.mark.parametrize(
    ("movement_v", "expected_mode"),
    (
        (0.199, ComparisonMode.SHORT_WINDOW),
        (0.200, ComparisonMode.FULL),
        (0.201, ComparisonMode.FULL),
    ),
)
def test_full_mode_movement_boundary_and_precedence(
    observation_factory,
    frozen_snapshot,
    movement_v,
    expected_mode,
):
    observations = tuple(
        observation_factory(
            second,
            voltage_v=13.2 - movement_v * max(0, second - 75) / 300.0,
        )
        for second in range(376)
    )
    result = compare_forward_model(observations, frozen_snapshot, _assessment(observations))
    assert result.mode == expected_mode
    assert result.evaluated_duration_s == pytest.approx(300.0)


def test_comparison_uses_frozen_snapshot_and_signed_residual(observation_factory, frozen_snapshot):
    observations = tuple(
        observation_factory(second, voltage_v=13.2 - 0.001 * second) for second in range(256)
    )
    first = compare_forward_model(observations, frozen_snapshot, _assessment(observations))
    second = compare_forward_model(observations, frozen_snapshot, _assessment(observations))
    assert first == second
    assert first.mean_residual_v is not None
    assert first.delivered_ah_proxy is not None
    assert first.delivered_ah_proxy > 0.0


def test_origin_uses_exact_window_medians_not_midpoint_outlier(
    observation_factory,
    frozen_snapshot,
):
    observations = tuple(
        observation_factory(second, voltage_v=13.0 if second == 75 else 12.0)
        for second in range(401)
    )

    result = compare_forward_model(observations, frozen_snapshot, _assessment(observations))

    assert result.evaluation_origin_monotonic_ns == 75_000_000_000
    assert result.mode == ComparisonMode.SHORT_WINDOW
    assert ComparisonReason.INSUFFICIENT_NORMALIZED_MOVEMENT not in result.reasons.values
