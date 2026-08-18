"""Frozen semantic values and validation for the private v3 work registry."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from src.adapters.jsonl_v3_errors import V3ValidationError
from src.adapters.jsonl_v3_storage_paths import (
    V3DamagedOffsetPathToken,
    V3DamagedSegmentPathToken,
    V3OffsetPathToken,
    V3SegmentPathToken,
    V3TerminalStagingToken,
    validate_uuid4_hex,
)
from src.application.blackout_storage_values import BlackoutCaptureCursor, BlackoutChainKind

REGISTRY_SCHEMA = "v3-blackout-work-registry-v1"
MAX_REGISTRY_BYTES = 256 * 1024
MAX_CAPTURE_BYTES = 64 * 1024 * 1024
MAX_PENDING_ENTRIES = 8
MAX_RECOVERY_PAGE_SIZE = 32
MAX_STORAGE_SEGMENTS = 64
MAX_CHAIN_SEQUENCE = 3_197
MAX_UINT64 = (1 << 64) - 1
HASH_RE = re.compile(r"\A[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class V3StorageSegmentReceipt:
    ordinal: int
    storage_id: str
    path_token: V3SegmentPathToken | V3DamagedSegmentPathToken
    offset_token: V3OffsetPathToken | V3DamagedOffsetPathToken
    trusted_bytes: int
    first_seq: int
    last_seq: int
    last_record_sha256: str
    damaged_file_sha256: str | None
    terminal_only: bool


@dataclass(frozen=True, slots=True)
class V3AppendIntent:
    chain: Literal["physical", "terminal"]
    operation: str
    expected_seq: int
    expected_previous_hash: str | None
    storage_ordinal: int | None
    file_offset: int
    line_utf8: str
    line_sha256: str
    line_length: int
    expected_cursor_sha256: str


@dataclass(frozen=True, slots=True)
class V3LastAppend:
    operation: str
    prior_cursor_sha256: str
    line_sha256: str
    resulting_cursor_sha256: str


@dataclass(frozen=True, slots=True)
class V3TailBuildIntent:
    tail_path_token: V3TerminalStagingToken
    expected_terminal_cursor: BlackoutCaptureCursor
    batch_sha256: str
    encoded_length: int
    encoded_sha256: str


@dataclass(frozen=True, slots=True)
class V3TailRecordReceipt:
    offset: int
    length: int
    hash: str
    type: str


@dataclass(frozen=True, slots=True)
class V3SealIntent:
    phase: Literal["reserved", "files_sealed", "locator_durable", "catalog_durable"]
    locator_seq: int
    catalog_offset: int
    locator_sha256: str | None
    catalog_line_sha256: str | None


@dataclass(frozen=True, slots=True)
class V3DamageContinuation:
    phase: Literal["reserved", "old_renamed", "successor_created", "gap_durable"]
    blackout_id: str
    logical_segment_id: str
    old_storage_id: str
    new_storage_id: str
    old_ordinal: int
    new_ordinal: int
    old_path_token: V3SegmentPathToken
    old_offset_token: V3OffsetPathToken
    damaged_path_token: V3DamagedSegmentPathToken
    damaged_offset_token: V3DamagedOffsetPathToken
    trusted_bytes: int
    trusted_last_seq: int
    trusted_last_record_sha256: str
    damaged_file_sha256: str
    gap_line_utf8: str
    gap_sha256: str
    gap_length: int
    new_physical_next_seq: int
    new_physical_last_record_sha256: str


@dataclass(frozen=True, slots=True)
class V3RolloverReservation:
    phase: Literal["reserved", "successor_started", "carrier_ended"]
    budget_kind: Literal["bytes", "segment_refs"]
    old_blackout_id: str
    old_logical_segment_id: str
    physical_episode_id: str
    old_storage_id: str
    old_path_token: V3SegmentPathToken
    successor_blackout_id: str
    successor_logical_segment_id: str
    successor_storage_id: str
    successor_path_token: V3SegmentPathToken
    successor_start_line_utf8: str
    carrier_end_line_utf8: str
    continuation_kind: Literal["size_rollover"]
    successor_start_sha256: str
    carrier_end_sha256: str
    successor_start_length: int
    carrier_end_length: int


@dataclass(frozen=True, slots=True)
class PreparingCaptureState:
    tag: Literal["preparing"]
    blackout_id: str
    logical_segment_id: str
    storage_id: str
    path_token: V3SegmentPathToken
    offset_token: V3OffsetPathToken
    start_line_utf8: str
    start_sha256: str
    start_length: int
    started_utc: str
    frozen_policy_revision: str


@dataclass(frozen=True, slots=True)
class CapturingState:
    tag: Literal["capturing"]
    blackout_id: str
    logical_segment_id: str
    physical_episode_id: str
    battery_epoch_id: str
    observation_origin: str
    uat_intent_id: str | None
    frozen_policy_revision: str
    physical_cursor: BlackoutCaptureCursor
    terminal_cursor: BlackoutCaptureCursor | None
    capture_bytes: int
    capture_record_count: int
    sample_count: int
    gap_count: int
    storage_segments: tuple[V3StorageSegmentReceipt, ...]
    append_intent: V3AppendIntent | None
    last_append: V3LastAppend | None
    damage_continuation: V3DamageContinuation | None
    rollover: V3RolloverReservation | None


@dataclass(frozen=True, slots=True)
class ProcessingState:
    tag: Literal["processing"]
    blackout_id: str
    logical_segment_id: str
    physical_episode_id: str
    battery_epoch_id: str
    observation_origin: str
    uat_intent_id: str | None
    frozen_policy_revision: str
    physical_cursor: BlackoutCaptureCursor
    terminal_cursor_after_end: BlackoutCaptureCursor
    terminal_root_sha256: str
    terminal_closing_anchor_sha256: str | None
    terminal_end_sha256: str
    capture_bytes: int
    capture_record_count: int
    sample_count: int
    gap_count: int
    storage_segments: tuple[V3StorageSegmentReceipt, ...]
    tail_build_intent: V3TailBuildIntent | None


@dataclass(frozen=True, slots=True)
class TailState:
    tag: Literal["tail"]
    blackout_id: str
    logical_segment_id: str
    physical_episode_id: str
    battery_epoch_id: str
    observation_origin: str
    uat_intent_id: str | None
    frozen_policy_revision: str
    physical_cursor: BlackoutCaptureCursor
    terminal_cursor_after_outcome: BlackoutCaptureCursor
    terminal_root_sha256: str
    terminal_closing_anchor_sha256: str | None
    terminal_end_sha256: str
    terminal_outcome_sha256: str
    capture_bytes: int
    capture_record_count: int
    sample_count: int
    gap_count: int
    storage_segments: tuple[V3StorageSegmentReceipt, ...]
    tail_path_token: V3TerminalStagingToken
    tail_length: int
    tail_sha256: str
    tail_records: tuple[V3TailRecordReceipt, ...]
    seal_intent: V3SealIntent | None


CaptureState: TypeAlias = PreparingCaptureState | CapturingState
PendingState: TypeAlias = ProcessingState | TailState


def validate_registry(state: Any) -> None:
    if len(state.pending) > MAX_PENDING_ENTRIES:
        raise V3ValidationError("pending entries exceed eight")
    identifiers: set[tuple[str, str]] = set()
    for item in state.pending:
        identity = (item.blackout_id, item.logical_segment_id)
        if identity in identifiers:
            raise V3ValidationError("duplicate pending aggregate")
        identifiers.add(identity)
        _validate_state(item)
    if state.capture is not None:
        _validate_state(state.capture)
        if (
            isinstance(state.capture, (PreparingCaptureState, CapturingState))
            and (state.capture.blackout_id, state.capture.logical_segment_id) in identifiers
        ):
            raise V3ValidationError("active capture duplicates pending aggregate")


def _validate_state(state: object) -> None:
    if isinstance(state, PreparingCaptureState):
        _validate_preparing(state)
        return
    if isinstance(state, (CapturingState, ProcessingState, TailState)):
        _validate_capture_common(state)
        if isinstance(state, CapturingState):
            _validate_capturing(state)
        elif isinstance(state, ProcessingState):
            _validate_processing(state)
        else:
            _validate_tail(state)
        return
    raise V3ValidationError("unknown registry state variant")


def _validate_preparing(state: PreparingCaptureState) -> None:
    _bind_token(
        state.path_token,
        state.blackout_id,
        state.logical_segment_id,
        state.storage_id,
        state.offset_token.ordinal,
    )
    _bind_token(
        state.offset_token,
        state.blackout_id,
        state.logical_segment_id,
        state.storage_id,
        state.path_token.ordinal,
    )
    _line(state.start_line_utf8, state.start_length, state.start_sha256)
    if state.path_token.started_utc != state.started_utc:
        raise V3ValidationError("preparing UTC does not match active token")
    if state.path_token.started_utc != state.offset_token.started_utc:
        raise V3ValidationError("preparing segment and offset UTC values differ")


def _validate_capturing(state: CapturingState) -> None:
    if state.damage_continuation is not None:
        _validate_damage(state.damage_continuation, state)
    if state.rollover is not None:
        _validate_rollover(state.rollover, state)
    if state.append_intent is not None:
        _validate_append_intent(state.append_intent, state)
    if state.last_append is not None:
        _validate_last_append(state.last_append)
    if (
        state.terminal_cursor is not None
        and state.terminal_cursor.chain is not BlackoutChainKind.TERMINAL
    ):
        raise V3ValidationError("terminal cursor chain is invalid")
    if state.terminal_cursor is not None:
        _cursor(state.terminal_cursor)
        _cursor_scope(
            state.terminal_cursor,
            state.blackout_id,
            state.logical_segment_id,
            BlackoutChainKind.TERMINAL,
        )
    if state.append_intent is not None and state.append_intent.chain == "terminal":
        if (
            state.terminal_cursor is None
            or state.append_intent.expected_seq != state.terminal_cursor.next_sequence
        ):
            raise V3ValidationError("terminal append intent is not tied to cursor")
        if state.append_intent.expected_previous_hash != state.terminal_cursor.last_record_sha256:
            raise V3ValidationError("terminal append intent hash is not tied to cursor")


def _validate_processing(state: ProcessingState) -> None:
    if state.tail_build_intent is not None:
        _validate_tail_build(state.tail_build_intent, state)
    _hash(state.terminal_root_sha256)
    _hash(state.terminal_end_sha256)
    _cursor(state.terminal_cursor_after_end)
    _cursor_scope(
        state.terminal_cursor_after_end,
        state.blackout_id,
        state.logical_segment_id,
        BlackoutChainKind.TERMINAL,
    )
    if state.terminal_cursor_after_end.last_record_sha256 != state.terminal_end_sha256:
        raise V3ValidationError("processing END cursor does not link END")


def _validate_tail(state: TailState) -> None:
    if state.tail_path_token.blackout_id != state.blackout_id:
        raise V3ValidationError("tail staging token is cross-bound")
    _hash(state.tail_sha256)
    _uint64(state.tail_length)
    _validate_tail_receipts(state.tail_records, state.tail_length, state.terminal_outcome_sha256)
    _hash(state.terminal_root_sha256)
    if state.terminal_closing_anchor_sha256 is not None:
        _hash(state.terminal_closing_anchor_sha256)
    _hash(state.terminal_end_sha256)
    _hash(state.terminal_outcome_sha256)
    _cursor(state.terminal_cursor_after_outcome)
    _cursor_scope(
        state.terminal_cursor_after_outcome,
        state.blackout_id,
        state.logical_segment_id,
        BlackoutChainKind.TERMINAL,
    )
    if state.terminal_cursor_after_outcome.last_record_sha256 != state.terminal_outcome_sha256:
        raise V3ValidationError("tail outcome cursor does not link outcome")
    if state.tail_records[-1].hash != state.terminal_outcome_sha256:
        raise V3ValidationError("tail outcome receipt does not link outcome")
    if state.seal_intent is not None:
        _validate_seal(state.seal_intent)


def _validate_tail_receipts(
    receipts: tuple[V3TailRecordReceipt, ...], tail_length: int, outcome_hash: str
) -> None:
    _validate_tail_coverage(receipts, tail_length, outcome_hash)
    _validate_tail_cardinality(receipts)


def _validate_tail_coverage(
    receipts: tuple[V3TailRecordReceipt, ...], tail_length: int, outcome_hash: str
) -> None:
    allowed_types = (
        "fragment_profile",
        "load_sag_assessment_summary",
        "curve_assessment_summary",
        "firmware_lb_assessment_summary",
        "learning_decision",
        "ir_model_commit_receipt",
        "terminal_outcome",
    )
    next_offset = 0
    previous_rank = -1
    previous_type = ""
    for receipt in receipts:
        _validate_tail_receipt(receipt)
        if receipt.type not in allowed_types:
            raise V3ValidationError("tail receipt type is invalid")
        rank = allowed_types.index(receipt.type)
        duplicate_fragment = receipt.type == "fragment_profile" and previous_type == receipt.type
        if (rank <= previous_rank and not duplicate_fragment) or receipt.offset != next_offset:
            raise V3ValidationError("tail receipt order is invalid")
        previous_rank = rank
        previous_type = receipt.type
        next_offset += receipt.length
    if not receipts or receipts[-1].type != "terminal_outcome":
        raise V3ValidationError("tail outcome receipt is missing or not last")
    if receipts[-1].hash != outcome_hash or next_offset != tail_length:
        raise V3ValidationError("tail receipts do not cover outcome and length")


def _validate_tail_cardinality(receipts: tuple[V3TailRecordReceipt, ...]) -> None:
    profile_count = sum(receipt.type == "fragment_profile" for receipt in receipts)
    if not 1 <= profile_count <= 96:
        raise V3ValidationError("tail profile count is out of bounds")
    required = {
        "load_sag_assessment_summary",
        "curve_assessment_summary",
        "firmware_lb_assessment_summary",
        "learning_decision",
    }
    actual = {receipt.type for receipt in receipts}
    if (
        not required <= actual
        or sum(receipt.type == "ir_model_commit_receipt" for receipt in receipts) > 1
    ):
        raise V3ValidationError("tail mandatory record grammar is invalid")


def _validate_capture_common(state: CapturingState | ProcessingState | TailState) -> None:
    validate_uuid4_hex(state.blackout_id, "blackout_id")
    validate_uuid4_hex(state.logical_segment_id, "logical_segment_id")
    _cursor(state.physical_cursor)
    _cursor_scope(
        state.physical_cursor,
        state.blackout_id,
        state.logical_segment_id,
        BlackoutChainKind.PHYSICAL,
    )
    if len(state.storage_segments) > MAX_STORAGE_SEGMENTS:
        raise V3ValidationError("storage segment bound exceeded")
    for segment in state.storage_segments:
        _validate_segment(segment, state.blackout_id, state.logical_segment_id)
    _counters(state.capture_bytes, state.capture_record_count, state.sample_count, state.gap_count)
    ordinals = [segment.ordinal for segment in state.storage_segments]
    if ordinals != sorted(set(ordinals)):
        raise V3ValidationError("storage segment ordinals are not unique and ordered")
    if state.sample_count + state.gap_count > state.capture_record_count:
        raise V3ValidationError("sample and gap counters exceed records")


def _validate_append_intent(value: V3AppendIntent, state: CapturingState) -> None:
    if value.chain not in {"physical", "terminal"} or value.operation == "":
        raise V3ValidationError("append intent vocabulary is invalid")
    if value.chain == "physical" and value.storage_ordinal is None:
        raise V3ValidationError("physical append intent lacks storage ordinal")
    if value.storage_ordinal is not None:
        _uint64(value.storage_ordinal)
    _uint64(value.expected_seq)
    _uint64(value.file_offset)
    _line(value.line_utf8, value.line_length, value.line_sha256)
    _hash(value.expected_cursor_sha256)
    if value.expected_seq != state.physical_cursor.next_sequence and value.chain == "physical":
        raise V3ValidationError("append intent sequence is stale")


def _validate_last_append(value: V3LastAppend) -> None:
    if not value.operation:
        raise V3ValidationError("last append operation is empty")
    _hash(value.prior_cursor_sha256)
    _hash(value.line_sha256)
    _hash(value.resulting_cursor_sha256)


def _validate_tail_receipt(value: V3TailRecordReceipt) -> None:
    _uint64(value.offset)
    _uint64(value.length)
    if value.length == 0 or not value.type:
        raise V3ValidationError("tail receipt bounds are invalid")
    _hash(value.hash)


def _validate_damage(value: V3DamageContinuation, state: CapturingState) -> None:
    if value.phase not in {"reserved", "old_renamed", "successor_created", "gap_durable"}:
        raise V3ValidationError("damage phase is invalid")
    if (value.blackout_id, value.logical_segment_id) != (
        state.blackout_id,
        state.logical_segment_id,
    ):
        raise V3ValidationError("damage continuation is cross-bound")
    for identifier, name in (
        (value.old_storage_id, "old_storage_id"),
        (value.new_storage_id, "new_storage_id"),
    ):
        validate_uuid4_hex(identifier, name)
    _bind_token(
        value.old_path_token,
        value.blackout_id,
        value.logical_segment_id,
        value.old_storage_id,
        value.old_ordinal,
    )
    _bind_token(
        value.old_offset_token,
        value.blackout_id,
        value.logical_segment_id,
        value.old_storage_id,
        value.old_ordinal,
    )
    _bind_token(
        value.damaged_path_token,
        value.blackout_id,
        value.logical_segment_id,
        value.old_storage_id,
        value.old_ordinal,
    )
    _bind_token(
        value.damaged_offset_token,
        value.blackout_id,
        value.logical_segment_id,
        value.old_storage_id,
        value.old_ordinal,
    )
    _hash(value.damaged_file_sha256)
    if (
        value.damaged_file_sha256 != value.damaged_path_token.file_sha256
        or value.damaged_file_sha256 != value.damaged_offset_token.file_sha256
    ):
        raise V3ValidationError("damage receipt hash is not bound to damaged files")
    if value.old_storage_id == value.new_storage_id or value.new_ordinal != value.old_ordinal + 1:
        raise V3ValidationError("damage successor identity is invalid")
    _line(value.gap_line_utf8, value.gap_length, value.gap_sha256)
    _uint64(value.trusted_bytes)
    _uint64(value.trusted_last_seq)
    _hash(value.trusted_last_record_sha256)
    _uint64(value.new_physical_next_seq)
    _hash(value.new_physical_last_record_sha256)
    _uint64(value.old_ordinal)
    _uint64(value.new_ordinal)


def _validate_rollover(value: V3RolloverReservation, state: CapturingState) -> None:
    if value.phase not in {
        "reserved",
        "successor_started",
        "carrier_ended",
    } or value.budget_kind not in {"bytes", "segment_refs"}:
        raise V3ValidationError("rollover vocabulary is invalid")
    if (
        value.physical_episode_id != state.physical_episode_id
        or value.continuation_kind != "size_rollover"
        or value.old_blackout_id != state.blackout_id
        or value.old_logical_segment_id != state.logical_segment_id
    ):
        raise V3ValidationError("rollover relation is invalid")
    if (
        value.successor_blackout_id == value.old_blackout_id
        or value.successor_logical_segment_id == value.old_logical_segment_id
        or value.successor_storage_id == value.old_storage_id
        or value.successor_path_token.ordinal != 0
    ):
        raise V3ValidationError("rollover successor identity is invalid")
    for identifier, name in (
        (value.old_blackout_id, "old_blackout_id"),
        (value.old_logical_segment_id, "old_logical_segment_id"),
        (value.old_storage_id, "old_storage_id"),
        (value.successor_blackout_id, "successor_blackout_id"),
        (value.successor_logical_segment_id, "successor_logical_segment_id"),
        (value.successor_storage_id, "successor_storage_id"),
    ):
        validate_uuid4_hex(identifier, name)
    _bind_token(
        value.old_path_token,
        value.old_blackout_id,
        value.old_logical_segment_id,
        value.old_storage_id,
        value.old_path_token.ordinal,
    )
    _bind_token(
        value.successor_path_token,
        value.successor_blackout_id,
        value.successor_logical_segment_id,
        value.successor_storage_id,
        value.successor_path_token.ordinal,
    )
    _line(
        value.successor_start_line_utf8, value.successor_start_length, value.successor_start_sha256
    )
    _line(value.carrier_end_line_utf8, value.carrier_end_length, value.carrier_end_sha256)


def _validate_seal(value: V3SealIntent) -> None:
    if value.phase not in {"reserved", "files_sealed", "locator_durable", "catalog_durable"}:
        raise V3ValidationError("seal phase is invalid")
    _uint64(value.locator_seq)
    _uint64(value.catalog_offset)
    if value.locator_sha256 is not None:
        _hash(value.locator_sha256)
    if value.catalog_line_sha256 is not None:
        _hash(value.catalog_line_sha256)


def _validate_tail_build(value: V3TailBuildIntent, state: ProcessingState) -> None:
    if value.expected_terminal_cursor != state.terminal_cursor_after_end:
        raise V3ValidationError("tail build cursor is stale")
    _hash(value.batch_sha256)
    _hash(value.encoded_sha256)
    _uint64(value.encoded_length)


def _bind_token(token: object, blackout: str, logical: str, storage: str, ordinal: int) -> None:
    if not isinstance(
        token,
        (
            V3SegmentPathToken,
            V3OffsetPathToken,
            V3DamagedSegmentPathToken,
            V3DamagedOffsetPathToken,
        ),
    ) or (
        token.blackout_id,
        token.logical_segment_id,
        token.storage_id,
        token.ordinal,
    ) != (blackout, logical, storage, ordinal):
        raise V3ValidationError("path token is cross-bound to another aggregate")


def _validate_segment(value: V3StorageSegmentReceipt, blackout: str, logical: str) -> None:
    _bind_token(value.path_token, blackout, logical, value.storage_id, value.ordinal)
    _bind_token(value.offset_token, blackout, logical, value.storage_id, value.ordinal)
    if value.path_token.ordinal != value.offset_token.ordinal or (
        value.damaged_file_sha256 is not None
    ) != isinstance(value.path_token, (V3DamagedSegmentPathToken, V3DamagedOffsetPathToken)):
        raise V3ValidationError("segment token pair is mismatched")
    if isinstance(value.path_token, V3SegmentPathToken) != isinstance(
        value.offset_token, V3OffsetPathToken
    ):
        raise V3ValidationError("segment token pair is mismatched")
    if isinstance(value.path_token, V3DamagedSegmentPathToken) != isinstance(
        value.offset_token, V3DamagedOffsetPathToken
    ):
        raise V3ValidationError("damaged token pair is mismatched")
    if (
        isinstance(value.path_token, V3SegmentPathToken)
        and isinstance(value.offset_token, V3OffsetPathToken)
        and value.path_token.started_utc != value.offset_token.started_utc
    ):
        raise V3ValidationError("active token UTC values are mismatched")
    if (
        isinstance(value.path_token, V3DamagedSegmentPathToken)
        and value.damaged_file_sha256 != value.path_token.file_sha256
    ):
        raise V3ValidationError("damaged token hash is mismatched")
    if (
        isinstance(value.offset_token, V3DamagedOffsetPathToken)
        and value.damaged_file_sha256 != value.offset_token.file_sha256
    ):
        raise V3ValidationError("damaged offset token hash is mismatched")
    for number in (value.trusted_bytes, value.first_seq, value.last_seq):
        _uint64(number)
    if value.first_seq > value.last_seq:
        raise V3ValidationError("segment sequence bounds are invalid")
    _hash(value.last_record_sha256)


def _cursor(value: BlackoutCaptureCursor) -> None:
    if (
        value.chain not in {BlackoutChainKind.PHYSICAL, BlackoutChainKind.TERMINAL}
        or value.next_sequence is None
        and value.last_record_sha256 is None
    ):
        raise V3ValidationError("cursor state is invalid")
    if value.next_sequence is not None and (
        type(value.next_sequence) is not int or not 0 <= value.next_sequence <= MAX_CHAIN_SEQUENCE
    ):
        raise V3ValidationError("cursor sequence is out of bounds")
    if value.last_record_sha256 is not None:
        _hash(value.last_record_sha256)


def _cursor_scope(
    value: BlackoutCaptureCursor, blackout: str, logical: str, chain: BlackoutChainKind
) -> None:
    if (value.blackout_id, value.segment_id) != (blackout, logical) or value.chain is not chain:
        raise V3ValidationError("cursor is cross-bound to another chain")


def _counters(capture_bytes: int, records: int, samples: int, gaps: int) -> None:
    if any(type(item) is not int or item < 0 for item in (capture_bytes, records, samples, gaps)):
        raise V3ValidationError("registry counters are negative or non-integer")
    if capture_bytes > MAX_CAPTURE_BYTES:
        raise V3ValidationError("capture bytes exceed capture bound")
    for item in (records, samples, gaps):
        _uint64(item)


def _uint64(value: object) -> None:
    if type(value) is not int or not 0 <= value <= MAX_UINT64:
        raise V3ValidationError("unsigned counter is out of bounds")


def _line(value: str, length: int, digest: str) -> None:
    if not value.endswith("\n") or len(value.encode()) != length:
        raise V3ValidationError("line length is inconsistent")
    _hash(digest)
    if hashlib.sha256(value.encode()).hexdigest() != digest:
        raise V3ValidationError("line hash is inconsistent")


def _hash(value: object) -> None:
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise V3ValidationError("hash is not lowercase SHA-256")


__all__ = [
    "REGISTRY_SCHEMA",
    "MAX_REGISTRY_BYTES",
    "MAX_CAPTURE_BYTES",
    "MAX_PENDING_ENTRIES",
    "MAX_RECOVERY_PAGE_SIZE",
    "MAX_STORAGE_SEGMENTS",
    "MAX_CHAIN_SEQUENCE",
    "MAX_UINT64",
    "V3StorageSegmentReceipt",
    "V3AppendIntent",
    "V3LastAppend",
    "V3TailBuildIntent",
    "V3TailRecordReceipt",
    "V3SealIntent",
    "V3DamageContinuation",
    "V3RolloverReservation",
    "PreparingCaptureState",
    "CapturingState",
    "ProcessingState",
    "TailState",
    "CaptureState",
    "PendingState",
    "validate_registry",
]
