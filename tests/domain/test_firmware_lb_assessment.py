"""Wave 2A firmware-LB prefix assessment tests."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from src.domain.firmware_lb_assessment import (
    DEFAULT_FIRMWARE_LB_POLICY,
    FirmwareLbDisposition,
    FirmwareLbReason,
    assess_firmware_lb,
)
from src.domain.fragment_policy import DEFAULT_DISCHARGE_FRAGMENT_POLICY
from src.domain.fragments import (
    CanonicalDischargeSample,
    DischargeFragmentProfile,
    DischargeSlice,
    ObservationOrigin,
    OmittedFragmentKind,
    ProfileReason,
    ReadinessProvenance,
    StartReadinessContext,
)


def _profile(observation_factory, **options):
    count = options.get("count", 121)
    lb_at = options.get("lb_at", 100)
    readiness = options.get("readiness", True)
    origin = options.get("origin", ObservationOrigin.NATURAL)
    mutate = options.get("mutate")
    observations = []
    for index in range(count):
        overrides = {"raw_status": "OB DISCHRG LB" if index == lb_at else "OB DISCHRG"}
        if mutate is not None:
            overrides.update(mutate(index))
        observations.append(observation_factory(index, **overrides))
    samples = tuple(
        CanonicalDischargeSample(
            index,
            hashlib.sha256(f"firmware-lb-{index}".encode()).hexdigest(),
            observation,
        )
        for index, observation in enumerate(observations)
    )
    slice_value = DischargeSlice(
        samples=samples,
        blackout_id="blackout-a",
        physical_episode_id="episode-a",
        battery_epoch_id="epoch-a",
        segment_id="segment-a",
        origin=origin,
        policy_revision=DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision,
        readiness_context=StartReadinessContext(
            readiness,
            "known-full" if readiness is True else "partial-start" if readiness is False else None,
            ReadinessProvenance.OPERATIONAL if readiness is not None else None,
        ),
        uat_intent_id="uat-a" if origin is ObservationOrigin.UAT else None,
    )
    return DischargeFragmentProfile((), (slice_value,), (), slice_value.policy_revision)


def _split_profile(observation_factory, **options):
    split = options["split"]
    second_start = options["second_start"]
    second_count = options["second_count"]
    second_boot = options.get("second_boot", "boot-a")
    lb_at = options.get("lb_at", 100)
    early_lb_at = options.get("early_lb_at")
    first = []
    for index in range(split):
        first.append(
            observation_factory(
                index,
                raw_status="OB DISCHRG LB" if index in {lb_at, early_lb_at} else "OB DISCHRG",
            )
        )
    second = [
        observation_factory(
            second_start + index,
            boot_id=second_boot,
            raw_status="OB DISCHRG LB"
            if second_start + index in {lb_at, early_lb_at}
            else "OB DISCHRG",
        )
        for index in range(second_count)
    ]
    slices = []
    for offset, observations in ((0, first), (split, second)):
        samples = tuple(
            CanonicalDischargeSample(
                offset + index,
                hashlib.sha256(f"split-firmware-lb-{offset + index}".encode()).hexdigest(),
                observation,
            )
            for index, observation in enumerate(observations)
        )
        slices.append(
            DischargeSlice(
                samples,
                "blackout-a",
                "episode-a",
                "epoch-a",
                f"segment-{offset}",
                ObservationOrigin.NATURAL,
                DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision,
                readiness_context=StartReadinessContext(
                    True, "known-full", ReadinessProvenance.OPERATIONAL
                ),
            )
        )
    return DischargeFragmentProfile((), tuple(slices), (), slices[0].policy_revision)


def test_natural_prefix_is_comparable_and_shadow_only(observation_factory):
    assessment = assess_firmware_lb(_profile(observation_factory))
    assert assessment.disposition is FirmwareLbDisposition.COMPARABLE
    assert assessment.comparable is True
    assert assessment.lb_sequence == 100
    assert assessment.raw_sample_count == 101
    assert assessment.shadow_only is True


def test_partial_start_is_diagnostic_not_erased(observation_factory):
    assessment = assess_firmware_lb(_profile(observation_factory, readiness=False))
    assert assessment.disposition is FirmwareLbDisposition.DIAGNOSTIC_ONLY
    assert assessment.comparable is False
    assert FirmwareLbReason.START_NOT_READY in assessment.reasons
    assert assessment.lb_sample_hash is not None


@pytest.mark.parametrize(
    "origin,mutate,reason",
    (
        (ObservationOrigin.SELF_TEST, None, FirmwareLbReason.NON_NATURAL_ORIGIN),
        (ObservationOrigin.UAT, None, FirmwareLbReason.NON_NATURAL_ORIGIN),
        (
            ObservationOrigin.NATURAL,
            lambda index: {"raw_status": "OB CAL"} if index == 20 else {},
            FirmwareLbReason.CALIBRATION_STATUS,
        ),
        (
            ObservationOrigin.NATURAL,
            lambda index: {"input_voltage_v": 120.0} if index == 20 else {},
            FirmwareLbReason.HIGH_INPUT_VOLTAGE,
        ),
    ),
)
def test_unnatural_or_calibration_prefix_refuses_science(
    observation_factory, origin, mutate, reason
):
    assessment = assess_firmware_lb(_profile(observation_factory, origin=origin, mutate=mutate))
    assert assessment.disposition is FirmwareLbDisposition.REFUSED
    assert reason in assessment.reasons


def test_pre_lb_gap_after_early_lb_is_rejected(observation_factory):
    profile = _split_profile(
        observation_factory,
        split=80,
        second_start=90,
        second_count=40,
        lb_at=110,
        early_lb_at=30,
    )
    assessment = assess_firmware_lb(profile)
    assert assessment.disposition is FirmwareLbDisposition.REFUSED
    assert FirmwareLbReason.PRE_LB_ACQUISITION_GAP in assessment.reasons


def test_pre_lb_reboot_is_rejected_by_record_order(observation_factory):
    profile = _split_profile(
        observation_factory,
        split=100,
        second_start=1,
        second_count=20,
        second_boot="boot-b",
        lb_at=20,
    )
    assessment = assess_firmware_lb(profile)
    assert assessment.disposition is FirmwareLbDisposition.REFUSED
    assert FirmwareLbReason.PRE_LB_REBOOT in assessment.reasons


def test_post_lb_damage_does_not_invalidate_prefix(observation_factory):
    profile = _split_profile(
        observation_factory,
        split=101,
        second_start=200,
        second_count=8,
        second_boot="boot-b",
        lb_at=100,
    )
    assessment = assess_firmware_lb(profile)
    assert assessment.disposition is FirmwareLbDisposition.COMPARABLE
    assert assessment.lb_sequence == 100
    assert assessment.raw_sample_count == 101


def test_absent_lb_is_explicit_refusal(observation_factory):
    profile = _profile(observation_factory, lb_at=None)
    assessment = assess_firmware_lb(profile)
    assert assessment.disposition is FirmwareLbDisposition.REFUSED
    assert FirmwareLbReason.NO_FIRMWARE_LB in assessment.reasons


def test_invalid_load_and_voltage_are_bounded_refusals(observation_factory):
    assessment = assess_firmware_lb(
        _profile(
            observation_factory,
            mutate=lambda index: {"load_percent": None, "voltage_v": None} if index == 95 else {},
        )
    )
    assert assessment.disposition is FirmwareLbDisposition.REFUSED
    assert FirmwareLbReason.INVALID_LOAD in assessment.reasons
    assert FirmwareLbReason.INVALID_VOLTAGE in assessment.reasons


def test_profile_overflow_is_retained_without_erasing_prefix(observation_factory):
    profile = replace(
        _profile(observation_factory),
        profile_issues=(ProfileReason.FRAGMENT_BUDGET_EXHAUSTED,),
        issue_overflow_count=1,
        slice_overflow_count=1,
        first_unprofiled_raw_hash="f" * 64,
        first_unprofiled_kind=OmittedFragmentKind.SLICE,
    )
    assessment = assess_firmware_lb(profile)
    assert assessment.profile_issue_overflow_count == 1
    assert assessment.disposition is FirmwareLbDisposition.COMPARABLE


def test_policy_mismatch_is_explicit(observation_factory):
    assessment = assess_firmware_lb(
        _profile(observation_factory),
        replace(DEFAULT_FIRMWARE_LB_POLICY, fragment_policy_revision="other-policy"),
    )
    assert assessment.disposition is FirmwareLbDisposition.REFUSED
    assert FirmwareLbReason.POLICY_REVISION_MISMATCH in assessment.reasons
