"""Public capture-facade contract smoke tests."""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.adapters import jsonl_v3_capture_append
from src.adapters.jsonl_v3_blackout_start_codec import encode_blackout_start
from src.adapters.jsonl_v3_canonical import decode_v3_record
from src.adapters.jsonl_v3_capture_store import JsonlV3CaptureStore
from src.adapters.jsonl_v3_discharge_sample_codec import encode_discharge_sample
from src.adapters.jsonl_v3_errors import V3AppendConflict, V3CapacityError, V3ValidationError
from src.adapters.jsonl_v3_evidence_store import (
    ActiveRegistryEvidenceSnapshotProvider,
    JsonlV3EvidenceStore,
    JsonlV3FilesystemEvidenceOffsetReader,
    JsonlV3FilesystemEvidenceReader,
)
from src.adapters.jsonl_v3_filesystem import JsonlV3Filesystem
from src.adapters.jsonl_v3_registry import RegistrySnapshot, V3WorkRegistry
from src.adapters.jsonl_v3_registry_values import (
    PreparingCaptureState,
    V3AppendIntent,
    V3StorageSegmentReceipt,
)
from src.adapters.jsonl_v3_terminal_tail_codec import encode_endpoint_anchor
from src.application.blackout_storage_values import (
    BlackoutCaptureCursor,
    BlackoutChainKind,
    BlackoutProcessingStage,
    RecoveredCaptureWork,
)
from src.application.v3_active_capture_session import V3ActiveCaptureSession
from src.battery_math.lut import LutPoint
from src.domain.blackout_capture import (
    BlackoutStart,
    CapturedTelemetry,
    DischargeSample,
    DischargeSampleIdentity,
    FrozenModelCapture,
    RawNutToken,
)
from src.domain.blackout_terminal import (
    BlackoutEnd,
    BlackoutTermination,
    BudgetKind,
    ContinuationKind,
)
from src.domain.fragments import (
    AnchorKind,
    AnchorProvenance,
    EndpointAnchor,
    ObservationOrigin,
    ReadinessProvenance,
    StartReadinessContext,
)
from src.domain.values import FrozenModelSnapshot, PhysicalObservation


class _Lease:
    def __init__(self, root: Path) -> None:
        self.state_root_identity = (root.stat().st_dev, root.stat().st_ino)

    def validate(self, root: Path) -> None:
        if (root.stat().st_dev, root.stat().st_ino) != self.state_root_identity:
            raise RuntimeError("root changed")

    @contextmanager
    def hold(self):
        yield self


def _id(number: int) -> str:
    return f"{number:08x}000040008000000000000000"


def _store(tmp_path: Path, fault=None) -> JsonlV3CaptureStore:
    os.chmod(tmp_path, 0o700)
    filesystem = JsonlV3Filesystem(tmp_path, writer_lease=_Lease(tmp_path), fault_hook=fault)
    filesystem.ensure_layout()
    return JsonlV3CaptureStore(filesystem)


def _start() -> BlackoutStart:
    blackout, segment, epoch = _id(1), _id(2), _id(3)
    digest = "a" * 64
    snapshot = FrozenModelSnapshot(
        "model-v3",
        "evaluation-v3",
        epoch,
        digest,
        7.2,
        12.0,
        510.0,
        1.0,
        1.2,
        0.015,
        0.0,
        (LutPoint(13.7, 1.0, "standard"),),
    )
    return BlackoutStart(
        blackout,
        _id(4),
        epoch,
        segment,
        ObservationOrigin.NATURAL,
        datetime(2026, 8, 20, tzinfo=timezone.utc),
        1,
        "boot",
        "capture-v3",
        digest,
        FrozenModelCapture(snapshot, "b" * 64),
        StartReadinessContext(True, "known_full", ReadinessProvenance.PHYSICAL),
    )


def _sample(start: BlackoutStart, load: str = "20.0") -> DischargeSample:
    observation = PhysicalObservation(
        start.boot_id,
        2,
        start.wall_time_utc,
        "OB DISCHRG",
        "12.30",
        12.3,
        0.01,
        float(load),
        0.0,
    )
    telemetry = CapturedTelemetry(
        observation,
        (
            RawNutToken("battery.voltage", "12.30", "12.30"),
            RawNutToken("input.voltage", "0", "0"),
            RawNutToken("ups.load", load, load),
            RawNutToken("ups.status", "OB DISCHRG", "OB DISCHRG"),
        ),
    )
    return DischargeSample.from_telemetry(
        0,
        telemetry,
        DischargeSampleIdentity(
            start.blackout_id,
            start.physical_episode_id,
            start.battery_epoch_id,
            start.segment_id,
            start.observation_origin,
        ),
    )


