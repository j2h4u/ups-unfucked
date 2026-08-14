"""Durable, append-only journal for raw UPS discharge observations.

This module deliberately contains no UPS protocol or battery calculations.  It is a
small persistence boundary: records are validated before they are written, every
write is synchronised, and replay fails closed when anything other than a torn final
line is encountered.
"""

from __future__ import annotations

import json
import logging
import math
import os
import stat
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

logger = logging.getLogger("ups-battery-monitor.discharge-journal")

SCHEMA_VERSION = 1
RECORD_TYPES = frozenset({"start", "sample", "end", "applied"})
DEFAULT_MAX_LINE_BYTES = 64 * 1024
DEFAULT_MAX_RECORDS = 100_000
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
MAX_STRING_BYTES = 4096
MAX_SEQ = 2**31 - 1
MAX_MONOTONIC_NS = 2**63 - 1
AppliedDisposition = Literal["recorded_only", "applied", "rejected"]
APPLIED_DISPOSITIONS = frozenset({"recorded_only", "applied", "rejected"})


class JournalError(Exception):
    """Base class for journal validation and persistence failures."""


class JournalPathError(JournalError):
    """The journal path is unsafe or is not a regular file."""


class JournalCorruptionError(JournalError):
    """The journal contains a non-recoverable record or sequence error."""


class JournalPersistenceError(JournalError):
    """Opening, writing, or synchronising the journal failed."""


Payload = dict[str, Any]


@dataclass(frozen=True)
class JournalStart:
    """Payload for an event start record."""

    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JournalSample:
    """Payload for one accepted observation (or a reboot-gap marker)."""

    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JournalEnd:
    """Payload for an event end record."""

    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EventCursor:
    """Position after the last synchronised record for one event."""

    event_id: str
    seq: int
    boot_id: str
    closed: bool = False


