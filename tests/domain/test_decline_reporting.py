"""Evidence-only decline boundaries and bounded reporting tests."""

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.decline import (
    FirmwareReserveSample,
    LoadSagTrendSample,
    LongPartialSample,
    assess_firmware_lb_reserve,
    assess_load_sag_trend,
    assess_long_partial_curve,
)
from src.domain.evidence import EvidenceContext, assess_evidence
from src.domain.learning import make_learning_decision
from src.domain.reasons import ComparisonReason, DeclineReason, order_reasons
from src.domain.reporting import ReportEvidenceContext, build_plain_language_report
from src.domain.values import (
    BlackoutKind,
    ComparisonMode,
    DeclineVerdict,
    ForwardComparison,
    TerminalDisposition,
    TerminalOutcome,
)

START = datetime(2026, 8, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("recent", "declines"),
    ((0.0129, False), (0.0130, True), (0.0131, True)),
)
def test_load_sag_decline_threshold(recent, declines):
    values = (0.010, 0.010, 0.010, recent, recent, recent)
    samples = tuple(
        LoadSagTrendSample(
            blackout_id=str(index),
            event_started_utc=START + timedelta(days=index),
            battery_epoch_id="epoch-a",
            transition_monotonic_ns=index,
            k_settled_v_per_pp=value,
        )
        for index, value in enumerate(values)
    )
    result = assess_load_sag_trend(samples)
    assert (DeclineReason.POSSIBLE_LOAD_SAG_DEGRADATION in result.reasons.values) is declines


@pytest.mark.parametrize(
    ("ratio", "declines"),
    ((0.799, True), (0.800, True), (0.801, False)),
)
def test_firmware_reserve_ratio_boundary(ratio, declines):
    proxies = (100.0, 100.0, 100.0, 100.0 * ratio, 100.0 * ratio, 100.0 * ratio)
    samples = tuple(
        FirmwareReserveSample(
            blackout_id=str(index),
            event_started_utc=START + timedelta(days=index),
            battery_epoch_id="epoch-a",
            ready_at_start=True,
            start_voltage_v=13.4,
            mean_load_percent=20.0,
            load_stddev_percent=2.0,
            coverage_ratio=0.90,
            max_gap_s=5.0,
            reserve_proxy_pp_s=proxy,
        )
        for index, proxy in enumerate(proxies)
    )
    result = assess_firmware_lb_reserve(samples)
    assert (DeclineReason.POSSIBLE_RESERVE_DECLINE in result.reasons.values) is declines


@pytest.mark.parametrize(
    ("difference_v", "declines"),
    ((0.199, False), (0.200, True), (0.201, True)),
)
def test_long_partial_voltage_boundary(difference_v, declines):
    voltages = (13.0, 13.0, 13.0, 13.0 - difference_v, 13.0 - difference_v, 13.0 - difference_v)
    samples = tuple(
        LongPartialSample(
            blackout_id=str(index),
            event_started_utc=START + timedelta(days=index),
            battery_epoch_id="epoch-a",
            ready_at_start=True,
            duration_s=650.0,
            start_voltage_v=13.4,
            mean_load_percent=20.0,
            load_stddev_percent=2.0,
            coverage_ratio=0.90,
            max_gap_s=5.0,
            voltage_at_600s_v=voltage,
        )
        for index, voltage in enumerate(voltages)
    )
    result = assess_long_partial_curve(samples)
    assert (DeclineReason.POSSIBLE_RESERVE_DECLINE in result.reasons.values) is declines


def test_incomparable_event_is_not_dropped_to_improve_verdict():
    samples = tuple(
        FirmwareReserveSample(
            blackout_id=str(index),
            event_started_utc=START + timedelta(days=index),
            battery_epoch_id="epoch-a",
            ready_at_start=index != 2,
            start_voltage_v=13.4,
            mean_load_percent=20.0,
            load_stddev_percent=0.0,
            coverage_ratio=1.0,
            max_gap_s=1.0,
            reserve_proxy_pp_s=100.0 if index < 3 else 50.0,
        )
        for index in range(7)
    )
    result = assess_firmware_lb_reserve(samples)
    assert result.reasons.values == (DeclineReason.INSUFFICIENT_COMPARABLE_EVIDENCE,)


def test_load_sag_trend_refuses_mixed_battery_epochs():
    samples = tuple(
        LoadSagTrendSample(
            blackout_id=str(index),
            event_started_utc=START + timedelta(days=index),
            battery_epoch_id="epoch-b" if index == 5 else "epoch-a",
            transition_monotonic_ns=index,
            k_settled_v_per_pp=0.010,
        )
        for index in range(6)
    )

    result = assess_load_sag_trend(samples)

    assert result.reasons.values == (DeclineReason.INSUFFICIENT_COMPARABLE_EVIDENCE,)


