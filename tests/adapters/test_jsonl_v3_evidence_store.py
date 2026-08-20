from __future__ import annotations

import errno
import hashlib
import os
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import src.adapters.jsonl_v3_evidence_store as evidence_store
import src.adapters.jsonl_v3_filesystem_regions as filesystem_regions
from src.adapters.jsonl_v3_blackout_start_codec import encode_blackout_start
from src.adapters.jsonl_v3_errors import (
    V3CorruptionError,
    V3PathError,
    V3PersistenceError,
    V3ValidationError,
)
from src.adapters.jsonl_v3_evidence_store import (
    ActiveRegistryEvidenceSnapshotProvider,
    EvidenceAuthority,
    EvidenceSegmentReceipt,
    EvidenceSnapshot,
    JsonlV3EvidenceStore,
    JsonlV3FilesystemEvidenceOffsetReader,
    JsonlV3FilesystemEvidenceReader,
    SealedLocator,
    SealedLocatorEvidenceSnapshotProvider,
    _decode_value,
)
from src.adapters.jsonl_v3_filesystem import JsonlV3Filesystem
from src.adapters.jsonl_v3_filesystem_regions import V3ReadOnlyFilesystemRegions
from src.adapters.jsonl_v3_registry import JsonlV3WorkRegistry
from src.adapters.jsonl_v3_registry_values import V3StorageSegmentReceipt
from src.adapters.jsonl_v3_segment_index import (
    OFFSET_ENTRY_SIZE,
    OFFSET_HEADER,
    OffsetEntry,
    OffsetRecordKind,
    SegmentIndexEntry,
    SegmentIndexPage,
    SegmentIndexSnapshot,
    encode_offset_entry,
)
from src.adapters.jsonl_v3_storage_paths import (
    V3DamagedOffsetPathToken,
    V3DamagedSegmentPathToken,
    V3OffsetPathToken,
    V3SegmentPathToken,
)
from src.adapters.jsonl_v3_terminal_tail_codec import encode_endpoint_anchor
from src.application.blackout_storage_values import (
    BlackoutCaptureCursor,
    BlackoutChainKind,
    BlackoutRef,
)
from src.domain.fragment_primitives import AnchorKind
from tests.adapters.test_jsonl_v3_physical_codecs import _start
from tests.adapters.test_jsonl_v3_terminal_tail_codec import _anchor_for_kind

HASH = "a" * 64
BLACKOUT = "00000000000040008000000000000001"
SEGMENT = "00000000000040008000000000000002"
STORAGE = "00000000000040008000000000000003"
UTC = "2026-08-18T12:34:56.123456Z"


class SnapshotProvider:
    def __init__(self, snapshot: EvidenceSnapshot) -> None:
        self.snapshot_value = snapshot
        self.calls = 0

    def snapshot(self, ref: BlackoutRef) -> EvidenceSnapshot:
        self.calls += 1
        return self.snapshot_value


class Reader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[int, int]] = []

    def read_exact(self, token: object, *, offset: int, length: int) -> bytes:
        del token
        self.calls.append((offset, length))
        return self.payload[offset : offset + length]

    def read_authenticated(self, receipt, entry):
        return self.read_exact(
            receipt.path_token, offset=entry.file_offset, length=entry.line_length
        )


class Offsets:
    def __init__(self, entry: SegmentIndexEntry) -> None:
        self.entry = entry
        self.calls: list[int] = []

    def snapshot_and_page(
        self, receipt: EvidenceSegmentReceipt, *, first_sequence: int, limit: int
    ) -> tuple[SegmentIndexEntry, ...]:
        del receipt, limit
        self.calls.append(first_sequence)
        return (self.entry,) if self.entry.sequence >= first_sequence else ()


