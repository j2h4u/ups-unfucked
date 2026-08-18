"""Pure values for the v3 physical blackout capture lane.

The values in this module deliberately stop at the capture boundary.  They
carry raw observations and loss facts, but do not know about JSONL, files, or
application storage.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from math import isfinite

from src.domain.blackout_terminal import ContinuationKind
from src.domain.fragment_primitives import MAX_MONOTONIC_NS, UINT64_MAX
from src.domain.fragments import ObservationOrigin, StartReadinessContext
from src.domain.values import FrozenModelSnapshot, PhysicalObservation

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_NUT_KEY_RE = re.compile(r"[A-Za-z0-9._-]+\Z")
_MAX_ID_BYTES = 128
_MAX_TOKEN_BYTES = 8192
_MAX_GAP_COUNT = UINT64_MAX
_MAX_GAP_SUBREASONS = 16
_EPOCH_SENTINEL = datetime(1970, 1, 1, tzinfo=timezone.utc)


class DischargeGapReason(StrEnum):
    """Closed reasons for a coalesced loss receipt."""

    PRESTART_OVERFLOW = "prestart_overflow"
    IN_FLIGHT_OVERFLOW = "in_flight_overflow"
    RESIDUAL_OVERFLOW = "residual_overflow"
    TELEMETRY_REPLY_LOST = "telemetry_reply_lost"
    CODEC_OVERSIZE = "codec_oversize"
    MALFORMED_REPLY = "malformed_reply"
    CAPTURE_UNAVAILABLE = "capture_unavailable"
    STORAGE_FAILURE = "storage_failure"
    CORRUPT_CHAIN = "corrupt_chain"
    BOOT_BOUNDARY = "boot_boundary"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RawNutToken:
    """One original NUT key with logical token and exact wire spelling.

    ``wire_lexeme`` is the exact UTF-8 text between the protocol's outer
    double quotes.  It therefore retains escape spelling (for example
    ``quoted \\\"value\\\"``), while ``token`` is the independently decoded
    logical value.
    """

    key: str
    token: str
    wire_lexeme: str

    def __post_init__(self) -> None:
        _text(self.key, "NUT key", maximum=_MAX_TOKEN_BYTES)
        if _NUT_KEY_RE.fullmatch(self.key) is None:
            raise ValueError("NUT key is not a supported identifier")
        _wire_text(self.token, "NUT token")
        _wire_text(self.wire_lexeme, "NUT wire lexeme")
        if _decode_nut_wire(self.wire_lexeme) != self.token:
            raise ValueError("NUT logical token does not match its wire lexeme")


@dataclass(frozen=True, slots=True)
class FrozenModelCapture:
    """Atomic model evidence retained by a blackout START.

    The persisted-file hash is deliberately supplied by the model owner.  It
    must not be reconstructed from the reduced scientific snapshot.
    """

    snapshot: FrozenModelSnapshot
    persisted_model_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, FrozenModelSnapshot):
            raise TypeError("model capture snapshot must be FrozenModelSnapshot")
        _hash(self.snapshot.scientific_fingerprint, "scientific fingerprint")
        _hash(self.persisted_model_sha256, "persisted model hash")


@dataclass(frozen=True, slots=True)
class CapturedTelemetry:
    """Typed observation plus the complete, ordered raw NUT reply."""

    observation: PhysicalObservation
    raw_tokens: tuple[RawNutToken, ...]
    complete: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.observation, PhysicalObservation):
            raise TypeError("captured observation must be PhysicalObservation")
        _validate_physical_observation(self.observation)
        if self.complete is not True:
            raise ValueError("captured telemetry must carry a complete raw-token map")
        if not isinstance(self.raw_tokens, tuple):
            raise TypeError("raw NUT tokens must be a tuple")
        if any(not isinstance(item, RawNutToken) for item in self.raw_tokens):
            raise TypeError("raw NUT tokens must contain RawNutToken values")
        keys = tuple(item.key for item in self.raw_tokens)
        if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
            raise ValueError("raw NUT tokens must be sorted by unique key")
        if len(self.canonical_raw_bytes) > 16 * 1024:
            raise ValueError("raw NUT token map exceeds 16 KiB")
        _validate_raw_observation_binding(self.observation, self.raw_tokens)

    @property
    def canonical_raw_bytes(self) -> bytes:
        """Return the deterministic raw-token map used by the evidence hash."""
        return _raw_token_bytes(self.raw_tokens)


@dataclass(frozen=True, slots=True)
class BlackoutStart:
    """Immutable START stamp for one physical blackout aggregate."""

    blackout_id: str
    physical_episode_id: str
    battery_epoch_id: str
    segment_id: str
    observation_origin: ObservationOrigin
    wall_time_utc: datetime
    monotonic_ns: int
    boot_id: str
    policy_revision: str
    capability_baseline_hash: str
    frozen_model_capture: FrozenModelCapture
    readiness_context: StartReadinessContext | None = None
    uat_intent_id: str | None = None
    continued_from: str | None = None
    continuation_kind: ContinuationKind | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.blackout_id, "blackout ID"),
            (self.physical_episode_id, "physical episode ID"),
            (self.battery_epoch_id, "battery epoch ID"),
            (self.segment_id, "segment ID"),
            (self.boot_id, "boot ID"),
            (self.policy_revision, "policy revision"),
        ):
            _text(value, name)
        _enum(self.observation_origin, ObservationOrigin, "observation origin")
        _utc(self.wall_time_utc)
        _bounded_monotonic(self.monotonic_ns, "monotonic time")
        _hash(self.capability_baseline_hash, "capability baseline hash")
        if not isinstance(self.frozen_model_capture, FrozenModelCapture):
            raise TypeError("frozen model capture must be FrozenModelCapture")
        if self.frozen_model_capture.snapshot.battery_epoch_id != self.battery_epoch_id:
            raise ValueError("model capture and START battery epochs must match")
        if self.readiness_context is not None and not isinstance(
            self.readiness_context, StartReadinessContext
        ):
            raise TypeError("readiness context must be StartReadinessContext or None")
        _validate_origin_intent(self.observation_origin, self.uat_intent_id)
        _optional_text(self.uat_intent_id, "UAT intent ID")
        _optional_text(self.continued_from, "continued-from blackout ID")
        _optional_enum(self.continuation_kind, ContinuationKind, "continuation kind")
        if (self.continued_from is None) != (self.continuation_kind is None):
            raise ValueError("continued-from and continuation kind must be supplied together")


@dataclass(frozen=True, slots=True)
class DischargeSample:
    """One physical discharge observation and its complete raw reply."""

    sequence: int
    captured: CapturedTelemetry
    blackout_id: str
    physical_episode_id: str
    battery_epoch_id: str
    segment_id: str
    observation_origin: ObservationOrigin
    canonical_hash: str
    uat_intent_id: str | None = None

    def __post_init__(self) -> None:
        _bounded_uint64(self.sequence, "sample sequence")
        if not isinstance(self.captured, CapturedTelemetry):
            raise TypeError("sample captured telemetry must be CapturedTelemetry")
        for value, name in (
            (self.blackout_id, "blackout ID"),
            (self.physical_episode_id, "physical episode ID"),
            (self.battery_epoch_id, "battery epoch ID"),
            (self.segment_id, "segment ID"),
        ):
            _text(value, name)
        _enum(self.observation_origin, ObservationOrigin, "observation origin")
        _hash(self.canonical_hash, "sample canonical hash")
        _validate_origin_intent(self.observation_origin, self.uat_intent_id)
        _optional_text(self.uat_intent_id, "UAT intent ID")

    @classmethod
    def from_telemetry(
        cls, sequence: int, captured: CapturedTelemetry, identity: "DischargeSampleIdentity"
    ) -> "DischargeSample":
        """Construct a sample with the canonical hash of typed plus raw fields."""
        digest = canonical_discharge_sample_hash(sequence, captured, identity)
        return cls(
            sequence,
            captured,
            identity.blackout_id,
            identity.physical_episode_id,
            identity.battery_epoch_id,
            identity.segment_id,
            identity.observation_origin,
            digest,
            identity.uat_intent_id,
        )


@dataclass(frozen=True, slots=True)
class DischargeGap:
    """A bounded coalesced loss receipt; missing raw values remain missing."""

    blackout_id: str
    physical_episode_id: str
    battery_epoch_id: str
    segment_id: str
    observation_origin: ObservationOrigin
    reason: DischargeGapReason
    count: int
    first_boot_id: str
    last_boot_id: str
    first_monotonic_ns: int
    last_monotonic_ns: int
    receipt_boot_id: str
    receipt_monotonic_ns: int
    receipt_wall_time_utc: datetime
    first_wall_time_utc: datetime | None = None
    last_wall_time_utc: datetime | None = None
    failed_command: str | None = None
    error_type: str | None = None
    loss_terminal_boundary_kind: str | None = None
    loss_terminal_boundary_wall_time_utc: datetime | None = None
    uat_intent_id: str | None = None
    subreason_counts: tuple["GapSubreasonCount", ...] = ()

    def __post_init__(self) -> None:
        for value, name in (
            (self.blackout_id, "blackout ID"),
            (self.physical_episode_id, "physical episode ID"),
            (self.battery_epoch_id, "battery epoch ID"),
            (self.segment_id, "segment ID"),
            (self.first_boot_id, "first boot ID"),
            (self.last_boot_id, "last boot ID"),
        ):
            _text(value, name)
        _enum(self.observation_origin, ObservationOrigin, "observation origin")
        _enum(self.reason, DischargeGapReason, "gap reason")
        if (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or not 0 < self.count <= _MAX_GAP_COUNT
        ):
            raise ValueError("gap count must be a positive bounded integer")
        _bounded_monotonic(self.first_monotonic_ns, "first monotonic time")
        _bounded_monotonic(self.last_monotonic_ns, "last monotonic time")
        _text(self.receipt_boot_id, "receipt boot ID")
        _bounded_monotonic(self.receipt_monotonic_ns, "receipt monotonic time")
        _utc(self.receipt_wall_time_utc)
        if self.receipt_wall_time_utc.year == _EPOCH_SENTINEL.year:
            raise ValueError("receipt wall time must be factual, not the epoch sentinel")
        if (
            self.first_boot_id == self.last_boot_id
            and self.last_monotonic_ns < self.first_monotonic_ns
        ):
            raise ValueError("gap monotonic boundaries must be ordered")
        _optional_utc(self.first_wall_time_utc)
        _optional_utc(self.last_wall_time_utc)
        _optional_text(self.failed_command, "failed command")
        _optional_text(self.error_type, "error type")
        if (self.failed_command is None) != (self.error_type is None):
            raise ValueError("failed command and error type must be supplied together")
        if self.loss_terminal_boundary_kind is not None:
            _enum(self.loss_terminal_boundary_kind, _LOSS_BOUNDARIES, "loss terminal boundary kind")
        _optional_utc(self.loss_terminal_boundary_wall_time_utc)
        if (
            self.loss_terminal_boundary_wall_time_utc is not None
            and self.loss_terminal_boundary_wall_time_utc.year == _EPOCH_SENTINEL.year
        ):
            raise ValueError("terminal boundary wall time must be factual")
        if (
            self.loss_terminal_boundary_kind is None
            and self.loss_terminal_boundary_wall_time_utc is not None
        ):
            raise ValueError("terminal boundary wall time requires a known boundary kind")
        _validate_origin_intent(self.observation_origin, self.uat_intent_id)
        _optional_text(self.uat_intent_id, "UAT intent ID")
        if not self.subreason_counts:
            object.__setattr__(
                self, "subreason_counts", (GapSubreasonCount(self.reason, self.count),)
            )
        _validate_subreason_counts(self.subreason_counts, self.reason, self.count)


@dataclass(frozen=True, slots=True)
class GapSubreasonCount:
    """One bounded, closed loss subreason count."""

    reason: DischargeGapReason
    count: int

    def __post_init__(self) -> None:
        _enum(self.reason, DischargeGapReason, "gap subreason")
        if (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or not 0 < self.count <= _MAX_GAP_COUNT
        ):
            raise ValueError("gap subreason count must be a positive uint64")


_LOSS_BOUNDARIES = frozenset(
    {"power_restored", "modeled_safe_shutdown", "service_stop", "boot_boundary"}
)


@dataclass(frozen=True, slots=True)
class DischargeSampleIdentity:
    """Scope and provenance used when deriving a sample's canonical hash."""

    blackout_id: str
    physical_episode_id: str
    battery_epoch_id: str
    segment_id: str
    observation_origin: ObservationOrigin
    uat_intent_id: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.blackout_id, "blackout ID"),
            (self.physical_episode_id, "physical episode ID"),
            (self.battery_epoch_id, "battery epoch ID"),
            (self.segment_id, "segment ID"),
        ):
            _text(value, name)
        _enum(self.observation_origin, ObservationOrigin, "observation origin")
        _validate_origin_intent(self.observation_origin, self.uat_intent_id)
        _optional_text(self.uat_intent_id, "UAT intent ID")