def test_load_sag_trend_caps_each_event_at_two_steps_before_latest_six() -> None:
    repeated = tuple(
        LoadSagTrendSample(
            blackout_id="event-a",
            event_started_utc=START,
            battery_epoch_id="epoch-a",
            transition_monotonic_ns=index,
            k_settled_v_per_pp=0.010,
        )
        for index in range(3)
    )
    distinct = tuple(
        LoadSagTrendSample(
            blackout_id=f"event-{index}",
            event_started_utc=START + timedelta(days=index),
            battery_epoch_id="epoch-a",
            transition_monotonic_ns=index,
            k_settled_v_per_pp=0.010,
        )
        for index in range(1, 5)
    )

    result = assess_load_sag_trend((*repeated, *distinct))

    assert result.verdict == DeclineVerdict.STABLE_WITHIN_OBSERVED_EVIDENCE
    assert result.event_ids.count("event-a") == 2
    assert len(result.event_ids) == 6


@pytest.mark.parametrize(
    ("duration_s", "eligible"),
    ((649.999, False), (650.0, True), (650.001, True)),
)
def test_long_partial_duration_boundary(duration_s, eligible):
    samples = tuple(
        LongPartialSample(
            blackout_id=str(index),
            event_started_utc=START + timedelta(days=index),
            battery_epoch_id="epoch-a",
            ready_at_start=True,
            duration_s=duration_s,
            start_voltage_v=13.4,
            mean_load_percent=20.0,
            load_stddev_percent=2.0,
            coverage_ratio=0.90,
            max_gap_s=5.0,
            voltage_at_600s_v=13.0,
        )
        for index in range(6)
    )

    result = assess_long_partial_curve(samples)

    assert (result.reasons.values != (DeclineReason.INSUFFICIENT_COMPARABLE_EVIDENCE,)) is eligible


def test_long_partial_requires_voltage_at_horizon_after_comparability() -> None:
    samples = tuple(
        LongPartialSample(
            blackout_id=str(index),
            event_started_utc=START + timedelta(days=index),
            battery_epoch_id="epoch-a",
            ready_at_start=True,
            duration_s=650.0,
            start_voltage_v=13.4,
            mean_load_percent=20.0,
            load_stddev_percent=2.0,
            coverage_ratio=0.90,
            max_gap_s=5.0,
            voltage_at_600s_v=None if index == 5 else 13.0,
        )
        for index in range(6)
    )

    result = assess_long_partial_curve(samples)

    assert result.reasons.values == (DeclineReason.INSUFFICIENT_COMPARABLE_EVIDENCE,)


def test_long_partial_requires_ready_start_for_every_event() -> None:
    samples = tuple(
        LongPartialSample(
            blackout_id=str(index),
            event_started_utc=START + timedelta(days=index),
            battery_epoch_id="epoch-a",
            ready_at_start=index != 5,
            duration_s=650.0,
            start_voltage_v=13.4,
            mean_load_percent=20.0,
            load_stddev_percent=2.0,
            coverage_ratio=0.90,
            max_gap_s=5.0,
            voltage_at_600s_v=13.0,
        )
        for index in range(6)
    )

    result = assess_long_partial_curve(samples)

    assert result.reasons.values == (DeclineReason.INSUFFICIENT_COMPARABLE_EVIDENCE,)


def test_report_calls_partial_censored_and_raw_lb_diagnostic_only(observation_factory):
    observations = tuple(observation_factory(second) for second in range(10))
    assessment = assess_evidence(
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
        order_reasons((ComparisonReason.COMPARISON_NOT_ATTEMPTED,)),
    )
    decision = make_learning_decision(
        assessment,
        comparison,
        None,
        decline_evidence_eligible=False,
    )
    outcome = TerminalOutcome(
        TerminalDisposition.RECORDED_ONLY,
        assessment,
        comparison,
        None,
        decision,
        None,
        order_reasons((ComparisonReason.COMPARISON_NOT_ATTEMPTED,)),
    )
    report = build_plain_language_report(
        outcome,
        blackout_id="event-a",
        generated_utc=START,
        evidence=ReportEvidenceContext(True, None),
        consumed_evidence_budget_remaining=252,
    )
    text = " ".join(report.lines)
    assert "censored evidence, not measured full runtime or SoH" in text
    assert "raw LB" in text
    assert "did not command virtual LB or FSD" in text
    assert "comparison_not_attempted" in text
    assert "252" in text