def _fixture() -> tuple[
    JsonlV3EvidenceStore, SnapshotProvider, Reader, BlackoutRef, SegmentIndexEntry, Offsets
]:
    ref = BlackoutRef(BLACKOUT, SEGMENT)
    start = _start(blackout_id=BLACKOUT, segment_id=SEGMENT)
    encoded = encode_blackout_start(start)
    token = V3SegmentPathToken(BLACKOUT, SEGMENT, "2026-08-18T12:34:56.123456Z", 0, STORAGE)
    offset = V3OffsetPathToken(BLACKOUT, SEGMENT, "2026-08-18T12:34:56.123456Z", 0, STORAGE)
    receipt = EvidenceSegmentReceipt(
        ref,
        0,
        STORAGE,
        token,
        offset,
        hashlib.sha256(encoded.line).hexdigest(),
        HASH,
        len(encoded.line),
        len(encoded.line),
        0,
        0,
        encoded.record_sha256,
        encoded.record_sha256,
        False,
    )
    entry = SegmentIndexEntry(
        0, 0, len(encoded.line), encoded.record_sha256, OffsetRecordKind.START
    )
    provider = SnapshotProvider(
        EvidenceSnapshot(ref, encoded.record_sha256, encoded.record_sha256, (receipt,))
    )
    reader = Reader(encoded.line)
    offsets = Offsets(entry)
    return JsonlV3EvidenceStore(provider, reader, offsets), provider, reader, ref, entry, offsets


def _readonly_offset_fixture(
    tmp_path: Path, *, entry_count: int = 1
) -> tuple[V3ReadOnlyFilesystemRegions, V3OffsetPathToken, Path]:
    os.chmod(tmp_path, 0o700)

    class Lease:
        state_root_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)

        def validate(self, state_root: Path) -> None:
            assert state_root == tmp_path
            assert (state_root.stat().st_dev, state_root.stat().st_ino) == self.state_root_identity

        @contextmanager
        def hold(self):
            yield self

    filesystem = JsonlV3Filesystem(tmp_path, writer_lease=Lease())
    filesystem.ensure_layout()
    paths = filesystem.paths
    assert paths is not None
    segment = paths.segment_token(UTC, BLACKOUT, SEGMENT, 0, STORAGE)
    offset = paths.offset_token(segment)
    with filesystem.write_transaction() as tx:
        snapshot = tx.create_offset_index(offset)
        for sequence in range(entry_count):
            snapshot = tx.append_offset_index(
                offset,
                expected=snapshot,
                entry=SegmentIndexEntry(
                    sequence,
                    sequence,
                    1,
                    HASH,
                    OffsetRecordKind.START if sequence == 0 else OffsetRecordKind.SAMPLE,
                ),
            )
        tx.seal(offset, expected_length=snapshot.byte_length, max_bytes=64 * 1024 * 1024)
    offset_path = next(paths.segments.glob("*.offsets"))
    return V3ReadOnlyFilesystemRegions(tmp_path), offset, offset_path


def test_page_reads_start_by_authorized_offset_and_is_physical_complete() -> None:
    store, provider, reader, ref, _, _ = _fixture()
    page = store.page(ref)
    assert page.complete and len(page.records) == 1
    assert page.records[0].ref.sequence == 0
    assert provider.calls == 1 and reader.calls


def test_page_rejects_terminal_or_forged_cursor() -> None:
    store, _, _, ref, _, _ = _fixture()
    terminal = BlackoutCaptureCursor(BLACKOUT, SEGMENT, BlackoutChainKind.TERMINAL, 1, HASH)
    with pytest.raises(V3ValidationError):
        store.page(ref, terminal)
    forged = BlackoutCaptureCursor(BLACKOUT, SEGMENT, BlackoutChainKind.PHYSICAL, 1, "b" * 64)
    with pytest.raises(V3CorruptionError):
        store.page(ref, forged)


def test_snapshot_is_requested_per_call_and_scope_is_verified() -> None:
    store, provider, _, ref, _, _ = _fixture()
    store.page(ref)
    store.page(ref)
    assert provider.calls == 2
    other = replace(ref, blackout_id="00000000000040008000000000000004")
    invalid = object.__new__(EvidenceSnapshot)
    object.__setattr__(invalid, "ref", other)
    object.__setattr__(invalid, "segments", provider.snapshot_value.segments)
    object.__setattr__(invalid, "physical_chain_root_record_sha256", HASH)
    object.__setattr__(invalid, "physical_chain_final_record_sha256", HASH)
    provider.snapshot_value = cast(EvidenceSnapshot, invalid)
    with pytest.raises(V3PathError):
        store.page(ref)


