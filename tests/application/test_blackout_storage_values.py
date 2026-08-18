"""Contract tests for typed v3 blackout application values."""

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from typing import Any, cast

import pytest

from src.application.blackout_storage_values import (
    MAX_CHAIN_SEQUENCE,
    MAX_RECOVERY_PAGE_SIZE,
    BlackoutCaptureCursor,
    BlackoutCaptureOpened,
    BlackoutChainKind,
    BlackoutProcessingRef,
    BlackoutProcessingStage,
    BlackoutRecordType,
    BlackoutRecoveryCursor,
    BlackoutRecoveryPage,
    BlackoutRef,
    BlackoutSummary,
    BlackoutSummaryPage,
    BlackoutTailBatch,
    ProfileChainRef,
    RawEvidencePage,
    RecoveredCaptureWork,
    StoredPhysicalRecord,
    StoredRecordRef,
)
from src.battery_math.lut import LutPoint
from src.domain.blackout_capture import (
    BlackoutStart,
    CapturedTelemetry,
    DischargeGap,
    DischargeGapReason,
    DischargeSample,
    DischargeSampleIdentity,
    FrozenModelCapture,
    RawNutToken,
)
from src.domain.blackout_terminal import BlackoutTermination
from src.domain.curve_assessment import CurveDisposition
from src.domain.firmware_lb_assessment import FirmwareLbDisposition
from src.domain.fragments import AnchorKind, AnchorProvenance, EndpointAnchor, ObservationOrigin
from src.domain.ir_learning_decision import IrLearningDisposition
from src.domain.load_sag_assessment import LoadSagDisposition
from src.domain.values import FrozenModelSnapshot, PhysicalObservation

H = "a" * 64
UTC = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)


def _ref(**changes: object) -> BlackoutRef:
    values: dict[str, object] = {"blackout_id": "blackout-1", "segment_id": "segment-1"}
    values.update(changes)
    return BlackoutRef(**cast(Any, values))


def _cursor(**changes: object) -> BlackoutCaptureCursor:
    values: dict[str, object] = {
        "blackout_id": "blackout-1",
        "segment_id": "segment-1",
        "chain": BlackoutChainKind.PHYSICAL,
        "next_sequence": 2,
        "last_record_sha256": H,
    }
    values.update(changes)
    return BlackoutCaptureCursor(**cast(Any, values))


def _sample(sequence: int = 2) -> DischargeSample:
    observation = PhysicalObservation(
        "boot-1", sequence, UTC, "OB DISCHRG", "12.30", 12.3, 0.01, 20.0, 0.0
    )
    captured = CapturedTelemetry(
        observation,
        (
            RawNutToken("battery.voltage", "12.30", "12.30"),
            RawNutToken("input.voltage", "0.00", "0.00"),
            RawNutToken("ups.load", "20.00", "20.00"),
            RawNutToken("ups.status", "OB DISCHRG", "OB DISCHRG"),
        ),
    )
    return DischargeSample.from_telemetry(
        sequence,
        captured,
        DischargeSampleIdentity(
            "blackout-1", "episode-1", "epoch-1", "segment-1", ObservationOrigin.NATURAL
        ),
    )


def _start() -> BlackoutStart:
    snapshot = FrozenModelSnapshot(
        "model-v3",
        "evaluation-v3",
        "epoch-1",
        H,
        7.2,
        12.0,
        510.0,
        1.0,
        1.2,
        0.015,
        0.0,
        (LutPoint(13.7, 1.0, "standard"), LutPoint(10.8, 0.0, "anchor")),
    )
    return BlackoutStart(
        "blackout-1",
        "episode-1",
        "epoch-1",
        "segment-1",
        ObservationOrigin.NATURAL,
        UTC,
        0,
        "boot-1",
        "capture-v3",
        H,
        FrozenModelCapture(snapshot, H),
    )


def _gap() -> DischargeGap:
    return DischargeGap(
        "blackout-1",
        "episode-1",
        "epoch-1",
        "segment-1",
        ObservationOrigin.NATURAL,
        DischargeGapReason.TELEMETRY_REPLY_LOST,
        1,
        "boot-1",
        "boot-1",
        3,
        4,
        "boot-1",
        5,
        UTC,
        UTC,
        UTC,
    )


def _anchor() -> EndpointAnchor:
    return EndpointAnchor(
        H,
        AnchorKind.TRANSFER_TO_BATTERY,
        AnchorProvenance.PHYSICAL,
        "boot-1",
        UTC,
        1,
        None,
        "blackout-1",
        "episode-1",
        "segment-1",
    )


