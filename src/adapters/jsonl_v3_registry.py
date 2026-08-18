"""Strict, typed private work-registry codec and transaction-scoped CAS."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from src.adapters.jsonl_v3_errors import (
    V3AppendConflict,
    V3CorruptionError,
    V3FileNotFound,
    V3PathError,
    V3PersistenceError,
    V3ValidationError,
)
from src.adapters.jsonl_v3_filesystem import (
    JsonlV3Filesystem,
    V3FileSnapshot,
    V3WriteTransaction,
)
from src.adapters.jsonl_v3_registry_codec import state_wire, token_parse
from src.adapters.jsonl_v3_registry_values import (
    MAX_RECOVERY_PAGE_SIZE,
    MAX_REGISTRY_BYTES,
    REGISTRY_SCHEMA,
    CaptureState,
    CapturingState,
    PendingState,
    PreparingCaptureState,
    ProcessingState,
    TailState,
    V3AppendIntent,
    V3DamageContinuation,
    V3LastAppend,
    V3RolloverReservation,
    V3SealIntent,
    V3StorageSegmentReceipt,
    V3TailBuildIntent,
    V3TailRecordReceipt,
    validate_registry,
)
from src.adapters.jsonl_v3_storage_paths import (
    V3RegistryToken,
)
from src.application.blackout_storage_values import BlackoutCaptureCursor, BlackoutChainKind


@dataclass(frozen=True, slots=True)
class V3WorkRegistry:
    capture: CaptureState | None
    pending: tuple[PendingState, ...]

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> "V3WorkRegistry":
        try:
            _exact(value, {"capture", "pending", "schema"})
            if value["schema"] != REGISTRY_SCHEMA or not isinstance(value["pending"], list):
                raise V3ValidationError("registry schema is invalid")
            capture = None if value["capture"] is None else _capture_from_wire(value["capture"])
            pending = tuple(_pending_from_wire(item) for item in value["pending"])
            state = cls(capture, pending)
            validate_registry(state)
            return state
        except (KeyError, TypeError, ValueError, V3PathError) as exc:
            raise V3ValidationError("registry fields have invalid types") from exc

    def to_wire(self) -> dict[str, Any]:
        value = {
            "capture": None if self.capture is None else state_wire(self.capture),
            "pending": [state_wire(item) for item in self.pending],
            "schema": REGISTRY_SCHEMA,
        }
        validate_registry(self)
        return value


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    state: V3WorkRegistry
    byte_length: int
    canonical_sha256: str


@dataclass(frozen=True, slots=True)
class V3RecoveryCursor:
    processing_offset: int
    active_capture_emitted: bool


@dataclass(frozen=True, slots=True)
class V3RecoveryPage:
    active_capture: CapturingState | None
    pending: tuple[PendingState, ...]
    next_cursor: V3RecoveryCursor | None
    complete: bool


def empty_registry() -> dict[str, Any]:
    return {"capture": None, "pending": [], "schema": REGISTRY_SCHEMA}


def canonical_registry_bytes(value: Mapping[str, Any]) -> bytes:
    state = V3WorkRegistry.from_wire(value)
    raw = (
        json.dumps(
            state.to_wire(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
        + b"\n"
    )
    if len(raw) > MAX_REGISTRY_BYTES:
        raise V3ValidationError("registry exceeds its 256 KiB bound")
    return raw


def decode_registry(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or len(raw) > MAX_REGISTRY_BYTES or not raw.endswith(b"\n"):
        raise V3CorruptionError("registry bytes are not bounded canonical JSON")
    try:
        value = json.loads(
            raw[:-1].decode(), object_pairs_hook=_unique_object, parse_constant=_reject
        )
        if not isinstance(value, dict) or canonical_registry_bytes(value) != raw:
            raise V3CorruptionError("registry JSON is not canonical")
        return value
    except (UnicodeDecodeError, json.JSONDecodeError, V3ValidationError) as exc:
        raise V3CorruptionError("registry JSON is invalid") from exc


def _capture_from_wire(value: Any) -> CaptureState:
    _exact(
        value,
        {
            "tag",
            *(
                _PREPARING_FIELDS
                if isinstance(value, dict) and value.get("tag") == "preparing"
                else _CAPTURING_FIELDS
            ),
        },
    )
    if value.get("tag") == "preparing":
        return PreparingCaptureState(
            **{
                **value,
                "path_token": token_parse(value["path_token"], "segment"),
                "offset_token": token_parse(value["offset_token"], "offset"),
            }
        )
    if value.get("tag") != "capturing":
        raise V3ValidationError("capture tag is not closed")
    return _construct_capturing(value)


def _construct_capturing(value: dict[str, Any]) -> CapturingState:
    converted = dict(value)
    converted["physical_cursor"] = _cursor_from_wire(converted["physical_cursor"])
    converted["terminal_cursor"] = (
        None
        if converted["terminal_cursor"] is None
        else _cursor_from_wire(converted["terminal_cursor"])
    )
    converted["storage_segments"] = tuple(
        _segment_from_wire(item) for item in converted["storage_segments"]
    )
    converted["append_intent"] = _intent_from_wire(converted["append_intent"])
    converted["last_append"] = _last_from_wire(converted["last_append"])
    converted["damage_continuation"] = _damage_from_wire(converted["damage_continuation"])
    converted["rollover"] = _rollover_from_wire(converted["rollover"])
    return CapturingState(**converted)


def _pending_from_wire(value: Any) -> PendingState:
    if not isinstance(value, dict) or value.get("tag") not in {"processing", "tail"}:
        raise V3ValidationError("pending variant is invalid")
    _exact(
        value, {"tag", *(_PROCESSING_FIELDS if value.get("tag") == "processing" else _TAIL_FIELDS)}
    )
    converted = dict(value)
    for name in ("physical_cursor", "terminal_cursor_after_end", "terminal_cursor_after_outcome"):
        if name in converted:
            converted[name] = _cursor_from_wire(converted[name])
    converted["storage_segments"] = tuple(
        _segment_from_wire(item) for item in converted["storage_segments"]
    )
    if value["tag"] == "processing":
        converted["tail_build_intent"] = _tail_build_from_wire(converted["tail_build_intent"])
        return ProcessingState(**converted)
    converted["terminal_cursor_after_outcome"] = _cursor_from_wire(
        value["terminal_cursor_after_outcome"]
    )
    converted["tail_path_token"] = token_parse(value["tail_path_token"], "tail")
    converted["tail_records"] = tuple(
        V3TailRecordReceipt(**_nested(item, V3TailRecordReceipt)) for item in value["tail_records"]
    )
    converted["seal_intent"] = _seal_from_wire(converted["seal_intent"])
    return TailState(**converted)


def _cursor_from_wire(value: Any) -> BlackoutCaptureCursor:
    if not isinstance(value, dict):
        raise V3ValidationError("cursor is not an object")
    _exact(value, {"blackout_id", "segment_id", "chain", "next_sequence", "last_record_sha256"})
    converted = dict(value)
    try:
        converted["chain"] = BlackoutChainKind(converted["chain"])
    except (TypeError, ValueError) as exc:
        raise V3ValidationError("cursor chain is invalid") from exc
    return BlackoutCaptureCursor(**converted)


def _segment_from_wire(value: Any) -> V3StorageSegmentReceipt:
    if not isinstance(value, dict):
        raise V3ValidationError("segment receipt is not an object")
    _exact(value, {field.name for field in fields(V3StorageSegmentReceipt)})
    result = dict(value)
    damaged = isinstance(value.get("path_token"), str) and value["path_token"].startswith("v3dam1:")
    result["path_token"] = token_parse(
        value["path_token"], "damaged_segment" if damaged else "segment"
    )
    result["offset_token"] = token_parse(
        value["offset_token"], "damaged_offset" if damaged else "offset"
    )
    return V3StorageSegmentReceipt(**result)


def _intent_from_wire(value: Any) -> V3AppendIntent | None:
    return None if value is None else V3AppendIntent(**_nested(value, V3AppendIntent))


def _last_from_wire(value: Any) -> V3LastAppend | None:
    return None if value is None else V3LastAppend(**_nested(value, V3LastAppend))


def _tail_build_from_wire(value: Any) -> V3TailBuildIntent | None:
    if value is None:
        return None
    result = _nested(value, V3TailBuildIntent)
    result["tail_path_token"] = token_parse(result["tail_path_token"], "tail")
    result["expected_terminal_cursor"] = _cursor_from_wire(result["expected_terminal_cursor"])
    return V3TailBuildIntent(**result)


def _seal_from_wire(value: Any) -> V3SealIntent | None:
    return None if value is None else V3SealIntent(**_nested(value, V3SealIntent))


def _damage_from_wire(value: Any) -> V3DamageContinuation | None:
    if value is None:
        return None
    result = _nested(value, V3DamageContinuation)
    for field_name, kind in (
        ("old_path_token", "segment"),
        ("old_offset_token", "offset"),
        ("damaged_path_token", "damaged_segment"),
        ("damaged_offset_token", "damaged_offset"),
    ):
        result[field_name] = token_parse(result[field_name], kind)
    return V3DamageContinuation(**result)


def _rollover_from_wire(value: Any) -> V3RolloverReservation | None:
    if value is None:
        return None
    result = _nested(value, V3RolloverReservation)
    for field_name in ("old_path_token", "successor_path_token"):
        result[field_name] = token_parse(result[field_name], "segment")
    return V3RolloverReservation(**result)


_PREPARING_FIELDS = frozenset(f.name for f in fields(PreparingCaptureState))
_CAPTURING_FIELDS = frozenset(f.name for f in fields(CapturingState))
_PROCESSING_FIELDS = frozenset(f.name for f in fields(ProcessingState))
_TAIL_FIELDS = frozenset(f.name for f in fields(TailState))


def _exact(value: Any, expected: set[str] | frozenset[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise V3ValidationError("registry fields are not exact")


def _nested(value: Any, cls: type[Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise V3ValidationError("nested registry value is not an object")
    _exact(value, {field.name for field in fields(cls)})
    return dict(value)


def recovery_page(
    registry: V3WorkRegistry,
    *,
    processing_offset: int,
    limit: int,
    active_capture_emitted: bool = False,
) -> V3RecoveryPage:
    if (
        type(limit) is not int
        or not 1 <= limit <= MAX_RECOVERY_PAGE_SIZE
        or type(processing_offset) is not int
        or processing_offset < 0
        or processing_offset > len(registry.pending)
    ):
        raise V3ValidationError("recovery page bounds are invalid")
    active = (
        registry.capture
        if isinstance(registry.capture, CapturingState)
        and not active_capture_emitted
        and processing_offset == 0
        else None
    )
    available = limit - (1 if active is not None else 0)
    items = registry.pending[processing_offset : processing_offset + available]
    consumed = processing_offset + len(items)
    complete = consumed >= len(registry.pending)
    return V3RecoveryPage(
        active,
        items,
        None if complete else V3RecoveryCursor(consumed, active is not None),
        complete,
    )


class JsonlV3WorkRegistry:
    def __init__(self, filesystem: JsonlV3Filesystem) -> None:
        self.filesystem = filesystem

    def _check_tx(self, tx: V3WriteTransaction) -> None:
        if not isinstance(tx, V3WriteTransaction):
            raise V3PersistenceError("foreign registry transaction")
        tx.assert_owner(self.filesystem)

    def _snapshot(self, tx: V3WriteTransaction) -> RegistrySnapshot:
        raw, file_snapshot = tx.read_bounded(
            V3RegistryToken.WORK_REGISTRY, max_bytes=MAX_REGISTRY_BYTES
        )
        state = V3WorkRegistry.from_wire(decode_registry(raw))
        return RegistrySnapshot(state, file_snapshot.byte_length, file_snapshot.content_sha256)

    def open_or_create(self, tx: V3WriteTransaction) -> RegistrySnapshot:
        self._check_tx(tx)
        try:
            return self._snapshot(tx)
        except V3FileNotFound:
            raw = canonical_registry_bytes(empty_registry())
            snap = tx.replace_bounded(
                V3RegistryToken.WORK_REGISTRY,
                expected=None,
                contents=raw,
                max_bytes=MAX_REGISTRY_BYTES,
            )
            return RegistrySnapshot(
                V3WorkRegistry.from_wire(empty_registry()), snap.byte_length, snap.content_sha256
            )

    def read(self, tx: V3WriteTransaction) -> RegistrySnapshot:
        self._check_tx(tx)
        return self._snapshot(tx)

    def compare_and_replace(
        self, tx: V3WriteTransaction, *, expected: RegistrySnapshot, replacement: V3WorkRegistry
    ) -> RegistrySnapshot:
        self._check_tx(tx)
        current = self._snapshot(tx)
        if current != expected:
            raise V3AppendConflict("registry snapshot differs from expected")
        raw = canonical_registry_bytes(replacement.to_wire())
        snap = tx.replace_bounded(
            V3RegistryToken.WORK_REGISTRY,
            expected=V3FileSnapshot(expected.byte_length, expected.canonical_sha256),
            contents=raw,
            max_bytes=MAX_REGISTRY_BYTES,
        )
        return RegistrySnapshot(replacement, snap.byte_length, snap.content_sha256)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise V3ValidationError("duplicate registry key")
        result[key] = value
    return result


def _reject(value: str) -> None:
    raise V3ValidationError(f"non-finite constant: {value}")
