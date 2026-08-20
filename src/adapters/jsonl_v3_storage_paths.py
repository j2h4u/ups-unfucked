"""Pure v3 resolver and typed path-token grammar.

Tokens in this module are capabilities, not paths.  Only the private resolver
turns a validated token into a directory-relative basename for the filesystem
transaction.
"""

from __future__ import annotations

import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Generic, TypeAlias, TypeVar, cast

from src.adapters.jsonl_v3_errors import V3PathError, V3ValidationError

_T = TypeVar("_T")

V3_ROOT_NAME = "events-v3"
BLACKOUTS_DIR_NAME = "blackouts"
SEGMENTS_DIR_NAME = "segments"
TERMINAL_CHAINS_DIR_NAME = "terminal-chains"
TRANSACTIONS_DIR_NAME = "transactions"
TERMINAL_LOCATORS_DIR_NAME = "terminal-locators"
HISTORY_DIR_NAME = "history"
HISTORY_RUNS_DIR_NAME = "runs"
DIRECTORY_MODE = 0o700
MUTABLE_MODE = 0o600
SEALED_MODE = 0o400

UUID4_HEX_RE = re.compile(r"\A[0-9a-f]{32}\Z")
_UTC_RE = re.compile(r"\A\d{8}T\d{12}Z\Z")
_HASH_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"\A[^/\\\x00-\x1f\x7f]+\Z")
_TEMP_RE = re.compile(r"\A[0-9a-f]{32}\Z")
SEGMENT_TOKEN_RE = re.compile(
    r"\Ablk-(\d{8}T\d{12}Z)-([0-9a-f]{32})-p(\d{6})-([0-9a-f]{32})\.jsonl\Z"
)
OFFSET_TOKEN_RE = re.compile(
    r"\Ablk-(\d{8}T\d{12}Z)-([0-9a-f]{32})-p(\d{6})-([0-9a-f]{32})\.offsets\Z"
)
DAMAGED_JSONL_TOKEN_RE = re.compile(
    r"\Adamaged-([0-9a-f]{32})-([0-9a-f]{32})-p(\d{6})-([0-9a-f]{32})-([0-9a-f]{64})\.jsonl\Z"
)
DAMAGED_OFFSETS_TOKEN_RE = re.compile(
    r"\Adamaged-([0-9a-f]{32})-([0-9a-f]{32})-p(\d{6})-([0-9a-f]{32})-([0-9a-f]{64})\.offsets\Z"
)


def validate_uuid4_hex(value: object, field: str = "UUID") -> str:
    if not isinstance(value, str) or UUID4_HEX_RE.fullmatch(value) is None:
        raise V3ValidationError(f"{field} must be lowercase UUID4 hex")
    try:
        parsed = uuid.UUID(hex=value)
    except ValueError as exc:
        raise V3ValidationError(f"{field} is not a UUID") from exc
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        raise V3ValidationError(f"{field} must be RFC-4122 UUID4")
    return value


def validate_ordinal(value: object, field: str = "ordinal") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 63:
        raise V3ValidationError(f"{field} must be an integer from 0 through 63")
    return value


def validate_path_token(value: object, field: str = "path token") -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise V3PathError(f"{field} is not a safe basename")
    if value in {".", ".."} or Path(value).is_absolute():
        raise V3PathError(f"{field} is not a relative basename")
    return value


def utc_filename_token(value: object) -> str:
    if not isinstance(value, str):
        raise V3ValidationError("started UTC must be text")
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{6})Z", value)
    if match is None:
        raise V3ValidationError("started UTC is not canonical microsecond UTC")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise V3ValidationError("started UTC is not calendar-valid") from exc
    return parsed.strftime("%Y%m%dT%H%M%S%fZ")


def _token_id(value: str, field: str) -> None:
    try:
        validate_uuid4_hex(value, field)
    except V3ValidationError as exc:
        raise V3PathError("token identifier is not UUID4") from exc