def _record(
    ref: BlackoutRef,
    kind: BlackoutRecordType,
    sequence: int = 2,
    chain: BlackoutChainKind | None = None,
) -> StoredRecordRef:
    if chain is None:
        chain = (
            BlackoutChainKind.PHYSICAL
            if kind
            in {
                BlackoutRecordType.START,
                BlackoutRecordType.SAMPLE,
                BlackoutRecordType.GAP,
                BlackoutRecordType.ANCHOR,
            }
            else BlackoutChainKind.TERMINAL
        )
    return StoredRecordRef(ref, chain, kind, sequence, H, 100)


def _summary(**changes: object) -> BlackoutSummary:
    values: dict[str, object] = {
        "blackout_id": "blackout-1",
        "physical_episode_id": "episode-1",
        "battery_epoch_id": "epoch-1",
        "segment_id": "segment-1",
        "observation_origin": ObservationOrigin.NATURAL,
        "started_at_utc": UTC,
        "ended_at_utc": UTC,
        "termination": BlackoutTermination.POWER_RESTORED,
        "sample_count": 2,
        "gap_count": 1,
        "load_sag_result_hash": H,
        "curve_result_hash": H,
        "firmware_lb_result_hash": H,
        "ir_learning_result_hash": H,
        "load_sag_disposition": LoadSagDisposition.ADMITTED,
        "curve_disposition": CurveDisposition.ADMITTED,
        "firmware_lb_disposition": FirmwareLbDisposition.COMPARABLE,
        "ir_learning_disposition": IrLearningDisposition.NO_CHANGE,
    }
    values.update(changes)
    return BlackoutSummary(**cast(Any, values))


def test_storage_values_are_frozen_and_slot_based() -> None:
    ref = _ref()
    values = (
        ref,
        _cursor(),
        BlackoutCaptureOpened(ref, _cursor()),
        _record(ref, BlackoutRecordType.SAMPLE),
        ProfileChainRef(ref, H, (_record(ref, BlackoutRecordType.PROFILE),)),
        RawEvidencePage(
            ref,
            (
                StoredPhysicalRecord(_record(ref, BlackoutRecordType.SAMPLE), _sample()),
                StoredPhysicalRecord(_record(ref, BlackoutRecordType.GAP, 3), _gap()),
                StoredPhysicalRecord(_record(ref, BlackoutRecordType.ANCHOR, 4), _anchor()),
            ),
            None,
            True,
        ),
        BlackoutProcessingRef(ref, BlackoutProcessingStage.PROCESSING, H, "policy-v1"),
        _summary(),
        BlackoutSummaryPage((_summary(),), None, True),
    )
    for value in values:
        assert not hasattr(value, "__dict__")
        assert fields(value)
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, None)


def test_chain_sequence_and_physical_record_bounds_are_named_and_strict() -> None:
    ref = _ref()
    with pytest.raises(TypeError, match="BlackoutChainKind"):
        _cursor(chain="physical")
    assert _cursor(next_sequence=MAX_CHAIN_SEQUENCE).next_sequence == MAX_CHAIN_SEQUENCE
    exhausted = _cursor(next_sequence=None)
    assert exhausted.next_sequence is None
    assert _record(ref, BlackoutRecordType.END, MAX_CHAIN_SEQUENCE).sequence == MAX_CHAIN_SEQUENCE
    with pytest.raises((TypeError, ValueError)):
        _cursor(next_sequence=MAX_CHAIN_SEQUENCE + 1)
    with pytest.raises((TypeError, ValueError)):
        _record(ref, BlackoutRecordType.END, MAX_CHAIN_SEQUENCE + 1)
    with pytest.raises(ValueError):
        StoredRecordRef(
            ref,
            BlackoutChainKind.PHYSICAL,
            BlackoutRecordType.SAMPLE,
            1,
            H,
            20 * 1024 + 1,
        )


def test_exhausted_cursor_requires_the_final_record_hash() -> None:
    with pytest.raises(ValueError, match="last record hash"):
        _cursor(next_sequence=None, last_record_sha256=None)
    with pytest.raises(ValueError, match="initial state"):
        _cursor(next_sequence=0, last_record_sha256=H)
    with pytest.raises(ValueError, match="prior record hash"):
        _cursor(next_sequence=2, last_record_sha256=None)


