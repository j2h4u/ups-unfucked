"""Versioned numeric policy for the immutable fragment domain kernel.

The byte values here are construction reservations from the authoritative
capture plan.  They are arithmetic admission bounds, not a wire encoder or a
serialization contract.
"""

from dataclasses import dataclass
from math import isfinite

UINT64_MAX = (1 << 64) - 1
MAX_PHYSICAL_SAMPLES = 3_170
MAX_PROFILE_RECORDS = 96
MAX_COMPACT_DESCRIPTORS = 256
MAX_SPANS_PER_SLICE = 64
MAX_CONTRIBUTING_HASHES = 128
MAX_PROFILE_ISSUES = 8


def _positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class DerivedTailBudget:
    """Plan-level derived-tail reservation, independent of canonical encoding."""

    max_derived_records: int = 128
    max_derived_record_bytes: int = 8 * 1024
    max_compact_descriptors: int = 256
    max_descriptor_bytes: int = 256
    max_total_bytes: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        for value, name in (
            (self.max_derived_records, "derived record budget"),
            (self.max_derived_record_bytes, "derived record size"),
            (self.max_compact_descriptors, "compact descriptor budget"),
            (self.max_descriptor_bytes, "compact descriptor size"),
            (self.max_total_bytes, "derived total budget"),
        ):
            _positive_int(value, name)
        if self.max_derived_records * self.max_derived_record_bytes > self.max_total_bytes:
            raise ValueError("derived record reservation exceeds total derived budget")
        if self.max_compact_descriptors * self.max_descriptor_bytes > self.max_total_bytes:
            raise ValueError("compact descriptor reservation exceeds total derived budget")

    @property
    def max_record_total_bytes(self) -> int:
        """The plan's full derived-record reservation (128 x 8 KiB)."""
        return self.max_derived_records * self.max_derived_record_bytes


@dataclass(frozen=True, slots=True)
class DischargeFragmentPolicy:
    """Immutable, supported policy revision for discharge fragments.

    ``max_physical_samples`` is the physical-sample cardinality ceiling for a
    fragment.  It is independent of how canonical sample spans are chunked.
    """

    revision: str = "discharge-fragments-v1"
    normal_gap_s: float = 5.0
    load_step_gap_s: float = 2.5
    min_coverage_ratio: float = 0.90
    min_voltage_v: float = 8.0
    max_voltage_v: float = 15.0
    min_voltage_quanta: int = 1
    min_voltage_span_v: float = 0.20
    slope_window_points: int = 31
    noise_window_points: int = 31
    stable_load_band_pp: float = 2.0
    stable_load_window_points: int = 31
    allowed_origins: tuple[str, ...] = ("natural", "self_test", "uat")
    uat_intent_required: bool = True
    continuation_copies_origin: bool = True
    same_boot_required: bool = True
    short_curve_duration_s: float = 180.0
    full_curve_duration_s: float = 300.0
    full_curve_span_v: float = 0.20
    origin_delay_s: float = 60.0
    origin_window_points: int = 31
    max_load_stddev_pp: float = 2.0
    # Capture append construction inequality from the plan:
    # START + samples + GAP + ANCHOR + END <= 62 MiB.
    capture_append_limit_bytes: int = 62 * 1024 * 1024
    max_physical_record_bytes: int = 20 * 1024
    reserved_start_records: int = 1
    reserved_gap_records: int = 1
    reserved_anchor_records: int = 1
    reserved_end_records: int = 1
    max_physical_samples: int = MAX_PHYSICAL_SAMPLES
    max_profile_records: int = MAX_PROFILE_RECORDS
    max_slices_per_record: int = 16
    max_anchors_per_record: int = 32
    max_load_steps_per_record: int = 16
    max_anchors_per_slice: int = 2
    max_load_steps_per_slice: int = 1
    max_contributing_hashes_per_step: int = MAX_CONTRIBUTING_HASHES
    max_profile_issues: int = MAX_PROFILE_ISSUES
    derived_tail_budget: DerivedTailBudget = DerivedTailBudget()

    def __post_init__(self) -> None:
        _validate_policy(self)

    @property
    def physical_reservation_records(self) -> int:
        return (
            self.reserved_start_records
            + self.reserved_gap_records
            + self.reserved_anchor_records
            + self.reserved_end_records
        )

    @property
    def physical_construction_bytes(self) -> int:
        return (
            self.max_physical_samples + self.physical_reservation_records
        ) * self.max_physical_record_bytes


def _validate_policy(policy: DischargeFragmentPolicy) -> None:
    if not isinstance(policy.revision, str) or not policy.revision.strip():
        raise ValueError("fragment policy revision must be a non-empty string")
    numeric = (
        policy.normal_gap_s,
        policy.load_step_gap_s,
        policy.min_coverage_ratio,
        policy.min_voltage_v,
        policy.max_voltage_v,
        policy.min_voltage_span_v,
        policy.short_curve_duration_s,
        policy.full_curve_duration_s,
        policy.full_curve_span_v,
        policy.origin_delay_s,
        policy.max_load_stddev_pp,
        policy.stable_load_band_pp,
    )
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value))
        for value in numeric
    ):
        raise ValueError("fragment policy numeric values must be finite numbers")
    _validate_thresholds(policy)
    _validate_bools(policy)
    for value, name in _budgets(policy):
        _positive_int(value, name)
    _validate_physical_construction(policy)
    if not isinstance(policy.derived_tail_budget, DerivedTailBudget):
        raise TypeError("derived tail budget must be DerivedTailBudget")