def test_receipt_rejects_damaged_hash_and_gap_topology() -> None:
    _, _, _, ref, _, _ = _fixture()
    token = V3DamagedSegmentPathToken(BLACKOUT, SEGMENT, 0, STORAGE, HASH)
    offset = V3DamagedOffsetPathToken(BLACKOUT, SEGMENT, 0, STORAGE, HASH)
    with pytest.raises(V3CorruptionError):
        EvidenceSegmentReceipt(
            ref, 0, STORAGE, token, offset, "b" * 64, HASH, 1, 1, 0, 0, HASH, HASH, True
        )


def test_offset_kind_tampering_is_rejected() -> None:
    store, _, _, ref, entry, offsets = _fixture()
    offsets.entry = replace(entry, record_kind=OffsetRecordKind.SAMPLE)
    with pytest.raises(V3CorruptionError):
        store.page(ref)


def test_topology_mutation_and_offset_page_overflow_fail_closed() -> None:
    store, provider, _, ref, entry, offsets = _fixture()
    receipt = provider.snapshot_value.segments[0]
    with pytest.raises((V3CorruptionError, V3PathError)):
        provider.snapshot_value = replace(
            provider.snapshot_value,
            segments=(replace(receipt, ordinal=1),),
        )
        store.page(ref)

    store, _, _, ref, entry, _ = _fixture()

    class TooManyOffsets(Offsets):
        def snapshot_and_page(self, receipt, *, first_sequence, limit):
            del receipt, first_sequence, limit
            return tuple(replace(entry, sequence=index) for index in range(1025))

    store = JsonlV3EvidenceStore(store._snapshots, store._reader, TooManyOffsets(entry))
    with pytest.raises(V3CorruptionError):
        store.page(ref)


def test_short_authenticated_read_and_sealed_damaged_receipt_are_typed() -> None:
    store, provider, reader, ref, _, _ = _fixture()
    reader.payload = b""
    with pytest.raises(V3CorruptionError):
        store.page(ref)

    class Locator:
        def read(self, requested):
            assert requested == ref
            return SealedLocator(
                ref,
                locator_snapshot.physical_chain_root_record_sha256,
                locator_snapshot.physical_chain_final_record_sha256,
                (damaged,),
            )

    damaged_hash = provider.snapshot_value.segments[0].file_sha256
    assert damaged_hash is not None
    damaged = replace(
        provider.snapshot_value.segments[0],
        path_token=V3DamagedSegmentPathToken(BLACKOUT, SEGMENT, 0, STORAGE, damaged_hash),
        offset_token=V3DamagedOffsetPathToken(BLACKOUT, SEGMENT, 0, STORAGE, damaged_hash),
        byte_length=provider.snapshot_value.segments[0].byte_length + 1,
        trusted_byte_length=provider.snapshot_value.segments[0].trusted_byte_length,
        file_sha256=damaged_hash,
        damaged=True,
    )
    locator_snapshot = provider.snapshot_value
    locator = Locator()
    sealed = SealedLocatorEvidenceSnapshotProvider(locator).snapshot(ref)
    assert sealed.segments[0].authority.value == "sealed_locator"
    assert sealed.segments[0].damaged


def test_authenticated_reader_passes_a_64_mib_file_bound_without_scanning() -> None:
    _, _, _, ref, _, _ = _fixture()
    receipt = _fixture()[0]._snapshots.snapshot_value.segments[0]
    calls = []

    class Transaction:
        def read_region(self, token, *, offset, length, max_file_bytes):
            calls.append((token, offset, length, max_file_bytes))
            return SimpleNamespace(file_length=64 * 1024 * 1024, contents=b"x")

    token = receipt.path_token
    output = JsonlV3FilesystemEvidenceReader(Transaction()).read_exact(
        token, offset=64 * 1024 * 1024 - 1, length=1
    )
    assert output == b"x"
    assert calls[0][3] == 64 * 1024 * 1024


def test_authenticated_reader_verifies_decoded_envelope_hash() -> None:
    _, _, _, ref, entry, _ = _fixture()
    receipt = _fixture()[0]._snapshots.snapshot_value.segments[0]
    encoded = encode_blackout_start(_start(blackout_id=BLACKOUT, segment_id=SEGMENT))
    malformed = encoded.line.replace(
        b'"record_sha256":"' + encoded.record_sha256.encode(), b'"record_sha256":"' + HASH.encode()
    )

    class Transaction:
        def read_region(self, token, *, offset, length, max_file_bytes):
            del token, offset, length, max_file_bytes
            return SimpleNamespace(file_length=receipt.byte_length, contents=malformed)

    with pytest.raises(V3CorruptionError):
        JsonlV3FilesystemEvidenceReader(Transaction()).read_authenticated(receipt, entry)


