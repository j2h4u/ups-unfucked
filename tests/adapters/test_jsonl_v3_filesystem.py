from __future__ import annotations

import errno
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from src.adapters.jsonl_v3_errors import (
    V3CapacityError,
    V3CorruptionError,
    V3PersistenceError,
    V3TransactionClosed,
    V3ValidationError,
    V3WriterOwnershipError,
)
from src.adapters.jsonl_v3_filesystem import JsonlV3Filesystem, V3WriteTransaction, _read_exact
from src.adapters.jsonl_v3_storage_paths import DIRECTORY_MODE


class Capability:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_root_identity = (root.stat().st_dev, root.stat().st_ino)
        self.rlock = threading.RLock()
        self.holds = 0

    def validate(self, state_root: Path) -> None:
        assert state_root == self.root
        assert (self.root.stat().st_dev, self.root.stat().st_ino) == self.state_root_identity

    @contextmanager
    def hold(self):
        with self.rlock:
            self.holds += 1
            assert (self.root.stat().st_dev, self.root.stat().st_ino) == self.state_root_identity
            yield self


def fs(tmp_path: Path) -> tuple[JsonlV3Filesystem, Capability]:
    os.chmod(tmp_path, DIRECTORY_MODE)
    capability = Capability(tmp_path)
    filesystem = JsonlV3Filesystem(tmp_path, writer_lease=capability)
    filesystem.ensure_layout()
    return filesystem, capability


def test_hold_precedes_path_lstat_and_layout_never_uses_monitor_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.chmod(tmp_path, DIRECTORY_MODE)
    capability = Capability(tmp_path)
    calls: list[str] = []
    original = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda self: (calls.append(str(self)), original(self))[1])
    filesystem = JsonlV3Filesystem(tmp_path, writer_lease=capability)
    filesystem.ensure_layout()
    assert calls
    assert capability.holds == 1
    assert not (tmp_path / "monitor.lock").exists()


def test_root_replacement_after_lease_validation_is_rejected(tmp_path: Path) -> None:
    os.chmod(tmp_path, DIRECTORY_MODE)
    capability = Capability(tmp_path)
    original_validate = capability.validate
    replaced = False

    def replace_root(state_root: Path) -> None:
        nonlocal replaced
        original_validate(state_root)
        if not replaced:
            replaced = True
            displaced = state_root.with_name("validated-root")
            state_root.rename(displaced)
            state_root.mkdir(mode=DIRECTORY_MODE)

    capability.validate = replace_root  # type: ignore[method-assign]
    filesystem = JsonlV3Filesystem(tmp_path, writer_lease=capability)
    with pytest.raises(V3WriterOwnershipError):
        filesystem.ensure_layout()
    assert replaced


def test_descendant_mode_replacement_is_rejected_before_file_open(tmp_path: Path) -> None:
    filesystem, _ = fs(tmp_path)
    paths = filesystem.paths
    assert paths is not None
    os.chmod(paths.segments, 0o755)
    token = paths.segment_token(
        "2026-08-18T12:34:56.123456Z",
        uuid.uuid4().hex,
        uuid.uuid4().hex,
        0,
        uuid.uuid4().hex,
    )
    with pytest.raises(V3WriterOwnershipError):
        with filesystem.write_transaction() as tx:
            tx.replace_bounded(token, expected=None, contents=b"x", max_bytes=8)


def test_bounded_replace_read_append_and_seal(tmp_path: Path) -> None:
    filesystem, _ = fs(tmp_path)
    paths = filesystem.paths
    assert paths is not None
    token = paths.segment_token(
        "2026-08-18T12:34:56.123456Z",
        uuid.uuid4().hex,
        uuid.uuid4().hex,
        0,
        uuid.uuid4().hex,
    )  # type: ignore[union-attr]
    with filesystem.write_transaction() as tx:
        replaced = tx.replace_bounded(token, expected=None, contents=b"abc", max_bytes=16)
        assert tx.read_bounded(token, max_bytes=16)[1] == replaced
        receipt = tx.append_and_sync(token, expected_offset=3, contents=b"\n", max_result_bytes=16)
        assert receipt.resulting_length == 4
        sealed = tx.seal(token, expected_length=4, max_bytes=16)
        assert sealed.byte_length == 4


