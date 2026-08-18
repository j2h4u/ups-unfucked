"""Strict canonical envelope bytes for the fresh JSONL v3 authority."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 3
MAX_LINE_BYTES = 128 * 1024
MAX_ID_BYTES = 128
MAX_SEQ = 3_197
MAX_MONOTONIC_NS = 2**63 - 1
HASH_HEX_LENGTH = 64
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_UTC_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z\Z")
_ENVELOPE_FIELDS = frozenset(
    {
        "schema_version",
        "record_type",
        "provenance",
        "blackout_id",
        "segment_id",
        "seq",
        "boot_id",
        "wall_time_utc",
        "monotonic_ns",
        "prev_record_sha256",
        "payload",
        "record_sha256",
    }
)


class V3CodecError(ValueError):
    """A v3 envelope or payload cannot be represented canonically."""


@dataclass(frozen=True, slots=True)
class V3RecordEnvelope:
    """The exact v3 record envelope before or after hash derivation."""

    schema_version: int
    record_type: str
    provenance: str
    blackout_id: str
    segment_id: str
    seq: int
    boot_id: str
    wall_time_utc: str
    monotonic_ns: int
    prev_record_sha256: str | None
    payload: Mapping[str, Any]
    record_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise V3CodecError("schema_version must be exactly 3")
        for value, name in (
            (self.record_type, "record_type"),
            (self.provenance, "provenance"),
            (self.blackout_id, "blackout_id"),
            (self.segment_id, "segment_id"),
            (self.boot_id, "boot_id"),
            (self.wall_time_utc, "wall_time_utc"),
        ):
            if not isinstance(value, str) or not value:
                raise V3CodecError(f"{name} must be a non-empty string")
            try:
                encoded = value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise V3CodecError(f"{name} must be valid UTF-8") from exc
            if len(encoded) > MAX_ID_BYTES or any(ord(char) < 0x20 for char in value):
                raise V3CodecError(f"{name} exceeds the bounded text contract")
        _validate_utc(self.wall_time_utc)
        if (
            isinstance(self.seq, bool)
            or not isinstance(self.seq, int)
            or not 0 <= self.seq <= MAX_SEQ
        ):
            raise V3CodecError("seq must be within the bounded v3 sequence range")
        if (
            isinstance(self.monotonic_ns, bool)
            or not isinstance(self.monotonic_ns, int)
            or not 0 <= self.monotonic_ns <= MAX_MONOTONIC_NS
        ):
            raise V3CodecError("monotonic_ns must be within the bounded v3 range")
        _validate_optional_hash(self.prev_record_sha256, "prev_record_sha256")
        _validate_optional_hash(self.record_sha256, "record_sha256")
        if not isinstance(self.payload, Mapping):
            raise V3CodecError("payload must be an object")
        _validate_json_value(self.payload)


@dataclass(frozen=True, slots=True)
class EncodedV3Record:
    """Canonical bytes and the hash-complete envelope for one JSONL line."""

    envelope: V3RecordEnvelope
    canonical_bytes: bytes
    line: bytes

    @property
    def record_sha256(self) -> str:
        """Return the hash stored in the complete envelope."""
        if self.envelope.record_sha256 is None:
            raise V3CodecError("encoded envelope is missing record_sha256")
        return self.envelope.record_sha256


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Encode one JSON object with the v3 canonical JSON rules."""
    if not isinstance(value, Mapping):
        raise V3CodecError("canonical JSON root must be an object")
    _validate_json_value(value)
    try:
        return json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise V3CodecError("value is not canonical JSON") from exc


def encode_v3_record(envelope: V3RecordEnvelope) -> EncodedV3Record:
    """Hash and encode one v3 envelope; the newline is excluded from the hash."""
    without_hash = _envelope_mapping(envelope, include_hash=False)
    canonical_without_hash = canonical_json_bytes(without_hash)
    digest = hashlib.sha256(canonical_without_hash).hexdigest()
    if envelope.record_sha256 is not None and envelope.record_sha256 != digest:
        raise V3CodecError("record_sha256 does not match canonical envelope")
    complete = dict(without_hash)
    complete["record_sha256"] = digest
    canonical = canonical_json_bytes(complete)
    line = canonical + b"\n"
    if len(line) > MAX_LINE_BYTES:
        raise V3CodecError("v3 record line exceeds 128 KiB")
    complete_envelope = V3RecordEnvelope(**complete)
    return EncodedV3Record(complete_envelope, canonical, line)