def test_capture_store_exposes_the_wave2_port() -> None:
    for name in (
        "open",
        "append_sample",
        "append_gap",
        "append_anchor",
        "rollover",
        "close",
        "recover",
    ):
        assert callable(getattr(JsonlV3CaptureStore, name))


def test_recover_rejects_an_unbounded_page_before_touching_storage() -> None:
    store = object.__new__(JsonlV3CaptureStore)
    with pytest.raises(V3ValidationError):
        store.recover(limit=33)


def test_registry_first_start_and_exact_open_retry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    assert store.open(start) == opened
    page = store.recover(limit=1)
    assert page.active_capture is not None
    assert page.active_capture.cursor == opened.cursor


def test_session_rollover_preserves_sample_at_byte_threshold(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    sample = _sample(start)
    encoded = encode_discharge_sample(
        sample,
        seq=opened.cursor.next_sequence or 0,
        previous_record_sha256=opened.cursor.last_record_sha256,
    )
    with store.filesystem.write_transaction() as tx:
        state = store._registry.read(tx).state.capture
        assert state is not None
        threshold = state.capture_bytes + len(encoded.line) - 1
    original_limit = jsonl_v3_capture_append.MAX_PHYSICAL_CAPTURE_BYTES
    monkeypatch.setattr(jsonl_v3_capture_append, "MAX_PHYSICAL_CAPTURE_BYTES", threshold)
    original_rollover = store.rollover

    def rollover_then_restore(*args, **kwargs):
        successor = original_rollover(*args, **kwargs)
        monkeypatch.setattr(jsonl_v3_capture_append, "MAX_PHYSICAL_CAPTURE_BYTES", original_limit)
        return successor

    monkeypatch.setattr(store, "rollover", rollover_then_restore)
    session = V3ActiveCaptureSession(store)
    session.attach(opened)
    session.append_sample(sample)

    assert session.ref is not None
    assert session.ref != opened.ref
    page = store.recover(limit=1)
    assert page.active_capture is not None
    assert page.active_capture.ref == session.ref
    assert page.active_capture.cursor == session.physical_cursor
    _assert_successor_sample_evidence(store, session, sample, opened.ref)


def test_session_rollover_preserves_sample_after_sequence_exhaustion(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    exhausted = BlackoutCaptureCursor(
        opened.ref.blackout_id,
        opened.ref.segment_id,
        BlackoutChainKind.PHYSICAL,
        None,
        opened.cursor.last_record_sha256,
    )
    with store.filesystem.write_transaction() as tx:
        snapshot = store._registry.read(tx)
        state = snapshot.state.capture
        assert state is not None
        store._registry.compare_and_replace(
            tx,
            expected=snapshot,
            replacement=V3WorkRegistry(
                replace(state, physical_cursor=exhausted), snapshot.state.pending
            ),
        )

    session = V3ActiveCaptureSession(store)
    session.attach_recovered(RecoveredCaptureWork(opened.ref, exhausted, None))
    accepted = _sample(start)
    session.append_sample(accepted)

    assert session.ref is not None
    assert session.ref != opened.ref
    assert session.physical_cursor is not None
    assert session.physical_cursor.next_sequence == 2
    _assert_successor_sample_evidence(store, session, accepted, opened.ref)


def _assert_successor_sample_evidence(
    store: JsonlV3CaptureStore,
    session: V3ActiveCaptureSession,
    accepted: DischargeSample,
    predecessor: object,
) -> None:
    assert session.ref is not None
    assert session.ref != predecessor
    snapshot = ActiveRegistryEvidenceSnapshotProvider(store.filesystem).snapshot(session.ref)

    class Snapshot:
        def snapshot(self, ref):
            assert ref == session.ref
            return snapshot

    with store.filesystem.write_transaction() as tx:
        evidence = JsonlV3EvidenceStore(
            Snapshot(),
            JsonlV3FilesystemEvidenceReader(tx),
            JsonlV3FilesystemEvidenceOffsetReader(tx),
        )
        page = evidence.page(session.ref, limit=2)
    assert page.complete
    assert len(page.records) == 2
    decoded = page.records[1].value
    assert isinstance(decoded, DischargeSample)
    assert decoded.blackout_id == session.ref.blackout_id
    assert decoded.segment_id == session.ref.segment_id
    assert decoded.physical_episode_id == accepted.physical_episode_id
    assert decoded.battery_epoch_id == accepted.battery_epoch_id
    assert decoded.captured == accepted.captured
    assert decoded.observation_origin is accepted.observation_origin


def test_same_append_retry_rejects_cursor_with_wrong_scope(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    sample = _sample(start)
    store.append_sample(opened.ref, opened.cursor, sample)
    wrong_scope = replace(
        opened.cursor,
        blackout_id="wrong-blackout",
        segment_id="wrong-segment",
    )

    with pytest.raises(V3AppendConflict):
        store.append_sample(opened.ref, wrong_scope, sample)


def test_session_linked_budget_close_retries_with_terminal_cursor_after_restart(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    session = V3ActiveCaptureSession(store)
    session.attach(opened)
    marker = EndpointAnchor(
        "a" * 64,
        AnchorKind.MODELED_SAFE_SHUTDOWN,
        AnchorProvenance.MODELED,
        start.boot_id,
        start.wall_time_utc,
        2,
        opened.cursor.last_record_sha256 or "0" * 64,
        start.blackout_id,
        start.physical_episode_id,
        start.segment_id,
    )
    session.append_anchor(marker)
    session.append_sample(_sample(start))
    terminal = session.terminal_cursor
    assert terminal is not None
    end = BlackoutEnd(
        start.blackout_id,
        start.physical_episode_id,
        start.battery_epoch_id,
        start.segment_id,
        BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED,
        start.observation_origin,
        start.wall_time_utc,
        3,
        start.boot_id,
        budget_kind=BudgetKind.BYTES,
        continued_by=_id(5),
        continuation_kind=ContinuationKind.SIZE_ROLLOVER,
    )
    original = store._registry.compare_and_replace
    crashed = False

    def crash_before_processing(tx: object, *, expected: object, replacement: object) -> object:
        nonlocal crashed
        if not crashed and getattr(replacement, "capture", object()) is None:
            crashed = True
            raise RuntimeError("processing transition crash")
        return original(tx, expected=expected, replacement=replacement)

    setattr(store._registry, "compare_and_replace", crash_before_processing)
    with pytest.raises(RuntimeError):
        session.close(end)

    restarted = JsonlV3CaptureStore(store.filesystem)
    page = restarted.recover(limit=1)
    assert page.processing
    retry = restarted.close(opened.ref, terminal, end)
    assert retry.stage is BlackoutProcessingStage.PROCESSING


def test_open_restart_completes_a_persisted_preparing_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    encoded = encode_blackout_start(start)
    with store.filesystem.write_transaction() as tx:
        snapshot = store._registry.read(tx)
        state = snapshot.state.capture
        assert state is not None
        receipt = state.storage_segments[0]
        preparing = PreparingCaptureState(
            "preparing",
            start.blackout_id,
            start.segment_id,
            receipt.storage_id,
            receipt.path_token,
            receipt.offset_token,
            encoded.line.decode(),
            hashlib.sha256(encoded.line).hexdigest(),
            len(encoded.line),
            receipt.path_token.started_utc,
            start.policy_revision,
        )
        store._registry.compare_and_replace(
            tx, expected=snapshot, replacement=V3WorkRegistry(preparing, ())
        )
    recovered = JsonlV3CaptureStore(store.filesystem).open(start)
    assert recovered.ref == opened.ref
    assert recovered.cursor.next_sequence == 1


def test_physical_append_is_exact_and_different_retry_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    sample = _sample(start)
    advanced = store.append_sample(opened.ref, opened.cursor, sample)
    assert advanced.next_sequence == 2
    assert store.append_sample(opened.ref, opened.cursor, sample) == advanced
    with pytest.raises(V3AppendConflict):
        store.append_sample(opened.ref, opened.cursor, _sample(start, "21.0"))


def test_terminal_anchor_keeps_physical_cursor_and_close_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    anchor = EndpointAnchor(
        "a" * 64,
        AnchorKind.MODELED_SAFE_SHUTDOWN,
        AnchorProvenance.MODELED,
        start.boot_id,
        start.wall_time_utc,
        2,
        opened.cursor.last_record_sha256 or "0" * 64,
        start.blackout_id,
        start.physical_episode_id,
        start.segment_id,
    )
    terminal = store.append_anchor(opened.ref, opened.cursor, anchor)
    assert store.append_anchor(opened.ref, opened.cursor, anchor) == terminal
    assert terminal.chain.value == "terminal"
    assert terminal.next_sequence == 1
    end = BlackoutEnd(
        start.blackout_id,
        start.physical_episode_id,
        start.battery_epoch_id,
        start.segment_id,
        BlackoutTermination.POWER_RESTORED,
        start.observation_origin,
        start.wall_time_utc,
        3,
        start.boot_id,
        terminal_anchor_record_hash=terminal.last_record_sha256,
    )
    processing = store.close(opened.ref, terminal, end)
    assert store.close(opened.ref, terminal, end) == processing
    with pytest.raises(V3AppendConflict):
        store.close(opened.ref, opened.cursor, end)
    with pytest.raises(V3AppendConflict):
        store.close(
            opened.ref,
            BlackoutCaptureCursor(
                opened.ref.blackout_id,
                opened.ref.segment_id,
                BlackoutChainKind.TERMINAL,
                0,
                None,
            ),
            end,
        )
    with pytest.raises(V3AppendConflict):
        store.close(opened.ref, terminal, replace(end, monotonic_ns=4))


def test_active_close_validation_rejects_stale_cursor_variants(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)

    with store.filesystem.write_transaction() as tx:
        snapshot = store._registry.read(tx)
        with pytest.raises(V3AppendConflict):
            store._validate_active_close_cursor(
                snapshot, replace(opened.ref, blackout_id=_id(9)), opened.cursor, budget=True
            )
        with pytest.raises(V3AppendConflict):
            store._validate_active_close_cursor(
                snapshot, opened.ref, replace(opened.cursor, next_sequence=2), budget=True
            )
        with pytest.raises(V3AppendConflict):
            store._validate_active_close_cursor(snapshot, opened.ref, opened.cursor, budget=False)

    anchor = EndpointAnchor(
        "a" * 64,
        AnchorKind.MODELED_SAFE_SHUTDOWN,
        AnchorProvenance.MODELED,
        start.boot_id,
        start.wall_time_utc,
        2,
        opened.cursor.last_record_sha256 or "0" * 64,
        start.blackout_id,
        start.physical_episode_id,
        start.segment_id,
    )
    terminal = store.append_anchor(opened.ref, opened.cursor, anchor)
    with store.filesystem.write_transaction() as tx:
        snapshot = store._registry.read(tx)
        with pytest.raises(V3AppendConflict):
            store._validate_active_close_cursor(
                snapshot,
                opened.ref,
                replace(terminal, next_sequence=0, last_record_sha256=None),
                budget=True,
            )
        with pytest.raises(V3AppendConflict):
            store._validate_active_close_cursor(snapshot, opened.ref, opened.cursor, budget=True)


def test_later_terminal_anchor_retry_uses_original_cursor_hash(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    first = EndpointAnchor(
        "a" * 64,
        AnchorKind.MODELED_SAFE_SHUTDOWN,
        AnchorProvenance.MODELED,
        start.boot_id,
        start.wall_time_utc,
        2,
        opened.cursor.last_record_sha256 or "0" * 64,
        start.blackout_id,
        start.physical_episode_id,
        start.segment_id,
    )
    first_terminal = store.append_anchor(opened.ref, opened.cursor, first)
    second = EndpointAnchor(
        "b" * 64,
        AnchorKind.POWER_RESTORED,
        AnchorProvenance.PHYSICAL,
        start.boot_id,
        start.wall_time_utc,
        3,
        first_terminal.last_record_sha256,
        start.blackout_id,
        start.physical_episode_id,
        start.segment_id,
    )
    second_terminal = store.append_anchor(opened.ref, first_terminal, second)
    assert store.append_anchor(opened.ref, first_terminal, second) == second_terminal


def test_end_write_to_processing_crash_recovers_on_original_cursor_retry(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    anchor = EndpointAnchor(
        "a" * 64,
        AnchorKind.MODELED_SAFE_SHUTDOWN,
        AnchorProvenance.MODELED,
        start.boot_id,
        start.wall_time_utc,
        2,
        opened.cursor.last_record_sha256 or "0" * 64,
        start.blackout_id,
        start.physical_episode_id,
        start.segment_id,
    )
    terminal = store.append_anchor(opened.ref, opened.cursor, anchor)
    end = BlackoutEnd(
        start.blackout_id,
        start.physical_episode_id,
        start.battery_epoch_id,
        start.segment_id,
        BlackoutTermination.POWER_RESTORED,
        start.observation_origin,
        start.wall_time_utc,
        3,
        start.boot_id,
        terminal_anchor_record_hash=terminal.last_record_sha256,
    )
    original = store._registry.compare_and_replace
    fired = False

    def crash_after_end(tx: object, *, expected: object, replacement: object) -> object:
        nonlocal fired
        if not fired and getattr(replacement, "capture", object()) is None:
            fired = True
            raise RuntimeError("processing transition crash")
        return original(tx, expected=expected, replacement=replacement)

    setattr(store._registry, "compare_and_replace", crash_after_end)
    with pytest.raises(RuntimeError):
        store.close(opened.ref, terminal, end)
    restarted = JsonlV3CaptureStore(store.filesystem)
    assert restarted.recover(limit=1).processing
    assert restarted.close(opened.ref, terminal, end).stage is BlackoutProcessingStage.PROCESSING


def test_transfer_anchor_is_part_of_the_physical_chain(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    transfer = EndpointAnchor(
        "a" * 64,
        AnchorKind.TRANSFER_TO_BATTERY,
        AnchorProvenance.PHYSICAL,
        start.boot_id,
        start.wall_time_utc,
        2,
        opened.cursor.last_record_sha256 or "0" * 64,
        start.blackout_id,
        start.physical_episode_id,
        start.segment_id,
    )
    advanced = store.append_anchor(opened.ref, opened.cursor, transfer)
    assert advanced.chain is BlackoutChainKind.PHYSICAL
    assert advanced.next_sequence == 2


def test_budget_close_persists_terminal_end_without_physical_anchor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    end = BlackoutEnd(
        start.blackout_id,
        start.physical_episode_id,
        start.battery_epoch_id,
        start.segment_id,
        BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED,
        start.observation_origin,
        start.wall_time_utc,
        3,
        start.boot_id,
        budget_kind=BudgetKind.BYTES,
        continued_by=_id(5),
        continuation_kind=ContinuationKind.SIZE_ROLLOVER,
    )
    processing = store.close(opened.ref, opened.cursor, end)
    assert processing.stage is BlackoutProcessingStage.PROCESSING
    assert store.close(opened.ref, opened.cursor, end) == processing
    with pytest.raises(V3AppendConflict):
        store.close(
            opened.ref,
            BlackoutCaptureCursor(
                opened.ref.blackout_id,
                opened.ref.segment_id,
                BlackoutChainKind.TERMINAL,
                opened.cursor.next_sequence,
                opened.cursor.last_record_sha256,
            ),
            end,
        )
    assert store.recover(limit=1).active_capture is None


def test_pending_damage_rollover_ignores_incomplete_carriers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    end = BlackoutEnd(
        start.blackout_id,
        start.physical_episode_id,
        start.battery_epoch_id,
        start.segment_id,
        BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED,
        start.observation_origin,
        start.wall_time_utc,
        3,
        start.boot_id,
        budget_kind=BudgetKind.BYTES,
        continued_by=_id(5),
        continuation_kind=ContinuationKind.SIZE_ROLLOVER,
    )
    store.close(opened.ref, opened.cursor, end)
    with store.filesystem.write_transaction() as tx:
        item = store._registry.read(tx).state.pending[0]

    first = item.storage_segments[0]
    short = replace(item, storage_segments=(first,))
    without_damage = replace(
        item,
        storage_segments=tuple(replace(first, ordinal=index) for index in range(64)),
        terminal_closing_anchor_sha256="d" * 64,
    )
    carrier = replace(
        item,
        storage_segments=tuple(
            replace(
                first,
                ordinal=index,
                damaged_file_sha256="c" * 64 if index == 1 else None,
            )
            for index in range(64)
        ),
        terminal_closing_anchor_sha256="d" * 64,
    )
    without_anchor = replace(carrier, terminal_closing_anchor_sha256=None)
    assert not store._is_pending_damage_carrier(short)

    snapshot = RegistrySnapshot(
        V3WorkRegistry(None, (short, without_damage, without_anchor, carrier)), 0, ""
    )
    checked: list[object] = []

    def not_capture_damage(tx: object, candidate: object) -> bool:
        del tx
        checked.append(candidate)
        return False

    setattr(store, "_pending_is_capture_damage", not_capture_damage)
    assert store._pending_damage_rollover(object(), snapshot) is None
    assert checked == [carrier]

    setattr(store, "_pending_is_capture_damage", lambda tx, candidate: True)
    assert store._pending_damage_rollover(object(), snapshot) == opened.ref


def test_budget_close_links_after_terminal_marker_and_keeps_true_root(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    marker = EndpointAnchor(
        "a" * 64,
        AnchorKind.MODELED_SAFE_SHUTDOWN,
        AnchorProvenance.MODELED,
        start.boot_id,
        start.wall_time_utc,
        2,
        opened.cursor.last_record_sha256 or "0" * 64,
        start.blackout_id,
        start.physical_episode_id,
        start.segment_id,
    )
    terminal = store.append_anchor(opened.ref, opened.cursor, marker)
    store.append_sample(opened.ref, opened.cursor, _sample(start))
    end = BlackoutEnd(
        start.blackout_id,
        start.physical_episode_id,
        start.battery_epoch_id,
        start.segment_id,
        BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED,
        start.observation_origin,
        start.wall_time_utc,
        3,
        start.boot_id,
        budget_kind=BudgetKind.BYTES,
        continued_by=_id(5),
        continuation_kind=ContinuationKind.SIZE_ROLLOVER,
    )

    processing = store.close(opened.ref, terminal, end)
    assert processing.stage is BlackoutProcessingStage.PROCESSING
    with store.filesystem.write_transaction() as tx:
        pending = store._registry.read(tx).state.pending
        assert len(pending) == 1
        state = pending[0]
        assert state.terminal_root_sha256 == terminal.last_record_sha256
        assert state.terminal_closing_anchor_sha256 is None
        assert state.terminal_cursor_after_end.next_sequence == 2
        assert state.terminal_cursor_after_end.last_record_sha256 == processing.last_record_sha256
        paths = store.filesystem.paths
        assert paths is not None
        raw, _ = tx.read_bounded(
            paths.terminal_staging_token(start.blackout_id), max_bytes=2 * 1024 * 1024
        )
        records = [decode_v3_record(line) for line in raw.splitlines(keepends=True)]
        assert records[-1].envelope.seq == 1
        assert records[-1].envelope.prev_record_sha256 == terminal.last_record_sha256

    assert store.close(opened.ref, terminal, end) == processing
    with pytest.raises(V3AppendConflict):
        store.close(opened.ref, terminal, replace(end, monotonic_ns=4))


def test_linked_budget_close_processing_crash_replays_exact_end(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    marker = EndpointAnchor(
        "a" * 64,
        AnchorKind.MODELED_SAFE_SHUTDOWN,
        AnchorProvenance.MODELED,
        start.boot_id,
        start.wall_time_utc,
        2,
        opened.cursor.last_record_sha256 or "0" * 64,
        start.blackout_id,
        start.physical_episode_id,
        start.segment_id,
    )
    terminal = store.append_anchor(opened.ref, opened.cursor, marker)
    store.append_sample(opened.ref, opened.cursor, _sample(start))
    end = BlackoutEnd(
        start.blackout_id,
        start.physical_episode_id,
        start.battery_epoch_id,
        start.segment_id,
        BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED,
        start.observation_origin,
        start.wall_time_utc,
        3,
        start.boot_id,
        budget_kind=BudgetKind.BYTES,
        continued_by=_id(5),
        continuation_kind=ContinuationKind.SIZE_ROLLOVER,
    )
    original = store._registry.compare_and_replace
    fired = False

    def crash_after_end(tx: object, *, expected: object, replacement: object) -> object:
        nonlocal fired
        if not fired and getattr(replacement, "capture", object()) is None:
            fired = True
            raise RuntimeError("processing transition crash")
        return original(tx, expected=expected, replacement=replacement)

    setattr(store._registry, "compare_and_replace", crash_after_end)
    with pytest.raises(RuntimeError):
        store.close(opened.ref, terminal, end)

    restarted = JsonlV3CaptureStore(store.filesystem)
    page = restarted.recover(limit=1)
    assert page.processing
    with restarted.filesystem.write_transaction() as tx:
        processing = restarted._registry.read(tx).state.pending[0]
        assert processing.terminal_root_sha256 == terminal.last_record_sha256
        assert processing.terminal_closing_anchor_sha256 is None
    assert restarted.close(opened.ref, terminal, end).stage is BlackoutProcessingStage.PROCESSING


def test_final_damage_receipt_routes_to_corruption_terminal_close(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    with store.filesystem.write_transaction() as tx:
        state = store._registry.read(tx).state.capture
        assert state is not None
    with pytest.raises(V3CapacityError):
        store._finalize_reference_damage(opened.ref, opened.cursor, state)
    page = store.recover(limit=1)
    assert page.active_capture is not None
    assert page.active_capture.ref != opened.ref
    with store.filesystem.write_transaction() as tx:
        pending = store._registry.read(tx).state.pending
        processing = pending[0]
        assert processing.terminal_closing_anchor_sha256 is not None
        assert processing.terminal_end_sha256 != processing.terminal_closing_anchor_sha256


def test_ref64_recovery_never_writes_triggering_record_as_continuation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    sample = _sample(start)
    encoded = encode_discharge_sample(
        sample, seq=1, previous_record_sha256=opened.cursor.last_record_sha256
    )
    with store.filesystem.write_transaction() as tx:
        snapshot = store._registry.read(tx)
        state = snapshot.state.capture
        assert state is not None
        paths = store.filesystem.paths
        assert paths is not None
        first = state.storage_segments[0]

        def receipts_for_ref64() -> tuple[V3StorageSegmentReceipt, ...]:
            receipts: list[V3StorageSegmentReceipt] = []
            for ordinal in range(64):
                storage_id = first.storage_id if ordinal == 0 else _id(100 + ordinal)
                active = paths.segment_token(
                    first.path_token.started_utc,
                    state.blackout_id,
                    state.logical_segment_id,
                    ordinal,
                    storage_id,
                )
                offset = paths.offset_token(active)
                damaged_hash = None
                if ordinal == 1:
                    damaged_hash = "c" * 64
                    active, offset = paths.damaged_tokens(
                        state.blackout_id,
                        state.logical_segment_id,
                        ordinal,
                        storage_id,
                        damaged_hash,
                    )
                receipts.append(
                    replace(
                        first,
                        ordinal=ordinal,
                        storage_id=storage_id,
                        path_token=active,
                        offset_token=offset,
                        damaged_file_sha256=damaged_hash,
                    )
                )
            return tuple(receipts)

        intent = V3AppendIntent(
            "physical",
            "sample",
            1,
            opened.cursor.last_record_sha256,
            0,
            first.trusted_bytes,
            encoded.line.decode(),
            hashlib.sha256(encoded.line).hexdigest(),
            len(encoded.line),
            opened.cursor.last_record_sha256 or "0" * 64,
        )
        saturated = replace(state, storage_segments=receipts_for_ref64(), append_intent=intent)
        store._registry.compare_and_replace(
            tx, expected=snapshot, replacement=V3WorkRegistry(saturated, snapshot.state.pending)
        )

    def fake_recovery(
        tx: object,
        snapshot: RegistrySnapshot,
        recovered_line: object,
        expected: object,
    ) -> RegistrySnapshot:
        del recovered_line, expected
        current = snapshot.state.capture
        assert current is not None
        damaged = replace(current, append_intent=None, gap_count=current.gap_count + 1)
        return store._registry.compare_and_replace(
            tx,
            expected=snapshot,
            replacement=V3WorkRegistry(damaged, snapshot.state.pending),
        )

    setattr(store, "_recover_existing_append", fake_recovery)
    with pytest.raises(V3CapacityError):
        store.append_sample(opened.ref, opened.cursor, sample)
    recovered_page = store.recover(limit=1)
    assert recovered_page.active_capture is not None
    assert recovered_page.active_capture.ref != opened.ref
    with store.filesystem.write_transaction() as tx:
        pending = store._registry.read(tx).state.pending
        assert pending and pending[0].terminal_closing_anchor_sha256 is not None


def test_append_crash_after_line_write_converges_on_retry(tmp_path: Path) -> None:
    fired = False

    def fault(point: object) -> None:
        nonlocal fired
        if not fired and str(point) == "append.after_write":
            fired = True
            raise RuntimeError("crash")

    store = _store(tmp_path, fault)
    start = _start()
    opened = store.open(start)
    sample = _sample(start)
    with pytest.raises(RuntimeError):
        store.append_sample(opened.ref, opened.cursor, sample)
    recovered = JsonlV3CaptureStore(store.filesystem)
    assert recovered.append_sample(opened.ref, opened.cursor, sample).next_sequence == 2


def test_zero_byte_physical_intent_replay_appends_exactly_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    sample = _sample(start)
    encoded = encode_discharge_sample(
        sample, seq=1, previous_record_sha256=opened.cursor.last_record_sha256
    )
    with store.filesystem.write_transaction() as tx:
        snapshot = store._registry.read(tx)
        state = snapshot.state.capture
        assert state is not None
        receipt = state.storage_segments[-1]
        intent = V3AppendIntent(
            "physical",
            "sample",
            1,
            opened.cursor.last_record_sha256,
            receipt.ordinal,
            receipt.trusted_bytes,
            encoded.line.decode(),
            hashlib.sha256(encoded.line).hexdigest(),
            len(encoded.line),
            opened.cursor.last_record_sha256 or "0" * 64,
        )
        store._registry.compare_and_replace(
            tx,
            expected=snapshot,
            replacement=V3WorkRegistry(replace(state, append_intent=intent), ()),
        )
    recovered = JsonlV3CaptureStore(store.filesystem)
    advanced = recovered.append_sample(opened.ref, opened.cursor, sample)
    assert advanced.next_sequence == 2
    assert recovered.append_sample(opened.ref, opened.cursor, sample) == advanced


def test_zero_byte_terminal_intent_replay_appends_exactly_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    anchor = EndpointAnchor(
        "a" * 64,
        AnchorKind.MODELED_SAFE_SHUTDOWN,
        AnchorProvenance.MODELED,
        start.boot_id,
        start.wall_time_utc,
        2,
        opened.cursor.last_record_sha256 or "0" * 64,
        start.blackout_id,
        start.physical_episode_id,
        start.segment_id,
    )
    encoded = encode_endpoint_anchor(anchor, seq=0, previous_record_sha256=None)
    with store.filesystem.write_transaction() as tx:
        snapshot = store._registry.read(tx)
        state = snapshot.state.capture
        assert state is not None
        temporary = BlackoutCaptureCursor(
            opened.ref.blackout_id,
            opened.ref.segment_id,
            BlackoutChainKind.TERMINAL,
            0,
            None,
        )
        intent = V3AppendIntent(
            "terminal",
            "anchor",
            0,
            None,
            None,
            0,
            encoded.line.decode(),
            hashlib.sha256(encoded.line).hexdigest(),
            len(encoded.line),
            "0" * 64,
        )
        store._registry.compare_and_replace(
            tx,
            expected=snapshot,
            replacement=V3WorkRegistry(
                replace(state, terminal_cursor=temporary, append_intent=intent), ()
            ),
        )
    recovered = JsonlV3CaptureStore(store.filesystem)
    terminal = recovered.append_anchor(opened.ref, opened.cursor, anchor)
    assert terminal.next_sequence == 1
    assert recovered.append_anchor(opened.ref, opened.cursor, anchor) == terminal


@pytest.mark.parametrize("target_bytes", [61 * 1024 * 1024, 62 * 1024 * 1024])
def test_capture_size_boundaries_accept_61_and_62_mib(tmp_path: Path, target_bytes: int) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    sample = _sample(start)
    line_length = len(
        encode_discharge_sample(
            sample, seq=1, previous_record_sha256=opened.cursor.last_record_sha256
        ).line
    )
    with store.filesystem.write_transaction() as tx:
        snapshot = store._registry.read(tx)
        state = snapshot.state.capture
        assert state is not None
        adjusted = replace(state, capture_bytes=target_bytes - line_length)
        store._registry.compare_and_replace(
            tx, expected=snapshot, replacement=V3WorkRegistry(adjusted, snapshot.state.pending)
        )
    store.append_sample(opened.ref, opened.cursor, sample)
    with store.filesystem.write_transaction() as tx:
        state = store._registry.read(tx).state.capture
        assert state is not None
        assert state.capture_bytes == target_bytes


@pytest.mark.parametrize("target_bytes", [63 * 1024 * 1024, 64 * 1024 * 1024])
def test_capture_size_overflow_is_refused_before_file_append(
    tmp_path: Path, target_bytes: int
) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    sample = _sample(start)
    line_length = len(
        encode_discharge_sample(
            sample, seq=1, previous_record_sha256=opened.cursor.last_record_sha256
        ).line
    )
    with store.filesystem.write_transaction() as tx:
        snapshot = store._registry.read(tx)
        state = snapshot.state.capture
        assert state is not None
        adjusted = replace(state, capture_bytes=target_bytes - line_length)
        store._registry.compare_and_replace(
            tx, expected=snapshot, replacement=V3WorkRegistry(adjusted, snapshot.state.pending)
        )
    with pytest.raises(V3CapacityError):
        store.append_sample(opened.ref, opened.cursor, sample)