def test_repeated_child_fstat_failures_keep_proc_fd_count_stable(
    tmp_path: Path, monkeypatch
) -> None:
    regions, offset, offset_path = _readonly_offset_fixture(tmp_path)
    target = offset_path.parent.stat()
    real_fstat = os.fstat

    def fail_child_fstat(fd: int):
        info = real_fstat(fd)
        if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
            raise OSError(errno.EIO, "injected child fstat failure")
        return info

    monkeypatch.setattr(filesystem_regions.os, "fstat", fail_child_fstat)
    before = len(list(Path("/proc/self/fd").iterdir()))
    for _ in range(32):
        with pytest.raises(V3PersistenceError):
            regions.file_sha256(offset)
    after = len(list(Path("/proc/self/fd").iterdir()))
    assert after == before


def test_repeated_final_file_fstat_failures_keep_proc_fd_count_stable(
    tmp_path: Path, monkeypatch
) -> None:
    regions, offset, offset_path = _readonly_offset_fixture(tmp_path)
    target = offset_path.stat()
    real_fstat = os.fstat

    def fail_final_fstat(fd: int):
        info = real_fstat(fd)
        if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
            raise OSError(errno.EIO, "injected final fstat failure")
        return info

    monkeypatch.setattr(filesystem_regions.os, "fstat", fail_final_fstat)
    before = len(list(Path("/proc/self/fd").iterdir()))
    for _ in range(32):
        with pytest.raises(V3PersistenceError):
            regions.file_sha256(offset)
    after = len(list(Path("/proc/self/fd").iterdir()))
    assert after == before


def test_snapshot_fstat_oserror_is_typed_persistence_error(tmp_path: Path, monkeypatch) -> None:
    regions, offset, offset_path = _readonly_offset_fixture(tmp_path)
    target = offset_path.stat()
    real_fstat = os.fstat
    target_calls = 0

    def fail_snapshot_fstat(fd: int):
        nonlocal target_calls
        info = real_fstat(fd)
        if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
            target_calls += 1
            if target_calls == 2:
                raise OSError(errno.EIO, "injected snapshot fstat failure")
        return info

    monkeypatch.setattr(filesystem_regions.os, "fstat", fail_snapshot_fstat)
    with pytest.raises(V3PersistenceError):
        regions.snapshot_offset_index(offset)
    assert target_calls == 2


def test_page_pread_oserror_is_typed_persistence_error(tmp_path: Path, monkeypatch) -> None:
    regions, offset, _ = _readonly_offset_fixture(tmp_path, entry_count=2)
    real_pread = os.pread
    entry_reads = 0

    def fail_page_pread(fd: int, size: int, position: int) -> bytes:
        nonlocal entry_reads
        if size == OFFSET_ENTRY_SIZE and position == 8:
            entry_reads += 1
            if entry_reads == 2:
                raise OSError(errno.EIO, "injected page pread failure")
        return real_pread(fd, size, position)

    monkeypatch.setattr(filesystem_regions.os, "pread", fail_page_pread)
    with pytest.raises(V3PersistenceError):
        regions.page_offset_index(offset, entry_ordinal=0, limit=1)
    assert entry_reads == 2


def test_digest_pread_oserror_is_typed_persistence_error(tmp_path: Path, monkeypatch) -> None:
    regions, offset, _ = _readonly_offset_fixture(tmp_path)

    def fail_digest_pread(fd: int, size: int, position: int) -> bytes:
        del fd, size, position
        raise OSError(errno.EIO, "injected digest pread failure")

    monkeypatch.setattr(filesystem_regions.os, "pread", fail_digest_pread)
    with pytest.raises(V3PersistenceError):
        regions.file_sha256(offset)


def test_final_symlink_is_refused_as_an_unsafe_path(tmp_path: Path) -> None:
    regions, offset, offset_path = _readonly_offset_fixture(tmp_path)
    outside = tmp_path / "outside.offsets"
    outside.write_bytes(OFFSET_HEADER)
    os.chmod(outside, 0o400)
    offset_path.unlink()
    offset_path.symlink_to(outside)
    with pytest.raises(V3PathError):
        regions.file_sha256(offset)