def test_transaction_invalidates_and_rejects_unbounded_read(tmp_path: Path) -> None:
    filesystem, _ = fs(tmp_path)
    token = filesystem.paths.registry_token()  # type: ignore[union-attr]
    with filesystem.write_transaction() as tx:
        tx.replace_bounded(token, expected=None, contents=b"x", max_bytes=8)
    with pytest.raises(V3TransactionClosed):
        tx.read_bounded(token, max_bytes=8)
    with filesystem.write_transaction() as tx:
        with pytest.raises(V3CapacityError):
            tx.read_bounded(token, max_bytes=0)


def test_short_write_and_eintr_are_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    filesystem, _ = fs(tmp_path)
    token = filesystem.paths.registry_token()  # type: ignore[union-attr]
    real_write = os.write
    calls = 0

    def short(fd: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EINTR, "interrupted")
        return real_write(fd, data[:1])

    monkeypatch.setattr(os, "write", short)
    with filesystem.write_transaction() as tx:
        tx.replace_bounded(token, expected=None, contents=b"abcd", max_bytes=8)
    assert calls >= 5


def test_zero_write_surfaces_persistence_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filesystem, _ = fs(tmp_path)
    token = filesystem.paths.registry_token()  # type: ignore[union-attr]
    monkeypatch.setattr(os, "write", lambda *_: 0)
    with filesystem.write_transaction() as tx, pytest.raises(V3PersistenceError):
        tx.replace_bounded(token, expected=None, contents=b"x", max_bytes=8)


def test_no_fd_escape_and_requires_injected_capability(tmp_path: Path) -> None:
    with pytest.raises(V3WriterOwnershipError):
        JsonlV3Filesystem(tmp_path, writer_lease=object())  # type: ignore[arg-type]
    filesystem, _ = fs(tmp_path)
    assert not hasattr(filesystem, "create_file")
    assert not hasattr(filesystem, "open_file")
    assert not hasattr(filesystem, "fileno")


def test_transaction_cannot_be_fabricated_or_enumerate_descriptors(tmp_path: Path) -> None:
    filesystem, capability = fs(tmp_path)
    with pytest.raises(TypeError):
        V3WriteTransaction(filesystem, object(), object())  # type: ignore[arg-type]
    with filesystem.write_transaction() as tx:
        assert not hasattr(tx, "fileno")
        assert not hasattr(tx, "opener")
        assert not hasattr(tx, "fds")
        assert not hasattr(tx, "filesystem")
    assert capability.holds == 2


def test_object_new_cannot_fabricate_usable_transaction() -> None:
    fabricated = object.__new__(V3WriteTransaction)
    assert not hasattr(fabricated, "__dict__")
    assert not any(name in {"fds", "fileno", "opener", "filesystem"} for name in dir(fabricated))
    with pytest.raises((AttributeError, V3TransactionClosed)):
        fabricated._check()  # type: ignore[attr-defined]


def test_replace_cleans_exact_uuid_residue_after_create_failure(tmp_path: Path) -> None:
    os.chmod(tmp_path, DIRECTORY_MODE)
    capability = Capability(tmp_path)
    fired = False

    def fault(point: object) -> None:
        nonlocal fired
        if not fired and str(point).endswith("replace.after_temp_create"):
            fired = True
            raise RuntimeError("simulated crash")

    filesystem = JsonlV3Filesystem(tmp_path, writer_lease=capability, fault_hook=fault)
    filesystem.ensure_layout()
    paths = filesystem.paths
    assert paths is not None
    token = paths.segment_token(
        "2026-08-18T12:34:56.123456Z", uuid.uuid4().hex, uuid.uuid4().hex, 0, uuid.uuid4().hex
    )
    with pytest.raises(RuntimeError):
        with filesystem.write_transaction() as tx:
            tx.replace_bounded(token, expected=None, contents=b"x", max_bytes=8)
    with filesystem.write_transaction() as tx:
        tx.replace_bounded(token, expected=None, contents=b"y", max_bytes=8)
    assert list(paths.blackouts.glob(".*.tmp-*")) == []