def canonical_discharge_sample_hash(
    sequence: int,
    captured: CapturedTelemetry,
    identity: DischargeSampleIdentity,
) -> str:
    """Return the stable domain hash shared by the physical sample codec."""
    _bounded_uint64(sequence, "sample sequence")
    if not isinstance(identity, DischargeSampleIdentity):
        raise TypeError("sample hash requires DischargeSampleIdentity")
    observation = captured.observation
    parts = [
        "discharge_sample-v1",
        str(sequence),
        identity.blackout_id,
        identity.physical_episode_id,
        identity.battery_epoch_id,
        identity.segment_id,
        identity.observation_origin.value,
        identity.uat_intent_id or "",
        observation.boot_id,
        str(observation.monotonic_ns),
        _wall(observation.wall_time_utc),
        observation.raw_status,
        observation.battery_voltage_raw or "",
        _number(observation.battery_voltage_v),
        _number(observation.voltage_token_quantum_v),
        _number(observation.load_percent),
        _number(observation.input_voltage_v),
    ]
    for token in captured.raw_tokens:
        parts.extend((token.key, token.token, token.wire_lexeme))
    return sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _raw_token_bytes(tokens: tuple[RawNutToken, ...]) -> bytes:
    return json.dumps(
        {
            "raw_tokens": [
                {"key": item.key, "token": item.token, "wire_lexeme": item.wire_lexeme}
                for item in tokens
            ]
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _validate_physical_observation(value: PhysicalObservation) -> None:
    _text(value.boot_id, "observation boot ID")
    _bounded_monotonic(value.monotonic_ns, "observation monotonic time")
    _utc(value.wall_time_utc)
    _text(value.raw_status, "raw status")
    for number, name in (
        (value.battery_voltage_v, "battery voltage"),
        (value.voltage_token_quantum_v, "voltage token quantum"),
        (value.load_percent, "load percent"),
        (value.input_voltage_v, "input voltage"),
    ):
        if number is not None and (isinstance(number, bool) or not isfinite(number)):
            raise ValueError(f"{name} must be finite when present")
    _optional_text(value.battery_voltage_raw, "battery voltage raw token")


def _validate_raw_observation_binding(
    observation: PhysicalObservation, tokens: tuple[RawNutToken, ...]
) -> None:
    """Prove every physical observation field came from this complete reply."""
    raw = {item.key: item.token for item in tokens}
    status_token = raw.get("ups.status")
    if status_token is None or not status_token or observation.raw_status != status_token:
        raise ValueError("typed status does not match raw NUT token")
    voltage_token = raw.get("battery.voltage")
    if observation.battery_voltage_raw != voltage_token:
        raise ValueError("battery voltage raw token does not match raw NUT reply")
    _validate_numeric_field(observation.battery_voltage_v, voltage_token, "battery voltage")
    _validate_quantum(observation.voltage_token_quantum_v, voltage_token)
    _validate_numeric_field(observation.load_percent, raw.get("ups.load"), "load")
    _validate_numeric_field(observation.input_voltage_v, raw.get("input.voltage"), "input voltage")


def _validate_numeric_field(value: float | None, token: str | None, field: str) -> None:
    parsed = _parse_finite_token(token)
    if parsed is None:
        if value is not None:
            raise ValueError(f"typed {field} must be unavailable when raw token is unavailable")
        return
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"typed {field} is missing or not numeric")
    if not isfinite(float(value)) or float(value) != parsed:
        raise ValueError(f"typed {field} does not match raw NUT token")


def _validate_quantum(value: float | None, token: str | None) -> None:
    if token is None:
        if value is not None:
            raise ValueError("typed voltage token quantum must be unavailable without a raw token")
        return
    parsed = _parse_finite_token(token)
    if parsed is None:
        if value is not None:
            raise ValueError(
                "typed voltage token quantum must be unavailable for an unusable token"
            )
        return
    quantum = _token_quantum(token)
    if value is None or quantum is None or not isfinite(float(value)) or float(value) != quantum:
        raise ValueError("typed voltage token quantum does not match raw NUT token")


def _parse_finite_token(token: str | None) -> float | None:
    if token is None:
        return None
    try:
        parsed = float(token)
    except (TypeError, ValueError):
        return None
    if not isfinite(parsed):
        return None
    return parsed


def _token_quantum(token: str) -> float | None:
    from decimal import Decimal, InvalidOperation

    try:
        decimal = Decimal(token)
    except InvalidOperation:
        return None
    if not decimal.is_finite():
        return None
    exponent = decimal.as_tuple().exponent
    if not isinstance(exponent, int):
        return None
    quantum = float(Decimal(1).scaleb(exponent))
    return quantum if isfinite(quantum) and quantum > 0 else None


def _decode_nut_wire(wire: str) -> str:
    """Decode a NUT lexeme while accepting only quote/backslash escapes."""
    decoded: list[str] = []
    index = 0
    while index < len(wire):
        char = wire[index]
        if char == '"':
            raise ValueError("NUT wire lexeme contains an unescaped quote")
        if char == "\\":
            index += 1
            if index >= len(wire) or wire[index] not in {'"', "\\"}:
                raise ValueError("NUT wire lexeme contains an unsupported escape")
            decoded.append(wire[index])
        else:
            decoded.append(char)
        index += 1
    return "".join(decoded)


def _validate_subreason_counts(
    values: tuple[GapSubreasonCount, ...], reason: DischargeGapReason, total: int
) -> None:
    if not isinstance(values, tuple):
        raise TypeError("gap subreason counts must be a tuple")
    if not values:
        values = (GapSubreasonCount(reason, total),)
    if len(values) > _MAX_GAP_SUBREASONS:
        raise ValueError("gap subreason counts exceed their bound")
    if any(not isinstance(value, GapSubreasonCount) for value in values):
        raise TypeError("gap subreason counts must contain GapSubreasonCount values")
    reasons = tuple(value.reason for value in values)
    if len(set(reasons)) != len(reasons):
        raise ValueError("gap subreason counts must be unique")
    if sum(value.count for value in values) != total:
        raise ValueError("gap subreason counts must sum exactly to total")


def _number(value: float | None) -> str:
    return "" if value is None else repr(float(value))


def _wall(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _text(value: object, field: str, *, maximum: int = _MAX_ID_BYTES) -> None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field} must be a bounded non-empty UTF-8 string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} must not contain control characters")