def test_final_hardlink_is_refused_when_nlink_exceeds_one(tmp_path: Path) -> None:
    regions, offset, offset_path = _readonly_offset_fixture(tmp_path)
    os.link(offset_path, tmp_path / "offset-alias.offsets")
    with pytest.raises(V3PathError):
        regions.file_sha256(offset)


def test_authenticated_page_uses_same_fd_after_pathname_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    regions, offset, offset_path = _readonly_offset_fixture(tmp_path)
    original = offset_path.read_bytes()
    replacement = tmp_path / "replacement.offsets"
    replacement_payload = OFFSET_HEADER + encode_offset_entry(
        OffsetEntry(99, 99, 1, HASH, int(OffsetRecordKind.START))
    )
    replacement.write_bytes(replacement_payload)
    os.chmod(replacement, 0o400)
    original_digest = hashlib.sha256(original).hexdigest()
    original_digest_method = filesystem_regions._OffsetReadHandle.digest

    def replace_after_digest(handle, length: int) -> str:
        digest = original_digest_method(handle, length)
        os.replace(replacement, offset_path)
        return digest

    monkeypatch.setattr(filesystem_regions._OffsetReadHandle, "digest", replace_after_digest)
    page = regions.authenticated_offset_page(
        offset,
        entry_ordinal=0,
        limit=1,
        expected_sha256=original_digest,
        sealed=True,
    )
    assert page.entries[0].sequence == 0
    assert offset_path.read_bytes() == replacement_payload


def test_real_filesystem_start_uses_authenticated_canonical_read(tmp_path) -> None:
    os.chmod(tmp_path, 0o700)

    class Lease:
        state_root_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)

        def validate(self, state_root):
            assert state_root == tmp_path
            assert (state_root.stat().st_dev, state_root.stat().st_ino) == self.state_root_identity

        @contextmanager
        def hold(self):
            yield self

    filesystem = JsonlV3Filesystem(tmp_path, writer_lease=Lease())
    filesystem.ensure_layout()
    start = encode_blackout_start(_start(blackout_id=BLACKOUT, segment_id=SEGMENT))
    token = filesystem.paths.segment_token(  # type: ignore[union-attr]
        "2026-08-18T12:34:56.123456Z", BLACKOUT, SEGMENT, 0, STORAGE
    )
    offset = filesystem.paths.offset_token(token)  # type: ignore[union-attr]
    with filesystem.write_transaction() as tx:
        tx.create_and_sync(token, start.line, 64 * 1024 * 1024)
        expected = tx.create_offset_index(offset)
        entry = SegmentIndexEntry(
            0, 0, len(start.line), start.record_sha256, OffsetRecordKind.START
        )
        tx.append_offset_index(offset, expected=expected, entry=entry)
        receipt = EvidenceSegmentReceipt(
            BlackoutRef(BLACKOUT, SEGMENT),
            0,
            STORAGE,
            token,
            offset,
            hashlib.sha256(start.line).hexdigest(),
            tx.file_sha256(offset),
            len(start.line),
            len(start.line),
            0,
            0,
            start.record_sha256,
            start.record_sha256,
            False,
            EvidenceAuthority.SEALED_LOCATOR,
        )
        snapshot = EvidenceSnapshot(
            receipt.ref, start.record_sha256, start.record_sha256, (receipt,)
        )

        class Provider:
            def snapshot(self, ref):
                assert ref == receipt.ref
                return snapshot

    for path in filesystem.paths.segments.glob("blk-*"):  # type: ignore[union-attr]
        os.chmod(path, 0o400)
    regions = V3ReadOnlyFilesystemRegions(tmp_path)
    store = JsonlV3EvidenceStore(
        Provider(),
        JsonlV3FilesystemEvidenceReader(regions),
        JsonlV3FilesystemEvidenceOffsetReader(regions),
    )
    page = store.page(receipt.ref)
    assert page.complete and page.records[0].ref.record_sha256 == start.record_sha256
    segment_path = next(filesystem.paths.segments.glob("blk-*.jsonl"))  # type: ignore[union-attr]
    original = segment_path.read_bytes()
    os.chmod(segment_path, 0o600)
    with pytest.raises(V3PathError):
        store.page(receipt.ref)
    os.chmod(segment_path, 0o400)
    os.chmod(segment_path, 0o600)
    segment_path.write_bytes(original[:-1] + b"x")
    os.chmod(segment_path, 0o400)
    with pytest.raises(V3CorruptionError):
        store.page(receipt.ref)
    os.chmod(segment_path, 0o600)
    segment_path.write_bytes(original + b"x")
    os.chmod(segment_path, 0o400)
    with pytest.raises(V3CorruptionError):
        store.page(receipt.ref)

    replacement = tmp_path.with_name(f"{tmp_path.name}-replaced")
    tmp_path.rename(replacement)
    tmp_path.mkdir(mode=0o700)
    try:
        with pytest.raises((V3PathError, V3PersistenceError)):
            regions.file_sha256(offset)
    finally:
        tmp_path.rmdir()
        replacement.rename(tmp_path)


