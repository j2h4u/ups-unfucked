"""Typed values shared by the v3 blackout application ports.

The application boundary deliberately contains no wire envelope, encoded line,
filesystem path, or generic payload.  Adapters translate their durable records
to these values before crossing into the application.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from src.domain.blackout_capture import BlackoutStart, DischargeGap, DischargeSample
from src.domain.blackout_terminal import BlackoutTermination
from src.domain.curve_assessment import CurveAssessment, CurveDisposition
from src.domain.firmware_lb_assessment import FirmwareLbAssessment, FirmwareLbDisposition
from src.domain.fragments import DischargeFragmentProfile, EndpointAnchor, ObservationOrigin
from src.domain.ir_learning_decision import IrLearningDecision, IrLearningDisposition
from src.domain.load_sag_assessment import LoadSagAssessment, LoadSagDisposition
from src.domain.terminal_outcome import TerminalOutcome
from src.domain.values import ModelCommitReceipt

# A v3 envelope has at most 3198 sequence values, 0 through 3197.  This is
# intentionally independent from the uint64 logical counters in domain values.
MAX_CHAIN_SEQUENCE = 3_197
MAX_STORED_RECORD_BYTES = 20 * 1024
MAX_EVIDENCE_PAGE_RECORDS = 1_024
MAX_EVIDENCE_PAGE_BYTES = 4 * 1024 * 1024
MAX_SUMMARY_PAGE_SIZE = 100
MAX_RECOVERY_PAGE_SIZE = 32
MAX_TAIL_PROFILES = 96
MAX_TAIL_RESULTS = 96


class BlackoutRecordType(StrEnum):
    """Closed record vocabulary visible to the application."""

    START = "blackout_start"
    SAMPLE = "discharge_sample"
    GAP = "discharge_gap"
    ANCHOR = "endpoint_anchor"
    END = "blackout_end"
    PROFILE = "discharge_profile"
    LOAD_SAG = "load_sag_assessment"
    CURVE = "curve_assessment"
    FIRMWARE_LB = "firmware_lb_assessment"
    IR_DECISION = "ir_learning_decision"
    MODEL_RECEIPT = "model_commit_receipt"
    TERMINAL_OUTCOME = "terminal_outcome"


class BlackoutChainKind(StrEnum):
    """Closed durable chain vocabulary shared by cursors and record refs."""

    PHYSICAL = "physical"
    TERMINAL = "terminal"


class BlackoutProcessingStage(StrEnum):
    """Closed, resume-relevant stages of the durable handoff."""

    CAPTURING = "capturing"
    PROCESSING = "processing"
    TAIL = "tail"
    SEALED = "sealed"


@dataclass(frozen=True, slots=True)
class BlackoutRef:
    """Stable aggregate identity; it carries no adapter path or file name."""

    blackout_id: str
    segment_id: str

    def __post_init__(self) -> None:
        _text(self.blackout_id, "blackout ID")
        _text(self.segment_id, "segment ID")


@dataclass(frozen=True, slots=True)
class BlackoutCaptureCursor:
    """Compare-and-append position for one independently-owned chain.

    ``next_sequence=None`` is the explicit exhausted state after sequence
    ``MAX_CHAIN_SEQUENCE`` has been durably appended.
    """

    blackout_id: str
    segment_id: str
    chain: BlackoutChainKind
    next_sequence: int | None
    last_record_sha256: str | None

    def __post_init__(self) -> None:
        _text(self.blackout_id, "blackout ID")
        _text(self.segment_id, "segment ID")
        _require_chain_kind(self.chain)
        if self.next_sequence is not None:
            _chain_sequence(self.next_sequence, "next sequence")
        if self.last_record_sha256 is not None:
            _hash(self.last_record_sha256, "last record hash")
        _validate_cursor_state(self.next_sequence, self.last_record_sha256)


@dataclass(frozen=True, slots=True)
class BlackoutCaptureOpened:
    """Idempotent START response carrying the exact append cursor."""

    ref: BlackoutRef
    cursor: BlackoutCaptureCursor

    def __post_init__(self) -> None:
        if not isinstance(self.ref, BlackoutRef):
            raise TypeError("opened capture ref must be BlackoutRef")
        if not isinstance(self.cursor, BlackoutCaptureCursor):
            raise TypeError("opened capture cursor must be BlackoutCaptureCursor")
        if self.cursor.chain is not BlackoutChainKind.PHYSICAL:
            raise ValueError("opened capture cursor must belong to the physical chain")
        if (
            self.ref.blackout_id != self.cursor.blackout_id
            or self.ref.segment_id != self.cursor.segment_id
        ):
            raise ValueError("opened capture cursor scope differs from its ref")


@dataclass(frozen=True, slots=True)
class StoredRecordRef:
    """Immutable identity and bounded size of one durable record."""

    ref: BlackoutRef
    chain: BlackoutChainKind
    record_type: BlackoutRecordType
    sequence: int
    record_sha256: str
    byte_length: int

    def __post_init__(self) -> None:
        _require_stored_record_ref(self.ref)
        _require_chain_kind(self.chain)
        _require_record_type(self.record_type)
        _validate_record_chain(self.chain, self.record_type)
        _chain_sequence(self.sequence, "record sequence")
        _hash(self.record_sha256, "record hash")
        _validate_record_size(self.byte_length)


@dataclass(frozen=True, slots=True)
class ProfileChainRef:
    """Immutable references to a bounded derived profile chain."""

    ref: BlackoutRef
    series_id: str
    records: tuple[StoredRecordRef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.ref, BlackoutRef):
            raise TypeError("profile chain ref must be BlackoutRef")
        _hash(self.series_id, "profile series ID")
        if not isinstance(self.records, tuple) or not self.records:
            raise ValueError("profile chain must contain a record reference")
        if len(self.records) > MAX_TAIL_PROFILES:
            raise ValueError("profile chain exceeds its bounded record budget")
        if any(not isinstance(item, StoredRecordRef) for item in self.records):
            raise TypeError("profile chain records must be StoredRecordRef values")
        if any(item.ref != self.ref for item in self.records):
            raise ValueError("profile chain record scope differs from its aggregate")
        if any(item.chain is not BlackoutChainKind.TERMINAL for item in self.records):
            raise ValueError("profile chain records must belong to the terminal chain")


@dataclass(frozen=True, slots=True)
class StoredPhysicalRecord:
    """One ordered physical record paired with its immutable durable identity."""

    ref: StoredRecordRef
    value: BlackoutStart | DischargeSample | DischargeGap | EndpointAnchor

    def __post_init__(self) -> None:
        if not isinstance(self.ref, StoredRecordRef):
            raise TypeError("physical record ref must be StoredRecordRef")
        expected_type = _physical_record_type(self.value)
        _validate_physical_record_link(self, expected_type)


@dataclass(frozen=True, slots=True)
class RawEvidencePage:
    """A bounded, verified page of typed physical evidence.

    The ordered tuple preserves exact physical-chain order, including START
    and intermediate anchors. Each item pairs a closed domain union with
    immutable durable identity and hash; canonical JSONL bytes never cross
    this boundary.
    """

    ref: BlackoutRef
    records: tuple[StoredPhysicalRecord, ...]
    next_cursor: BlackoutCaptureCursor | None
    complete: bool

    def __post_init__(self) -> None:
        _validate_raw_page_types(self)
        _validate_raw_page_bounds(self)
        _validate_raw_page_cursor(self)


@dataclass(frozen=True, slots=True)
class BlackoutProcessingRef:
    """Durable handoff identity and exact stage needed for resume."""

    ref: BlackoutRef
    stage: BlackoutProcessingStage
    last_record_sha256: str
    frozen_policy_revision: str

    def __post_init__(self) -> None:
        if not isinstance(self.ref, BlackoutRef):
            raise TypeError("processing ref must be BlackoutRef")
        if not isinstance(self.stage, BlackoutProcessingStage):
            raise TypeError("processing stage must be BlackoutProcessingStage")
        _hash(self.last_record_sha256, "processing record hash")
        _text(self.frozen_policy_revision, "processing policy revision")


@dataclass(frozen=True, slots=True)
class RecoveredCaptureWork:
    """Active capture work paired with its exact append cursor."""

    ref: BlackoutRef
    cursor: BlackoutCaptureCursor

    def __post_init__(self) -> None:
        if not isinstance(self.ref, BlackoutRef):
            raise TypeError("recovered capture ref must be BlackoutRef")
        if not isinstance(self.cursor, BlackoutCaptureCursor):
            raise TypeError("recovered capture cursor must be BlackoutCaptureCursor")
        if self.cursor.chain is not BlackoutChainKind.PHYSICAL:
            raise ValueError("recovered capture cursor must belong to the physical chain")
        if (
            self.ref.blackout_id != self.cursor.blackout_id
            or self.ref.segment_id != self.cursor.segment_id
        ):
            raise ValueError("recovered capture cursor scope differs from its ref")


@dataclass(frozen=True, slots=True)
class BlackoutRecoveryCursor:
    """Bounded continuation position for deterministic recovery pages."""

    processing_offset: int
    active_capture_emitted: bool

    def __post_init__(self) -> None:
        _uint64(self.processing_offset, "recovery processing offset")
        if not isinstance(self.active_capture_emitted, bool):
            raise TypeError("recovery active-capture marker must be bool")


@dataclass(frozen=True, slots=True)
class BlackoutRecoveryPage:
    """A bounded restart work batch with a continuation cursor."""

    active_capture: RecoveredCaptureWork | None
    processing: tuple[BlackoutProcessingRef, ...]
    next_cursor: BlackoutRecoveryCursor | None
    complete: bool

    def __post_init__(self) -> None:
        if self.active_capture is not None and not isinstance(
            self.active_capture, RecoveredCaptureWork
        ):
            raise TypeError("active recovery work must be RecoveredCaptureWork")
        _tuple_of(self.processing, BlackoutProcessingRef, "processing recovery work")
        if len(self.processing) > MAX_RECOVERY_PAGE_SIZE:
            raise ValueError("recovery page exceeds work bound")
        identifiers = tuple((item.ref.blackout_id, item.ref.segment_id) for item in self.processing)
        if identifiers != tuple(sorted(identifiers)) or len(set(identifiers)) != len(identifiers):
            raise ValueError("processing recovery work must be deterministic and unique")
        if not isinstance(self.complete, bool):
            raise TypeError("recovery page complete must be bool")
        if self.complete and self.next_cursor is not None:
            raise ValueError("complete recovery page must not carry a next cursor")
        if not self.complete and self.next_cursor is None:
            raise ValueError("incomplete recovery page must carry a next cursor")


@dataclass(frozen=True, slots=True)
class BlackoutTailBatch:
    """Closed derived-tail command; no arbitrary encoded record collection."""

    profiles: tuple[DischargeFragmentProfile, ...]
    load_sag_results: tuple[LoadSagAssessment, ...]
    curve_results: tuple[CurveAssessment, ...]
    firmware_lb_results: tuple[FirmwareLbAssessment, ...]
    ir_decision: IrLearningDecision
    model_commit_receipt: ModelCommitReceipt | None
    terminal_outcome: TerminalOutcome
    anchors: tuple[EndpointAnchor, ...]

    def __post_init__(self) -> None:
        _validate_tail_batch_types(self)
        _validate_tail_batch_bounds(self)
        _validate_tail_batch_values(self)


@dataclass(frozen=True, slots=True)
class BlackoutSummary:
    """Rebuildable history projection with explicit consumer result pairings."""

    blackout_id: str
    physical_episode_id: str
    battery_epoch_id: str
    segment_id: str
    observation_origin: ObservationOrigin
    started_at_utc: datetime
    ended_at_utc: datetime | None
    termination: BlackoutTermination | None
    sample_count: int
    gap_count: int
    load_sag_result_hash: str | None
    curve_result_hash: str | None
    firmware_lb_result_hash: str | None
    ir_learning_result_hash: str | None
    load_sag_disposition: LoadSagDisposition | None = None
    curve_disposition: CurveDisposition | None = None
    firmware_lb_disposition: FirmwareLbDisposition | None = None
    ir_learning_disposition: IrLearningDisposition | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.blackout_id, "blackout ID"),
            (self.physical_episode_id, "physical episode ID"),
            (self.battery_epoch_id, "battery epoch ID"),
            (self.segment_id, "segment ID"),
        ):
            _text(value, name)
        _enum(self.observation_origin, ObservationOrigin, "observation origin")
        _utc(self.started_at_utc)
        if self.ended_at_utc is not None:
            _utc(self.ended_at_utc)
        if (self.ended_at_utc is None) != (self.termination is None):
            raise ValueError("ended_at and termination must be supplied together")
        if self.ended_at_utc is not None and self.ended_at_utc < self.started_at_utc:
            raise ValueError("summary end must not precede its start")
        if self.termination is not None:
            _enum(self.termination, BlackoutTermination, "termination")
        for disposition, enum_type in (
            (self.load_sag_disposition, LoadSagDisposition),
            (self.curve_disposition, CurveDisposition),
            (self.firmware_lb_disposition, FirmwareLbDisposition),
            (self.ir_learning_disposition, IrLearningDisposition),
        ):
            if disposition is not None:
                _enum(disposition, enum_type, "summary disposition")
        _uint64(self.sample_count, "sample count")
        _uint64(self.gap_count, "gap count")
        _paired_hash(self.load_sag_result_hash, self.load_sag_disposition, "load-sag")
        _paired_hash(self.curve_result_hash, self.curve_disposition, "curve")
        _paired_hash(self.firmware_lb_result_hash, self.firmware_lb_disposition, "firmware-LB")
        _paired_hash(self.ir_learning_result_hash, self.ir_learning_disposition, "IR learning")


@dataclass(frozen=True, slots=True)
class BlackoutSummaryPage:
    summaries: tuple[BlackoutSummary, ...]
    next_cursor: str | None
    complete: bool

    def __post_init__(self) -> None:
        _tuple_of(self.summaries, BlackoutSummary, "summary page")
        if len(self.summaries) > MAX_SUMMARY_PAGE_SIZE:
            raise ValueError("summary page exceeds page-size bound")
        if self.next_cursor is not None:
            _text(self.next_cursor, "summary cursor")
        if not isinstance(self.complete, bool):
            raise TypeError("summary page complete must be bool")
        if self.complete and self.next_cursor is not None:
            raise ValueError("complete summary page must not carry a next cursor")
        if not self.complete and self.next_cursor is None:
            raise ValueError("incomplete summary page must carry a next cursor")


def _text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 128:
        raise ValueError(f"{field} must be a bounded non-empty string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} must not contain control characters")


def _hash(value: object, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{field} must be lowercase SHA-256")


def _require_stored_record_ref(value: object) -> None:
    if not isinstance(value, BlackoutRef):
        raise TypeError("stored record ref must be BlackoutRef")


def _require_record_type(value: object) -> None:
    if not isinstance(value, BlackoutRecordType):
        raise TypeError("record type must be BlackoutRecordType")


def _require_chain_kind(value: object) -> None:
    if not isinstance(value, BlackoutChainKind):
        raise TypeError("chain must be BlackoutChainKind")


def _validate_record_chain(chain: BlackoutChainKind, record_type: BlackoutRecordType) -> None:
    physical_only_types = {
        BlackoutRecordType.START,
        BlackoutRecordType.SAMPLE,
        BlackoutRecordType.GAP,
    }
    if record_type is BlackoutRecordType.ANCHOR:
        return
    expected_chain = (
        BlackoutChainKind.PHYSICAL
        if record_type in physical_only_types
        else BlackoutChainKind.TERMINAL
    )
    if chain is not expected_chain:
        raise ValueError(
            f"{record_type.value} record must belong to the {expected_chain.value} chain"
        )


def _validate_record_size(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("record byte length must be an integer")
    if not 1 <= value <= MAX_STORED_RECORD_BYTES:
        raise ValueError("record byte length exceeds the 20 KiB physical bound")


def _chain_sequence(value: object, field: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_CHAIN_SEQUENCE
    ):
        raise ValueError(f"{field} must be within the v3 chain range 0..{MAX_CHAIN_SEQUENCE}")


def _uint64(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= (1 << 64) - 1:
        raise ValueError(f"{field} must be an unsigned 64-bit integer")


def _utc(value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("timestamp must be UTC")


def _enum(value: object, enum_type: type[StrEnum], field: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"{field} must be {enum_type.__name__}")


def _tuple_of(value: object, item_type: type, field: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{field} must be a tuple")
    if any(not isinstance(item, item_type) for item in value):
        raise TypeError(f"{field} contains an invalid typed value")


def _validate_raw_page_types(value: RawEvidencePage) -> None:
    if not isinstance(value.ref, BlackoutRef):
        raise TypeError("evidence page ref must be BlackoutRef")
    _tuple_of(value.records, StoredPhysicalRecord, "records")


def _validate_raw_page_bounds(value: RawEvidencePage) -> None:
    if len(value.records) > MAX_EVIDENCE_PAGE_RECORDS:
        raise ValueError("evidence page exceeds record bound")
    if any(item.ref.ref != value.ref for item in value.records):
        raise ValueError("evidence record scope differs from its page")
    if sum(item.ref.byte_length for item in value.records) > MAX_EVIDENCE_PAGE_BYTES:
        raise ValueError("evidence page exceeds byte bound")
    sequences = tuple(item.ref.sequence for item in value.records)
    if sequences and sequences != tuple(range(sequences[0], sequences[0] + len(sequences))):
        raise ValueError("evidence record sequences must be strictly contiguous")


def _validate_raw_page_cursor(value: RawEvidencePage) -> None:
    _validate_page_cursor_type_and_scope(value)
    _validate_page_cursor_completion(value)
    if not value.records:
        _validate_empty_page_cursor(value)
        return
    if value.complete:
        return
    _validate_nonempty_incomplete_cursor(value)


def _validate_page_cursor_type_and_scope(value: RawEvidencePage) -> None:
    if not isinstance(value.complete, bool):
        raise TypeError("evidence page complete must be bool")
    if value.next_cursor is not None:
        if not isinstance(value.next_cursor, BlackoutCaptureCursor):
            raise TypeError("evidence page cursor must be BlackoutCaptureCursor")
        if (
            value.next_cursor.blackout_id != value.ref.blackout_id
            or value.next_cursor.segment_id != value.ref.segment_id
        ):
            raise ValueError("evidence page cursor scope differs from its page")
        if value.records:
            record_chain = value.records[0].ref.chain
            if any(item.ref.chain is not record_chain for item in value.records):
                raise ValueError("evidence page records must belong to one chain")
            if value.next_cursor.chain is not record_chain:
                raise ValueError("evidence page cursor must remain on one chain")


def _validate_page_cursor_completion(value: RawEvidencePage) -> None:
    if value.complete and value.next_cursor is not None:
        raise ValueError("complete evidence page must not carry a next cursor")
    if not value.complete and value.next_cursor is None:
        raise ValueError("incomplete evidence page must carry a next cursor")


def _validate_empty_page_cursor(value: RawEvidencePage) -> None:
    if value.complete:
        return
    if value.next_cursor is None:
        raise ValueError("empty incomplete evidence page requires its initial cursor")
    _validate_initial_cursor(value.next_cursor)


def _validate_nonempty_incomplete_cursor(value: RawEvidencePage) -> None:
    if value.next_cursor is None:
        raise ValueError("incomplete evidence page requires its final-record cursor")
    final = value.records[-1].ref
    if value.next_cursor.chain is not final.chain:
        raise ValueError("evidence page cursor must remain on one chain")
    expected_next = None if final.sequence == MAX_CHAIN_SEQUENCE else final.sequence + 1
    if value.next_cursor.next_sequence != expected_next:
        raise ValueError("evidence page cursor does not follow its final record")
    if value.next_cursor.last_record_sha256 != final.record_sha256:
        raise ValueError("evidence page cursor hash does not match its final record")


def _paired_hash(value: str | None, disposition: object | None, field: str) -> None:
    if (value is None) != (disposition is None):
        raise ValueError(f"{field} result hash and disposition must be supplied together")
    if value is not None:
        _hash(value, f"{field} result hash")


def _validate_cursor_state(next_sequence: int | None, last_hash: str | None) -> None:
    if next_sequence is None and last_hash is None:
        raise ValueError("exhausted cursor requires its last record hash")
    if next_sequence == 0 and last_hash is not None:
        raise ValueError("cursor initial state is next_sequence=0 with no prior hash")
    if next_sequence not in (None, 0) and last_hash is None:
        raise ValueError("non-initial cursor requires its prior record hash")


def _validate_initial_cursor(value: BlackoutCaptureCursor) -> None:
    if value.next_sequence != 0 or value.last_record_sha256 is not None:
        raise ValueError("empty incomplete evidence page requires its initial cursor")


def _validate_physical_record_link(
    value: StoredPhysicalRecord, expected_type: BlackoutRecordType
) -> None:
    if value.ref.record_type is not expected_type:
        raise ValueError("physical record type does not match its immutable reference")
    if value.ref.chain is not BlackoutChainKind.PHYSICAL:
        raise ValueError("physical record must belong to the physical chain")
    if (
        value.value.blackout_id != value.ref.ref.blackout_id
        or value.value.segment_id != value.ref.ref.segment_id
    ):
        raise ValueError("physical record scope differs from its immutable reference")


def _validate_tail_batch_types(value: BlackoutTailBatch) -> None:
    _tuple_of(value.profiles, DischargeFragmentProfile, "profiles")
    _tuple_of(value.load_sag_results, LoadSagAssessment, "load-sag results")
    _tuple_of(value.curve_results, CurveAssessment, "curve results")
    _tuple_of(value.firmware_lb_results, FirmwareLbAssessment, "firmware-LB results")
    _tuple_of(value.anchors, EndpointAnchor, "terminal anchors")


def _validate_tail_batch_bounds(value: BlackoutTailBatch) -> None:
    if len(value.profiles) > MAX_TAIL_PROFILES:
        raise ValueError("tail profile batch exceeds its bounded budget")
    if (
        max(len(value.load_sag_results), len(value.curve_results), len(value.firmware_lb_results))
        > MAX_TAIL_RESULTS
    ):
        raise ValueError("tail assessment batch exceeds its bounded budget")


def _validate_tail_batch_values(value: BlackoutTailBatch) -> None:
    if not isinstance(value.ir_decision, IrLearningDecision):
        raise TypeError("tail IR decision must be IrLearningDecision")
    if value.model_commit_receipt is not None and not isinstance(
        value.model_commit_receipt, ModelCommitReceipt
    ):
        raise TypeError("tail model receipt must be ModelCommitReceipt or None")
    if not isinstance(value.terminal_outcome, TerminalOutcome):
        raise TypeError("tail outcome must be TerminalOutcome")


def _physical_record_type(
    value: BlackoutStart | DischargeSample | DischargeGap | EndpointAnchor,
) -> BlackoutRecordType:
    if isinstance(value, BlackoutStart):
        return BlackoutRecordType.START
    if isinstance(value, DischargeSample):
        return BlackoutRecordType.SAMPLE
    if isinstance(value, DischargeGap):
        return BlackoutRecordType.GAP
    if isinstance(value, EndpointAnchor):
        return BlackoutRecordType.ANCHOR
    raise TypeError("physical record must be a closed domain record")


__all__ = [
    "MAX_CHAIN_SEQUENCE",
    "MAX_EVIDENCE_PAGE_BYTES",
    "MAX_EVIDENCE_PAGE_RECORDS",
    "MAX_RECOVERY_PAGE_SIZE",
    "MAX_STORED_RECORD_BYTES",
    "MAX_SUMMARY_PAGE_SIZE",
    "BlackoutChainKind",
    "BlackoutCaptureCursor",
    "BlackoutCaptureOpened",
    "BlackoutProcessingRef",
    "BlackoutProcessingStage",
    "BlackoutRecordType",
    "BlackoutRecoveryCursor",
    "BlackoutRecoveryPage",
    "BlackoutRef",
    "BlackoutSummary",
    "BlackoutSummaryPage",
    "BlackoutTailBatch",
    "ProfileChainRef",
    "RawEvidencePage",
    "RecoveredCaptureWork",
    "StoredRecordRef",
    "StoredPhysicalRecord",
]