def test_cleanup_budget_rejects_65_entries_without_deleting_unrelated(tmp_path: Path) -> None:
    filesystem, _ = fs(tmp_path)
    paths = filesystem.paths
    assert paths is not None
    for index in range(65):
        (paths.blackouts / f"unrelated-{index}").write_bytes(b"x")
    with filesystem.write_transaction() as tx:
        tx.replace_bounded(paths.registry_token(), expected=None, contents=b"x", max_bytes=8)
    assert (paths.blackouts / "unrelated-64").exists()
    assert (paths.blackouts / paths.registry_token().value).read_bytes() == b"x"


def test_cleanup_defers_seventeenth_residue_and_retries_next_replace(tmp_path: Path) -> None:
    filesystem, _ = fs(tmp_path)
    paths = filesystem.paths
    assert paths is not None
    token = paths.registry_token()
    for _ in range(17):
        residue = paths.blackouts / f".{token.value}.tmp-{uuid.uuid4().hex}"
        residue.write_bytes(b"stale")
        residue.chmod(0o600)
    with filesystem.write_transaction() as tx:
        first = tx.replace_bounded(token, expected=None, contents=b"first", max_bytes=16)
    remaining = list(paths.blackouts.glob(f".{token.value}.tmp-*"))
    assert len(remaining) == 1
    with filesystem.write_transaction() as tx:
        tx.replace_bounded(token, expected=first, contents=b"second", max_bytes=16)
    assert not list(paths.blackouts.glob(f".{token.value}.tmp-*"))


def test_cleanup_uses_held_directory_after_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filesystem, _ = fs(tmp_path)
    paths = filesystem.paths
    assert paths is not None
    token = paths.registry_token()
    residue = paths.blackouts / f".{token.value}.tmp-{uuid.uuid4().hex}"
    residue.write_bytes(b"stale")
    residue.chmod(0o600)
    real_scandir = os.scandir
    replaced = False

    def replace_path(path: int | str | os.PathLike[str]):
        nonlocal replaced
        if isinstance(path, int) and not replaced:
            replaced = True
            displaced = paths.blackouts.with_name("blackouts-displaced")
            paths.blackouts.rename(displaced)
            paths.blackouts.mkdir(mode=DIRECTORY_MODE)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", replace_path)
    with filesystem.write_transaction() as tx:
        tx.replace_bounded(token, expected=None, contents=b"new", max_bytes=16)
    assert replaced
    displaced = paths.blackouts.with_name("blackouts-displaced")
    assert not (displaced / residue.name).exists()
    assert (displaced / token.value).read_bytes() == b"new"
    assert not (paths.blackouts / token.value).exists()


def test_fchmod_and_unlink_failures_are_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filesystem, _ = fs(tmp_path)
    paths = filesystem.paths
    assert paths is not None
    token = paths.segment_token(
        "2026-08-18T12:34:56.123456Z",
        uuid.uuid4().hex,
        uuid.uuid4().hex,
        0,
        uuid.uuid4().hex,
    )
    with filesystem.write_transaction() as tx:
        tx.replace_bounded(token, expected=None, contents=b"x", max_bytes=8)
    monkeypatch.setattr(os, "fchmod", lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "io")))
    with pytest.raises(V3PersistenceError):
        with filesystem.write_transaction() as tx:
            tx.seal(token, expected_length=1, max_bytes=8)
    basename = (
        f"blk-20260818T123456123456Z-{token.blackout_id}-p{token.ordinal:06d}-"
        f"{token.storage_id}.jsonl"
    )
    residue = paths.segments / f".{basename}.tmp-{uuid.uuid4().hex}"
    residue.write_bytes(b"x")
    residue.chmod(0o600)
    monkeypatch.setattr(
        os, "unlink", lambda *args, **kwargs: (_ for _ in ()).throw(OSError(errno.EIO, "io"))
    )
    with pytest.raises(V3PersistenceError):
        with filesystem.write_transaction() as tx:
            tx.replace_bounded(token, expected=None, contents=b"y", max_bytes=8)


