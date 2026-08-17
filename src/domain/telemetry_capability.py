"""Pure identity signatures for status-scoped telemetry capabilities."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256

_NUMERIC_TOKEN = re.compile(
    r"^(?P<sign>[+-]?)(?:(?P<int>\d+)(?:\.(?P<fraction>\d*))?|\.(?P<leading_fraction>\d+))"
    r"(?:[eE](?P<exponent>[+-]?\d+))?$"
)
CAPABILITY_IDENTITY_POLICY_REVISION = "telemetry-capability-identity-v1"


class CapabilitySignatureError(ValueError):
    """A telemetry reply cannot produce a trustworthy stable signature."""


@dataclass(frozen=True, slots=True)
class TokenShape:
    """Stable lexical and numeric-quantisation shape of one raw token."""

    parsed_type: str
    lexical_form: str
    sign_form: str | None
    decimal_places: int | None
    quantization_exponent: int | None
    exponent: int | None
    string_value_sha256: str | None


@dataclass(frozen=True, slots=True)
class CapabilityReply:
    """Immutable reply retaining exact raw tokens and validated parsed values."""

    status: str
    tokens: tuple[tuple[str, str], ...]
    values: tuple[tuple[str, float | str], ...]

    def token_map(self) -> dict[str, str]:
        """Return a defensive token mapping for adapter-side serialization."""
        return dict(self.tokens)


@dataclass(frozen=True, slots=True)
class FieldSignature:
    """Stable presence/type/token-shape signature without observed values."""

    present_count: int
    missing_count: int
    presence_mode: str
    parsed_types: tuple[str, ...]
    token_shapes: tuple[TokenShape, ...]


@dataclass(frozen=True, slots=True)
class StateSignature:
    """Signature for every field observed or absent in one status state."""

    status: str
    reply_count: int
    fields: tuple[tuple[str, FieldSignature], ...]


def token_shape(token: str) -> TokenShape:
    """Classify one exact NUT token without retaining its dynamic value."""
    if not isinstance(token, str) or "\n" in token or "\r" in token:
        raise CapabilitySignatureError("raw token is invalid")
    match = _NUMERIC_TOKEN.fullmatch(token)
    if match is None:
        return TokenShape(
            "string",
            "text",
            None,
            None,
            None,
            None,
            sha256(token.encode("utf-8")).hexdigest(),
        )
    fraction = match.group("fraction")
    leading_fraction = match.group("leading_fraction")
    decimal_places = len(fraction if fraction is not None else leading_fraction or "")
    exponent_text = match.group("exponent")
    exponent = int(exponent_text) if exponent_text is not None else None
    quantization_exponent = (exponent or 0) - decimal_places
    sign = match.group("sign") or None
    sign_form = {None: "implicit", "+": "explicit_positive", "-": "negative"}[sign]
    lexical_form = "scientific" if exponent_text is not None else "decimal"
    return TokenShape(
        "number",
        lexical_form,
        sign_form,
        decimal_places,
        quantization_exponent,
        exponent,
        None,
    )


def make_capability_reply(
    status: str,
    values: Mapping[str, float | str],
    tokens: Mapping[str, str],
) -> CapabilityReply:
    """Freeze one reply after proving parsed values agree with exact tokens."""
    if not isinstance(status, str) or not status:
        raise CapabilitySignatureError("status is invalid")
    if set(values) != set(tokens) or not tokens:
        raise CapabilitySignatureError("reply values and tokens do not cover the same fields")
    if "ups.status" in tokens and tokens["ups.status"] != status:
        raise CapabilitySignatureError("status disagrees with raw token: ups.status")
    frozen_values: list[tuple[str, float | str]] = []
    frozen_tokens: list[tuple[str, str]] = []
    for key in sorted(tokens):
        token = tokens[key]
        value = values[key]
        shape = token_shape(token)
        if shape.parsed_type == "number":
            _validate_numeric_value(key, token, value)
        elif not isinstance(value, str) or value != token:
            raise CapabilitySignatureError(f"parsed value disagrees with raw token: {key}")
        frozen_values.append((key, value))
        frozen_tokens.append((key, token))
    return CapabilityReply(status, tuple(frozen_tokens), tuple(frozen_values))


def _validate_numeric_value(key: str, token: str, value: float | str) -> None:
    try:
        expected = float(token)
    except ValueError as exc:
        raise CapabilitySignatureError(f"numeric token cannot be parsed: {key}") from exc
    if not math.isfinite(expected):
        raise CapabilitySignatureError(f"numeric token is not finite: {key}")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CapabilitySignatureError(f"parsed value disagrees with raw token: {key}")
    if not math.isfinite(float(value)) or float(value) != expected:
        raise CapabilitySignatureError(f"parsed value disagrees with raw token: {key}")


def build_state_signatures(replies: Sequence[CapabilityReply]) -> tuple[StateSignature, ...]:
    """Build stable signatures while retaining no dynamic token value sets."""
    if not replies:
        raise CapabilitySignatureError("at least one reply is required")
    statuses = tuple(sorted({reply.status for reply in replies}))
    all_keys = tuple(sorted({key for reply in replies for key, _ in reply.tokens}))
    return tuple(
        _state_signature(
            status, tuple(reply for reply in replies if reply.status == status), all_keys
        )
        for status in statuses
    )


def _state_signature(
    status: str,
    replies: tuple[CapabilityReply, ...],
    all_keys: tuple[str, ...],
) -> StateSignature:
    fields = tuple((key, _field_signature(replies, key)) for key in all_keys)
    return StateSignature(status, len(replies), fields)


def _field_signature(replies: tuple[CapabilityReply, ...], key: str) -> FieldSignature:
    token_maps = [reply.token_map() for reply in replies]
    present = [mapping[key] for mapping in token_maps if key in mapping]
    observed_shapes = [token_shape(token) for token in present]
    shapes = tuple(
        sorted(
            {token_shape_sort_key(shape): shape for shape in observed_shapes}.values(),
            key=token_shape_sort_key,
        )
    )
    parsed_types = tuple(sorted({shape.parsed_type for shape in shapes}))
    present_count = len(present)
    missing_count = len(replies) - present_count
    presence_mode = field_presence_mode(present_count, len(replies))
    return FieldSignature(
        present_count,
        missing_count,
        presence_mode,
        parsed_types,
        shapes,
    )


def field_presence_mode(present_count: int, reply_count: int) -> str:
    """Classify field presence for one complete state-scoped reply window."""
    if present_count < 0 or reply_count <= 0 or present_count > reply_count:
        raise CapabilitySignatureError("field presence counts are invalid")
    if present_count == 0:
        return "absent"
    if present_count == reply_count:
        return "always_present"
    return "intermittent"


def token_shape_sort_key(shape: TokenShape) -> tuple[object, ...]:
    """Return the canonical ordering key for token shapes."""
    return (
        shape.parsed_type,
        shape.lexical_form,
        shape.sign_form or "",
        shape.decimal_places if shape.decimal_places is not None else -1,
        shape.quantization_exponent if shape.quantization_exponent is not None else -1,
        shape.exponent if shape.exponent is not None else 0,
        shape.string_value_sha256 or "",
    )
