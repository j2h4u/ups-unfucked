from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import pytest

from src.adapters.jsonl_v3_errors import V3AppendConflict, V3ValidationError
from src.adapters.jsonl_v3_filesystem import JsonlV3Filesystem
from src.adapters.jsonl_v3_segment_index import (
    OFFSET_ENTRY_SIZE,
    OFFSET_HEADER,
    JsonlV3SegmentIndex,
    OffsetRecordKind,
    SegmentIndexEntry,
)
from src.adapters.jsonl_v3_storage_paths import DIRECTORY_MODE
from tests.adapters.test_jsonl_v3_filesystem import Capability


def setup_index(tmp_path: Path):
    os.chmod(tmp_path, DIRECTORY_MODE)
    capability = Capability(tmp_path)
    filesystem = JsonlV3Filesystem(tmp_path, writer_lease=capability)
    filesystem.ensure_layout()
    paths = filesystem.paths
    assert paths is not None
    segment = paths.segment_token(
        "2026-08-18T12:34:56.123456Z", uuid.uuid4().hex, uuid.uuid4().hex, 0, uuid.uuid4().hex
    )
    index = JsonlV3SegmentIndex(filesystem, paths.offset_token(segment))
    return filesystem, index


def entry(sequence: int, offset: int = 0) -> SegmentIndexEntry:
    return SegmentIndexEntry(
        sequence, offset, 3, hashlib.sha256(b"x").hexdigest(), OffsetRecordKind.SAMPLE
    )


def test_create_snapshot_formula_and_nonzero_first_sequence(tmp_path: Path) -> None:
    filesystem, index = setup_index(tmp_path)
    with filesystem.write_transaction() as tx:
        snapshot = index.create(tx)
        assert snapshot.entry_count == 0
        assert OFFSET_HEADER == b"UBMV3OF\x01"
        snapshot = index.append(tx, expected=snapshot, entry=entry(4))
        assert snapshot.first_sequence == 4
        assert snapshot.append_state_sha256
        assert index.get(tx, 3) is None
        assert index.get(tx, 4) == entry(4)


def test_fixed_width_entry_and_restart_snapshot_are_stable(tmp_path: Path) -> None:
    filesystem, index = setup_index(tmp_path)
    with filesystem.write_transaction() as tx:
        snapshot = index.create(tx)
        snapshot = index.append(tx, expected=snapshot, entry=entry(9))
        assert OFFSET_ENTRY_SIZE == 56
        restarted = index.snapshot(tx)
        assert restarted == snapshot


def test_append_cas_and_bounded_page(tmp_path: Path) -> None:
    filesystem, index = setup_index(tmp_path)
    with filesystem.write_transaction() as tx:
        snap = index.create(tx)
        snap = index.append(tx, expected=snap, entry=entry(1))
        with pytest.raises(V3AppendConflict):
            index.append(tx, expected=type(snap)(0, None, None, 8, "0" * 64), entry=entry(2, 3))
        snap = index.append(tx, expected=snap, entry=entry(2, 3))
        page = index.page(tx, limit=1)
        assert len(page.entries) == 1 and page.next_entry_ordinal == 1
        assert not page.complete
        assert snap.entry_count == 2


def test_invalid_kind_and_page_bounds_fail_closed(tmp_path: Path) -> None:
    filesystem, index = setup_index(tmp_path)
    with filesystem.write_transaction() as tx:
        snap = index.create(tx)
        with pytest.raises(V3ValidationError):
            index.append(tx, expected=snap, entry=SegmentIndexEntry(1, 0, 3, "0" * 64, 99))  # type: ignore[arg-type]
        with pytest.raises(V3ValidationError):
            index.append(
                tx,
                expected=snap,
                entry=SegmentIndexEntry(1, 0, 3, "0" * 64, "SAMPLE"),  # type: ignore[arg-type]
            )
        with pytest.raises(V3ValidationError):
            index.page(tx, limit=0)


def test_typed_temporary_offset_rebuild_promotes_existing_api(tmp_path: Path) -> None:
    filesystem, index = setup_index(tmp_path)
    paths = filesystem.paths
    assert paths is not None
    with filesystem.write_transaction() as tx:
        temporary = paths.temporary_token(index.token)
        snapshot = tx.create_offset_index(temporary)
        snapshot = tx.append_offset_index(temporary, expected=snapshot, entry=entry(7))
        sealed = tx.seal(temporary, expected_length=snapshot.byte_length, max_bytes=4096)
        promoted = tx.promote(
            temporary, index.token, expected_source=sealed, require_target_absent=True
        )
        assert promoted == sealed
        assert tx.snapshot_offset_index(index.token) == snapshot


def test_3198_offset_appends_keep_fd_count_bounded(tmp_path: Path) -> None:
    filesystem, index = setup_index(tmp_path)
    with filesystem.write_transaction() as tx:
        snapshot = index.create(tx)
        baseline = len(list(Path("/proc/self/fd").iterdir()))
        for sequence in range(3198):
            snapshot = index.append(tx, expected=snapshot, entry=entry(sequence, sequence * 3))
        assert len(list(Path("/proc/self/fd").iterdir())) - baseline < 16