def test_raw_page_contains_typed_records_and_only_immutable_refs() -> None:
    ref = _ref()
    page = RawEvidencePage(
        ref,
        (
            StoredPhysicalRecord(_record(ref, BlackoutRecordType.SAMPLE), _sample()),
            StoredPhysicalRecord(_record(ref, BlackoutRecordType.GAP, 3), _gap()),
            StoredPhysicalRecord(_record(ref, BlackoutRecordType.ANCHOR, 4), _anchor()),
        ),
        _cursor(next_sequence=5),
        False,
    )
    assert not any(isinstance(field.type, type) and field.type is bytes for field in fields(page))
    with pytest.raises(TypeError):
        RawEvidencePage(ref, cast(Any, (b"wire\n",)), None, True)


def test_raw_page_preserves_physical_records_in_exact_chain_order() -> None:
    ref = _ref()
    page = RawEvidencePage(
        ref,
        (
            StoredPhysicalRecord(_record(ref, BlackoutRecordType.START, 0), _start()),
            StoredPhysicalRecord(_record(ref, BlackoutRecordType.SAMPLE, 1), _sample(2)),
            StoredPhysicalRecord(_record(ref, BlackoutRecordType.ANCHOR, 2), _anchor()),
        ),
        None,
        True,
    )
    assert [item.ref.record_type for item in page.records] == [
        BlackoutRecordType.START,
        BlackoutRecordType.SAMPLE,
        BlackoutRecordType.ANCHOR,
    ]


def test_raw_page_rejects_duplicate_reverse_and_skipped_sequences() -> None:
    ref = _ref()
    sample = _sample()
    for sequences in ((2, 2), (3, 2), (2, 4)):
        records = tuple(
            StoredPhysicalRecord(_record(ref, BlackoutRecordType.SAMPLE, sequence), sample)
            for sequence in sequences
        )
        with pytest.raises(ValueError, match="contiguous"):
            RawEvidencePage(ref, records, None, True)


def test_raw_page_requires_cursor_to_follow_final_sequence_and_hash() -> None:
    ref = _ref()
    record = StoredPhysicalRecord(_record(ref, BlackoutRecordType.SAMPLE, 2), _sample())
    with pytest.raises(ValueError, match="follow"):
        RawEvidencePage(ref, (record,), _cursor(next_sequence=4), False)
    with pytest.raises(ValueError, match="hash"):
        RawEvidencePage(
            ref,
            (record,),
            _cursor(next_sequence=3, last_record_sha256="b" * 64),
            False,
        )


def test_physical_and_terminal_pages_have_independent_cursors() -> None:
    ref = _ref()
    physical_cursor = _cursor(
        chain=BlackoutChainKind.PHYSICAL, next_sequence=3, last_record_sha256=H
    )
    physical_page = RawEvidencePage(
        ref,
        (
            StoredPhysicalRecord(_record(ref, BlackoutRecordType.START, 0), _start()),
            StoredPhysicalRecord(_record(ref, BlackoutRecordType.SAMPLE, 1), _sample(1)),
            StoredPhysicalRecord(_record(ref, BlackoutRecordType.ANCHOR, 2), _anchor()),
        ),
        physical_cursor,
        False,
    )
    terminal_root = _cursor(
        chain=BlackoutChainKind.TERMINAL, next_sequence=0, last_record_sha256=None
    )
    terminal_page = RawEvidencePage(ref, (), terminal_root, False)
    assert physical_page.next_cursor is physical_cursor
    assert physical_page.next_cursor is not None
    assert physical_page.next_cursor.chain is BlackoutChainKind.PHYSICAL
    assert terminal_page.next_cursor is terminal_root
    assert terminal_page.next_cursor is not None
    assert terminal_page.next_cursor.chain is BlackoutChainKind.TERMINAL


def test_raw_page_rejects_mixed_chain_cursor_and_nonroot_terminal_page() -> None:
    ref = _ref()
    sample = StoredPhysicalRecord(_record(ref, BlackoutRecordType.SAMPLE, 2), _sample())
    terminal_root = _cursor(
        chain=BlackoutChainKind.TERMINAL, next_sequence=0, last_record_sha256=None
    )
    with pytest.raises(ValueError, match="one chain"):
        RawEvidencePage(ref, (sample,), terminal_root, False)

    terminal_non_root = _cursor(
        chain=BlackoutChainKind.TERMINAL, next_sequence=1, last_record_sha256=H
    )
    with pytest.raises(ValueError, match="initial cursor"):
        RawEvidencePage(ref, (), terminal_non_root, False)