@dataclass(frozen=True)
class JournalRecord:
    """Validated JSONL envelope."""

    schema_version: int
    record_type: str
    event_id: str
    seq: int
    boot_id: str
    wall_time_utc: str
    monotonic_ns: int
    payload: Payload

    def as_dict(self) -> dict[str, Any]:
        """Return the wire representation of this envelope."""
        return {
            "schema_version": self.schema_version,
            "record_type": self.record_type,
            "event_id": self.event_id,
            "seq": self.seq,
            "boot_id": self.boot_id,
            "wall_time_utc": self.wall_time_utc,
            "monotonic_ns": self.monotonic_ns,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class EventProjection:
    """Replay projection for one event."""

    event_id: str
    records: tuple[JournalRecord, ...]
    start: Optional[JournalRecord]
    samples: tuple[JournalRecord, ...]
    end: Optional[JournalRecord]
    applied: Optional[JournalRecord]

    @property
    def closed(self) -> bool:
        """Whether the event has a durable end marker."""
        return self.end is not None

    @property
    def reboot_gaps(self) -> tuple[JournalRecord, ...]:
        """Return explicit reboot-gap sample markers."""
        return tuple(record for record in self.samples if record.payload.get("reboot_gap") is True)


@dataclass(frozen=True)
class JournalProjection:
    """Complete validated journal projection."""

    events: Mapping[str, EventProjection]
    records: tuple[JournalRecord, ...]
    open_event_id: Optional[str]
    torn_tail_recovered: bool = False


@dataclass(frozen=True)
class JournalHealth:
    """Bounded health snapshot suitable for a future health endpoint."""

    healthy: bool
    open_event_id: Optional[str]
    last_synced_seq: Optional[int]
    last_error: Optional[str]


def _boot_id() -> str:
    """Read Linux's stable-per-boot identifier with a safe fallback."""
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if value and len(value.encode("utf-8")) <= MAX_STRING_BYTES:
        return value
    return "unknown-boot"


def _wall_time_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _bounded_error(exc: BaseException) -> str:
    text = str(exc).replace("\n", " ")
    return text[:512] or exc.__class__.__name__


class DischargeJournal:
    """A synchronised JSONL journal with strict replay semantics."""

    def __init__(
        self,
        path: str | Path,
        *,
        boot_id: Optional[str] = None,
        wall_clock: Callable[[], str] = _wall_time_utc,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        if max_line_bytes < 256 or max_records < 1 or max_bytes < max_line_bytes:
            raise ValueError("journal bounds are too small")
        self.path = Path(path)
        self._boot_id = boot_id or _boot_id()
        self._wall_clock = wall_clock
        self._monotonic_ns = monotonic_ns
        self._max_line_bytes = max_line_bytes
        self._max_records = max_records
        self._max_bytes = max_bytes
        self._fd: Optional[int] = None
        self._created = False
        self._healthy = True
        self._last_error: Optional[str] = None
        self._open_event_id: Optional[str] = None
        self._last_synced_seq: Optional[int] = None
        self._torn_tail_recovered = False
        self._open()
        try:
            projection = self.replay()
        except Exception:
            self.close()
            raise
        self._open_event_id = projection.open_event_id
        if projection.records:
            self._last_synced_seq = projection.records[-1].seq

    @property
    def health(self) -> JournalHealth:
        return JournalHealth(
            healthy=self._healthy,
            open_event_id=self._open_event_id,
            last_synced_seq=self._last_synced_seq,
            last_error=self._last_error,
        )

    def mark_degraded(self, error: BaseException | str) -> None:
        """Record bounded health degradation without interrupting safety paths."""
        self._healthy = False
        self._last_error = (
            _bounded_error(error) if isinstance(error, BaseException) else str(error)[:512]
        )

    def observed_duration(self, event_id: str) -> Optional[float]:
        """Return the durable closed duration or sum same-boot spans for gaps."""
        projection = self.replay()
        event = projection.events.get(event_id)
        if event is None:
            raise JournalError(f"unknown event {event_id}")
        has_reboot_gap = any(record.payload.get("reboot_gap") for record in event.samples)
        if event.end is not None and not has_reboot_gap:
            observed = event.end.payload.get("observed_duration_sec")
            if (
                isinstance(observed, (int, float))
                and not isinstance(observed, bool)
                and math.isfinite(float(observed))
                and observed >= 0
            ):
                return float(observed)
        by_boot: dict[str, list[float]] = {}
        for record in event.samples:
            if record.payload.get("reboot_gap"):
                continue
            timestamp = record.payload.get("timestamp")
            if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
                by_boot.setdefault(record.boot_id, []).append(float(timestamp))
        return sum(max(values) - min(values) for values in by_boot.values()) if by_boot else None

    def close(self) -> None:
        """Close the descriptor; subsequent operations fail explicitly."""
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def __enter__(self) -> "DischargeJournal":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def start_event(self, start: JournalStart) -> EventCursor:
        """Append sequence zero for a newly generated event."""
        if not self._healthy:
            raise JournalCorruptionError(self._last_error or "journal is unhealthy")
        if self._open_event_id is not None:
            raise JournalError(f"event {self._open_event_id} is already open")
        cursor = EventCursor(str(uuid.uuid4()), 0, self._boot_id)
        self._append(cursor, "start", start.payload)
        self._open_event_id = cursor.event_id
        return cursor

    def append_sample(self, cursor: EventCursor, sample: JournalSample) -> EventCursor:
        """Append and synchronise one observation."""
        self._require_open_cursor(cursor)
        next_cursor = EventCursor(cursor.event_id, cursor.seq + 1, cursor.boot_id, False)
        self._append(next_cursor, "sample", sample.payload)
        return next_cursor

    def resume_event(self, event_id: str, *, boot_id: Optional[str] = None) -> EventCursor:
        """Return a cursor for the one open event after a process restart."""
        projection = self.replay()
        event = projection.events.get(event_id)
        if event is None or event.closed or projection.open_event_id != event_id:
            raise JournalError(f"event {event_id} is not an open event")
        last = event.records[-1]
        return EventCursor(event_id, last.seq, boot_id or last.boot_id, False)

    def append_reboot_gap(
        self,
        cursor: EventCursor,
        *,
        previous_boot_id: Optional[str] = None,
        new_boot_id: Optional[str] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> EventCursor:
        """Append an explicit, non-integrable reboot gap and switch boot IDs."""
        self._require_open_cursor(cursor)
        old_boot = previous_boot_id or cursor.boot_id
        current_boot = new_boot_id or self._boot_id
        gap: Payload = {
            "reboot_gap": True,
            "from_boot_id": old_boot,
            "to_boot_id": current_boot,
        }
        if payload is not None:
            gap.update(payload)
        next_cursor = EventCursor(cursor.event_id, cursor.seq + 1, current_boot, False)
        self._append(next_cursor, "sample", gap)
        return next_cursor

    def close_event(self, cursor: EventCursor, end: JournalEnd) -> None:
        """Append a durable end marker and close the in-memory event."""
        self._require_open_cursor(cursor)
        next_cursor = EventCursor(cursor.event_id, cursor.seq + 1, cursor.boot_id, True)
        self._append(next_cursor, "end", end.payload)
        self._open_event_id = None

    def mark_applied(self, event_id: str, model_hash: str, disposition: AppliedDisposition) -> None:
        """Append an idempotent terminal disposition audit marker."""
        if not isinstance(disposition, str) or disposition not in APPLIED_DISPOSITIONS:
            raise ValueError(f"invalid terminal disposition: {disposition!r}")
        projection = self.replay()
        event = projection.events.get(event_id)
        if event is None:
            raise JournalError(f"unknown event {event_id}")
        if event.end is None:
            raise JournalError(f"event {event_id} is still open")
        if event.applied is not None:
            if (
                event.applied.payload.get("model_hash") == model_hash
                and event.applied.payload.get("disposition") == disposition
            ):
                return
            raise JournalError(
                f"event {event_id} already has a different applied hash or disposition"
            )
        cursor = EventCursor(event_id, event.records[-1].seq + 1, self._boot_id, True)
        self._append(
            cursor,
            "applied",
            {"model_hash": model_hash, "disposition": disposition},
        )

    def replay(self) -> JournalProjection:
        """Validate and project all records, ignoring only a torn final line."""
        fd = self._fd
        if fd is None:
            raise JournalPersistenceError("journal is closed")
        try:
            size = os.fstat(fd).st_size
            if size > self._max_bytes:
                self._corrupt("journal exceeds maximum total byte size")
            os.lseek(fd, 0, os.SEEK_SET)
            raw = bytearray()
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                if len(raw) + len(chunk) > self._max_bytes:
                    self._corrupt("journal exceeds maximum total byte size")
                raw.extend(chunk)
                if len(raw) > self._max_line_bytes * self._max_records:
                    raise JournalCorruptionError("journal exceeds maximum bounded size")
        except OSError as exc:
            self._fail(exc)
            raise JournalPersistenceError(f"journal read failed: {_bounded_error(exc)}") from exc

        records: list[JournalRecord] = []
        valid_bytes = 0
        torn_tail = False
        lines = bytes(raw).splitlines(keepends=True)
        for index, line in enumerate(lines):
            final = index == len(lines) - 1
            if not line.strip():
                self._corrupt("blank journal line")
            if len(line) > self._max_line_bytes:
                if final:
                    torn_tail = True
                    logger.warning("Ignoring oversized torn final journal line")
                    break
                self._corrupt("journal line exceeds maximum size")
            if not line.endswith(b"\n"):
                torn_tail = True
                logger.warning("Ignoring torn final/unterminated final journal line")
                break
            try:
                obj = json.loads(line.decode("utf-8"))
                record = self._validate_record(obj)
            except JournalCorruptionError as exc:
                self._corrupt(str(exc))
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                if final:
                    torn_tail = True
                    logger.warning("Ignoring torn final journal line: %s", _bounded_error(exc))
                    break
                self._corrupt(f"invalid journal record: {_bounded_error(exc)}")
            records.append(record)
            valid_bytes += len(line)
            if len(records) > self._max_records:
                self._corrupt("journal contains too many records")

        if torn_tail:
            # The daemon owns the exclusive writer lock.  Remove only the
            # proven invalid suffix, then replay the durable prefix from the
            # now-clean file descriptor before projecting it.
            self._truncate_torn_tail(valid_bytes)
            self._torn_tail_recovered = True
            clean = self.replay()
            return JournalProjection(
                clean.events,
                clean.records,
                clean.open_event_id,
                torn_tail_recovered=True,
            )

        events: dict[str, list[JournalRecord]] = {}
        expected: dict[str, int] = {}
        for record in records:
            sequence = expected.get(record.event_id, 0)
            if record.seq != sequence:
                self._corrupt(f"event {record.event_id} sequence {record.seq} expected {sequence}")
            expected[record.event_id] = sequence + 1
            events.setdefault(record.event_id, []).append(record)

        projections: dict[str, EventProjection] = {}
        for event_id, event_records in events.items():
            starts = [r for r in event_records if r.record_type == "start"]
            ends = [r for r in event_records if r.record_type == "end"]
            applied = [r for r in event_records if r.record_type == "applied"]
            if (
                len(starts) != 1
                or event_records[0].record_type != "start"
                or len(ends) > 1
                or len(applied) > 1
                or (applied and not ends)
            ):
                self._corrupt(f"invalid lifecycle for event {event_id}")
            if ends and event_records.index(ends[0]) != len(event_records) - (2 if applied else 1):
                self._corrupt(f"records follow end marker for event {event_id}")
            projections[event_id] = EventProjection(
                event_id=event_id,
                records=tuple(event_records),
                start=starts[0],
                samples=tuple(r for r in event_records if r.record_type == "sample"),
                end=ends[0] if ends else None,
                applied=applied[0] if applied else None,
            )

        open_events = [event_id for event_id, event in projections.items() if not event.closed]
        if len(open_events) > 1:
            self._corrupt("journal contains multiple open events")
        open_event_id = open_events[0] if open_events else None
        if self._healthy:
            self._last_error = None
        self._open_event_id = open_event_id
        self._last_synced_seq = records[-1].seq if records else None
        return JournalProjection(
            projections,
            tuple(records),
            open_event_id,
            self._torn_tail_recovered or torn_tail,
        )

    def _truncate_torn_tail(self, valid_bytes: int) -> None:
        """Durably remove an invalid final suffix after a torn write."""
        fd = self._fd
        if fd is None:
            raise JournalPersistenceError("journal is closed")
        try:
            os.ftruncate(fd, valid_bytes)
            os.fdatasync(fd)
            self._sync_parent(self.path.parent)
            os.lseek(fd, valid_bytes, os.SEEK_SET)
        except OSError as exc:
            self._fail(exc)
            raise JournalPersistenceError(
                f"journal torn-tail recovery failed: {_bounded_error(exc)}"
            ) from exc

    def _open(self) -> None:
        parent = self.path.parent
        fd: Optional[int] = None
        try:
            self._validate_parent(parent)
            flags = os.O_APPEND | os.O_RDWR | os.O_CLOEXEC
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            if self.path.exists() or self.path.is_symlink():
                target_info = self.path.lstat()
                if stat.S_ISLNK(target_info.st_mode):
                    raise JournalPathError("journal target is a symlink")
                if not stat.S_ISREG(target_info.st_mode):
                    raise JournalPathError("journal target is not a regular file")
            try:
                fd = os.open(self.path, flags | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
                self._created = True
            except FileExistsError:
                fd = os.open(self.path, flags | nofollow)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise JournalPathError("journal target is not a regular file")
            os.fchmod(fd, 0o600)
            self._fd = fd
            if self._created:
                os.fdatasync(fd)
                self._sync_parent(parent)
        except JournalError:
            if fd is not None and self._fd is None:
                os.close(fd)
            raise
        except OSError as exc:
            if fd is not None and self._fd is None:
                os.close(fd)
            elif self._fd is not None:
                os.close(self._fd)
                self._fd = None
            self._fail(exc)
            raise JournalPersistenceError(f"journal open failed: {_bounded_error(exc)}") from exc

    @staticmethod
    def _validate_parent(parent: Path) -> None:
        try:
            info = parent.lstat()
        except FileNotFoundError:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise JournalPathError("journal parent is not a directory")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise JournalPathError("journal parent permissions are broader than 0700")

    @staticmethod
    def _sync_parent(parent: Path) -> None:
        fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _require_open_cursor(self, cursor: EventCursor) -> None:
        if self._fd is None:
            raise JournalPersistenceError("journal is closed")
        if not self._healthy:
            raise JournalCorruptionError(self._last_error or "journal is unhealthy")
        if cursor.closed or cursor.event_id != self._open_event_id:
            raise JournalError(f"event cursor {cursor.event_id} is not the open event")
        if cursor.seq > MAX_SEQ - 1:
            raise JournalError("event sequence exhausted")

    def _append(self, cursor: EventCursor, record_type: str, payload: Mapping[str, Any]) -> None:
        record = JournalRecord(
            SCHEMA_VERSION,
            record_type,
            cursor.event_id,
            cursor.seq,
            cursor.boot_id,
            self._wall_clock(),
            self._monotonic_ns(),
            dict(payload),
        )
        self._validate_record(record.as_dict())
        encoded = self._encode_record(record)
        fd = self._fd
        if fd is None:
            raise JournalPersistenceError("journal is closed")
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short journal write")
                view = view[written:]
            os.fdatasync(fd)
        except (OSError, ValueError) as exc:
            self._fail(exc)
            raise JournalPersistenceError(f"journal append failed: {_bounded_error(exc)}") from exc
        self._last_synced_seq = cursor.seq

    def _encode_record(self, record: JournalRecord) -> bytes:
        try:
            encoded = (
                json.dumps(
                    record.as_dict(), ensure_ascii=False, separators=(",", ":"), allow_nan=False
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise JournalError(
                f"record payload is not JSON serializable: {_bounded_error(exc)}"
            ) from exc
        if len(encoded) > self._max_line_bytes:
            raise JournalError("record exceeds maximum line size")
        return encoded

    def _validate_record(self, obj: Any) -> JournalRecord:
        if not isinstance(obj, dict):
            raise JournalCorruptionError("record is not an object")
        required = {
            "schema_version",
            "record_type",
            "event_id",
            "seq",
            "boot_id",
            "wall_time_utc",
            "monotonic_ns",
            "payload",
        }
        if set(obj) != required:
            raise JournalCorruptionError("record fields do not match schema")
        if obj["schema_version"] != SCHEMA_VERSION:
            raise JournalCorruptionError("unknown journal schema version")
        record_type = obj["record_type"]
        event_id = obj["event_id"]
        boot_id = obj["boot_id"]
        if not isinstance(record_type, str) or record_type not in RECORD_TYPES:
            raise JournalCorruptionError("invalid record type")
        if (
            not isinstance(event_id, str)
            or not event_id
            or len(event_id.encode()) > MAX_STRING_BYTES
        ):
            raise JournalCorruptionError("invalid event ID")
        if not isinstance(boot_id, str) or not boot_id or len(boot_id.encode()) > MAX_STRING_BYTES:
            raise JournalCorruptionError("invalid boot ID")
        seq = obj["seq"]
        monotonic = obj["monotonic_ns"]
        if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= MAX_SEQ:
            raise JournalCorruptionError("invalid sequence")
        if (
            isinstance(monotonic, bool)
            or not isinstance(monotonic, int)
            or not 0 <= monotonic <= MAX_MONOTONIC_NS
        ):
            raise JournalCorruptionError("invalid monotonic timestamp")
        wall = obj["wall_time_utc"]
        if not isinstance(wall, str) or len(wall.encode()) > MAX_STRING_BYTES:
            raise JournalCorruptionError("invalid wall timestamp")
        try:
            parsed = datetime.fromisoformat(wall.replace("Z", "+00:00"))
        except ValueError as exc:
            raise JournalCorruptionError("invalid wall timestamp") from exc
        if parsed.tzinfo is None:
            raise JournalCorruptionError("wall timestamp must include UTC offset")
        payload = obj["payload"]
        if not isinstance(payload, dict):
            raise JournalCorruptionError("payload is not an object")
        try:
            payload_bytes = len(
                json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise JournalCorruptionError("payload is not valid JSON") from exc
        if payload_bytes > self._max_line_bytes:
            raise JournalCorruptionError("payload exceeds maximum size")
        return JournalRecord(
            SCHEMA_VERSION, record_type, event_id, seq, boot_id, wall, monotonic, payload
        )

    def _corrupt(self, message: str) -> None:
        self._healthy = False
        self._last_error = message[:512]
        raise JournalCorruptionError(message)

    def _fail(self, exc: BaseException) -> None:
        self._healthy = False
        self._last_error = _bounded_error(exc)
