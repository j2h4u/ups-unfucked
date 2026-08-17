"""Read-only Slice-0 telemetry capability baseline producer and validator.

The baseline is derived configuration, not scientific evidence.  Collection uses
only ordinary NUT ``LIST VAR`` replies, retains every returned key and exact value
token, and publishes one immutable owner-only JSON document after 60 complete
replies.  The module deliberately has no UPS command path.
"""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.domain.telemetry_capability import (
    CAPABILITY_IDENTITY_POLICY_REVISION,
    CapabilitySignatureError,
    FieldSignature,
    StateSignature,
    TokenShape,
    build_state_signatures,
    field_presence_mode,
    make_capability_reply,
    token_shape,
    token_shape_sort_key,
)
from src.nut_client import StrictNUTTelemetryPort

SCHEMA_VERSION = 1
ARTIFACT_TYPE = "telemetry-capability-baseline"
ARTIFACT_FILENAME = "telemetry-capability-baseline-v1.json"
REPLY_COUNT = 60
OWNER_ONLY_MODE = 0o600
MAX_BASELINE_BYTES = 8 * 1024 * 1024

_IDENTITY_KEYS: dict[str, tuple[str, ...]] = {
    "ups_model": ("device.model", "ups.model"),
    "ups_serial": ("device.serial", "ups.serial"),
    "ups_firmware": ("ups.firmware", "device.firmware"),
    "nut_driver_name": ("driver.name",),
    "nut_driver_version": ("driver.version",),
}
_OPTIONAL_IDENTITY_FIELDS = frozenset({"ups_firmware"})
_TOP_LEVEL_KEYS = frozenset(
    {
        "artifact_type",
        "capability_identity_policy_revision",
        "endpoint",
        "identity",
        "identity_source_keys",
        "observed_ups_status",
        "raw_keys",
        "replies",
        "reply_count",
        "schema_version",
        "state_scoped_signatures",
    }
)


class TelemetryCapabilityError(ValueError):
    """A baseline cannot be collected, persisted, or trusted."""


class BaselineRefusal(TelemetryCapabilityError):
    """A bounded refusal diagnostic for baseline coordination failures."""


class BaselinePublicationDurabilityError(TelemetryCapabilityError):
    """The canonical artifact exists, but its parent-directory fsync failed."""


@dataclass(frozen=True, slots=True)
class CollectionTiming:
    """Poll cadence seam; production uses ordinary one-second NUT cadence.

    ``monotonic`` and ``sleep`` are injected together so tests can model both
    reply latency and scheduler overruns without waiting in real time.
    """

    poll_interval_seconds: float = 1.0
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