def canonical_v3_line_size(envelope: V3RecordEnvelope) -> int:
    """Return the exact canonical JSONL size using a fixed 64-char hash."""
    value = _envelope_mapping(envelope, include_hash=True)
    value["record_sha256"] = "0" * HASH_HEX_LENGTH
    return len(canonical_json_bytes(value)) + 1


def decode_v3_record(line: bytes) -> EncodedV3Record:
    """Strictly decode one canonical v3 line and verify its complete hash."""
    if not isinstance(line, bytes) or not line.endswith(b"\n"):
        raise V3CodecError("v3 record line must be newline terminated bytes")
    if len(line) > MAX_LINE_BYTES:
        raise V3CodecError("v3 record line exceeds 128 KiB")
    value = _strict_loads(line[:-1])
    if not isinstance(value, dict) or set(value) != _ENVELOPE_FIELDS:
        raise V3CodecError("v3 envelope fields are not exact")
    encoded = _encoded_from_mapping(value, line)
    if encoded.line != line:
        raise V3CodecError("v3 record line is not canonical JSON")
    return encoded


def decode_v3_envelope(line: bytes) -> V3RecordEnvelope:
    """Decode one line and return its verified hash-complete envelope."""
    return decode_v3_record(line).envelope


def _encoded_from_mapping(value: dict[str, Any], line: bytes) -> EncodedV3Record:
    digest = value["record_sha256"]
    if not isinstance(digest, str) or _HASH_RE.fullmatch(digest) is None:
        raise V3CodecError("record_sha256 must be lowercase SHA-256 hex")
    without_hash = dict(value)
    del without_hash["record_sha256"]
    envelope = V3RecordEnvelope(**without_hash)
    encoded = encode_v3_record(envelope)
    if encoded.record_sha256 != digest:
        raise V3CodecError("record_sha256 does not match canonical envelope")
    if encoded.line != line:
        raise V3CodecError("v3 record line is not canonical JSON")
    return encoded


def _envelope_mapping(envelope: V3RecordEnvelope, *, include_hash: bool) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": envelope.schema_version,
        "record_type": envelope.record_type,
        "provenance": envelope.provenance,
        "blackout_id": envelope.blackout_id,
        "segment_id": envelope.segment_id,
        "seq": envelope.seq,
        "boot_id": envelope.boot_id,
        "wall_time_utc": envelope.wall_time_utc,
        "monotonic_ns": envelope.monotonic_ns,
        "prev_record_sha256": envelope.prev_record_sha256,
        "payload": dict(envelope.payload),
    }
    if include_hash:
        value["record_sha256"] = envelope.record_sha256
    return value


def _strict_loads(data: bytes) -> Any:
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, V3CodecError) as exc:
        raise V3CodecError("v3 record is not strict JSON") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise V3CodecError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise V3CodecError(f"non-finite JSON constant is forbidden: {value}")


def _validate_json_value(value: Any) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise V3CodecError("non-finite floats are forbidden")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise V3CodecError("JSON object keys must be strings")
            _validate_json_value(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_value(item)
        return
    raise V3CodecError(f"unsupported JSON value type: {type(value).__name__}")


def _validate_optional_hash(value: str | None, name: str) -> None:
    if value is not None and _HASH_RE.fullmatch(value) is None:
        raise V3CodecError(f"{name} must be lowercase SHA-256 hex or null")


def _validate_utc(value: str) -> None:
    if _UTC_RE.fullmatch(value) is None:
        raise V3CodecError("wall_time_utc must be canonical UTC Z text")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise V3CodecError("wall_time_utc is not a valid UTC timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise V3CodecError("wall_time_utc must be UTC")