def test_sealed_offset_hash_uses_full_digest_and_keeps_active_cas_distinct() -> None:
    store, _, _, ref, entry, _ = _fixture()
    receipt = store._snapshots.snapshot_value.segments[0]
    actual_full = "c" * 64
    sealed = replace(
        receipt,
        authority=EvidenceAuthority.SEALED_LOCATOR,
        file_sha256="d" * 64,
        offset_table_sha256=actual_full,
    )
    snapshot = SegmentIndexSnapshot(1, 0, 0, 64, "e" * 64)

    class Transaction:
        def snapshot_offset_index(self, token):
            del token
            return snapshot

        def file_sha256(self, token):
            del token
            return actual_full

        def page_offset_index(self, token, *, entry_ordinal, limit):
            del token, entry_ordinal, limit
            return SegmentIndexPage((entry,), None, True)

    offsets = JsonlV3FilesystemEvidenceOffsetReader(Transaction())
    assert offsets.snapshot_and_page(sealed, first_sequence=0, limit=1) == (entry,)
    bad = replace(sealed, offset_table_sha256=snapshot.append_state_sha256)
    with pytest.raises(V3CorruptionError):
        offsets.snapshot_and_page(bad, first_sequence=0, limit=1)


def test_1024_record_budget_is_global_across_segments(monkeypatch) -> None:
    ref = BlackoutRef(BLACKOUT, SEGMENT)
    start = encode_blackout_start(_start(blackout_id=BLACKOUT, segment_id=SEGMENT))
    anchor = replace(
        _anchor_for_kind(AnchorKind.TRANSFER_TO_BATTERY),
        blackout_id=BLACKOUT,
        segment_id=SEGMENT,
    )
    lines = [start]
    for sequence in range(1, 1024):
        encoded = encode_endpoint_anchor(
            anchor, seq=sequence, previous_record_sha256=lines[-1].record_sha256
        )
        lines.append(encoded)
    tokens = [
        V3SegmentPathToken(BLACKOUT, SEGMENT, "2026-08-18T12:34:56.123456Z", ordinal, storage)
        for ordinal, storage in enumerate((STORAGE, "00000000000040008000000000000004"))
    ]
    segments = []
    entries: dict[object, tuple[SegmentIndexEntry, ...]] = {}
    payloads: dict[object, bytes] = {}
    groups = (tuple(range(0, 512)), tuple(range(512, 1024)))
    for ordinal, (token, group) in enumerate(zip(tokens, groups, strict=True)):
        selected = tuple(
            SegmentIndexEntry(
                index,
                sum(len(lines[item].line) for item in group[:position]),
                len(lines[index].line),
                lines[index].record_sha256,
                OffsetRecordKind.START if index == 0 else OffsetRecordKind.ANCHOR,
            )
            for position, index in enumerate(group)
        )
        entries[token] = selected
        payloads[token] = b"".join(lines[index].line for index in group)
        offset = V3OffsetPathToken(
            token.blackout_id,
            token.logical_segment_id,
            token.started_utc,
            ordinal,
            token.storage_id,
        )
        segments.append(
            EvidenceSegmentReceipt(
                ref,
                ordinal,
                token.storage_id,
                token,
                offset,
                None,
                HASH,
                len(payloads[token]),
                len(payloads[token]),
                group[0],
                group[-1],
                lines[group[0]].record_sha256,
                lines[group[-1]].record_sha256,
                False,
            )
        )

    class Provider:
        def snapshot(self, requested):
            assert requested == ref
            return EvidenceSnapshot(
                ref, lines[0].record_sha256, lines[-1].record_sha256, tuple(segments)
            )

    class Reader:
        def read_exact(self, token, *, offset, length):
            return payloads[token][offset : offset + length]

        def read_authenticated(self, receipt, entry):
            return self.read_exact(
                receipt.path_token, offset=entry.file_offset, length=entry.line_length
            )

    class Offsets:
        def snapshot_and_page(self, receipt, *, first_sequence, limit):
            return tuple(
                item for item in entries[receipt.path_token] if item.sequence >= first_sequence
            )[:limit]

    page = JsonlV3EvidenceStore(Provider(), Reader(), Offsets()).page(ref)
    assert page.complete and len(page.records) == 1024
    exact_boundary = sum(len(lines[index].line) for index in groups[0])
    monkeypatch.setattr(evidence_store, "MAX_EVIDENCE_PAGE_BYTES", exact_boundary)
    bounded = JsonlV3EvidenceStore(Provider(), Reader(), Offsets()).page(ref)
    assert not bounded.complete and len(bounded.records) == 512
    assert bounded.next_cursor is not None and bounded.next_cursor.next_sequence == 512