def _token_hash(value: str) -> None:
    if _HASH_RE.fullmatch(value) is None:
        raise V3PathError("token hash is not lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class V3SegmentPathToken:
    blackout_id: str
    logical_segment_id: str
    started_utc: str
    ordinal: int
    storage_id: str

    def __post_init__(self) -> None:
        _token_id(self.blackout_id, "blackout_id")
        _token_id(self.logical_segment_id, "logical_segment_id")
        _token_id(self.storage_id, "storage_id")
        utc_filename_token(self.started_utc)
        validate_ordinal(self.ordinal)


@dataclass(frozen=True, slots=True)
class V3OffsetPathToken(V3SegmentPathToken):
    pass


@dataclass(frozen=True, slots=True)
class V3DamagedSegmentPathToken:
    blackout_id: str
    logical_segment_id: str
    ordinal: int
    storage_id: str
    file_sha256: str

    def __post_init__(self) -> None:
        _token_id(self.blackout_id, "blackout_id")
        _token_id(self.logical_segment_id, "logical_segment_id")
        _token_id(self.storage_id, "storage_id")
        validate_ordinal(self.ordinal)
        _token_hash(self.file_sha256)


@dataclass(frozen=True, slots=True)
class V3DamagedOffsetPathToken(V3DamagedSegmentPathToken):
    pass


class V3RegistryToken(Enum):
    WORK_REGISTRY = "work-registry-v1.json"


@dataclass(frozen=True, slots=True)
class _BlackoutToken:
    blackout_id: str

    def __post_init__(self) -> None:
        _token_id(self.blackout_id, "blackout_id")


class V3TerminalStagingToken(_BlackoutToken):
    pass


class V3TerminalChainToken(_BlackoutToken):
    pass


class V3TerminalLocatorToken(_BlackoutToken):
    pass


class V3CatalogToken(Enum):
    TERMINAL_CATALOG = "terminal-catalog-v1.jsonl"


class V3CatalogHeadToken(Enum):
    TERMINAL_CATALOG_HEAD = "terminal-catalog-head-v1.json"


class V3CatalogIntentToken(Enum):
    TERMINAL_CATALOG_APPEND_INTENT = "terminal-catalog-append-intent-v1.json"


class V3HistoryFileToken:
    __slots__ = ("name",)
    _NAMES = frozenset(
        {
            "index-v1.jsonl",
            "index-head-v1.json",
            "rebuild-state-v1.json",
            "event-scan-v1.json",
            "merge-v1.jsonl",
        }
    )

    def __init__(self, name: str) -> None:
        if name not in self._NAMES:
            raise V3PathError("history token is outside the closed grammar")
        self.name = name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, V3HistoryFileToken) and self.name == other.name

    def __hash__(self) -> int:
        return hash((type(self), self.name))


@dataclass(frozen=True, slots=True)
class V3TemporaryFileToken(Generic[_T]):
    destination: object
    nonce: str

    def __post_init__(self) -> None:
        try:
            validate_uuid4_hex(self.nonce, "temporary nonce")
        except V3ValidationError:
            raise V3PathError("temporary token nonce is not UUID4 hex")


V3OffsetIndexToken: TypeAlias = V3OffsetPathToken | V3TemporaryFileToken
V3CreatableCaptureToken: TypeAlias = V3SegmentPathToken | V3TerminalStagingToken
V3ReadableFileToken: TypeAlias = (
    V3RegistryToken
    | V3SegmentPathToken
    | V3OffsetPathToken
    | V3DamagedSegmentPathToken
    | V3DamagedOffsetPathToken
    | V3TerminalStagingToken
    | V3TerminalChainToken
    | V3TerminalLocatorToken
    | V3CatalogToken
    | V3CatalogHeadToken
    | V3CatalogIntentToken
    | V3HistoryFileToken
    | V3TemporaryFileToken
)
V3MutableFileToken: TypeAlias = (
    V3RegistryToken
    | V3SegmentPathToken
    | V3OffsetPathToken
    | V3TerminalStagingToken
    | V3CatalogToken
    | V3CatalogHeadToken
    | V3CatalogIntentToken
    | V3HistoryFileToken
    | V3TemporaryFileToken
)
V3AppendableFileToken: TypeAlias = (
    V3SegmentPathToken
    | V3TerminalStagingToken
    | V3CatalogToken
    | V3HistoryFileToken
    | V3TemporaryFileToken
)
V3SealableFileToken: TypeAlias = (
    V3SegmentPathToken
    | V3OffsetPathToken
    | V3TerminalStagingToken
    | V3TerminalLocatorToken
    | V3HistoryFileToken
    | V3TemporaryFileToken
)
V3PromotedFileToken: TypeAlias = (
    V3TerminalChainToken
    | V3TerminalLocatorToken
    | V3HistoryFileToken
    | V3SegmentPathToken
    | V3OffsetPathToken
)


