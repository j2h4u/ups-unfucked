"""Private bounded region and capture-file primitives for the v3 transaction."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from src.adapters.jsonl_v3_errors import (
    V3CapacityError,
    V3CorruptionError,
    V3PathBindingConflict,
    V3PathError,
    V3PersistenceError,
    V3ValidationError,
    bounded_os_error,
)
from src.adapters.jsonl_v3_segment_index import (
    OFFSET_ENTRY_SIZE,
    OFFSET_HEADER,
    OffsetRecordKind,
    SegmentIndexEntry,
    SegmentIndexPage,
    SegmentIndexSnapshot,
    append_state_hash,
    decode_offset_entry,
)
from src.adapters.jsonl_v3_storage_paths import (
    DIRECTORY_MODE,
    MUTABLE_MODE,
    SEALED_MODE,
    V3CreatableCaptureToken,
    V3DamagedOffsetPathToken,
    V3DamagedSegmentPathToken,
    V3OffsetPathToken,
    V3ReadableFileToken,
    V3SegmentPathToken,
    V3StoragePaths,
    V3TerminalStagingToken,
    _resolve_token,
)

MAX_READBACK_BYTES = 4 * 1024 * 1024
MAX_SEAL_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class V3FileSnapshot:
    byte_length: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class V3FileRegion:
    file_length: int
    contents: bytes


def offset_snapshot(fd: int) -> SegmentIndexSnapshot:
    info = os.fstat(fd)
    payload = info.st_size - len(OFFSET_HEADER)
    if payload < 0 or payload % OFFSET_ENTRY_SIZE or os.pread(fd, 8, 0) != OFFSET_HEADER:
        raise V3CorruptionError("offset table shape is invalid")
    count = payload // OFFSET_ENTRY_SIZE
    first_raw = last_raw = None
    first = last = None
    if count:
        first_raw = os.pread(fd, OFFSET_ENTRY_SIZE, 8)
        last_raw = os.pread(fd, OFFSET_ENTRY_SIZE, 8 + (count - 1) * OFFSET_ENTRY_SIZE)
        first, last = decode_offset_entry(first_raw), decode_offset_entry(last_raw)
        if last.seq != first.seq + count - 1:
            raise V3CorruptionError("offset sequences are not contiguous")
    return SegmentIndexSnapshot(
        count,
        first.seq if first else None,
        last.seq if last else None,
        info.st_size,
        append_state_hash(info.st_size, first_raw, last_raw),
    )


def _validate_offset_page_bounds(entry_ordinal: object, limit: object) -> tuple[int, int]:
    if type(entry_ordinal) is not int or entry_ordinal < 0:
        raise V3ValidationError("offset page bounds are invalid")
    if type(limit) is not int or not 1 <= limit <= 1024:
        raise V3ValidationError("offset page bounds are invalid")
    return int(entry_ordinal), int(limit)


def _read_offset_page(
    fd: int, snapshot: SegmentIndexSnapshot, entry_ordinal: int, limit: int
) -> SegmentIndexPage:
    stop = min(snapshot.entry_count, entry_ordinal + limit)
    entries = tuple(
        _read_offset_page_entry(fd, snapshot, index) for index in range(entry_ordinal, stop)
    )
    complete = stop == snapshot.entry_count
    return SegmentIndexPage(entries, None if complete else stop, complete)


def _read_offset_page_entry(
    fd: int, snapshot: SegmentIndexSnapshot, index: int
) -> SegmentIndexEntry:
    value = decode_offset_entry(os.pread(fd, OFFSET_ENTRY_SIZE, 8 + index * OFFSET_ENTRY_SIZE))
    if snapshot.first_sequence is not None and value.seq != snapshot.first_sequence + index:
        raise V3CorruptionError("offset sequence does not match position")
    return SegmentIndexEntry(
        value.seq,
        value.file_offset,
        value.line_length,
        value.record_sha256,
        OffsetRecordKind(value.record_kind),
    )


class _OffsetReadHandle:
    def __init__(self, fd: int) -> None:
        self._fd = fd

    def snapshot(self) -> SegmentIndexSnapshot:
        try:
            return offset_snapshot(self._fd)
        except OSError as exc:
            raise V3PersistenceError(f"offset snapshot failed: {bounded_os_error(exc)}") from exc

    def digest(self, length: int) -> str:
        return _hash_fd(self._fd, length)

    def page(self, snapshot: SegmentIndexSnapshot, ordinal: int, limit: int) -> SegmentIndexPage:
        if ordinal >= snapshot.entry_count:
            return SegmentIndexPage((), None, True)
        stop = min(snapshot.entry_count, ordinal + limit)
        values = []
        for index in range(ordinal, stop):
            value = decode_offset_entry(
                _read_exact(self._fd, 8 + index * OFFSET_ENTRY_SIZE, OFFSET_ENTRY_SIZE)
            )
            if snapshot.first_sequence is not None and value.seq != snapshot.first_sequence + index:
                raise V3CorruptionError("offset sequence does not match position")
            values.append(
                SegmentIndexEntry(
                    value.seq,
                    value.file_offset,
                    value.line_length,
                    value.record_sha256,
                    OffsetRecordKind(value.record_kind),
                )
            )
        return SegmentIndexPage(
            tuple(values),
            None if stop == snapshot.entry_count else stop,
            stop == snapshot.entry_count,
        )


class V3ReadOnlyFilesystemRegions:
    """Verified, lease-free bounded reads for immutable v3 files."""

    def __init__(self, state_root: Path, *, owner_uid: int | None = None) -> None:
        try:
            self._paths = V3StoragePaths(Path(state_root), owner_uid=owner_uid)
            self._owner_uid = self._paths.owner_uid
            root = os.stat(self._paths.state_root, follow_symlinks=False)
        except OSError as exc:
            raise V3PersistenceError(
                f"read-only state root inspection failed: {bounded_os_error(exc)}"
            ) from exc
        self._root_identity = (root.st_dev, root.st_ino, root.st_uid, stat.S_IMODE(root.st_mode))

    def read_region(
        self,
        token: V3SegmentPathToken | V3DamagedSegmentPathToken,
        *,
        offset: int,
        length: int,
        max_file_bytes: int,
    ) -> V3FileRegion:
        offset, length, max_file_bytes = _validate_region_bounds(offset, length, max_file_bytes)
        if length > MAX_READBACK_BYTES:
            raise V3CapacityError("file region exceeds the bounded read limit")
        fd, descriptors = self._open_readonly(token)
        try:
            info = os.fstat(fd)
            if (
                info.st_size > max_file_bytes
                or offset > info.st_size
                or length > info.st_size - offset
            ):
                raise V3CorruptionError("file region exceeds durable file")
            return V3FileRegion(info.st_size, _read_exact(fd, offset, length))
        except OSError as exc:
            raise V3PersistenceError(f"read-only region failed: {bounded_os_error(exc)}") from exc
        finally:
            os.close(fd)
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def file_sha256(self, token: V3OffsetPathToken | V3DamagedOffsetPathToken) -> str:
        fd, descriptors = self._open_readonly(token)
        try:
            return _hash_fd(fd, os.fstat(fd).st_size)
        except OSError as exc:
            raise V3PersistenceError(f"read-only digest failed: {bounded_os_error(exc)}") from exc
        finally:
            os.close(fd)
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def authenticated_offset_page(
        self,
        token: V3OffsetPathToken | V3DamagedOffsetPathToken,
        *,
        entry_ordinal: int,
        limit: int,
        expected_sha256: str | None,
        sealed: bool,
    ) -> SegmentIndexPage:
        """Authorize, hash, snapshot, and page one immutable fd."""
        entry_ordinal, limit = _validate_offset_page_bounds(entry_ordinal, limit)
        with self._offset_handle(token) as handle:
            snapshot = handle.snapshot()
            digest = handle.digest(snapshot.byte_length)
            if sealed and expected_sha256 != digest:
                raise V3CorruptionError("sealed evidence offset-table hash differs")
            if (
                not sealed
                and expected_sha256 is not None
                and snapshot.append_state_sha256 != expected_sha256
            ):
                raise V3CorruptionError("active evidence offset CAS differs")
            page = handle.page(snapshot, entry_ordinal, limit)
            if handle.snapshot() != snapshot:
                raise V3CorruptionError("evidence offset snapshot changed during read")
            return page

    @contextmanager
    def _offset_handle(self, token: V3OffsetPathToken | V3DamagedOffsetPathToken):
        fd, descriptors = self._open_readonly(token)
        try:
            yield _OffsetReadHandle(fd)
        finally:
            os.close(fd)
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def snapshot_offset_index(
        self, token: V3OffsetPathToken | V3DamagedOffsetPathToken
    ) -> SegmentIndexSnapshot:
        fd, descriptors = self._open_readonly(token)
        try:
            info = os.fstat(fd)
            payload = info.st_size - len(OFFSET_HEADER)
            if payload < 0 or payload % OFFSET_ENTRY_SIZE or os.pread(fd, 8, 0) != OFFSET_HEADER:
                raise V3CorruptionError("offset table shape is invalid")
            count = payload // OFFSET_ENTRY_SIZE
            first_raw = last_raw = None
            first = last = None
            if count:
                first_raw = os.pread(fd, OFFSET_ENTRY_SIZE, 8)
                last_raw = os.pread(fd, OFFSET_ENTRY_SIZE, 8 + (count - 1) * OFFSET_ENTRY_SIZE)
                first, last = decode_offset_entry(first_raw), decode_offset_entry(last_raw)
                if last.seq != first.seq + count - 1:
                    raise V3CorruptionError("offset sequences are not contiguous")
            return SegmentIndexSnapshot(
                count,
                first.seq if first else None,
                last.seq if last else None,
                info.st_size,
                append_state_hash(info.st_size, first_raw, last_raw),
            )
        except OSError as exc:
            raise V3PersistenceError(
                f"read-only offset snapshot failed: {bounded_os_error(exc)}"
            ) from exc
        finally:
            os.close(fd)
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def page_offset_index(
        self,
        token: V3OffsetPathToken | V3DamagedOffsetPathToken,
        *,
        entry_ordinal: int,
        limit: int,
    ) -> SegmentIndexPage:
        entry_ordinal, limit = _validate_offset_page_bounds(entry_ordinal, limit)
        snapshot = self.snapshot_offset_index(token)
        if entry_ordinal >= snapshot.entry_count:
            return SegmentIndexPage((), None, True)
        fd, descriptors = self._open_readonly(token)
        try:
            return _read_offset_page(fd, snapshot, entry_ordinal, limit)
        except OSError as exc:
            raise V3PersistenceError(
                f"read-only offset page failed: {bounded_os_error(exc)}"
            ) from exc
        finally:
            os.close(fd)
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _open_readonly(self, token: V3ReadableFileToken) -> tuple[int, list[int]]:
        descriptors: list[int] = []
        try:
            parent, basename = _resolve_token(self._paths, token)
            root = self._paths.state_root
            current = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
            descriptors.append(current)
            root_info = os.fstat(current)
            if (
                not stat.S_ISDIR(root_info.st_mode)
                or (
                    root_info.st_dev,
                    root_info.st_ino,
                    root_info.st_uid,
                    stat.S_IMODE(root_info.st_mode),
                )
                != self._root_identity
            ):
                raise V3PathError("v3 state root identity changed")
            for component in parent.relative_to(root).parts:
                current = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=current,
                )
                descriptors.append(current)
                info = os.fstat(current)
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != self._owner_uid
                    or stat.S_IMODE(info.st_mode) != DIRECTORY_MODE
                ):
                    raise V3PathError("v3 directory has unsafe identity")
            fd = os.open(basename, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=current)
            descriptors.append(fd)
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != self._owner_uid
                or stat.S_IMODE(info.st_mode) != SEALED_MODE
            ):
                raise V3PathError("v3 file has unsafe identity")
            return fd, descriptors[:-1]
        except OSError as exc:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            if exc.errno == errno.ELOOP:
                raise V3PathError("v3 file path is a symlink") from exc
            raise V3PersistenceError(
                f"read-only filesystem operation failed: {bounded_os_error(exc)}"
            ) from exc
        except BaseException:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise


class RegionTransaction(Protocol):
    def _check(self) -> None: ...

    def _location(self, token: V3ReadableFileToken) -> tuple[int, str]: ...

    def _open(self, token: V3ReadableFileToken, flags: int, mode: int) -> tuple[int, int]: ...

    def _track_fd(self, fd: int) -> None: ...


def create_and_sync(
    tx: RegionTransaction,
    token: V3CreatableCaptureToken,
    contents: bytes,
    max_result_bytes: int,
) -> V3FileSnapshot:
    tx._check()
    if type(token) not in {V3SegmentPathToken, V3TerminalStagingToken}:
        raise V3ValidationError("capture creation token is invalid")
    if (
        isinstance(max_result_bytes, bool)
        or not isinstance(max_result_bytes, int)
        or max_result_bytes < 0
        or len(contents) > max_result_bytes
        or len(contents) > MAX_SEAL_BYTES
    ):
        raise V3CapacityError("capture exceeds its bounded size")
    parent_fd, basename = tx._location(token)
    try:
        fd = os.open(
            basename,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            MUTABLE_MODE,
            dir_fd=parent_fd,
        )
        tx._track_fd(fd)
        _write_all(fd, contents)
        os.fdatasync(fd)
        if not _readback_equal(fd, contents):
            raise V3CorruptionError("capture readback differs")
        os.fsync(parent_fd)
        return V3FileSnapshot(len(contents), hashlib.sha256(contents).hexdigest())
    except FileExistsError as exc:
        raise V3PersistenceError("capture target already exists") from exc
    except OSError as exc:
        raise V3PersistenceError(f"capture creation failed: {bounded_os_error(exc)}") from exc


def read_region(
    tx: RegionTransaction,
    token: V3SegmentPathToken | V3DamagedSegmentPathToken | V3TerminalStagingToken,
    *,
    offset: int,
    length: int,
    max_file_bytes: int,
) -> V3FileRegion:
    tx._check()
    _validate_region_token(token)
    offset, length, max_file_bytes = _validate_region_bounds(offset, length, max_file_bytes)
    _validate_region_capacity(length)
    fd, _ = tx._open(token, os.O_RDONLY, MUTABLE_MODE)
    return _read_region_from_fd(fd, offset, length, max_file_bytes)


def rename_damaged(
    tx: RegionTransaction,
    source: V3SegmentPathToken | V3OffsetPathToken,
    target: V3DamagedSegmentPathToken | V3DamagedOffsetPathToken,
    expected: V3FileSnapshot,
) -> V3FileSnapshot:
    tx._check()
    _validate_damage_binding(source, target)
    source_fd, source_parent = tx._open(source, os.O_RDONLY, MUTABLE_MODE)
    actual = _verify_damage_snapshot(source_fd, expected, target)
    target_parent, target_basename = tx._location(target)
    _, source_basename = tx._location(source)
    _ensure_damage_target_absent(target_parent, target_basename)
    _link_damaged(
        source_fd,
        source_parent,
        source_basename,
        target_parent,
        target_basename,
    )
    return actual


def _validate_region_token(
    token: V3SegmentPathToken | V3DamagedSegmentPathToken | V3TerminalStagingToken,
) -> None:
    if type(token) not in {
        V3SegmentPathToken,
        V3DamagedSegmentPathToken,
        V3TerminalStagingToken,
    }:
        raise V3ValidationError("file region token is invalid")


def _validate_region_capacity(length: int) -> None:
    if length > MAX_READBACK_BYTES:
        raise V3CapacityError("file region exceeds the bounded read limit")


def _read_region_from_fd(fd: int, offset: int, length: int, maximum: int) -> V3FileRegion:
    info = os.fstat(fd)
    if info.st_size > maximum or offset > info.st_size or length > info.st_size - offset:
        raise V3CorruptionError("file region exceeds durable file")
    return V3FileRegion(info.st_size, _read_exact(fd, offset, length))


def _validate_damage_binding(
    source: V3SegmentPathToken | V3OffsetPathToken,
    target: V3DamagedSegmentPathToken | V3DamagedOffsetPathToken,
) -> None:
    valid_pair = (
        type(source) is V3SegmentPathToken and type(target) is V3DamagedSegmentPathToken
    ) or (type(source) is V3OffsetPathToken and type(target) is V3DamagedOffsetPathToken)
    if not valid_pair:
        raise V3PathBindingConflict()
    if (
        source.blackout_id != target.blackout_id
        or source.logical_segment_id != target.logical_segment_id
        or source.ordinal != target.ordinal
        or source.storage_id != target.storage_id
    ):
        raise V3PathBindingConflict()


def _verify_damage_snapshot(
    source_fd: int,
    expected: V3FileSnapshot,
    target: V3DamagedSegmentPathToken | V3DamagedOffsetPathToken,
) -> V3FileSnapshot:
    info = os.fstat(source_fd)
    digest = _hash_fd(source_fd, info.st_size)
    actual = V3FileSnapshot(info.st_size, digest)
    if actual != expected or target.file_sha256 != digest:
        raise V3PersistenceError("damaged source compare-and-swap mismatch")
    return actual


def _ensure_damage_target_absent(target_parent: int, target_basename: str) -> None:
    try:
        os.lstat(target_basename, dir_fd=target_parent)
    except FileNotFoundError:
        return
    raise V3PersistenceError("damaged target already exists")


def _link_damaged(
    source_fd: int,
    source_parent: int,
    source_basename: str,
    target_parent: int,
    target_basename: str,
) -> None:
    try:
        os.fchmod(source_fd, SEALED_MODE)
        os.fsync(source_fd)
        os.link(
            source_basename,
            target_basename,
            src_dir_fd=source_parent,
            dst_dir_fd=target_parent,
            follow_symlinks=False,
        )
        os.fsync(target_parent)
        os.unlink(source_basename, dir_fd=source_parent)
        os.fsync(source_parent)
    except OSError as exc:
        raise V3PersistenceError(f"damaged rename failed: {bounded_os_error(exc)}") from exc


def _validate_region_bounds(
    offset: object, length: object, maximum: object
) -> tuple[int, int, int]:
    offset_value = _region_int(offset)
    length_value = _region_int(length)
    maximum_value = _region_int(maximum)
    if offset_value < 0 or length_value < 0 or maximum_value < 0:
        raise V3ValidationError("file region bounds are invalid")
    if offset_value > maximum_value or length_value > maximum_value - offset_value:
        raise V3ValidationError("file region bounds are invalid")
    return offset_value, length_value, maximum_value


def _region_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise V3ValidationError("file region bounds are invalid")
    return value


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            count = os.write(fd, view)
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            raise V3PersistenceError("capture write failed") from exc
        if count <= 0 or count > len(view):
            raise V3PersistenceError("write returned an invalid byte count")
        view = view[count:]


def _readback_equal(fd: int, expected: bytes) -> bool:
    offset = 0
    while offset < len(expected):
        amount = min(MAX_READBACK_BYTES, len(expected) - offset)
        if _read_exact(fd, offset, amount) != expected[offset : offset + amount]:
            return False
        offset += amount
    return True


def _read_exact(fd: int, offset: int, length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        try:
            chunk = os.pread(fd, length - len(result), offset + len(result))
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            raise V3PersistenceError("capture read failed") from exc
        if not chunk:
            raise V3CorruptionError("durable readback ended early")
        result.extend(chunk)
    return bytes(result)


def _hash_fd(fd: int, size: int) -> str:
    if size > MAX_SEAL_BYTES:
        raise V3CapacityError("damaged source exceeds its bounded size")
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        try:
            chunk = os.pread(fd, min(MAX_READBACK_BYTES, size - offset), offset)
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            raise V3PersistenceError("damaged source read failed") from exc
        if not chunk:
            raise V3CorruptionError("damaged source ended early")
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()
