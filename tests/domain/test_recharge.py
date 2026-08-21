from datetime import datetime, timedelta, timezone

from src.domain.recharge import (
    RechargeAssessmentKind,
    RechargeObservationContext,
    RechargeSampleKind,
    RechargeSamplingPolicy,
    RechargeTerminalContext,
    RechargeTermination,
    decide_observation,
    observation_identity,
    terminal_assessment,
)
from src.domain.values import PhysicalObservation


def observation(second: int, *, voltage: float = 12.3, status: str = "OL") -> PhysicalObservation:
    return PhysicalObservation(
        "boot-a",
        second * 1_000_000_000,
        datetime(2026, 8, 21, tzinfo=timezone.utc) + timedelta(seconds=second),
        status,
        f"{voltage:.2f}",
        voltage,
        0.01,
        20.0,
        230.0,
    )


def test_sampling_keeps_backbone_and_slows_sparse_enrichment() -> None:
    policy = RechargeSamplingPolicy(
        backbone_interval_s=5.0,
        dense_enrichment_interval_s=1.0,
        sparse_enrichment_interval_s=10.0,
        dense_window_s=2.0,
    )
    first = observation(0)
    dense = decide_observation(
        RechargeObservationContext(policy, first, first, observation(1), 1.0, 2, 0)
    )
    sparse = decide_observation(
        RechargeObservationContext(policy, first, observation(2), observation(7), 7.0, 8, 0)
    )
    assert dense.persist is True
    assert dense.sample_kind is RechargeSampleKind.ENRICHMENT
    assert sparse.persist is True
    assert sparse.sample_kind is RechargeSampleKind.UNIFORM_BACKBONE


def test_identity_changes_when_raw_observation_changes() -> None:
    assert observation_identity(observation(1)) == observation_identity(observation(1))
    assert observation_identity(observation(1)) != observation_identity(observation(2))
    assert observation_identity(observation(1)) != observation_identity(
        observation(1, voltage=12.4)
    )


def test_terminal_result_names_diagnostic_usable_and_refused() -> None:
    policy = RechargeSamplingPolicy(
        required_consecutive_stable_windows=2,
        minimum_stabilization_duration_s=30.0,
    )
    diagnostic = terminal_assessment(
        RechargeTermination.SERVICE_STOP,
        RechargeTerminalContext(2, 3, 0, policy),
    )
    usable = terminal_assessment(
        RechargeTermination.CHARGE_STABILIZED,
        RechargeTerminalContext(3, 4, 2, policy, 30.0),
    )
    refused = terminal_assessment(
        RechargeTermination.GAP,
        RechargeTerminalContext(3, 4, 2, policy),
    )
    assert diagnostic.kind is RechargeAssessmentKind.DIAGNOSTIC
    assert usable.kind is RechargeAssessmentKind.USABLE
    assert "full charge is not established" in usable.reason
    assert refused.kind is RechargeAssessmentKind.REFUSED


def test_default_stabilization_duration_rejects_short_flat_window() -> None:
    policy = RechargeSamplingPolicy()
    assessment = terminal_assessment(
        RechargeTermination.CHARGE_STABILIZED,
        RechargeTerminalContext(
            4,
            4,
            policy.required_consecutive_stable_windows,
            policy,
            3.0,
        ),
    )
    assert assessment.kind is RechargeAssessmentKind.DIAGNOSTIC
    assert "incomplete" in assessment.reason