def _optional_text(value: object, field: str) -> None:
    if value is not None:
        _text(value, field)


def _wire_text(value: object, field: str) -> None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_TOKEN_BYTES:
        raise ValueError(f"{field} must be bounded UTF-8 text")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} must not contain control characters")


def _hash(value: object, field: str) -> None:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be lowercase SHA-256")


def _nonnegative(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")


def _bounded_uint64(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= UINT64_MAX:
        raise ValueError(f"{field} must be an unsigned 64-bit integer")


def _bounded_monotonic(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_MONOTONIC_NS:
        raise ValueError(f"{field} must be within the bounded monotonic range")


def _utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("wall time must be timezone-aware UTC")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("wall time must be UTC")
    return value


def _optional_utc(value: object) -> None:
    if value is not None:
        _utc(value)


def _enum(value: object, enum_type: type[StrEnum] | frozenset[str], field: str) -> None:
    if isinstance(enum_type, frozenset):
        if not isinstance(value, str) or value not in enum_type:
            raise ValueError(f"{field} is not closed")
    elif not isinstance(value, enum_type):
        raise TypeError(f"{field} must be {enum_type.__name__}")


def _optional_enum(value: object, enum_type: type[StrEnum], field: str) -> None:
    if value is not None:
        _enum(value, enum_type, field)


def _validate_origin_intent(origin: ObservationOrigin, intent: str | None) -> None:
    if origin is ObservationOrigin.UAT and not intent:
        raise ValueError("UAT capture requires intent ID")
    if origin is not ObservationOrigin.UAT and intent is not None:
        raise ValueError("UAT intent ID is only valid for UAT capture")


__all__ = [
    "BlackoutStart",
    "CapturedTelemetry",
    "DischargeGap",
    "DischargeGapReason",
    "DischargeSample",
    "DischargeSampleIdentity",
    "FrozenModelCapture",
    "GapSubreasonCount",
    "RawNutToken",
    "canonical_discharge_sample_hash",
]