@pytest.mark.parametrize(
    ("kind", "accepted"),
    (
        (AnchorKind.TRANSFER_TO_BATTERY, True),
        (AnchorKind.RAW_FIRMWARE_LB, True),
        (AnchorKind.MODELED_SAFE_SHUTDOWN, False),
        (AnchorKind.POWER_RESTORED, False),
        (AnchorKind.SERVICE_STOP, False),
        (AnchorKind.BOOT_BOUNDARY, False),
        (AnchorKind.CHARGE_STABILIZED, False),
        (AnchorKind.GAP, False),
        (AnchorKind.CORRUPTION, False),
    ),
)
def test_only_the_two_intermediate_anchor_kinds_are_physical(
    kind: AnchorKind, accepted: bool
) -> None:
    encoded = encode_endpoint_anchor(_anchor_for_kind(kind), seq=1, previous_record_sha256="b" * 64)
    if accepted:
        value, record_type = _decode_value("endpoint_anchor", encoded.line)
        assert value.kind is kind
        assert record_type.value == "endpoint_anchor"
    else:
        with pytest.raises(V3CorruptionError):
            _decode_value("endpoint_anchor", encoded.line)


def test_active_provider_fails_closed_for_missing_or_empty_registry_work() -> None:
    class FakeFilesystem:
        @contextmanager
        def write_transaction(self):
            yield object()

    class FakeRegistry:
        def __init__(self, state: object) -> None:
            self.state = state

        def read(self, transaction: object) -> object:
            del transaction
            state = self.state
            if state is None:
                state = type("Empty", (), {"capture": None, "pending": ()})()
            else:
                state = type("Active", (), {"capture": state, "pending": ()})()
            return type("Snapshot", (), {"state": state})()

    provider = object.__new__(ActiveRegistryEvidenceSnapshotProvider)
    provider._filesystem = cast(JsonlV3Filesystem, FakeFilesystem())
    provider._registry = cast(JsonlV3WorkRegistry, FakeRegistry(None))
    ref = BlackoutRef(BLACKOUT, SEGMENT)
    with pytest.raises(V3PathError):
        provider.snapshot(ref)
    empty_state = type(
        "State",
        (),
        {"blackout_id": BLACKOUT, "logical_segment_id": SEGMENT, "storage_segments": ()},
    )()
    provider._registry = cast(JsonlV3WorkRegistry, FakeRegistry(empty_state))
    with pytest.raises(V3PathError):
        provider.snapshot(ref)


class _RegistryTransaction:
    def __init__(self, entry: SegmentIndexEntry | None, file_length: object) -> None:
        self.snapshot = SegmentIndexSnapshot(1, 0, 0, 64, HASH)
        self.entry = entry
        self.file_length_value = file_length

    def snapshot_offset_index(self, token: object) -> SegmentIndexSnapshot:
        del token
        return self.snapshot

    def get_offset_index(self, token: object, *, sequence: int) -> SegmentIndexEntry | None:
        del token, sequence
        return self.entry

    def file_length(self, token: object) -> object:
        del token
        return self.file_length_value


