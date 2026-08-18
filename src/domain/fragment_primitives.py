"""Primitive raw-linked fragment values and validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from math import isfinite
from typing import TypeVar, cast

from src.domain.values import PhysicalObservation

UINT64_MAX = (1 << 64) - 1
MAX_MONOTONIC_NS = 2**63 - 1
MAX_PHYSICAL_SAMPLES = 3_170
MAX_PROFILE_RECORDS = 96
MAX_COMPACT_DESCRIPTORS = 256
MAX_SPANS_PER_SLICE = 64
MAX_CONTRIBUTING_HASHES = 128
MAX_PROFILE_ISSUES = 8

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_EnumT = TypeVar("_EnumT", bound=StrEnum)


class ObservationOrigin(StrEnum):
    """Closed capture origin carried by every discharge fragment."""

    NATURAL = "natural"
    SELF_TEST = "self_test"
    UAT = "uat"


class AnchorKind(StrEnum):
    """Closed v3 boundary vocabulary from the unified evidence plan."""

    TRANSFER_TO_BATTERY = "transfer_to_battery"
    RAW_FIRMWARE_LB = "raw_firmware_lb"
    MODELED_SAFE_SHUTDOWN = "modeled_safe_shutdown"
    POWER_RESTORED = "power_restored"
    SERVICE_STOP = "service_stop"
    BOOT_BOUNDARY = "boot_boundary"
    CHARGE_STABILIZED = "charge_stabilized"
    GAP = "gap"
    CORRUPTION = "corruption"


class AnchorProvenance(StrEnum):
    """Allowed boundary provenance; there is no synthetic SYSTEM class."""

    PHYSICAL = "physical"
    FIRMWARE = "firmware"
    MODELED = "modeled"
    OPERATIONAL = "operational"


class ReadinessProvenance(StrEnum):
    """Raw provenance classes usable for the start-readiness fact."""

    PHYSICAL = "physical"
    FIRMWARE = "firmware"
    OPERATIONAL = "operational"


class ProfileReason(StrEnum):
    """Closed, bounded reasons retained on a derived profile."""

    MIXED_BLACKOUT_ID = "mixed_blackout_id"
    MIXED_PHYSICAL_EPISODE_ID = "mixed_physical_episode_id"
    MIXED_BATTERY_EPOCH_ID = "mixed_battery_epoch_id"
    MIXED_ORIGIN = "mixed_origin"
    MIXED_UAT_INTENT_ID = "mixed_uat_intent_id"
    POLICY_REVISION_MISMATCH = "policy_revision_mismatch"
    DUPLICATE_SAMPLE_HASH = "duplicate_sample_hash"
    MISSING_PARENT_SLICE = "missing_parent_slice"
    SAMPLE_BUDGET_EXCEEDED = "sample_budget_exceeded"
    ANCHOR_BUDGET_EXCEEDED = "anchor_budget_exceeded"
    LOAD_STEP_BUDGET_EXCEEDED = "load_step_budget_exceeded"
    FRAGMENT_BUDGET_EXHAUSTED = "fragment_budget_exhausted"


class OmittedFragmentKind(StrEnum):
    """Retention unit category named by the first omitted raw evidence."""

    ANCHOR = "anchor"
    SLICE = "slice"
    LOAD_STEP = "load_step"


_ANCHOR_PROVENANCE = {
    AnchorKind.TRANSFER_TO_BATTERY: AnchorProvenance.PHYSICAL,
    AnchorKind.RAW_FIRMWARE_LB: AnchorProvenance.FIRMWARE,
    AnchorKind.MODELED_SAFE_SHUTDOWN: AnchorProvenance.MODELED,
    AnchorKind.POWER_RESTORED: AnchorProvenance.PHYSICAL,
    AnchorKind.SERVICE_STOP: AnchorProvenance.OPERATIONAL,
    AnchorKind.BOOT_BOUNDARY: AnchorProvenance.OPERATIONAL,
    AnchorKind.CHARGE_STABILIZED: AnchorProvenance.OPERATIONAL,
    AnchorKind.GAP: AnchorProvenance.OPERATIONAL,
    AnchorKind.CORRUPTION: AnchorProvenance.OPERATIONAL,
}


@dataclass(frozen=True, slots=True)
class StartReadinessContext:
    """Only the raw readiness fact, reason, and provenance are retained."""

    ready: bool | None
    reason: str | None = None
    provenance: ReadinessProvenance | None = None

    def __post_init__(self) -> None:
        if self.ready is not None and not isinstance(self.ready, bool):
            raise TypeError("readiness fact must be bool or None")
        if self.reason is not None:
            _validate_id(self.reason, "readiness reason")
        if self.provenance is not None:
            provenance = _coerce_enum(self.provenance, ReadinessProvenance, "readiness provenance")
            object.__setattr__(self, "provenance", provenance)


@dataclass(frozen=True, slots=True)
class CanonicalDischargeSample:
    """One ordered physical sample addressed by its canonical raw hash."""

    sequence: int
    canonical_hash: str
    observation: PhysicalObservation

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("sample sequence must be an integer")
        if not 0 <= self.sequence <= UINT64_MAX:
            raise ValueError("sample sequence must be within unsigned 64-bit range")
        _require_sha256(self.canonical_hash, "sample canonical hash")
        if not isinstance(self.observation, PhysicalObservation):
            raise TypeError("sample observation must be PhysicalObservation")
        _validate_observation(self.observation)


@dataclass(frozen=True, slots=True)
class CanonicalSampleSpan:
    """A bounded descriptor of an ordered raw-sample span.

    The complete sample tuple is intentionally not part of this value.  A
    replay can validate the ordered SHA-256 descriptor against supplied
    samples with :func:`validate_canonical_sample_span`.
    """

    first_sequence: int
    last_sequence: int
    sample_count: int
    first_sample_hash: str
    last_sample_hash: str
    ordered_sample_hashes_sha256: str
    boot_id: str
    first_monotonic_ns: int
    last_monotonic_ns: int
    first_wall_time_utc: datetime
    last_wall_time_utc: datetime

    def __post_init__(self) -> None:
        _validate_nonnegative_int(self.first_sequence, "span first sequence")
        _validate_nonnegative_int(self.last_sequence, "span last sequence")
        _validate_nonnegative_int(self.sample_count, "span sample count")
        if self.sample_count <= 0:
            raise ValueError("sample span must be non-empty")
        if self.last_sequence - self.first_sequence + 1 != self.sample_count:
            raise ValueError("span sequence bounds do not match sample count")
        _require_sha256(self.first_sample_hash, "span first sample hash")
        _require_sha256(self.last_sample_hash, "span last sample hash")
        _require_sha256(self.ordered_sample_hashes_sha256, "span ordered sample hash digest")
        _validate_id(self.boot_id, "span boot ID")
        _validate_monotonic_int(self.first_monotonic_ns, "span first monotonic time")
        _validate_monotonic_int(self.last_monotonic_ns, "span last monotonic time")
        if self.last_monotonic_ns < self.first_monotonic_ns:
            raise ValueError("span monotonic bounds must be ordered")
        _validate_wall_time(self.first_wall_time_utc)
        _validate_wall_time(self.last_wall_time_utc)


def build_canonical_sample_span(
    samples: tuple[CanonicalDischargeSample, ...],
) -> CanonicalSampleSpan:
    """Build one replayable descriptor from a non-empty ordered sample tuple."""
    _validate_sample_sequence(samples)
    first = samples[0]
    last = samples[-1]
    digest = sha256(
        "".join(sample.canonical_hash for sample in samples).encode("ascii")
    ).hexdigest()
    return CanonicalSampleSpan(
        first_sequence=first.sequence,
        last_sequence=last.sequence,
        sample_count=len(samples),
        first_sample_hash=first.canonical_hash,
        last_sample_hash=last.canonical_hash,
        ordered_sample_hashes_sha256=digest,
        boot_id=first.observation.boot_id,
        first_monotonic_ns=first.observation.monotonic_ns,
        last_monotonic_ns=last.observation.monotonic_ns,
        first_wall_time_utc=first.observation.wall_time_utc,
        last_wall_time_utc=last.observation.wall_time_utc,
    )


def validate_canonical_sample_span(
    span: CanonicalSampleSpan,
    samples: tuple[CanonicalDischargeSample, ...],
) -> None:
    """Replay supplied samples and reject any mismatch with ``span``."""
    if not isinstance(span, CanonicalSampleSpan):
        raise TypeError("span must be CanonicalSampleSpan")
    candidate = build_canonical_sample_span(samples)
    if candidate != span:
        raise ValueError("supplied samples do not match canonical sample span")


@dataclass(frozen=True, slots=True)
class EndpointAnchor:
    """A typed physical, firmware, modeled, or operational boundary."""

    canonical_hash: str
    kind: AnchorKind
    provenance: AnchorProvenance
    boot_id: str
    wall_time_utc: datetime
    monotonic_ns: int
    source_sample_hash: str | None = None
    blackout_id: str = ""
    physical_episode_id: str = ""
    segment_id: str = ""

    def __post_init__(self) -> None:
        _require_sha256(self.canonical_hash, "anchor canonical hash")
        kind = _coerce_enum(self.kind, AnchorKind, "anchor kind")
        provenance = _coerce_enum(self.provenance, AnchorProvenance, "anchor provenance")
        if _ANCHOR_PROVENANCE[kind] is not provenance:
            raise ValueError(f"{kind.value} requires {_ANCHOR_PROVENANCE[kind].value} provenance")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "provenance", provenance)
        _validate_id(self.boot_id, "anchor boot ID")
        _validate_wall_time(self.wall_time_utc)
        _validate_monotonic_int(self.monotonic_ns, "anchor monotonic time")
        if self.source_sample_hash is not None:
            _require_sha256(self.source_sample_hash, "anchor source sample hash")
        for field_value, name in (
            (self.blackout_id, "anchor blackout ID"),
            (self.physical_episode_id, "anchor physical episode ID"),
            (self.segment_id, "anchor segment ID"),
        ):
            _validate_id(field_value, name)


def _validate_observation(observation: PhysicalObservation) -> None:
    _validate_id(observation.boot_id, "sample observation boot ID")
    _validate_monotonic_int(observation.monotonic_ns, "sample observation monotonic time")
    _validate_wall_time(observation.wall_time_utc)
    _validate_optional_finite(observation.battery_voltage_v, "battery voltage")
    _validate_optional_finite(observation.voltage_token_quantum_v, "voltage quantum")
    _validate_optional_finite(observation.load_percent, "load percentage")
    _validate_optional_finite(observation.input_voltage_v, "input voltage")


def _validate_wall_time(value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("wall time must be timezone-aware UTC")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("wall time must be UTC")


def _validate_optional_finite(value: object, field: str) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value))
    ):
        raise ValueError(f"{field} must be finite or None")


def _validate_sample_sequence(
    samples: tuple[CanonicalDischargeSample, ...], normal_gap_s: float | None = None
) -> None:
    if not isinstance(samples, tuple) or not samples:
        raise ValueError("sample sequence must be a non-empty tuple")
    first_boot = samples[0].observation.boot_id
    previous = samples[0]
    seen_hashes = {previous.canonical_hash}
    for current in samples[1:]:
        if current.observation.boot_id != first_boot:
            raise ValueError("sample sequence cannot span boot identities")
        if current.sequence != previous.sequence + 1:
            raise ValueError("samples must have contiguous increasing sequence")
        if current.canonical_hash in seen_hashes:
            raise ValueError("samples must have unique canonical hashes")
        if current.observation.monotonic_ns <= previous.observation.monotonic_ns:
            raise ValueError("samples must have increasing monotonic time")
        if normal_gap_s is not None:
            monotonic_gap = (
                current.observation.monotonic_ns - previous.observation.monotonic_ns
            ) / 1_000_000_000
            if monotonic_gap > normal_gap_s:
                raise ValueError("sample sequence cannot span an acquisition gap")
        seen_hashes.add(current.canonical_hash)
        previous = current


def _require_sha256(value: object, field: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")


def _validate_id(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8") from exc
    if len(encoded) > 128 or any(ord(char) < 0x20 for char in value):
        raise ValueError(f"{field} exceeds the bounded text contract")


def _validate_nonnegative_int(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < 0:
        raise ValueError(f"{field} must be nonnegative")
    if value > UINT64_MAX:
        raise ValueError(f"{field} exceeds unsigned 64-bit maximum")


def _validate_monotonic_int(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < 0:
        raise ValueError(f"{field} must be nonnegative")
    if value > MAX_MONOTONIC_NS:
        raise ValueError(f"{field} exceeds signed 63-bit maximum")


def _validate_positive_int(value: object, field: str) -> None:
    _validate_nonnegative_int(value, field)
    if value == 0:
        raise ValueError(f"{field} must be positive")


def _coerce_enum(value: object, enum_type: type[_EnumT], field: str) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return cast(_EnumT, enum_type(value))
        except ValueError as exc:
            raise ValueError(f"unknown {field}: {value!r}") from exc
    raise TypeError(f"{field} must be {enum_type.__name__}")
