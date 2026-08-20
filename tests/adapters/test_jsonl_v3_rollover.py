"""Size-rollover wire identity smoke tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_jsonl_v3_capture_store import _sample, _start, _store

from src.adapters.jsonl_v3_blackout_start_codec import decode_blackout_start
from src.adapters.jsonl_v3_canonical import decode_v3_record
from src.adapters.jsonl_v3_capture_store import JsonlV3CaptureStore
from src.adapters.jsonl_v3_terminal_tail_codec import decode_blackout_end, encode_blackout_end
from src.domain.blackout_terminal import (
    BlackoutEnd,
    BlackoutTermination,
    BudgetKind,
    ContinuationKind,
)
from src.domain.fragments import AnchorKind, AnchorProvenance, EndpointAnchor, ObservationOrigin


def test_budget_end_retains_successor_link_and_rollover_kind() -> None:
    value = BlackoutEnd(
        "10000000000040008000000000000001",
        "10000000000040008000000000000002",
        "10000000000040008000000000000003",
        "10000000000040008000000000000004",
        BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED,
        ObservationOrigin.NATURAL,
        datetime(2026, 8, 18, tzinfo=timezone.utc),
        1,
        "boot",
        budget_kind=BudgetKind.BYTES,
        continued_by="10000000000040008000000000000005",
        continuation_kind=ContinuationKind.SIZE_ROLLOVER,
    )
    assert decode_blackout_end(encode_blackout_end(value).line) == value


def test_rollover_publishes_successor_after_carrier_end(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    successor = store.rollover(opened.ref, opened.cursor, budget_kind=BudgetKind.BYTES)

    assert successor.ref != opened.ref
    assert successor.cursor.next_sequence == 1
    page = store.recover(limit=2)
    assert page.active_capture is not None
    assert page.active_capture.ref == successor.ref
    assert [item.ref for item in page.processing] == [opened.ref]
    paths = store.filesystem.paths
    assert paths is not None
    successor_files = list(
        paths.segments.glob(f"blk-*-{successor.ref.blackout_id}-p000000-*.jsonl")
    )
    assert len(successor_files) == 1
    assert (
        decode_blackout_start(successor_files[0].read_bytes()).continued_from
        == opened.ref.blackout_id
    )
    carrier = paths.terminal_staging_token(opened.ref.blackout_id)
    with store.filesystem.write_transaction() as tx:
        carrier_bytes, _ = tx.read_bounded(carrier, max_bytes=2 * 1024 * 1024)
    end = decode_blackout_end(carrier_bytes)
    assert end.continued_by == successor.ref.blackout_id
    assert end.continuation_kind is ContinuationKind.SIZE_ROLLOVER


def test_anchor_then_budget_rollover_appends_contiguous_end_and_preserves_root(
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
    successor = store.rollover(opened.ref, opened.cursor)
    paths = store.filesystem.paths
    assert paths is not None
    with store.filesystem.write_transaction() as tx:
        raw, _ = tx.read_bounded(
            paths.terminal_staging_token(opened.ref.blackout_id), max_bytes=2 * 1024 * 1024
        )
        processing = store._registry.read(tx).state.pending[0]
    records = [decode_v3_record(line) for line in raw.splitlines(keepends=True)]
    assert [record.envelope.seq for record in records] == [0, 1]
    assert records[-1].envelope.prev_record_sha256 == terminal.last_record_sha256
    assert decode_blackout_end(records[-1].line).terminal_anchor_record_hash is None
    assert processing.terminal_root_sha256 == terminal.last_record_sha256
    assert processing.terminal_closing_anchor_sha256 is None
    assert processing.terminal_cursor_after_end.next_sequence == 2
    assert processing.terminal_cursor_after_end.last_record_sha256 == records[-1].record_sha256
    assert successor.ref != opened.ref


@pytest.mark.parametrize(
    "fault_point",
    [
        "rollover.after_registry_reserve",
        "rollover.after_successor_create",
        "rollover.after_successor_fdatasync",
        "rollover.after_successor_dirsync",
        "rollover.after_carrier_end_fdatasync",
        "rollover.after_registry_swap",
    ],
)
def test_anchor_rollover_fault_recovery_is_byte_identical(tmp_path: Path, fault_point: str) -> None:
    successor_ids = iter(
        (
            "00000004000040008000000000000000",
            "00000005000040008000000000000000",
            "00000006000040008000000000000000",
            "00000007000040008000000000000000",
        )
    )
    baseline_root = tmp_path / "baseline"
    baseline_root.mkdir()
    baseline = _store(baseline_root)
    baseline._uuid4_hex = lambda: next(successor_ids)
    opened = baseline.open(_start())
    start = _start()
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
    baseline.append_anchor(opened.ref, opened.cursor, anchor)
    baseline.rollover(opened.ref, opened.cursor)
    baseline_bytes = _durable_tree_bytes(baseline_root)

    fired = False

    def fault(point: object) -> None:
        nonlocal fired
        if not fired and str(point) == fault_point:
            fired = True
            raise RuntimeError("rollover crash")

    crashed_root = tmp_path / "crashed"
    crashed_root.mkdir()
    crashed = _store(crashed_root, fault)
    crashed_ids = iter(
        (
            "00000004000040008000000000000000",
            "00000005000040008000000000000000",
            "00000006000040008000000000000000",
            "00000007000040008000000000000000",
        )
    )
    crashed._uuid4_hex = lambda: next(crashed_ids)
    opened = crashed.open(start)
    crashed.append_anchor(opened.ref, opened.cursor, anchor)
    with pytest.raises(RuntimeError):
        crashed.rollover(opened.ref, opened.cursor)
    restarted = JsonlV3CaptureStore(crashed.filesystem)
    restarted.recover(limit=2)
    assert _durable_tree_bytes(crashed_root) == baseline_bytes


def _durable_tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and "transactions" not in path.parts
        and path.name != "work-registry-v1.json"
    }


def test_rollover_restart_reuses_the_atomically_published_successor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    successor = store.rollover(opened.ref, opened.cursor)
    restarted = JsonlV3CaptureStore(store.filesystem)
    page = restarted.recover(limit=1)
    assert page.active_capture is not None
    assert page.active_capture.ref == successor.ref


def _close_ref64_damage(store: JsonlV3CaptureStore, start, opened) -> None:
    anchor = EndpointAnchor(
        "a" * 64,
        AnchorKind.CORRUPTION,
        AnchorProvenance.OPERATIONAL,
        start.boot_id,
        start.wall_time_utc,
        2,
        opened.cursor.last_record_sha256 or "0" * 64,
        start.blackout_id,
        start.physical_episode_id,
        start.segment_id,
    )
    terminal = store.append_anchor(opened.ref, opened.cursor, anchor)
    store.close(
        opened.ref,
        terminal,
        BlackoutEnd(
            start.blackout_id,
            start.physical_episode_id,
            start.battery_epoch_id,
            start.segment_id,
            BlackoutTermination.CAPTURE_DAMAGED,
            start.observation_origin,
            start.wall_time_utc,
            2,
            start.boot_id,
            terminal_anchor_record_hash=terminal.last_record_sha256,
        ),
    )


def test_ref64_processing_rollover_writes_two_sided_segment_ref_link(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    _close_ref64_damage(store, start, opened)
    successor = store.rollover_damaged_processing(opened.ref)
    page = store.recover(limit=2)
    assert successor.ref != opened.ref
    assert page.active_capture is not None
    assert page.active_capture.ref == successor.ref
    assert len(page.processing) == 1
    paths = store.filesystem.paths
    assert paths is not None
    with store.filesystem.write_transaction() as tx:
        raw, _ = tx.read_bounded(
            paths.terminal_staging_token(opened.ref.blackout_id), max_bytes=2 * 1024 * 1024
        )
    records = [decode_v3_record(line) for line in raw.splitlines(keepends=True)]
    assert [record.envelope.seq for record in records] == [0, 1, 2]
    damaged = decode_blackout_end(records[1].line, terminal_anchor_record=records[0].line)
    carrier = decode_blackout_end(records[2].line)
    assert damaged.termination is BlackoutTermination.CAPTURE_DAMAGED
    assert carrier.termination is BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED
    assert carrier.budget_kind is BudgetKind.SEGMENT_REFS
    assert carrier.continued_by == successor.ref.blackout_id
    assert carrier.continuation_kind is ContinuationKind.SIZE_ROLLOVER
    successor_files = list(
        paths.segments.glob(f"blk-*-{successor.ref.blackout_id}-p000000-*.jsonl")
    )
    assert len(successor_files) == 1
    successor_start = decode_blackout_start(successor_files[0].read_bytes())
    assert successor_start.continued_from == opened.ref.blackout_id
    assert successor_start.continuation_kind is ContinuationKind.SIZE_ROLLOVER


def test_ref64_processing_rollover_retry_after_swap_reuses_successor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    _close_ref64_damage(store, start, opened)
    first = store.rollover_damaged_processing(opened.ref)
    second = store.rollover_damaged_processing(opened.ref)
    assert second == first
    with store.filesystem.write_transaction() as tx:
        snapshot = store._registry.read(tx)
    assert len(snapshot.state.pending) == 1
    assert snapshot.state.pending[0].blackout_id == opened.ref.blackout_id


@pytest.mark.parametrize(
    "fault_point",
    [
        "rollover.after_registry_reserve",
        "rollover.after_successor_create",
        "rollover.after_successor_fdatasync",
        "rollover.after_successor_dirsync",
        "rollover.after_carrier_end_fdatasync",
        "rollover.after_registry_swap",
    ],
)
def test_ref64_rollover_fault_phases_restart_with_both_links(
    tmp_path: Path, fault_point: str
) -> None:
    fired = False

    def fault(point: object) -> None:
        nonlocal fired
        if not fired and str(point) == fault_point:
            fired = True
            raise RuntimeError("ref64 rollover crash")

    store = _store(tmp_path, fault)
    start = _start()
    opened = store.open(start)
    _close_ref64_damage(store, start, opened)
    with pytest.raises(RuntimeError):
        store.rollover_damaged_processing(opened.ref)

    restarted = JsonlV3CaptureStore(store.filesystem)
    page = restarted.recover(limit=2)
    assert page.active_capture is not None
    assert page.active_capture.ref != opened.ref
    assert len(page.processing) == 1
    paths = restarted.filesystem.paths
    assert paths is not None
    with restarted.filesystem.write_transaction() as tx:
        raw, _ = tx.read_bounded(
            paths.terminal_staging_token(opened.ref.blackout_id), max_bytes=2 * 1024 * 1024
        )
    records = [decode_v3_record(line) for line in raw.splitlines(keepends=True)]
    assert [record.envelope.seq for record in records] == [0, 1, 2]
    carrier = decode_blackout_end(records[2].line)
    assert carrier.termination is BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED
    assert carrier.budget_kind is BudgetKind.SEGMENT_REFS
    assert carrier.continued_by == page.active_capture.ref.blackout_id


def test_rollover_reads_start_from_logical_first_segment_after_damage(tmp_path: Path) -> None:
    append_crash = True

    def fault(point: object) -> None:
        nonlocal append_crash
        if append_crash and str(point) == "append.after_write":
            append_crash = False
            raise RuntimeError("append crash")

    store = _store(tmp_path, fault)
    start = _start()
    opened = store.open(start)
    sample = _sample(start)
    with pytest.raises(RuntimeError):
        store.append_sample(opened.ref, opened.cursor, sample)
    paths = store.filesystem.paths
    assert paths is not None
    segment = next(paths.segments.glob("blk-*.jsonl"))
    segment.write_bytes(segment.read_bytes()[:-7])
    cursor = store.append_sample(opened.ref, opened.cursor, sample)

    successor = store.rollover(opened.ref, cursor)
    assert successor.ref != opened.ref


@pytest.mark.parametrize(
    "fault_point",
    [
        "rollover.after_registry_reserve",
        "rollover.after_successor_create",
        "rollover.after_successor_fdatasync",
        "rollover.after_successor_dirsync",
        "rollover.after_carrier_end_fdatasync",
        "rollover.after_registry_swap",
    ],
)
def test_rollover_fault_phases_restart_to_successor_and_pending_carrier(
    tmp_path: Path, fault_point: str
) -> None:
    fired = False

    def fault(point: object) -> None:
        nonlocal fired
        if not fired and str(point) == fault_point:
            fired = True
            raise RuntimeError("rollover crash")

    store = _store(tmp_path, fault)
    opened = store.open(_start())
    with pytest.raises(RuntimeError):
        store.rollover(opened.ref, opened.cursor)

    restarted = JsonlV3CaptureStore(store.filesystem)
    page = restarted.recover(limit=2)
    assert page.active_capture is not None
    assert page.active_capture.ref != opened.ref
    assert [item.ref for item in page.processing] == [opened.ref]