@pytest.mark.parametrize("field", ["entry_ordinal", "limit"])
def test_offset_page_rejects_non_integer_bounds_before_arithmetic(
    tmp_path: Path, field: str
) -> None:
    filesystem, _ = fs(tmp_path)
    paths = filesystem.paths
    assert paths is not None
    segment = paths.segment_token(
        "2026-08-18T12:34:56.123456Z",
        uuid.uuid4().hex,
        uuid.uuid4().hex,
        0,
        uuid.uuid4().hex,
    )
    token = paths.offset_token(segment)
    with filesystem.write_transaction() as tx:
        tx.create_offset_index(token)
        kwargs: dict[str, int | str | bool] = {"entry_ordinal": 0, "limit": 1}
        kwargs[field] = True
        with pytest.raises(V3ValidationError):
            tx.page_offset_index(token, **kwargs)  # type: ignore[arg-type]
        kwargs[field] = "0"
        with pytest.raises(V3ValidationError):
            tx.page_offset_index(token, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("syscall", ["open", "pread", "write", "fdatasync", "fsync", "replace"])
def test_public_transaction_maps_raw_syscall_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, syscall: str
) -> None:
    filesystem, _ = fs(tmp_path)
    paths = filesystem.paths
    assert paths is not None
    token = paths.registry_token()
    if syscall == "open":
        monkeypatch.setattr(
            os, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError(errno.EIO, "io"))
        )
    elif syscall == "pread":
        monkeypatch.setattr(
            os, "pread", lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "io"))
        )
    elif syscall == "write":
        monkeypatch.setattr(
            os, "write", lambda *args: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full"))
        )
    elif syscall == "fdatasync":
        monkeypatch.setattr(
            os, "fdatasync", lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "io"))
        )
    elif syscall == "fsync":
        monkeypatch.setattr(
            os, "fsync", lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "io"))
        )
    else:
        monkeypatch.setattr(
            os, "replace", lambda *args, **kwargs: (_ for _ in ()).throw(OSError(errno.EIO, "io"))
        )
    with pytest.raises(V3PersistenceError):
        with filesystem.write_transaction() as tx:
            tx.replace_bounded(token, expected=None, contents=b"x", max_bytes=8)


def test_transaction_owner_seam_rejects_foreign_and_inactive(tmp_path: Path) -> None:
    filesystem, _ = fs(tmp_path)
    other_root = tmp_path / "other"
    other_root.mkdir(mode=DIRECTORY_MODE)
    other, _ = fs(other_root)
    with filesystem.write_transaction() as tx:
        tx.assert_owner(filesystem)
        with pytest.raises(V3WriterOwnershipError):
            tx.assert_owner(other)
    with pytest.raises(V3TransactionClosed):
        tx.assert_owner(filesystem)


@pytest.mark.parametrize("offset,length", [(-1, 1), (0, -1), (0, 4 * 1024 * 1024 + 1), (9, 1)])
def test_read_exact_rejects_bounds_and_eof(tmp_path: Path, offset: int, length: int) -> None:
    path = tmp_path / "read"
    path.write_bytes(b"abc")
    fd = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises((V3CapacityError, V3CorruptionError)):
            _read_exact(fd, offset, length)
    finally:
        os.close(fd)


def test_read_exact_retries_eintr_and_short_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "read"
    path.write_bytes(b"abcdef")
    fd = os.open(path, os.O_RDONLY)
    real = os.pread
    calls = 0

    def short(descriptor: int, size: int, offset: int) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EINTR, "interrupted")
        return real(descriptor, min(size, 1), offset)

    monkeypatch.setattr(os, "pread", short)
    try:
        assert _read_exact(fd, 0, 6) == b"abcdef"
    finally:
        os.close(fd)
