"""Frozen numeric policy and non-persistable Wave 1 bounds."""

from dataclasses import replace

import pytest

from src.domain.fragment_policy import (
    DEFAULT_DISCHARGE_FRAGMENT_POLICY,
    DerivedTailBudget,
    DischargeFragmentPolicy,
    ensure_supported_fragment_policy,
    resolve_fragment_policy,
)


def test_default_policy_freezes_current_numeric_contract() -> None:
    policy = DEFAULT_DISCHARGE_FRAGMENT_POLICY

    assert policy.revision == "discharge-fragments-v1"
    assert policy.normal_gap_s == 5.0
    assert policy.load_step_gap_s == 2.5
    assert policy.min_coverage_ratio == 0.90
    assert (policy.min_voltage_v, policy.max_voltage_v) == (8.0, 15.0)
    assert (policy.min_voltage_quanta, policy.min_voltage_span_v) == (1, 0.20)
    assert (policy.short_curve_duration_s, policy.full_curve_duration_s) == (180.0, 300.0)
    assert policy.full_curve_span_v == 0.20
    assert (policy.origin_delay_s, policy.origin_window_points) == (60.0, 31)
    assert policy.max_load_stddev_pp == 2.0
    assert policy.max_physical_samples == 3_170
    assert policy.physical_reservation_records == 4
    assert policy.physical_construction_bytes <= policy.capture_append_limit_bytes
    assert (
        policy.physical_construction_bytes + policy.max_physical_record_bytes
        > policy.capture_append_limit_bytes
    )
    assert policy.max_profile_records == 96
    assert policy.max_slices_per_record == 16
    assert policy.max_anchors_per_record == 32
    assert policy.max_load_steps_per_record == 16
    assert policy.max_anchors_per_slice == 2
    assert policy.max_load_steps_per_slice == 1
    assert policy.derived_tail_budget.max_derived_records == 128
    assert policy.derived_tail_budget.max_derived_record_bytes == 8 * 1024
    assert policy.derived_tail_budget.max_compact_descriptors == 256
    assert policy.derived_tail_budget.max_descriptor_bytes == 256
    assert policy.derived_tail_budget.max_record_total_bytes == 1 * 1024 * 1024


@pytest.mark.parametrize(
    "changes",
    [
        {"revision": ""},
        {"normal_gap_s": 0.0},
        {"load_step_gap_s": 6.0},
        {"min_coverage_ratio": 0.0},
        {"min_coverage_ratio": 1.1},
        {"min_voltage_v": 15.0},
        {"full_curve_duration_s": 179.0},
        {"origin_window_points": 0},
        {"uat_intent_required": False},
        {"continuation_copies_origin": False},
        {"same_boot_required": False},
        {"max_physical_samples": 3_171},
    ],
)
def test_policy_rejects_invalid_boundaries(changes: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(DEFAULT_DISCHARGE_FRAGMENT_POLICY, **changes)


def test_policy_revision_must_resolve_exactly() -> None:
    assert (
        resolve_fragment_policy(DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision)
        == DEFAULT_DISCHARGE_FRAGMENT_POLICY
    )
    with pytest.raises(ValueError):
        resolve_fragment_policy("future-v2")
    with pytest.raises(ValueError):
        ensure_supported_fragment_policy(
            replace(DEFAULT_DISCHARGE_FRAGMENT_POLICY, revision="future-v2")
        )
    with pytest.raises(ValueError):
        ensure_supported_fragment_policy(
            replace(DEFAULT_DISCHARGE_FRAGMENT_POLICY, normal_gap_s=4.0)
        )


def test_policy_has_no_encoder_or_legacy_byte_contract() -> None:
    policy = DEFAULT_DISCHARGE_FRAGMENT_POLICY
    assert not hasattr(policy, "max_profile_line_bytes")
    assert not hasattr(policy, "max_derived_bytes")
    assert not hasattr(policy, "estimated_canonical_bytes")
    assert not hasattr(policy, "max_samples_per_slice")
    assert not hasattr(policy, "max_samples_per_profile")


def test_policy_type_is_frozen_and_numeric_values_are_finite() -> None:
    assert (
        DischargeFragmentPolicy(revision="discharge-fragments-v1")
        == DEFAULT_DISCHARGE_FRAGMENT_POLICY
    )
    with pytest.raises(ValueError):
        replace(DEFAULT_DISCHARGE_FRAGMENT_POLICY, stable_load_band_pp=float("nan"))
    with pytest.raises(ValueError):
        DerivedTailBudget(max_derived_records=129, max_derived_record_bytes=16 * 1024)
