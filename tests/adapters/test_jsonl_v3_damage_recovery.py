"""Typed damaged-file continuation boundary tests."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from test_jsonl_v3_capture_store import _sample, _start, _store

from src.adapters.jsonl_v3_capture_store import JsonlV3CaptureStore
from src.adapters.jsonl_v3_errors import V3CapacityError
from src.adapters.jsonl_v3_storage_paths import V3StoragePaths


def _id(seed: int) -> str:
    return uuid.UUID(int=seed, version=4).hex


def test_damage_tokens_bind_same_aggregate_and_immutable_hash(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    paths = V3StoragePaths(tmp_path)
    blackout, logical, storage = _id(1), _id(2), _id(3)
    segment, offset = paths.damaged_tokens(blackout, logical, 63, storage, "a" * 64)
    assert segment.blackout_id == offset.blackout_id == blackout
    assert segment.logical_segment_id == offset.logical_segment_id == logical
    assert segment.ordinal == offset.ordinal == 63
    assert segment.file_sha256 == offset.file_sha256 == "a" * 64


def test_torn_append_renames_damaged_segment_and_continues_with_explicit_gap(
    tmp_path: Path,
) -> None:
    fired = False

    def crash(point: object) -> None:
        nonlocal fired
        if not fired and str(point) == "append.after_write":
            fired = True
            raise RuntimeError("crash")

    store = _store(tmp_path, crash)
    start = _start()
    opened = store.open(start)
    sample = _sample(start)
    with pytest.raises(RuntimeError):
        store.append_sample(opened.ref, opened.cursor, sample)
    segment = next(store.filesystem.paths.segments.glob("blk-*.jsonl"))  # type: ignore[union-attr]
    segment.write_bytes(segment.read_bytes()[:-7])
    recovered = JsonlV3CaptureStore(store.filesystem)
    cursor = recovered.append_sample(opened.ref, opened.cursor, sample)
    assert cursor.next_sequence == 3
    with store.filesystem.write_transaction() as tx:
        state = recovered._registry.read(tx).state.capture
        assert state is not None
        assert len(state.storage_segments) == 2
        assert state.storage_segments[0].damaged_file_sha256 is not None
        assert state.storage_segments[1].first_seq == 1
        assert state.gap_count == 1
        assert state.blackout_id == opened.ref.blackout_id
        assert state.logical_segment_id == opened.ref.segment_id
    paths = recovered.filesystem.paths
    assert paths is not None
    assert len(list(paths.segments.glob("damaged-*.jsonl"))) == 1
    assert len(list(paths.segments.glob("damaged-*.offsets"))) == 1


def test_damage_refuse_before_file_creation_at_reference_65(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = _start()
    store.open(start)
    with store.filesystem.write_transaction() as tx:
        state = store._registry.read(tx).state.capture
        assert state is not None
        receipt = state.storage_segments[0]
        many = tuple(replace(receipt, ordinal=index) for index in range(64))
        saturated = replace(state, storage_segments=many, append_intent=object())
        with pytest.raises(V3CapacityError):
            store._damage_plan(tx, saturated)


@pytest.mark.parametrize("phase", ["rename", "successor", "receipts"])
def test_damage_restart_converges_after_each_durable_phase(tmp_path: Path, phase: str) -> None:
    store = _store(tmp_path)
    start = _start()
    opened = store.open(start)
    sample = _sample(start)
    fired = False

    def crash_after(method: Callable[..., object]) -> Callable[..., object]:
        def wrapper(*args: object, **kwargs: object) -> object:
            nonlocal fired
            result = method(*args, **kwargs)
            if not fired:
                fired = True
                raise RuntimeError("phase crash")
            return result

        return wrapper

    target = {
        "rename": "_resume_damage_rename",
        "successor": "_resume_damage_successor",
        "receipts": "_resume_damage_receipts",
    }[phase]
    original = getattr(store, target)
    setattr(store, target, crash_after(original))
    segment = next(store.filesystem.paths.segments.glob("blk-*.jsonl"))  # type: ignore[union-attr]
    segment.write_bytes(segment.read_bytes()[:-7])
    with pytest.raises(RuntimeError):
        store.append_sample(opened.ref, opened.cursor, sample)
    recovered = JsonlV3CaptureStore(store.filesystem)
    page = recovered.recover(limit=1)
    assert page.active_capture is not None
    with recovered.filesystem.write_transaction() as tx:
        state = recovered._registry.read(tx).state.capture
        assert state is not None
        assert state.damage_continuation is None
        assert state.gap_count == 1
        assert len(state.storage_segments) == 2


@pytest.mark.parametrize(
    "fault_point",
    [
        "damage.after_segment_rename",
        "damage.after_segments_dirsync",
        "damage.after_continuation_create",
        "damage.after_continuation_fdatasync",
        "damage.after_registry_advance",
    ],
)
def test_damage_fault_hooks_converge_when_segment_and_offset_renames_split(
    tmp_path: Path, fault_point: str
) -> None:
    append_crash = True
    damage_crash = True

    def fault(point: object) -> None:
        nonlocal append_crash, damage_crash
        if append_crash and str(point) == "append.after_write":
            append_crash = False
            raise RuntimeError("append crash")
        if damage_crash and str(point) == fault_point:
            damage_crash = False
            raise RuntimeError("damage crash")

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
    with pytest.raises(RuntimeError):
        store.append_sample(opened.ref, opened.cursor, sample)

    restarted = JsonlV3CaptureStore(store.filesystem)
    page = restarted.recover(limit=1)
    assert page.active_capture is not None
    with restarted.filesystem.write_transaction() as tx:
        state = restarted._registry.read(tx).state.capture
        assert state is not None
        assert state.damage_continuation is None
        assert state.gap_count == 1
        assert len(state.storage_segments) == 2