@dataclass(frozen=True, slots=True)
class NUTEndpoint:
    """Configured physical NUT endpoint used by the producer and preflight."""

    host: str = "localhost"
    port: int = 3493
    ups_name: str = "cyberpower"


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Return deterministic UTF-8 JSON bytes without a trailing newline."""
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TelemetryCapabilityError(f"baseline is not canonical JSON: {exc}") from exc


def _read_reply(client: StrictNUTTelemetryPort) -> tuple[dict[str, float | str], dict[str, str]]:
    try:
        values, tokens = client.get_ups_vars_with_tokens_strict()
    except (OSError, ValueError) as exc:
        raise TelemetryCapabilityError(f"LIST VAR reply is incomplete: {exc}") from exc
    if not isinstance(values, dict) or not isinstance(tokens, dict):
        raise TelemetryCapabilityError("LIST VAR reply must contain value and token mappings")
    if set(values) != set(tokens) or not tokens:
        raise TelemetryCapabilityError("LIST VAR reply has incomplete value/token coverage")
    for key, token in tokens.items():
        if not isinstance(key, str) or not isinstance(token, str) or "\n" in token or "\r" in token:
            raise TelemetryCapabilityError("LIST VAR reply contains an invalid raw token")
    return values, tokens


def _reply_identity(
    tokens: Mapping[str, str],
) -> tuple[dict[str, str | None], dict[str, str | None]]:
    identity: dict[str, str | None] = {}
    source_keys: dict[str, str | None] = {}
    for field, candidates in _IDENTITY_KEYS.items():
        present = [(key, tokens[key]) for key in candidates if key in tokens]
        if not present and field in _OPTIONAL_IDENTITY_FIELDS:
            identity[field] = None
            source_keys[field] = None
            continue
        if not present or any(not token for _, token in present):
            raise TelemetryCapabilityError(f"mandatory identity field is missing: {field}")
        if len({token for _, token in present}) != 1:
            raise TelemetryCapabilityError(f"identity aliases disagree: {field}")
        source_key, token = (
            next(item for item in present if item[0] == candidates[0])
            if candidates[0] in tokens
            else present[0]
        )
        identity[field] = token
        source_keys[field] = source_key
    return identity, source_keys


def _collect_replies(
    client: StrictNUTTelemetryPort,
    timing: CollectionTiming,
) -> tuple[
    list[dict[str, Any]],
    dict[str, str | None],
    dict[str, str | None],
]:
    if (
        isinstance(timing.poll_interval_seconds, bool)
        or not isinstance(timing.poll_interval_seconds, (int, float))
        or not math.isfinite(float(timing.poll_interval_seconds))
        or timing.poll_interval_seconds < 0
    ):
        raise TelemetryCapabilityError("poll interval must be non-negative")
    observations: list[dict[str, Any]] = []
    first_identity: dict[str, str | None] | None = None
    first_source_keys: dict[str, str | None] | None = None
    collection_start = timing.monotonic()
    for sequence in range(REPLY_COUNT):
        if sequence:
            deadline = collection_start + sequence * timing.poll_interval_seconds
            delay = deadline - timing.monotonic()
            if delay > 0:
                timing.sleep(delay)
        values, tokens = _read_reply(client)
        identity, source_keys = _reply_identity(tokens)
        if first_identity is None:
            first_identity = identity
            first_source_keys = source_keys
        elif identity != first_identity or source_keys != first_source_keys:
            raise TelemetryCapabilityError(f"identity changed in LIST VAR reply {sequence}")
        status = tokens.get("ups.status")
        if not status:
            raise TelemetryCapabilityError(f"ups.status is missing in LIST VAR reply {sequence}")
        try:
            domain_reply = make_capability_reply(status, values, tokens)
        except CapabilitySignatureError as exc:
            raise TelemetryCapabilityError(
                f"LIST VAR parsed values disagree with raw tokens in reply {sequence}"
            ) from exc
        observations.append(
            {
                "domain_reply": domain_reply,
                "sequence": sequence,
                "status": status,
                "tokens": dict(tokens),
                "values": values,
            }
        )
    assert first_identity is not None
    assert first_source_keys is not None
    return observations, dict(first_identity), dict(first_source_keys)


def _state_signatures(observations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    replies = tuple(observation["domain_reply"] for observation in observations)
    return {
        state.status: _serialize_state_signature(state) for state in build_state_signatures(replies)
    }


def _serialize_state_signature(state: StateSignature) -> dict[str, Any]:
    return {
        "fields": {
            field: _serialize_field_signature(signature) for field, signature in state.fields
        },
        "reply_count": state.reply_count,
    }


def _serialize_field_signature(signature: FieldSignature) -> dict[str, Any]:
    return {
        "missing_count": signature.missing_count,
        "parsed_types": list(signature.parsed_types),
        "presence_mode": signature.presence_mode,
        "present_count": signature.present_count,
        "token_shapes": [_serialize_token_shape(shape) for shape in signature.token_shapes],
    }


def _serialize_token_shape(shape: TokenShape) -> dict[str, Any]:
    return {
        "decimal_places": shape.decimal_places,
        "exponent": shape.exponent,
        "lexical_form": shape.lexical_form,
        "parsed_type": shape.parsed_type,
        "quantization_exponent": shape.quantization_exponent,
        "sign_form": shape.sign_form,
        "string_value_sha256": shape.string_value_sha256,
    }


def build_baseline(
    client: StrictNUTTelemetryPort,
    *,
    host: str = "localhost",
    port: int = 3493,
    ups_name: str = "cyberpower",
    timing: CollectionTiming | None = None,
) -> dict[str, Any]:
    """Collect exactly 60 replies and return the deterministic baseline mapping."""
    if not isinstance(host, str) or not host or "\n" in host or "\r" in host:
        raise TelemetryCapabilityError("NUT host is invalid")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise TelemetryCapabilityError("NUT port is invalid")
    if not isinstance(ups_name, str) or not ups_name or ups_name.endswith("-virtual"):
        raise TelemetryCapabilityError("baseline requires the configured physical UPS name")
    observations, identity, source_keys = _collect_replies(client, timing or CollectionTiming())
    statuses = sorted({item["status"] for item in observations})
    raw_keys = sorted({key for item in observations for key in item["tokens"]})
    return {
        "artifact_type": ARTIFACT_TYPE,
        "capability_identity_policy_revision": CAPABILITY_IDENTITY_POLICY_REVISION,
        "endpoint": {"host": host, "port": port, "ups_name": ups_name},
        "identity": identity,
        "identity_source_keys": source_keys,
        "observed_ups_status": statuses,
        "raw_keys": raw_keys,
        "replies": [
            {
                "sequence": item["sequence"],
                "tokens": dict(sorted(item["tokens"].items())),
                "ups_status": item["status"],
            }
            for item in observations
        ],
        "reply_count": REPLY_COUNT,
        "schema_version": SCHEMA_VERSION,
        "state_scoped_signatures": _state_signatures(observations),
    }


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TelemetryCapabilityError(f"{name} must be an object")
    return value


def _validate_shape(value: Mapping[str, Any]) -> None:
    if set(value) != _TOP_LEVEL_KEYS:
        raise TelemetryCapabilityError("baseline fields do not match schema v1")
    if (
        value["artifact_type"] != ARTIFACT_TYPE
        or value["schema_version"] != SCHEMA_VERSION
        or value["capability_identity_policy_revision"] != CAPABILITY_IDENTITY_POLICY_REVISION
    ):
        raise TelemetryCapabilityError("baseline schema version is not v1")
    if value["reply_count"] != REPLY_COUNT:
        raise TelemetryCapabilityError("baseline must contain exactly 60 replies")
    _validate_endpoint(value["endpoint"])
    identity, source_keys = _validated_identity(value)
    statuses, raw_keys = _validated_key_sets(value)
    replies = _validated_replies_shape(value["replies"])
    _validate_replies(replies, statuses, raw_keys, identity, source_keys)
    observed_statuses = sorted({reply["ups_status"] for reply in replies})
    observed_raw_keys = sorted(
        {key for reply in replies for key in _require_mapping(reply["tokens"], "reply tokens")}
    )
    if statuses != observed_statuses or raw_keys != observed_raw_keys:
        raise TelemetryCapabilityError("baseline status or raw key set is not derived from replies")
    _validate_signatures(value["state_scoped_signatures"], statuses, raw_keys, replies)


def _validate_endpoint(value: Any) -> None:
    endpoint = _require_mapping(value, "endpoint")
    if (
        set(endpoint) != {"host", "port", "ups_name"}
        or not isinstance(endpoint["host"], str)
        or not endpoint["host"]
        or "\n" in endpoint["host"]
        or "\r" in endpoint["host"]
    ):
        raise TelemetryCapabilityError("baseline endpoint is invalid")
    if (
        isinstance(endpoint["port"], bool)
        or not isinstance(endpoint["port"], int)
        or not 1 <= endpoint["port"] <= 65535
    ):
        raise TelemetryCapabilityError("baseline endpoint port is invalid")
    if (
        not isinstance(endpoint["ups_name"], str)
        or not endpoint["ups_name"]
        or endpoint["ups_name"].endswith("-virtual")
    ):
        raise TelemetryCapabilityError("baseline endpoint is not physical")


def _validated_identity(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    identity = _require_mapping(value["identity"], "identity")
    source_keys = _require_mapping(value["identity_source_keys"], "identity_source_keys")
    if set(identity) != set(_IDENTITY_KEYS) or set(source_keys) != set(_IDENTITY_KEYS):
        raise TelemetryCapabilityError("baseline identity fields are incomplete")
    for field in _IDENTITY_KEYS:
        identity_value = identity[field]
        source_key = source_keys[field]
        if field in _OPTIONAL_IDENTITY_FIELDS and identity_value is None and source_key is None:
            continue
        if not isinstance(identity_value, str) or not identity_value:
            raise TelemetryCapabilityError("baseline identity values are invalid")
        if (
            not isinstance(source_key, str)
            or not source_key
            or source_key not in _IDENTITY_KEYS[field]
        ):
            raise TelemetryCapabilityError("baseline identity source keys are invalid")
    return identity, source_keys


def _validated_key_sets(value: Mapping[str, Any]) -> tuple[list[Any], list[Any]]:
    statuses = value["observed_ups_status"]
    raw_keys = value["raw_keys"]
    if (
        not isinstance(statuses, list)
        or statuses != sorted(statuses)
        or len(statuses) != len(set(statuses))
        or not all(isinstance(item, str) and item for item in statuses)
    ):
        raise TelemetryCapabilityError("baseline status set is invalid")
    if (
        not isinstance(raw_keys, list)
        or raw_keys != sorted(raw_keys)
        or len(raw_keys) != len(set(raw_keys))
        or not all(isinstance(item, str) and item for item in raw_keys)
    ):
        raise TelemetryCapabilityError("baseline raw key set is invalid")
    return statuses, raw_keys


def _validated_replies_shape(value: Any) -> list[Any]:
    if not isinstance(value, list) or len(value) != REPLY_COUNT:
        raise TelemetryCapabilityError("baseline reply list is invalid")
    return value


def _validate_replies(
    replies: list[Any],
    statuses: list[Any],
    raw_keys: list[Any],
    identity: Mapping[str, Any],
    source_keys: Mapping[str, Any],
) -> None:
    for sequence, reply in enumerate(replies):
        item = _require_mapping(reply, "reply")
        if set(item) != {"sequence", "tokens", "ups_status"} or item["sequence"] != sequence:
            raise TelemetryCapabilityError("baseline reply sequence is invalid")
        tokens = _require_mapping(item["tokens"], "reply tokens")
        if item["ups_status"] not in statuses or tokens.get("ups.status") != item["ups_status"]:
            raise TelemetryCapabilityError("baseline reply status is invalid")
        if not set(tokens).issubset(raw_keys):
            raise TelemetryCapabilityError("baseline reply raw keys are invalid")
        if any(
            not isinstance(key, str) or not isinstance(token, str) for key, token in tokens.items()
        ):
            raise TelemetryCapabilityError("baseline reply token is invalid")
        reply_identity, reply_source_keys = _reply_identity(tokens)
        if reply_identity != identity or reply_source_keys != source_keys:
            raise TelemetryCapabilityError("baseline reply identity changed")


def _validate_signatures(
    value: Any, statuses: list[Any], raw_keys: list[Any], replies: list[Any]
) -> None:
    signatures = _require_mapping(value, "state_scoped_signatures")
    if set(signatures) != set(statuses):
        raise TelemetryCapabilityError("baseline state signatures do not match statuses")
    for status in statuses:
        selected = [reply for reply in replies if reply["ups_status"] == status]
        _validate_state_signature(signatures[status], raw_keys, selected)


def _validate_state_signature(value: Any, raw_keys: list[Any], selected: list[Any]) -> None:
    signature = _require_mapping(value, "state signature")
    if set(signature) != {"fields", "reply_count"}:
        raise TelemetryCapabilityError("baseline state signature shape is invalid")
    fields = _require_mapping(signature.get("fields"), "state signature fields")
    if signature.get("reply_count") != len(selected) or set(fields) != set(raw_keys):
        raise TelemetryCapabilityError("baseline state signature coverage is invalid")
    for key in raw_keys:
        present = [reply for reply in selected if key in reply["tokens"]]
        _validate_field_signature(fields[key], key, present, len(selected))


def _validate_field_signature(value: Any, key: str, present: list[Any], reply_count: int) -> None:
    field = _require_mapping(value, "field signature")
    if set(field) != {
        "missing_count",
        "parsed_types",
        "presence_mode",
        "present_count",
        "token_shapes",
    }:
        raise TelemetryCapabilityError("baseline field signature shape is invalid")
    _validate_presence_signature(field, len(present), reply_count)
    expected_shapes = _expected_shape_payloads(present, key)
    if field["token_shapes"] != expected_shapes:
        raise TelemetryCapabilityError("baseline token-shape signature is invalid")
    expected_types = sorted({shape["parsed_type"] for shape in expected_shapes})
    if field["parsed_types"] != expected_types:
        raise TelemetryCapabilityError("baseline parse signature is invalid")


def _validate_presence_signature(
    field: Mapping[str, Any], present_count: int, reply_count: int
) -> None:
    if (
        not _is_count(field["present_count"])
        or not _is_count(field["missing_count"])
        or field["present_count"] != present_count
        or field["missing_count"] != reply_count - present_count
    ):
        raise TelemetryCapabilityError("baseline missing-field signature is invalid")
    try:
        expected_presence_mode = field_presence_mode(present_count, reply_count)
    except CapabilitySignatureError as exc:
        raise TelemetryCapabilityError("baseline presence signature is invalid") from exc
    if field["presence_mode"] != expected_presence_mode:
        raise TelemetryCapabilityError("baseline presence signature is invalid")


def _expected_shape_payloads(present: list[Any], key: str) -> list[dict[str, Any]]:
    try:
        shape_values = [token_shape(reply["tokens"][key]) for reply in present]
    except CapabilitySignatureError as exc:
        raise TelemetryCapabilityError("baseline token-shape signature is invalid") from exc
    unique_shapes = {token_shape_sort_key(shape): shape for shape in shape_values}
    expected_shapes = [
        _serialize_token_shape(shape)
        for shape in sorted(unique_shapes.values(), key=token_shape_sort_key)
    ]
    return expected_shapes


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_baseline(value: Mapping[str, Any]) -> None:
    """Validate the exact versioned schema without touching the filesystem."""
    _validate_shape(value)


def _validate_parent(path: Path) -> None:
    try:
        parent_info = path.parent.lstat()
    except OSError as exc:
        raise TelemetryCapabilityError(f"destination parent is unavailable: {path.parent}") from exc
    if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
        raise TelemetryCapabilityError("destination parent is not a directory")
    if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) != 0o700:
        raise TelemetryCapabilityError("destination parent ownership or mode is unsafe")


def _validate_destination(path: Path) -> None:
    if path.name != ARTIFACT_FILENAME:
        raise TelemetryCapabilityError(f"destination must be named {ARTIFACT_FILENAME}")
    _validate_parent(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise TelemetryCapabilityError("destination cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode):
        raise TelemetryCapabilityError("destination is a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise TelemetryCapabilityError("destination is not a regular file")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != OWNER_ONLY_MODE:
        raise TelemetryCapabilityError("destination ownership or mode is unsafe")
    raise FileExistsError(f"destination exists and no-clobber is enforced: {path}")


def _acquire_lock(path: Path) -> int:
    return _acquire_flock(path, fcntl.LOCK_EX)


def _acquire_shared_lock(path: Path) -> int:
    return _acquire_flock(path, fcntl.LOCK_SH)


def _acquire_flock(path: Path, lock_type: int) -> int:
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, OWNER_ONLY_MODE)
    except OSError as exc:
        raise BaselineRefusal("baseline lock cannot be opened") from exc

    try:
        os.fchmod(fd, OWNER_ONLY_MODE)
        info = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise BaselineRefusal("baseline lock cannot be secured") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != OWNER_ONLY_MODE
    ):
        os.close(fd)
        raise BaselineRefusal("baseline lock ownership or mode is unsafe")

    try:
        fcntl.flock(fd, lock_type | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            raise BaselineRefusal("another baseline run is active") from exc
        raise BaselineRefusal("baseline lock cannot be acquired") from exc
    return fd


def _publish_no_clobber(path: Path, data: bytes) -> None:
    if len(data) > MAX_BASELINE_BYTES:
        raise BaselineRefusal("baseline exceeds publication size limit")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{id(data)}")
    _create_temporary(temporary, data)
    try:
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise FileExistsError(f"destination exists and no-clobber is enforced: {path}") from exc
        _best_effort_unlink(temporary)
        try:
            _fsync_directory(path.parent)
        except OSError as exc:
            raise BaselinePublicationDurabilityError(
                "baseline artifact was published but directory durability is unconfirmed"
            ) from exc
    except FileExistsError:
        raise
    except OSError as exc:
        raise TelemetryCapabilityError(f"cannot publish baseline: {exc}") from exc
    finally:
        _best_effort_unlink(temporary)


def _create_temporary(path: Path, data: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd: int | None = None
    temporary_created = False
    completed = False
    try:
        try:
            fd = os.open(path, flags, OWNER_ONLY_MODE)
        except FileExistsError as exc:
            raise TelemetryCapabilityError(
                f"cannot publish baseline: temporary path already exists: {path}"
            ) from exc
        temporary_created = True
        os.fchmod(fd, OWNER_ONLY_MODE)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short baseline write")
            view = view[written:]
        os.fsync(fd)
        completed = True
    except OSError as exc:
        raise TelemetryCapabilityError(f"cannot prepare baseline publication: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if temporary_created and not completed:
            _best_effort_unlink(path)


def _best_effort_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def record_baseline(
    client: StrictNUTTelemetryPort,
    destination: Path,
    *,
    endpoint: NUTEndpoint | None = None,
    timing: CollectionTiming | None = None,
) -> dict[str, Any]:
    """Collect and atomically publish one owner-only no-clobber baseline."""
    destination = destination.absolute()
    _validate_destination(destination)
    lock_fd = _acquire_lock(destination)
    try:
        _validate_destination(destination)
        configured = endpoint or NUTEndpoint()
        artifact = build_baseline(
            client,
            host=configured.host,
            port=configured.port,
            ups_name=configured.ups_name,
            timing=timing,
        )
        validate_baseline(artifact)
        serialized = canonical_json_bytes(artifact) + b"\n"
        if len(serialized) > MAX_BASELINE_BYTES:
            raise BaselineRefusal("baseline exceeds publication size limit")
        _publish_no_clobber(destination, serialized)
        return artifact
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def load_baseline(path: Path) -> dict[str, Any]:
    """Load and validate one canonical owner-only baseline artifact."""
    path = path.absolute()
    if path.name != ARTIFACT_FILENAME:
        raise TelemetryCapabilityError("baseline filename is invalid")
    _validate_parent(path)
    raw, info = _read_artifact(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TelemetryCapabilityError("baseline is not a regular file")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != OWNER_ONLY_MODE:
        raise TelemetryCapabilityError("baseline ownership or mode is unsafe")
    if not raw.endswith(b"\n"):
        raise TelemetryCapabilityError("baseline is missing its final newline")
    try:
        value = json.loads(raw[:-1])
    except json.JSONDecodeError as exc:
        raise TelemetryCapabilityError("baseline is not valid JSON") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) + b"\n" != raw:
        raise TelemetryCapabilityError("baseline JSON is not canonical")
    validate_baseline(value)
    return value


def _read_artifact(path: Path) -> tuple[bytes, os.stat_result]:
    """Read one regular-size candidate without following its final symlink."""
    fd: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if info.st_size < 2 or info.st_size > MAX_BASELINE_BYTES:
            raise TelemetryCapabilityError("baseline size is outside the trusted bound")
        raw_chunks: list[bytes] = []
        bytes_read = 0
        while True:
            # Request one byte beyond the remaining trusted capacity.  That
            # catches a file that grows after the initial fstat without ever
            # appending bytes beyond MAX_BASELINE_BYTES to raw_chunks.
            remaining = MAX_BASELINE_BYTES - bytes_read
            read_size = min(1024 * 1024, remaining + 1)
            chunk = os.read(fd, read_size)
            if not chunk:
                break
            if len(chunk) > remaining:
                raise TelemetryCapabilityError("baseline size is outside the trusted bound")
            raw_chunks.append(chunk)
            bytes_read += len(chunk)
        raw = b"".join(raw_chunks)
    except OSError as exc:
        raise TelemetryCapabilityError("baseline cannot be read") from exc
    finally:
        if fd is not None:
            os.close(fd)
    return raw, info


def verify_baseline(
    path: Path,
    client: StrictNUTTelemetryPort,
    *,
    host: str = "localhost",
    port: int = 3493,
    ups_name: str = "cyberpower",
) -> dict[str, Any]:
    """Require a trusted baseline and one current ordinary LIST VAR identity match."""
    path = path.absolute()
    if path.name != ARTIFACT_FILENAME:
        raise TelemetryCapabilityError("baseline filename is invalid")
    _validate_parent(path)
    lock_fd = _acquire_shared_lock(path)
    try:
        baseline = load_baseline(path)
        if baseline["endpoint"] != {"host": host, "port": port, "ups_name": ups_name}:
            raise TelemetryCapabilityError(
                "baseline endpoint does not match configured NUT endpoint"
            )
        _, tokens = _read_reply(client)
        identity, source_keys = _reply_identity(tokens)
        if identity != baseline["identity"] or source_keys != baseline["identity_source_keys"]:
            raise TelemetryCapabilityError("current physical NUT identity does not match baseline")
        return baseline
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