def validate_existing_entry(path: Path, *, owner_uid: int, mode: int) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise V3PathError("required v3 entry cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise V3PathError("v3 entry is not a regular file")
    if info.st_uid != owner_uid:
        raise V3PathError("v3 entry has wrong owner")
    if stat.S_IMODE(info.st_mode) != mode:
        raise V3PathError("v3 entry has unsafe mode")
    if info.st_nlink != 1:
        raise V3PathError("v3 entry has multiple links")
    return info


def validate_directory(path: Path, *, owner_uid: int | None) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise V3PathError("required v3 directory cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise V3PathError("v3 entry is not a directory")
    if owner_uid is not None and info.st_uid != owner_uid:
        raise V3PathError("v3 directory has wrong owner")
    if stat.S_IMODE(info.st_mode) != DIRECTORY_MODE:
        raise V3PathError("v3 directory has unsafe mode")
    return info


@dataclass(frozen=True, slots=True)
class V3StoragePaths:
    state_root: Path
    owner_uid: int | None = None

    def __post_init__(self) -> None:
        root = Path(self.state_root)
        object.__setattr__(self, "state_root", root)
        if self.owner_uid is None:
            object.__setattr__(self, "owner_uid", os.getuid())
        info = validate_directory(root, owner_uid=self.owner_uid)
        if stat.S_IMODE(info.st_mode) != DIRECTORY_MODE:
            raise V3PathError("STATE_ROOT has unsafe mode")

    @property
    def events_v3(self) -> Path:
        return self.state_root / V3_ROOT_NAME

    @property
    def blackouts(self) -> Path:
        return self.events_v3 / BLACKOUTS_DIR_NAME

    @property
    def segments(self) -> Path:
        return self.blackouts / SEGMENTS_DIR_NAME

    @property
    def terminal_chains(self) -> Path:
        return self.blackouts / TERMINAL_CHAINS_DIR_NAME

    @property
    def transactions(self) -> Path:
        return self.blackouts / TRANSACTIONS_DIR_NAME

    @property
    def terminal_locators(self) -> Path:
        return self.blackouts / TERMINAL_LOCATORS_DIR_NAME

    @property
    def history(self) -> Path:
        return self.blackouts / HISTORY_DIR_NAME

    @property
    def history_runs(self) -> Path:
        return self.history / HISTORY_RUNS_DIR_NAME

    def segment_token(
        self,
        started_utc: str,
        blackout_id: str,
        logical_segment_id: str,
        ordinal: int,
        storage_id: str,
    ) -> V3SegmentPathToken:
        return V3SegmentPathToken(blackout_id, logical_segment_id, started_utc, ordinal, storage_id)

    def offset_token(self, token: V3SegmentPathToken) -> V3OffsetPathToken:
        if not isinstance(token, V3SegmentPathToken) or isinstance(token, V3OffsetPathToken):
            raise V3PathError("offset token requires an active segment token")
        return V3OffsetPathToken(
            token.blackout_id,
            token.logical_segment_id,
            token.started_utc,
            token.ordinal,
            token.storage_id,
        )

    def damaged_tokens(
        self,
        blackout_id: str,
        logical_segment_id: str,
        ordinal: int,
        storage_id: str,
        file_sha256: str,
    ) -> tuple[V3DamagedSegmentPathToken, V3DamagedOffsetPathToken]:
        return (
            V3DamagedSegmentPathToken(
                blackout_id, logical_segment_id, ordinal, storage_id, file_sha256
            ),
            V3DamagedOffsetPathToken(
                blackout_id, logical_segment_id, ordinal, storage_id, file_sha256
            ),
        )

    def registry_token(self) -> V3RegistryToken:
        return V3RegistryToken.WORK_REGISTRY

    def terminal_staging_token(self, blackout_id: str) -> V3TerminalStagingToken:
        return V3TerminalStagingToken(blackout_id)

    def terminal_chain_token(self, blackout_id: str) -> V3TerminalChainToken:
        return V3TerminalChainToken(blackout_id)

    def locator_token(self, blackout_id: str) -> V3TerminalLocatorToken:
        return V3TerminalLocatorToken(blackout_id)

    def history_token(self, name: str) -> V3HistoryFileToken:
        return V3HistoryFileToken(name)

    def temporary_token(self, destination: V3MutableFileToken) -> V3TemporaryFileToken:
        return V3TemporaryFileToken(destination, uuid.uuid4().hex)


def _active_basename(token: V3SegmentPathToken, suffix: str) -> str:
    return f"blk-{utc_filename_token(token.started_utc)}-{token.blackout_id}-p{token.ordinal:06d}-{token.storage_id}.{suffix}"


def _damaged_basename(token: V3DamagedSegmentPathToken, suffix: str) -> str:
    return f"damaged-{token.blackout_id}-{token.logical_segment_id}-p{token.ordinal:06d}-{token.storage_id}-{token.file_sha256}.{suffix}"


def _resolve_token(paths: V3StoragePaths, token: V3ReadableFileToken) -> tuple[Path, str]:
    if token is V3RegistryToken.WORK_REGISTRY:
        return paths.blackouts, token.value
    segment = _segment_location(paths, token)
    if segment is not None:
        return segment
    named = _named_token_location(paths, token)
    if named is not None:
        return named
    if isinstance(token, V3TemporaryFileToken):
        parent, basename = _resolve_token(paths, cast(V3ReadableFileToken, token.destination))
        return parent, f".{basename}.tmp-{token.nonce}"
    raise V3PathError("unsupported v3 path token")


def _segment_location(paths: V3StoragePaths, token: object) -> tuple[Path, str] | None:
    if isinstance(token, V3OffsetPathToken):
        return paths.segments, _active_basename(token, "offsets")
    if isinstance(token, V3SegmentPathToken):
        return paths.segments, _active_basename(token, "jsonl")
    if isinstance(token, V3DamagedOffsetPathToken):
        return paths.segments, _damaged_basename(token, "offsets")
    if isinstance(token, V3DamagedSegmentPathToken):
        return paths.segments, _damaged_basename(token, "jsonl")
    return None


def _named_token_location(paths: V3StoragePaths, token: object) -> tuple[Path, str] | None:
    handlers = (
        (
            V3TerminalStagingToken,
            paths.transactions,
            lambda value: f"tail-{value.blackout_id}.jsonl",
        ),
        (V3TerminalChainToken, paths.terminal_chains, lambda value: f"{value.blackout_id}.jsonl"),
        (
            V3TerminalLocatorToken,
            paths.terminal_locators,
            lambda value: f"{value.blackout_id}.json",
        ),
        (
            V3CatalogToken,
            paths.blackouts,
            lambda value: value.value,
        ),
        (
            V3CatalogHeadToken,
            paths.blackouts,
            lambda value: value.value,
        ),
        (
            V3CatalogIntentToken,
            paths.blackouts,
            lambda value: value.value,
        ),
        (V3HistoryFileToken, paths.history, lambda value: value.name),
    )
    for kind, parent, name in handlers:
        if isinstance(token, kind):
            return parent, name(token)
    return None