def _validate_thresholds(policy: DischargeFragmentPolicy) -> None:
    _validate_gap_thresholds(policy)
    _validate_voltage_thresholds(policy)
    _validate_duration_thresholds(policy)
    _validate_window_thresholds(policy)


def _validate_gap_thresholds(policy: DischargeFragmentPolicy) -> None:
    if policy.normal_gap_s <= 0.0 or policy.load_step_gap_s <= 0.0:
        raise ValueError("fragment gap bounds must be positive")
    if policy.load_step_gap_s > policy.normal_gap_s:
        raise ValueError("load-step gap cannot exceed the normal gap")


def _validate_voltage_thresholds(policy: DischargeFragmentPolicy) -> None:
    if not 0.0 < policy.min_coverage_ratio <= 1.0:
        raise ValueError("coverage ratio must be in (0, 1]")
    if not 0.0 <= policy.min_voltage_v < policy.max_voltage_v:
        raise ValueError("battery voltage bounds are invalid")
    if policy.min_voltage_span_v <= 0.0:
        raise ValueError("minimum voltage span must be positive")


def _validate_duration_thresholds(policy: DischargeFragmentPolicy) -> None:
    if (
        policy.short_curve_duration_s <= 0.0
        or policy.full_curve_duration_s < policy.short_curve_duration_s
    ):
        raise ValueError("curve duration bounds are invalid")
    if policy.full_curve_span_v <= 0.0 or policy.origin_delay_s < 0.0:
        raise ValueError("curve span and origin delay must be valid")


def _validate_window_thresholds(policy: DischargeFragmentPolicy) -> None:
    _positive_int(policy.origin_window_points, "origin window points")
    for value, name in (
        (policy.min_voltage_quanta, "minimum voltage quanta"),
        (policy.slope_window_points, "slope window points"),
        (policy.noise_window_points, "noise window points"),
        (policy.stable_load_window_points, "stable-load window points"),
    ):
        _positive_int(value, name)
    if policy.allowed_origins != ("natural", "self_test", "uat"):
        raise ValueError("fragment origin vocabulary is closed")


def _validate_bools(policy: DischargeFragmentPolicy) -> None:
    for value, name in (
        (policy.uat_intent_required, "UAT intent requirement"),
        (policy.continuation_copies_origin, "continuation provenance rule"),
        (policy.same_boot_required, "same-boot rule"),
    ):
        if value is not True:
            raise ValueError(f"{name} is required by discharge-fragments-v1")


def _budgets(policy: DischargeFragmentPolicy) -> tuple[tuple[object, str], ...]:
    return (
        (policy.max_physical_samples, "physical sample budget"),
        (policy.max_profile_records, "profile record budget"),
        (policy.max_slices_per_record, "profile slice budget"),
        (policy.max_anchors_per_record, "profile anchor budget"),
        (policy.max_load_steps_per_record, "profile load-step budget"),
        (policy.max_anchors_per_slice, "anchors per slice budget"),
        (policy.max_load_steps_per_slice, "load steps per slice budget"),
        (policy.max_contributing_hashes_per_step, "load-step hash budget"),
        (policy.max_profile_issues, "profile issue budget"),
        (policy.capture_append_limit_bytes, "capture append limit"),
        (policy.max_physical_record_bytes, "maximum physical record size"),
        (policy.reserved_start_records, "START reservation"),
        (policy.reserved_gap_records, "gap reservation"),
        (policy.reserved_anchor_records, "anchor reservation"),
        (policy.reserved_end_records, "END reservation"),
    )


def _validate_physical_construction(policy: DischargeFragmentPolicy) -> None:
    reservation = policy.physical_reservation_records
    if (
        policy.max_physical_samples + reservation
        > policy.capture_append_limit_bytes // policy.max_physical_record_bytes
    ):
        raise ValueError("physical construction exceeds capture append limit")
    if (
        policy.max_physical_samples + reservation + 1
    ) * policy.max_physical_record_bytes <= policy.capture_append_limit_bytes:
        raise ValueError("physical sample budget is not the safe maximum")


DEFAULT_DISCHARGE_FRAGMENT_POLICY = DischargeFragmentPolicy()
SUPPORTED_FRAGMENT_POLICY_REVISIONS = frozenset({DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision})
_SUPPORTED_POLICIES = {
    DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision: DEFAULT_DISCHARGE_FRAGMENT_POLICY
}


def resolve_fragment_policy(revision: str) -> DischargeFragmentPolicy:
    """Resolve one exact policy revision; unknown revisions fail closed."""
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("fragment policy revision must be a non-empty string")
    try:
        return _SUPPORTED_POLICIES[revision]
    except KeyError as exc:
        raise ValueError(f"unknown fragment policy revision: {revision}") from exc


def ensure_supported_fragment_policy(policy: DischargeFragmentPolicy) -> DischargeFragmentPolicy:
    """Verify both the revision and every frozen value of a policy object."""
    if not isinstance(policy, DischargeFragmentPolicy):
        raise TypeError("fragment policy must be a DischargeFragmentPolicy")
    resolved = resolve_fragment_policy(policy.revision)
    if policy != resolved:
        raise ValueError("fragment policy values do not match its revision")
    return policy