def _registry_receipt(*, damaged: bool = False) -> V3StorageSegmentReceipt:
    _, provider, _, _, entry, _ = _fixture()
    source = provider.snapshot_value.segments[0]
    if damaged:
        file_hash = source.file_sha256
        assert file_hash is not None
        path_token = V3DamagedSegmentPathToken(BLACKOUT, SEGMENT, 0, STORAGE, file_hash)
        offset_token = V3DamagedOffsetPathToken(BLACKOUT, SEGMENT, 0, STORAGE, file_hash)
    else:
        file_hash = None
        path_token = source.path_token
        offset_token = source.offset_token
    return V3StorageSegmentReceipt(
        0,
        STORAGE,
        path_token,
        offset_token,
        entry.line_length,
        0,
        0,
        entry.record_sha256,
        file_hash,
        False,
    )


@pytest.mark.parametrize("damaged", [False, True])
def test_registry_receipt_materializes_active_and_damaged_variants(damaged: bool) -> None:
    _, provider, _, ref, entry, _ = _fixture()
    registry_receipt = _registry_receipt(damaged=damaged)
    transaction = _RegistryTransaction(entry, entry.line_length)
    result = evidence_store._receipt_from_registry(transaction, ref, registry_receipt)
    assert result.damaged is damaged
    assert result.trusted_byte_length == entry.line_length
    assert result.first_record_sha256 == entry.record_sha256
    assert result.last_record_sha256 == entry.record_sha256
    assert result.authority is EvidenceAuthority.ACTIVE_REGISTRY
    if damaged:
        assert isinstance(result.path_token, V3DamagedSegmentPathToken)
    else:
        assert result.path_token == provider.snapshot_value.segments[0].path_token


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing_entry", V3CorruptionError),
        ("snapshot_topology", V3CorruptionError),
        ("entry_topology", V3CorruptionError),
        ("invalid_length", V3CorruptionError),
        ("short_file", V3CorruptionError),
        ("wrong_token", V3PathError),
    ],
)
def test_registry_receipt_rejects_untrusted_bounds_and_tokens(
    case: str, expected: type[Exception]
) -> None:
    _, _, _, ref, entry, _ = _fixture()
    registry_receipt = _registry_receipt()
    transaction = _RegistryTransaction(entry, entry.line_length)
    if case == "missing_entry":
        transaction.entry = None
    elif case == "snapshot_topology":
        transaction.snapshot = replace(transaction.snapshot, first_sequence=1)
    elif case == "entry_topology":
        transaction.entry = replace(entry, sequence=1)
    elif case == "invalid_length":
        transaction.file_length_value = "not-an-int"
    elif case == "short_file":
        transaction.file_length_value = entry.line_length - 1
    else:
        registry_receipt = replace(registry_receipt, offset_token=object())  # type: ignore[arg-type]
    with pytest.raises(expected):
        evidence_store._receipt_from_registry(transaction, ref, registry_receipt)


def test_read_only_offset_page_handles_partial_empty_and_sequence_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    regions, offset, offset_path = _readonly_offset_fixture(tmp_path, entry_count=2)
    partial = regions.page_offset_index(offset, entry_ordinal=0, limit=1)
    assert len(partial.entries) == 1 and partial.next_entry_ordinal == 1
    complete = regions.page_offset_index(offset, entry_ordinal=1, limit=1)
    assert len(complete.entries) == 1 and complete.complete
    empty = regions.page_offset_index(offset, entry_ordinal=2, limit=1)
    assert empty.entries == () and empty.complete

    payload = offset_path.read_bytes()
    replacement = (
        OFFSET_HEADER
        + payload[8 : 8 + OFFSET_ENTRY_SIZE]
        + encode_offset_entry(OffsetEntry(9, 1, 1, HASH, int(OffsetRecordKind.SAMPLE)))
    )
    os.chmod(offset_path, 0o600)
    offset_path.write_bytes(replacement)
    os.chmod(offset_path, 0o400)
    monkeypatch.setattr(
        regions,
        "snapshot_offset_index",
        lambda token: SegmentIndexSnapshot(2, 0, 1, len(replacement), HASH),
    )
    with pytest.raises(V3CorruptionError):
        regions.page_offset_index(offset, entry_ordinal=1, limit=1)
