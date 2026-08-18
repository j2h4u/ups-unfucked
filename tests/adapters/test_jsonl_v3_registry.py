"""Targeted tests for the private v3 registry value/codec boundary."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from src.adapters.jsonl_v3_errors import (
    V3AppendConflict,
    V3CorruptionError,
    V3PathError,
    V3TransactionClosed,
    V3ValidationError,
    V3WriterOwnershipError,
)
from src.adapters.jsonl_v3_filesystem import JsonlV3Filesystem
from src.adapters.jsonl_v3_registry import (
    JsonlV3WorkRegistry,
    V3WorkRegistry,
    canonical_registry_bytes,
    decode_registry,
    empty_registry,
)
from src.adapters.jsonl_v3_registry_values import (
    REGISTRY_SCHEMA,
    CapturingState,
    PreparingCaptureState,
    ProcessingState,
    TailState,
    V3AppendIntent,
    V3DamageContinuation,
    V3LastAppend,
    V3RolloverReservation,
    V3StorageSegmentReceipt,
    V3TailRecordReceipt,
)
from src.adapters.jsonl_v3_storage_paths import DIRECTORY_MODE, V3StoragePaths
from src.application.blackout_storage_values import BlackoutCaptureCursor, BlackoutChainKind

HASH = "a" * 64


def _cursor(chain: str, sequence: int, digest: str) -> BlackoutCaptureCursor:
    return BlackoutCaptureCursor(BLACKOUT, LOGICAL, BlackoutChainKind(chain), sequence, digest)


RegistryState = PreparingCaptureState | CapturingState | ProcessingState | TailState


class _Lease:
    def __init__(self, root: Path) -> None:
        self.state_root_identity = (root.stat().st_dev, root.stat().st_ino)

    def validate(self, state_root: Path) -> None:
        del state_root

    @contextmanager
    def hold(self):
        yield self


def _filesystem(tmp_path: Path) -> JsonlV3Filesystem:
    tmp_path.mkdir()
    tmp_path.chmod(DIRECTORY_MODE)
    filesystem = JsonlV3Filesystem(tmp_path, writer_lease=_Lease(tmp_path))
    filesystem.ensure_layout()
    return filesystem


BLACKOUT = "00000000-0000-4000-8000-000000000001".replace("-", "")
LOGICAL = "00000000-0000-4000-8000-000000000002".replace("-", "")
STORAGE = "00000000-0000-4000-8000-000000000003".replace("-", "")
STARTED = "2026-08-18T12:34:56.123456Z"


def _preparing() -> PreparingCaptureState:
    paths = V3StoragePaths.__new__(V3StoragePaths)
    segment = paths.segment_token(STARTED, BLACKOUT, LOGICAL, 0, STORAGE)
    return PreparingCaptureState(
        "preparing",
        BLACKOUT,
        LOGICAL,
        STORAGE,
        segment,
        paths.offset_token(segment),
        '{"type":"blackout_start"}\n',
        hashlib.sha256(b'{"type":"blackout_start"}\n').hexdigest(),
        len('{"type":"blackout_start"}\n'.encode()),
        STARTED,
        "policy-v1",
    )


def _capturing() -> CapturingState:
    return CapturingState(
        "capturing",
        BLACKOUT,
        LOGICAL,
        "00000000-0000-4000-8000-000000000004".replace("-", ""),
        "00000000-0000-4000-8000-000000000005".replace("-", ""),
        "monitor",
        None,
        "policy-v1",
        _cursor("physical", 1, HASH),
        None,
        28,
        1,
        0,
        0,
        (),
        None,
        None,
        None,
        None,
    )


def _processing() -> ProcessingState:
    return ProcessingState(
        "processing",
        BLACKOUT,
        LOGICAL,
        "00000000-0000-4000-8000-000000000004".replace("-", ""),
        "00000000-0000-4000-8000-000000000005".replace("-", ""),
        "monitor",
        None,
        "policy-v1",
        _cursor("physical", 1, HASH),
        _cursor("terminal", 2, HASH),
        HASH,
        HASH,
        HASH,
        28,
        1,
        0,
        0,
        (),
        None,
    )


def _tail() -> TailState:
    records = _tail_records()
    return TailState(
        "tail",
        BLACKOUT,
        LOGICAL,
        "00000000-0000-4000-8000-000000000004".replace("-", ""),
        "00000000-0000-4000-8000-000000000005".replace("-", ""),
        "monitor",
        None,
        "policy-v1",
        _cursor("physical", 1, HASH),
        _cursor("terminal", 3, HASH),
        HASH,
        HASH,
        HASH,
        HASH,
        28,
        1,
        0,
        0,
        (),
        V3StoragePaths.__new__(V3StoragePaths).terminal_staging_token(BLACKOUT),
        len(records),
        HASH,
        records,
        None,
    )


def _tail_records(
    *, profile_count: int = 1, with_model_commit: bool = False
) -> tuple[V3TailRecordReceipt, ...]:
    types = ["fragment_profile"] * profile_count
    types.extend(
        [
            "load_sag_assessment_summary",
            "curve_assessment_summary",
            "firmware_lb_assessment_summary",
            "learning_decision",
        ]
    )
    if with_model_commit:
        types.append("ir_model_commit_receipt")
    types.append("terminal_outcome")
    return tuple(V3TailRecordReceipt(i, 1, HASH, kind) for i, kind in enumerate(types))


def _registry_for(state: RegistryState) -> V3WorkRegistry:
    if isinstance(state, (PreparingCaptureState, CapturingState)):
        return V3WorkRegistry(state, ())
    return V3WorkRegistry(None, (state,))


def test_empty_registry_has_exact_canonical_wire() -> None:
    raw = canonical_registry_bytes(empty_registry())
    assert raw == b'{"capture":null,"pending":[],"schema":"v3-blackout-work-registry-v1"}\n'
    assert decode_registry(raw) == empty_registry()


@pytest.mark.parametrize("state", [_preparing(), _capturing(), _processing(), _tail()])
def test_typed_variant_roundtrip_and_nested_values_are_frozen(state: RegistryState) -> None:
    registry = _registry_for(state)
    decoded = V3WorkRegistry.from_wire(json.loads(canonical_registry_bytes(registry.to_wire())))
    assert decoded == registry
    target = decoded.capture if decoded.capture is not None else decoded.pending[0]
    with pytest.raises((FrozenInstanceError, AttributeError)):
        setattr(target, "blackout_id", LOGICAL)


def test_unknown_missing_and_duplicate_fields_fail_closed() -> None:
    wire = {"capture": None, "pending": [], "schema": REGISTRY_SCHEMA}
    wire["extra"] = True
    with pytest.raises(V3ValidationError):
        V3WorkRegistry.from_wire(wire)
    wire.pop("extra")
    wire.pop("pending")
    with pytest.raises(V3ValidationError):
        V3WorkRegistry.from_wire(wire)
    duplicate = (
        b'{"capture":null,"capture":null,"pending":[],"schema":"v3-blackout-work-registry-v1"}\n'
    )
    with pytest.raises(V3CorruptionError):
        decode_registry(duplicate)


def test_malformed_nested_cursor_is_typed_validation() -> None:
    wire = V3WorkRegistry(_capturing(), ()).to_wire()
    del wire["capture"]["physical_cursor"]["segment_id"]
    with pytest.raises(V3ValidationError):
        V3WorkRegistry.from_wire(wire)


def test_capturing_terminal_cursor_and_append_intent_valid_paths() -> None:
    line = "x\n"
    digest = hashlib.sha256(line.encode()).hexdigest()
    terminal = _cursor("terminal", 1, HASH)
    intent = V3AppendIntent("terminal", "append", 1, HASH, None, 0, line, digest, 2, HASH)
    state = replace(_capturing(), terminal_cursor=terminal, append_intent=intent)
    V3WorkRegistry(state, ()).to_wire()


def test_capturing_physical_append_and_last_append_valid_paths() -> None:
    line = "x\n"
    digest = hashlib.sha256(line.encode()).hexdigest()
    intent = V3AppendIntent("physical", "append", 1, HASH, 0, 0, line, digest, 2, HASH)
    last = V3LastAppend("append", HASH, digest, HASH)
    state = replace(_capturing(), append_intent=intent, last_append=last)
    V3WorkRegistry(state, ()).to_wire()


def test_wire_mutation_does_not_mutate_typed_state() -> None:
    registry = V3WorkRegistry(_preparing(), ())
    wire = registry.to_wire()
    wire["capture"]["start_line_utf8"] = "tampered\n"
    capture = registry.capture
    assert isinstance(capture, PreparingCaptureState)
    assert capture.start_line_utf8 != wire["capture"]["start_line_utf8"]


def test_pending_bound_is_eight() -> None:
    state = _processing()
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(None, (state,) * 9).to_wire()


def test_storage_segment_bound_is_sixty_four() -> None:
    paths = V3StoragePaths.__new__(V3StoragePaths)
    token = paths.segment_token(STARTED, BLACKOUT, LOGICAL, 0, STORAGE)
    segment = V3StorageSegmentReceipt(
        0, STORAGE, token, paths.offset_token(token), 1, 0, 0, HASH, None, False
    )
    segments = (segment,) * 65
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(None, (replace(_processing(), storage_segments=segments),)).to_wire()


def test_cross_bound_token_and_terminal_relations_fail() -> None:
    preparing = _preparing()
    other = replace(preparing.path_token, logical_segment_id=BLACKOUT)
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(replace(preparing, path_token=other), ()).to_wire()
    processing = _processing()
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(None, (replace(processing, terminal_end_sha256="b" * 64),)).to_wire()
    tail = _tail()
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(None, (replace(tail, terminal_outcome_sha256="b" * 64),)).to_wire()
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(
            replace(
                preparing,
                offset_token=replace(
                    preparing.offset_token, started_utc="2026-08-18T12:34:57.123456Z"
                ),
            ),
            (),
        ).to_wire()


def test_append_intent_mutations_fail_closed() -> None:
    line = '{"x":1}\n'
    line_hash = hashlib.sha256(line.encode()).hexdigest()
    intent = V3AppendIntent(
        "physical", "sample", 1, HASH, 0, 0, line, line_hash, len(line.encode()), HASH
    )
    cases = (
        replace(intent, chain="unknown"),
        replace(intent, operation=""),
        replace(intent, storage_ordinal=None),
        replace(intent, storage_ordinal=-1),
        replace(intent, expected_seq=2),
        replace(intent, expected_cursor_sha256="b"),
        replace(intent, line_length=1),
        replace(intent, line_sha256="b" * 64),
    )
    for mutated in cases:
        with pytest.raises(V3ValidationError):
            V3WorkRegistry(replace(_capturing(), append_intent=mutated), ()).to_wire()


def test_damaged_segment_receipt_roundtrips_typed_pair() -> None:
    paths = V3StoragePaths.__new__(V3StoragePaths)
    damaged, damaged_offset = paths.damaged_tokens(BLACKOUT, LOGICAL, 0, STORAGE, HASH)
    segment = V3StorageSegmentReceipt(
        0, STORAGE, damaged, damaged_offset, 10, 0, 0, HASH, HASH, False
    )
    registry = V3WorkRegistry(None, (replace(_processing(), storage_segments=(segment,)),))
    decoded = V3WorkRegistry.from_wire(json.loads(canonical_registry_bytes(registry.to_wire())))
    receipt = decoded.pending[0].storage_segments[0]
    assert receipt.path_token == damaged
    assert receipt.offset_token == damaged_offset
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(
            None,
            (
                replace(
                    _processing(),
                    storage_segments=(
                        replace(
                            segment, offset_token=replace(damaged_offset, file_sha256="b" * 64)
                        ),
                    ),
                ),
            ),
        ).to_wire()


def test_damage_continuation_binds_all_file_tokens_and_receipt_hash() -> None:
    paths = V3StoragePaths.__new__(V3StoragePaths)
    new_storage = "00000000-0000-4000-8000-000000000006".replace("-", "")
    old = paths.segment_token(STARTED, BLACKOUT, LOGICAL, 0, STORAGE)
    old_offset = paths.offset_token(old)
    damaged, damaged_offset = paths.damaged_tokens(BLACKOUT, LOGICAL, 0, STORAGE, HASH)
    continuation = V3DamageContinuation(
        "reserved",
        BLACKOUT,
        LOGICAL,
        STORAGE,
        new_storage,
        0,
        1,
        old,
        old_offset,
        damaged,
        damaged_offset,
        10,
        0,
        HASH,
        HASH,
        "gap\n",
        hashlib.sha256(b"gap\n").hexdigest(),
        4,
        1,
        HASH,
    )
    V3WorkRegistry(replace(_capturing(), damage_continuation=continuation), ()).to_wire()
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(
            replace(
                _capturing(),
                damage_continuation=replace(continuation, damaged_file_sha256="b" * 64),
            ),
            (),
        ).to_wire()
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(
            replace(
                _capturing(),
                damage_continuation=replace(continuation, old_storage_id=new_storage),
            ),
            (),
        ).to_wire()
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(
            replace(
                _capturing(),
                damage_continuation=replace(continuation, new_ordinal=0),
            ),
            (),
        ).to_wire()


def test_rollover_binds_old_and_successor_tokens() -> None:
    paths = V3StoragePaths.__new__(V3StoragePaths)
    successor_blackout = "00000000-0000-4000-8000-000000000007".replace("-", "")
    successor_logical = "00000000-0000-4000-8000-000000000008".replace("-", "")
    successor_storage = "00000000-0000-4000-8000-000000000009".replace("-", "")
    old = paths.segment_token(STARTED, BLACKOUT, LOGICAL, 0, STORAGE)
    successor = paths.segment_token(
        STARTED, successor_blackout, successor_logical, 0, successor_storage
    )
    rollover = V3RolloverReservation(
        "reserved",
        "bytes",
        BLACKOUT,
        LOGICAL,
        "00000000-0000-4000-8000-000000000004".replace("-", ""),
        STORAGE,
        old,
        successor_blackout,
        successor_logical,
        successor_storage,
        successor,
        "start\n",
        "end\n",
        "size_rollover",
        hashlib.sha256(b"start\n").hexdigest(),
        hashlib.sha256(b"end\n").hexdigest(),
        6,
        4,
    )
    V3WorkRegistry(replace(_capturing(), rollover=rollover), ()).to_wire()
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(
            replace(_capturing(), rollover=replace(rollover, successor_storage_id=STORAGE)),
            (),
        ).to_wire()
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(
            replace(
                _capturing(),
                rollover=replace(rollover, successor_path_token=replace(successor, ordinal=1)),
            ),
            (),
        ).to_wire()
    rebound_blackout = replace(successor, blackout_id=BLACKOUT)
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(
            replace(
                _capturing(),
                rollover=replace(
                    rollover,
                    successor_blackout_id=BLACKOUT,
                    successor_path_token=rebound_blackout,
                ),
            ),
            (),
        ).to_wire()
    rebound_logical = replace(successor, logical_segment_id=LOGICAL)
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(
            replace(
                _capturing(),
                rollover=replace(
                    rollover,
                    successor_logical_segment_id=LOGICAL,
                    successor_path_token=rebound_logical,
                ),
            ),
            (),
        ).to_wire()


def test_tail_receipts_must_be_contiguous_and_within_tail_length() -> None:
    records = _tail_records()
    receipt = records[0]
    state = replace(_tail(), tail_length=len(records), tail_records=records)
    V3WorkRegistry(None, (state,)).to_wire()
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(
            None,
            (replace(state, tail_records=(replace(receipt, offset=1),) + records[1:]),),
        ).to_wire()
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(
            None,
            (replace(state, tail_records=(replace(receipt, type="unknown"),) + records[1:]),),
        ).to_wire()
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(
            None,
            (replace(state, tail_records=records[:-1] + (replace(records[-1], hash="b" * 64),)),),
        ).to_wire()


def test_tail_allows_profile_boundaries_but_rejects_later_reordering() -> None:
    two_profiles = _tail_records(profile_count=2)
    two = replace(
        _tail(),
        tail_length=len(two_profiles),
        tail_records=two_profiles,
    )
    V3WorkRegistry(None, (two,)).to_wire()
    profiles = _tail_records(profile_count=96)
    ninety_six = replace(
        _tail(),
        tail_length=len(profiles),
        tail_records=profiles,
    )
    V3WorkRegistry(None, (ninety_six,)).to_wire()
    invalid = two_profiles[:-3] + (
        V3TailRecordReceipt(2, 1, HASH, "curve_assessment_summary"),
        V3TailRecordReceipt(3, 1, HASH, "load_sag_assessment_summary"),
        V3TailRecordReceipt(4, 1, HASH, "firmware_lb_assessment_summary"),
        V3TailRecordReceipt(5, 1, HASH, "learning_decision"),
        V3TailRecordReceipt(6, 1, HASH, "terminal_outcome"),
    )
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(
            None, (replace(_tail(), tail_length=len(invalid), tail_records=invalid),)
        ).to_wire()


def test_tail_exact_cardinality_with_optional_model_commit() -> None:
    for records in (_tail_records(), _tail_records(with_model_commit=True)):
        V3WorkRegistry(
            None, (replace(_tail(), tail_length=len(records), tail_records=records),)
        ).to_wire()
    old_literal = _tail_records(with_model_commit=True)
    old_literal = tuple(
        replace(record, type="model_commit_receipt")
        if record.type == "ir_model_commit_receipt"
        else record
        for record in old_literal
    )
    with pytest.raises(V3ValidationError):
        V3WorkRegistry(
            None, (replace(_tail(), tail_length=len(old_literal), tail_records=old_literal),)
        ).to_wire()
    for records in (
        _tail_records(profile_count=0),
        _tail_records(profile_count=97),
    ):
        with pytest.raises(V3ValidationError):
            V3WorkRegistry(
                None, (replace(_tail(), tail_length=len(records), tail_records=records),)
            ).to_wire()
    canonical = _tail_records()
    for index in range(1, 5):
        omitted = canonical[:index] + canonical[index + 1 :]
        with pytest.raises(V3ValidationError):
            V3WorkRegistry(
                None, (replace(_tail(), tail_length=len(omitted), tail_records=omitted),)
            ).to_wire()


def test_recovery_page_is_bounded_and_emits_active_once() -> None:
    from src.adapters.jsonl_v3_registry import recovery_page

    registry = V3WorkRegistry(_capturing(), (_processing(), _tail()))
    first = recovery_page(registry, processing_offset=0, limit=1)
    assert first.active_capture == registry.capture
    assert first.pending == ()
    second = recovery_page(registry, processing_offset=0, limit=32, active_capture_emitted=True)
    assert second.active_capture is None
    assert len(second.pending) == 2
    with pytest.raises(V3ValidationError):
        recovery_page(registry, processing_offset=0, limit=33)


def test_transaction_scoped_open_cas_and_closed_or_foreign_rejection(tmp_path: Path) -> None:
    filesystem = _filesystem(tmp_path / "one")
    registry = JsonlV3WorkRegistry(filesystem)
    with filesystem.write_transaction() as tx:
        snapshot = registry.open_or_create(tx)
        replacement = V3WorkRegistry(_preparing(), ())
        updated = registry.compare_and_replace(tx, expected=snapshot, replacement=replacement)
        assert updated.state == replacement
        with pytest.raises(V3AppendConflict):
            registry.compare_and_replace(
                tx, expected=snapshot, replacement=V3WorkRegistry(None, ())
            )
        with pytest.raises(V3AppendConflict):
            registry.compare_and_replace(
                tx,
                expected=replace(snapshot, canonical_sha256="b" * 64),
                replacement=V3WorkRegistry(None, ()),
            )
    with pytest.raises(V3TransactionClosed):
        registry.read(tx)
    foreign = _filesystem(tmp_path / "two")
    with foreign.write_transaction() as foreign_tx:
        with pytest.raises(V3WriterOwnershipError):
            registry.read(foreign_tx)


def test_open_or_create_does_not_replace_unsafe_registry_path(tmp_path: Path) -> None:
    filesystem = _filesystem(tmp_path / "unsafe")
    registry = JsonlV3WorkRegistry(filesystem)
    assert filesystem.paths is not None
    target = filesystem.paths.blackouts / "outside"
    target.write_bytes(b"sentinel")
    registry_path = filesystem.paths.blackouts / "work-registry-v1.json"
    os.symlink(target, registry_path)
    with filesystem.write_transaction() as tx:
        with pytest.raises(V3PathError):
            registry.open_or_create(tx)
    assert target.read_bytes() == b"sentinel"
