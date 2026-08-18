from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from src.adapters.jsonl_v3_errors import V3PathError, V3ValidationError
from src.adapters.jsonl_v3_storage_paths import (
    DIRECTORY_MODE,
    V3CatalogHeadToken,
    V3CatalogIntentToken,
    V3CatalogToken,
    V3OffsetPathToken,
    V3StoragePaths,
    _resolve_token,
    validate_directory,
    validate_existing_entry,
    validate_uuid4_hex,
)

UTC = "2026-08-18T12:34:56.123456Z"


def test_resolver_is_pure_and_tokens_are_typed(tmp_path: Path) -> None:
    os.chmod(tmp_path, DIRECTORY_MODE)
    paths = V3StoragePaths(tmp_path)
    token = paths.segment_token(UTC, uuid.uuid4().hex, uuid.uuid4().hex, 2, uuid.uuid4().hex)
    offset = paths.offset_token(token)
    assert isinstance(offset, V3OffsetPathToken)
    assert not (tmp_path / "events-v3").exists()
    assert not hasattr(paths, "monitor_lock")
    assert not hasattr(paths, "ensure_layout")


@pytest.mark.parametrize("value", ["../x", "/tmp/x", "x/y", "bad\x00name"])
def test_grammar_rejects_untrusted_identifiers(tmp_path: Path, value: str) -> None:
    os.chmod(tmp_path, DIRECTORY_MODE)
    paths = V3StoragePaths(tmp_path)
    with pytest.raises((V3PathError, V3ValidationError)):
        paths.segment_token(UTC, value, uuid.uuid4().hex, 0, uuid.uuid4().hex)


def test_uuid4_and_directory_validation_are_strict(tmp_path: Path) -> None:
    os.chmod(tmp_path, DIRECTORY_MODE)
    assert validate_uuid4_hex(uuid.uuid4().hex)
    with pytest.raises(V3ValidationError):
        validate_uuid4_hex("0" * 32)
    assert validate_directory(tmp_path, owner_uid=os.getuid()).st_uid == os.getuid()
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(V3PathError):
        validate_directory(link, owner_uid=os.getuid())


@pytest.mark.parametrize(
    "case", ["missing", "directory", "symlink", "wrong-owner", "wrong-mode", "hardlink"]
)
def test_existing_entry_rejects_each_unsafe_identity(tmp_path: Path, case: str) -> None:
    target = tmp_path / "entry"
    target.write_bytes(b"x")
    os.chmod(target, 0o600)
    if case == "missing":
        target.unlink()
    elif case == "directory":
        target.unlink()
        target.mkdir(mode=0o600)
    elif case == "symlink":
        target.unlink()
        target.symlink_to(tmp_path / "other")
    elif case == "wrong-owner":
        pass
    elif case == "wrong-mode":
        os.chmod(target, 0o400)
    elif case == "hardlink":
        os.link(target, tmp_path / "alias")
    owner = os.getuid() + 1 if case == "wrong-owner" else os.getuid()
    with pytest.raises(V3PathError):
        validate_existing_entry(target, owner_uid=owner, mode=0o600)


def test_paths_reject_wrong_root_owner_and_cross_binding(tmp_path: Path) -> None:
    os.chmod(tmp_path, DIRECTORY_MODE)
    with pytest.raises(V3PathError):
        V3StoragePaths(tmp_path, owner_uid=os.getuid() + 1)
    paths = V3StoragePaths(tmp_path)
    token = paths.segment_token(UTC, uuid.uuid4().hex, uuid.uuid4().hex, 0, uuid.uuid4().hex)
    with pytest.raises(V3PathError):
        paths.offset_token(object())  # type: ignore[arg-type]
    assert token.blackout_id != uuid.uuid4().hex


def test_catalog_tokens_are_process_global_and_resolve_under_blackouts(tmp_path: Path) -> None:
    os.chmod(tmp_path, DIRECTORY_MODE)
    paths = V3StoragePaths(tmp_path)

    catalog = V3CatalogToken.TERMINAL_CATALOG
    head = V3CatalogHeadToken.TERMINAL_CATALOG_HEAD
    intent = V3CatalogIntentToken.TERMINAL_CATALOG_APPEND_INTENT
    assert catalog is V3CatalogToken.TERMINAL_CATALOG
    assert head is V3CatalogHeadToken.TERMINAL_CATALOG_HEAD
    assert intent is V3CatalogIntentToken.TERMINAL_CATALOG_APPEND_INTENT
    assert V3CatalogToken.TERMINAL_CATALOG is catalog
    assert V3CatalogHeadToken.TERMINAL_CATALOG_HEAD is head
    assert V3CatalogIntentToken.TERMINAL_CATALOG_APPEND_INTENT is intent
    assert not hasattr(catalog, "blackout_id")
    assert not hasattr(head, "blackout_id")
    assert not hasattr(intent, "blackout_id")

    assert _resolve_token(paths, catalog) == (
        paths.blackouts,
        "terminal-catalog-v1.jsonl",
    )
    assert _resolve_token(paths, head) == (
        paths.blackouts,
        "terminal-catalog-head-v1.json",
    )
    assert _resolve_token(paths, intent) == (
        paths.blackouts,
        "terminal-catalog-append-intent-v1.json",
    )