def test_record_refs_reject_wrong_chain_and_profile_mixing() -> None:
    ref = _ref()
    assert _record(ref, BlackoutRecordType.ANCHOR, chain=BlackoutChainKind.TERMINAL).chain is (
        BlackoutChainKind.TERMINAL
    )
    assert _record(ref, BlackoutRecordType.END, chain=BlackoutChainKind.TERMINAL).chain is (
        BlackoutChainKind.TERMINAL
    )
    with pytest.raises(ValueError, match="terminal chain"):
        _record(ref, BlackoutRecordType.END, chain=BlackoutChainKind.PHYSICAL)
    with pytest.raises(ValueError, match="terminal chain"):
        _record(ref, BlackoutRecordType.PROFILE, chain=BlackoutChainKind.PHYSICAL)
    with pytest.raises(ValueError, match="physical chain"):
        _record(ref, BlackoutRecordType.SAMPLE, chain=BlackoutChainKind.TERMINAL)

    terminal_profile = _record(ref, BlackoutRecordType.PROFILE)
    physical_sample = _record(ref, BlackoutRecordType.SAMPLE)
    with pytest.raises(ValueError, match="terminal chain"):
        ProfileChainRef(ref, H, (terminal_profile, physical_sample))


def test_capture_open_and_recovery_require_physical_cursor() -> None:
    ref = _ref()
    terminal_root = _cursor(
        chain=BlackoutChainKind.TERMINAL, next_sequence=0, last_record_sha256=None
    )
    with pytest.raises(ValueError, match="physical chain"):
        BlackoutCaptureOpened(ref, terminal_root)
    with pytest.raises(ValueError, match="physical chain"):
        RecoveredCaptureWork(ref, terminal_root)


def test_raw_page_empty_semantics_are_initial_incomplete_or_complete_only() -> None:
    ref = _ref()
    assert RawEvidencePage(ref, (), None, True).records == ()
    assert (
        RawEvidencePage(ref, (), _cursor(next_sequence=0, last_record_sha256=None), False).records
        == ()
    )
    with pytest.raises(ValueError, match="initial cursor"):
        RawEvidencePage(ref, (), _cursor(), False)


def test_recovery_page_is_bounded_repeatable_and_supports_multiple_work_items() -> None:
    ref_a = _ref(blackout_id="a")
    ref_b = _ref(blackout_id="b")
    processing = (
        BlackoutProcessingRef(ref_a, BlackoutProcessingStage.PROCESSING, H, "policy-v1"),
        BlackoutProcessingRef(ref_b, BlackoutProcessingStage.TAIL, H, "policy-v1"),
    )
    page = BlackoutRecoveryPage(
        RecoveredCaptureWork(_ref(), _cursor()),
        processing,
        BlackoutRecoveryCursor(2, True),
        False,
    )
    assert page.active_capture is not None
    assert len(page.processing) == 2
    assert page.next_cursor is not None
    assert page.next_cursor.processing_offset == 2
    with pytest.raises(ValueError):
        BlackoutRecoveryPage(None, processing, None, False)
    with pytest.raises(ValueError):
        BlackoutRecoveryPage(None, processing, BlackoutRecoveryCursor(0, False), True)
    too_many = processing * (MAX_RECOVERY_PAGE_SIZE // len(processing) + 1)
    with pytest.raises(ValueError):
        BlackoutRecoveryPage(None, too_many, None, True)


def test_close_processing_ref_and_tail_command_are_typed_not_wire_values() -> None:
    ref = _ref()
    result = BlackoutProcessingRef(ref, BlackoutProcessingStage.TAIL, H, "policy-v1")
    assert result.ref == ref
    with pytest.raises(TypeError):
        BlackoutTailBatch(
            cast(Any, (b"jsonl",)), (), (), (), cast(Any, None), None, cast(Any, None), ()
        )


def test_summary_requires_hash_disposition_pairings_and_ended_termination_pairing() -> None:
    with pytest.raises(ValueError):
        _summary(curve_result_hash=None)
    with pytest.raises(ValueError):
        _summary(ir_learning_disposition=None)
    with pytest.raises(ValueError):
        _summary(ended_at_utc=None)
    with pytest.raises(ValueError):
        _summary(termination=None)
    with pytest.raises(ValueError):
        _summary(ended_at_utc=datetime(2026, 8, 15, tzinfo=timezone.utc))


def test_summary_page_cursor_coheres_with_completion() -> None:
    with pytest.raises(ValueError):
        BlackoutSummaryPage((_summary(),), "next", True)
    with pytest.raises(ValueError):
        BlackoutSummaryPage((_summary(),), None, False)
