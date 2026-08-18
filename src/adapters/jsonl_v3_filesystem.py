"""Transaction-scoped, fd-relative primitives for the JSONL v3 tree."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, final

from src.adapters.jsonl_v3_errors import (
    V3AppendConflict,
    V3CapacityError,
    V3CorruptionError,
    V3FileNotFound,
    V3PathBindingConflict,
    V3PathError,
    V3PersistenceError,
    V3TransactionClosed,
    V3ValidationError,
    V3WriterOwnershipError,
    bounded_os_error,
)
from src.adapters.jsonl_v3_segment_index import (
    OFFSET_ENTRY_SIZE,
    OFFSET_HEADER,
    OffsetEntry,
    OffsetRecordKind,
    SegmentIndexEntry,
    SegmentIndexPage,
    SegmentIndexSnapshot,
    append_state_hash,
    decode_offset_entry,
    encode_offset_entry,
)
from src.adapters.jsonl_v3_storage_paths import (
    DIRECTORY_MODE,
    MUTABLE_MODE,
    SEALED_MODE,
    V3AppendableFileToken,
    V3MutableFileToken,
    V3OffsetIndexToken,
    V3OffsetPathToken,
    V3PromotedFileToken,
    V3ReadableFileToken,
    V3SealableFileToken,
    V3StoragePaths,
    V3TemporaryFileToken,
    _resolve_token,
    validate_path_token,
)

MAX_READBACK_BYTES = 4 * 1024 * 1024
MAX_SEAL_BYTES = 64 * 1024 * 1024
_TEMP_NAME_RE = re.compile(r"\A\.(?P<target>[^/]+)\.tmp-(?P<nonce>[0-9a-f]{32})\Z")
_TEMP_SCAN_BUDGET = 64


def _validate_offset_token(token: V3OffsetIndexToken) -> None:
    if isinstance(token, V3OffsetPathToken):
        return
    if isinstance(token, V3TemporaryFileToken) and isinstance(token.destination, V3OffsetPathToken):
        return
    raise V3PathError("offset operation requires an offset token")


def _validate_offset_page_bounds(entry_ordinal: object, limit: object) -> tuple[int, int]:
    if isinstance(entry_ordinal, bool) or not isinstance(entry_ordinal, int):
        raise V3ValidationError("offset page bounds are invalid")
    if entry_ordinal < 0:
        raise V3ValidationError("offset page bounds are invalid")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise V3ValidationError("offset page bounds are invalid")
    if not 1 <= limit <= 1024:
        raise V3ValidationError("offset page bounds are invalid")
    return entry_ordinal, limit


class V3FaultPoint(StrEnum):
    LAYOUT_AFTER_DIR_CREATE = "layout.after_dir_create"
    REPLACE_AFTER_TEMP_CREATE = "replace.after_temp_create"
    REPLACE_AFTER_TEMP_FSYNC = "replace.after_temp_fsync"
    REPLACE_AFTER_RENAME = "replace.after_rename"
    APPEND_AFTER_WRITE = "append.after_write"
    APPEND_AFTER_FDATASYNC = "append.after_fdatasync"
    SEAL_AFTER_FCHMOD = "seal.after_fchmod"
    PROMOTE_AFTER_FCHMOD = "promote.after_fchmod"
    PROMOTE_AFTER_RENAME = "promote.after_rename"
    INDEX_AFTER_GENERATION_CURSOR = "index.after_generation_cursor"


class ValidatedWriterLease(Protocol):
    @property
    def state_root_identity(self) -> tuple[int, int]: ...

    def validate(self, state_root: Path) -> None: ...


class ModelOwnerWriterLease(Protocol):
    def hold(self) -> AbstractContextManager[ValidatedWriterLease]: ...


def _validate_lease_identity(value: object) -> tuple[int, int]:
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        raise V3WriterOwnershipError()
    return value


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            count = os.write(fd, view)
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            raise V3PersistenceError(f"write failed: {bounded_os_error(exc)}") from exc
        if count <= 0 or count > len(view):
            raise V3PersistenceError("write returned an invalid byte count")
        view = view[count:]


def _read_exact(fd: int, offset: int, length: int) -> bytes:
    if offset < 0 or length < 0 or length > MAX_READBACK_BYTES:
        raise V3CapacityError("read exceeds the bounded filesystem limit")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise V3PathError("v3 file descriptor is not a private regular file")
        if offset > info.st_size or length > info.st_size - offset:
            raise V3CorruptionError("durable readback is shorter than expected")
        chunks = bytearray()
        while len(chunks) < length:
            try:
                part = os.pread(fd, length - len(chunks), offset + len(chunks))
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise V3PersistenceError(f"read failed: {bounded_os_error(exc)}") from exc
            if not part:
                raise V3CorruptionError("durable readback ended early")
            chunks.extend(part)
        return bytes(chunks)
    except (V3CapacityError, V3CorruptionError, V3PathError, V3PersistenceError):
        raise
    except OSError as exc:
        raise V3PersistenceError(f"read failed: {bounded_os_error(exc)}") from exc


def _hash_fd(fd: int, size: int, max_bytes: int) -> str:
    if size > max_bytes or size > MAX_SEAL_BYTES:
        raise V3CapacityError("sealed file exceeds the bounded limit")
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        try:
            chunk = os.pread(fd, min(1024 * 1024, size - offset), offset)
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            raise V3PersistenceError("seal read failed") from exc
        if not chunk:
            raise V3CorruptionError("seal read ended early")
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class V3FileSnapshot:
    byte_length: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class V3AppendReceipt:
    previous_length: int
    appended_length: int
    resulting_length: int
    appended_sha256: str


@final
class V3WriteTransaction:
    __slots__ = ("__a", "__b", "__c", "__d", "__e", "__f", "__g")

    def __init__(
        self, filesystem: "JsonlV3Filesystem", lease: ValidatedWriterLease, token: object
    ) -> None:
        raise TypeError("transaction construction is private")

    def _check(self) -> None:
        if self.__c:
            raise V3TransactionClosed()

    def _fault(self, point: V3FaultPoint) -> None:
        self.__a.fault(point)

    def assert_owner(self, filesystem: object) -> None:
        """Validate active ownership without exposing descriptors or paths."""
        self._check()
        if self.__a is not filesystem:
            raise V3WriterOwnershipError()

    def _paths(self) -> V3StoragePaths:
        self._check()
        if self.__a.paths is None:
            self.__a.paths = V3StoragePaths(self.__a.state_root)
        return self.__a.paths

    def _location(self, token: V3ReadableFileToken) -> tuple[int, str]:
        paths = self._paths()
        parent, basename = _resolve_token(paths, token)
        validate_path_token(basename)
        return self._parent_fd(parent), basename

    def _parent_fd(self, parent: Path) -> int:
        if parent in self.__g:
            return self.__g[parent]
        if self.__e is None:
            self.__e = os.open(
                self.__a.state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
            )
            self.__d.append(self.__e)
        fd = self.__e
        for component in parent.relative_to(self.__a.state_root).parts:
            fd = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd
            )
            info = os.fstat(fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != self.__a.owner_uid
                or stat.S_IMODE(info.st_mode) != DIRECTORY_MODE
            ):
                os.close(fd)
                raise V3WriterOwnershipError()
            self.__d.append(fd)
        self.__g[parent] = fd
        return fd

    def _open(
        self, token: V3ReadableFileToken, flags: int, mode: int = MUTABLE_MODE
    ) -> tuple[int, int]:
        pfd, basename = self._location(token)
        if self.__f is not None:
            try:
                os.close(self.__f)
            except OSError:
                pass
            self.__f = None
        try:
            fd = os.open(basename, flags | os.O_CLOEXEC | os.O_NOFOLLOW, mode, dir_fd=pfd)
            self.__f = fd
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != self.__a.owner_uid
                or stat.S_IMODE(info.st_mode) not in {MUTABLE_MODE, SEALED_MODE}
            ):
                raise V3PathError("v3 file has unsafe identity")
            return fd, pfd
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise V3FileNotFound() from exc
            raise V3PathError("v3 file cannot be opened") from exc

    def read_bounded(
        self, token: V3ReadableFileToken, *, max_bytes: int
    ) -> tuple[bytes, V3FileSnapshot]:
        if max_bytes < 0 or max_bytes > MAX_READBACK_BYTES:
            raise V3CapacityError("read exceeds the bounded filesystem limit")
        fd, _ = self._open(token, os.O_RDONLY, SEALED_MODE)
        info = os.fstat(fd)
        if info.st_size > max_bytes:
            raise V3CapacityError("file exceeds the bounded filesystem limit")
        data = _read_exact(fd, 0, info.st_size)
        return data, V3FileSnapshot(info.st_size, hashlib.sha256(data).hexdigest())

    def replace_bounded(
        self,
        token: V3MutableFileToken,
        *,
        expected: V3FileSnapshot | None,
        contents: bytes,
        max_bytes: int,
    ) -> V3FileSnapshot:
        self._check()
        if len(contents) > max_bytes or len(contents) > MAX_READBACK_BYTES:
            raise V3CapacityError("replacement exceeds the bounded filesystem limit")
        pfd, basename = self._location(token)
        self._cleanup_temps(pfd, basename)
        current: V3FileSnapshot | None = None
        try:
            fd, _ = self._open(token, os.O_RDONLY, MUTABLE_MODE)
        except V3FileNotFound:
            fd = -1
        if fd >= 0:
            info = os.fstat(fd)
            if stat.S_IMODE(info.st_mode) != MUTABLE_MODE:
                raise V3PathError("replacement target has unsafe mode")
            data = _read_exact(fd, 0, info.st_size)
            current = V3FileSnapshot(info.st_size, hashlib.sha256(data).hexdigest())
        if current != expected:
            raise V3PersistenceError("replace compare-and-swap mismatch")
        tmp = f".{basename}.tmp-{uuid.uuid4().hex}"
        tfd = os.open(
            tmp,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            MUTABLE_MODE,
            dir_fd=pfd,
        )
        self.__d.append(tfd)
        self._fault(V3FaultPoint.REPLACE_AFTER_TEMP_CREATE)
        try:
            _write_all(tfd, contents)
            os.fdatasync(tfd)
            self._fault(V3FaultPoint.REPLACE_AFTER_TEMP_FSYNC)
            if _read_exact(tfd, 0, len(contents)) != contents:
                raise V3CorruptionError("replacement readback differs")
            os.replace(tmp, basename, src_dir_fd=pfd, dst_dir_fd=pfd)
            self._fault(V3FaultPoint.REPLACE_AFTER_RENAME)
            os.fsync(pfd)
        except OSError as exc:
            raise V3PersistenceError(f"replace failed: {bounded_os_error(exc)}") from exc
        return V3FileSnapshot(len(contents), hashlib.sha256(contents).hexdigest())

    def _cleanup_temps(self, parent_fd: int, target: str) -> None:
        prefix = f".{target}.tmp-"
        removed = 0
        try:
            with os.scandir(parent_fd) as entries:
                for inspected, entry in enumerate(entries, 1):
                    if inspected > _TEMP_SCAN_BUDGET:
                        break
                    name = entry.name
                    if not name.startswith(prefix) or _TEMP_NAME_RE.fullmatch(name) is None:
                        continue
                    if removed >= 16:
                        break
                    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != self.__a.owner_uid
                        or stat.S_IMODE(info.st_mode) != MUTABLE_MODE
                        or info.st_nlink != 1
                    ):
                        raise V3PathError("unsafe temporary residue")
                    os.unlink(name, dir_fd=parent_fd)
                    removed += 1
        except (V3PathError, V3PersistenceError):
            raise
        except OSError as exc:
            raise V3PersistenceError("temporary cleanup failed") from exc
        if removed:
            os.fsync(parent_fd)

    def append_and_sync(
        self,
        token: V3AppendableFileToken,
        *,
        expected_offset: int,
        contents: bytes,
        max_result_bytes: int,
    ) -> V3AppendReceipt:
        if expected_offset < 0 or len(contents) > max_result_bytes:
            raise V3ValidationError("append bounds are invalid")
        fd, _ = self._open(token, os.O_RDWR | os.O_APPEND, MUTABLE_MODE)
        info = os.fstat(fd)
        if info.st_size != expected_offset or info.st_size + len(contents) > max_result_bytes:
            raise V3PersistenceError("append compare-and-swap mismatch")
        _write_all(fd, contents)
        self._fault(V3FaultPoint.APPEND_AFTER_WRITE)
        os.fdatasync(fd)
        self._fault(V3FaultPoint.APPEND_AFTER_FDATASYNC)
        if _read_exact(fd, expected_offset, len(contents)) != contents:
            raise V3CorruptionError("append readback differs")
        return V3AppendReceipt(
            expected_offset,
            len(contents),
            expected_offset + len(contents),
            hashlib.sha256(contents).hexdigest(),
        )

    def seal(
        self, token: V3SealableFileToken, *, expected_length: int, max_bytes: int
    ) -> V3FileSnapshot:
        fd, pfd = self._open(token, os.O_RDWR, MUTABLE_MODE)
        info = os.fstat(fd)
        if info.st_size != expected_length or info.st_size > max_bytes:
            raise V3PersistenceError("seal compare-and-swap mismatch")
        digest = _hash_fd(fd, info.st_size, max_bytes)
        os.fchmod(fd, SEALED_MODE)
        self._fault(V3FaultPoint.SEAL_AFTER_FCHMOD)
        os.fsync(fd)
        os.fdatasync(fd)
        os.fsync(pfd)
        return V3FileSnapshot(info.st_size, digest)

    def promote(
        self,
        source: V3TemporaryFileToken,
        target: V3PromotedFileToken,
        *,
        expected_source: V3FileSnapshot,
        require_target_absent: bool,
    ) -> V3FileSnapshot:
        if source.destination != target:
            raise V3PathBindingConflict()
        source_fd, source_parent = self._open(source, os.O_RDONLY, SEALED_MODE)
        info = os.fstat(source_fd)
        digest = hashlib.sha256()
        offset = 0
        while offset < info.st_size:
            chunk = os.pread(source_fd, min(1024 * 1024, info.st_size - offset), offset)
            if not chunk:
                raise V3CorruptionError("promotion source ended early")
            digest.update(chunk)
            offset += len(chunk)
        snap = V3FileSnapshot(info.st_size, digest.hexdigest())
        if snap != expected_source:
            raise V3PersistenceError("promotion source compare-and-swap mismatch")
        if require_target_absent:
            target_parent, target_basename = self._location(target)
            try:
                os.lstat(target_basename, dir_fd=target_parent)
            except FileNotFoundError:
                pass
            else:
                raise V3PersistenceError("promotion target already exists")
        else:
            target_parent, target_basename = self._location(target)
        os.fchmod(source_fd, SEALED_MODE)
        self._fault(V3FaultPoint.PROMOTE_AFTER_FCHMOD)
        os.fsync(source_fd)
        os.fdatasync(source_fd)
        os.fsync(source_parent)
        try:
            _, source_basename = self._location(source)
            os.replace(
                source_basename, target_basename, src_dir_fd=source_parent, dst_dir_fd=target_parent
            )
            self._fault(V3FaultPoint.PROMOTE_AFTER_RENAME)
            os.fsync(target_parent)
        except OSError as exc:
            raise V3PersistenceError(f"promotion failed: {bounded_os_error(exc)}") from exc
        return snap

    def create_offset_index(self, token: V3OffsetIndexToken):
        _validate_offset_token(token)
        parent, basename = self._location(token)
        try:
            fd = os.open(
                basename,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                MUTABLE_MODE,
                dir_fd=parent,
            )
        except FileExistsError:
            return self.snapshot_offset_index(token)
        self.__d.append(fd)
        _write_all(fd, OFFSET_HEADER)
        os.fdatasync(fd)
        os.fsync(parent)
        return self._offset_snapshot(fd)

    def snapshot_offset_index(self, token: V3OffsetIndexToken):
        _validate_offset_token(token)
        fd, _ = self._open(token, os.O_RDONLY, SEALED_MODE)
        return self._offset_snapshot(fd)

    def append_offset_index(self, token: V3OffsetIndexToken, *, expected, entry):
        _validate_offset_token(token)
        fd, _ = self._open(token, os.O_RDWR | os.O_APPEND, MUTABLE_MODE)
        current = self._offset_snapshot(fd)
        if current != expected:
            raise V3AppendConflict("offset snapshot differs from expected")
        if current.last_sequence is not None and entry.sequence != current.last_sequence + 1:
            raise V3ValidationError("offset sequence is not contiguous")
        if current.entry_count:
            previous = decode_offset_entry(
                os.pread(fd, OFFSET_ENTRY_SIZE, current.byte_length - OFFSET_ENTRY_SIZE)
            )
            if entry.file_offset < previous.file_offset + previous.line_length:
                raise V3ValidationError("offset file offsets overlap")
        raw = encode_offset_entry(
            OffsetEntry(
                entry.sequence,
                entry.file_offset,
                entry.line_length,
                entry.record_sha256,
                int(entry.record_kind),
            )
        )
        _write_all(fd, raw)
        os.fdatasync(fd)
        if os.pread(fd, len(raw), current.byte_length) != raw:
            raise V3CorruptionError("offset append readback differs")
        return self._offset_snapshot(fd)

    def get_offset_index(self, token: V3OffsetIndexToken, *, sequence: int):
        _validate_offset_token(token)
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise V3ValidationError("offset sequence is invalid")
        fd, _ = self._open(token, os.O_RDONLY, SEALED_MODE)
        snapshot = self._offset_snapshot(fd)
        if (
            snapshot.first_sequence is None
            or sequence < snapshot.first_sequence
            or (snapshot.last_sequence is not None and sequence > snapshot.last_sequence)
        ):
            return None
        raw = os.pread(
            fd, OFFSET_ENTRY_SIZE, 8 + (sequence - snapshot.first_sequence) * OFFSET_ENTRY_SIZE
        )
        value = decode_offset_entry(raw)
        if value.seq != sequence:
            raise V3CorruptionError("offset sequence does not match position")
        return SegmentIndexEntry(
            value.seq,
            value.file_offset,
            value.line_length,
            value.record_sha256,
            OffsetRecordKind(value.record_kind),
        )

    def page_offset_index(self, token: V3OffsetIndexToken, *, entry_ordinal: int, limit: int):
        _validate_offset_token(token)
        entry_ordinal, limit = _validate_offset_page_bounds(entry_ordinal, limit)
        fd, _ = self._open(token, os.O_RDONLY, SEALED_MODE)
        snapshot = self._offset_snapshot(fd)
        if entry_ordinal >= snapshot.entry_count:
            return SegmentIndexPage((), None, True)
        stop = min(snapshot.entry_count, entry_ordinal + limit)
        values = []
        for index in range(entry_ordinal, stop):
            value = decode_offset_entry(
                os.pread(fd, OFFSET_ENTRY_SIZE, 8 + index * OFFSET_ENTRY_SIZE)
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

    def _offset_snapshot(self, fd: int) -> SegmentIndexSnapshot:
        info = os.fstat(fd)
        payload = info.st_size - 8
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

    def _close(self) -> None:
        failed = False
        if self.__f is not None:
            try:
                os.close(self.__f)
            except OSError:
                failed = True
            self.__f = None
        for fd in reversed(self.__d):
            try:
                os.close(fd)
            except OSError:
                failed = True
        self.__d.clear()
        self.__c = True
        if failed:
            raise V3PersistenceError("transaction descriptor cleanup failed")

    def ensure_layout(self) -> None:
        self._check()
        self.__a.paths = V3StoragePaths(self.__a.state_root)
        root_fd = os.open(
            self.__a.state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
        self.__d.append(root_fd)
        for chain in (
            ("events-v3",),
            ("events-v3", "blackouts"),
            ("events-v3", "blackouts", "segments"),
            ("events-v3", "blackouts", "terminal-chains"),
            ("events-v3", "blackouts", "transactions"),
            ("events-v3", "blackouts", "terminal-locators"),
            ("events-v3", "blackouts", "history"),
            ("events-v3", "blackouts", "history", "runs"),
        ):
            fd = root_fd
            for name in chain:
                try:
                    os.mkdir(name, DIRECTORY_MODE, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd
                )
                info = os.fstat(child)
                if (
                    info.st_uid != self.__a.owner_uid
                    or stat.S_IMODE(info.st_mode) != DIRECTORY_MODE
                ):
                    os.close(child)
                    raise V3PathError("v3 directory has unsafe identity")
                self.__d.append(child)
                fd = child


class JsonlV3Filesystem:
    def __init__(
        self,
        state_root: Path,
        *,
        writer_lease: ModelOwnerWriterLease,
        fault_hook: Callable[[V3FaultPoint], None] | None = None,
        monotonic_clock_ns: Callable[[], int] = lambda: 0,
    ) -> None:
        try:
            hold = writer_lease.hold
        except AttributeError as exc:
            raise V3WriterOwnershipError() from exc
        if not callable(hold):
            raise V3WriterOwnershipError()
        self.state_root = Path(state_root)
        self.writer_lease = writer_lease
        self.fault_hook = fault_hook
        self.monotonic_clock_ns = monotonic_clock_ns
        self.paths: V3StoragePaths | None = None
        self.owner_uid = os.getuid()

    @contextmanager
    def write_transaction(self) -> Iterator[V3WriteTransaction]:
        with self.writer_lease.hold() as lease:
            lease.validate(self.state_root)
            expected_identity = _validate_lease_identity(lease.state_root_identity)
            tx = object.__new__(V3WriteTransaction)
            object.__setattr__(tx, "_V3WriteTransaction__a", self)
            object.__setattr__(tx, "_V3WriteTransaction__b", lease)
            object.__setattr__(tx, "_V3WriteTransaction__c", False)
            object.__setattr__(tx, "_V3WriteTransaction__d", [])
            object.__setattr__(tx, "_V3WriteTransaction__e", None)
            object.__setattr__(tx, "_V3WriteTransaction__f", None)
            object.__setattr__(tx, "_V3WriteTransaction__g", {})
            try:
                root_fd = tx._parent_fd(self.state_root)
            except OSError as exc:
                raise V3PersistenceError(
                    f"transaction root open failed: {bounded_os_error(exc)}"
                ) from exc
            root_info = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != self.owner_uid
                or stat.S_IMODE(root_info.st_mode) != DIRECTORY_MODE
                or (root_info.st_dev, root_info.st_ino) != expected_identity
            ):
                tx._close()
                raise V3WriterOwnershipError()
            try:
                yield tx
            except OSError as exc:
                raise V3PersistenceError(
                    f"transaction syscall failed: {bounded_os_error(exc)}"
                ) from exc
            finally:
                tx._close()

    def ensure_layout(self) -> None:
        with self.write_transaction() as transaction:
            transaction.ensure_layout()

    def fault(self, point: V3FaultPoint | str) -> None:
        if self.fault_hook is not None:
            try:
                self.fault_hook(V3FaultPoint(point))
            except ValueError as exc:
                raise V3PathError("unknown v3 fault point") from exc
