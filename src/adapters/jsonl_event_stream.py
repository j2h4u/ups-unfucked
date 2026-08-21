"""Event filename, segment, and strict tail-stream primitives."""

import os
import stat
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.adapters.jsonl_errors import (
    EventConflictError,
    EventCorruptionError,
    EventPathError,
    EventPersistenceError,
    EventStoreError,
    EventValidationError,
)
from src.adapters.jsonl_event_capacity import (
    JsonlEventCapacity,
)
from src.adapters.jsonl_event_read_codec import (
    _EventCapacityExceeded,
)
from src.adapters.jsonl_event_read_codec import (
    damaged_hashes as read_damaged_hashes,
)
from src.adapters.jsonl_event_read_codec import (
    last_record as read_last_record,
)
from src.adapters.jsonl_event_read_codec import (
    project_event as read_project_event,
)
from src.adapters.jsonl_event_read_codec import (
    read_all_records as read_all_event_records,
)
from src.adapters.jsonl_event_read_codec import (
    trusted_prefix as read_trusted_prefix,
)
from src.adapters.jsonl_filesystem import JsonlFilesystem, _file_sha256, _read_exact_fd
from src.adapters.jsonl_record_codec import (
    MAX_DAMAGED_HASHES,
    MAX_LINE_BYTES,
    MAX_SEGMENT_REFS,
    SCHEMA_VERSION,
    JsonValue,
    _bounded_error,
    _decode_record_line,
    _EnvelopeParts,
    _is_sha256,
    _parse_utc,
    _StoredRecord,
    _validate_record_without_hash,
    canonical_record_line,
)
from src.adapters.jsonl_summary_codec import _bounded_tail_lines

if TYPE_CHECKING:
    from src.adapters.jsonl_work_registry import JsonlWorkRegistry
from src.application.storage_values import (
    CapturingEventRef,
    EventHandle,
    EventProjection,
    EventRef,
    ProcessingRef,
    RecoveredCapture,
    RecoveredObservation,
    SealedEventRef,
    WorkRegistry,
)


def _initial_event_filename(wall_time_utc: str, blackout_id: str) -> str:
    moment = _parse_utc(wall_time_utc)
    timestamp = moment.strftime("%Y%m%dT%H%M%S.%f")[:-3] + "Z"
    return f"evt-{timestamp}-{blackout_id}.jsonl"


def _continuation_event_filename(
    wall_time_utc: str,
    blackout_id: str,
    segment_id: str,
    *,
    ordinal: int,
) -> str:
    moment = _parse_utc(wall_time_utc)
    timestamp = moment.strftime("%Y%m%dT%H%M%S.%f")[:-3] + "Z"
    if not 1 <= ordinal <= MAX_SEGMENT_REFS:
        raise EventValidationError("continuation segment count exceeds its bound")
    return f"evt-{timestamp}-{blackout_id}-seg-{ordinal:06d}-{segment_id}.jsonl"


def _corrupt_original_filename(filename: str) -> str | None:
    if not filename.startswith("corrupt-") or len(filename) < 74:
        return None
    digest = filename[8:72]
    if not _is_sha256(digest) or filename[72] != "-":
        return None
    original = filename[73:]
    return original if original.startswith("evt-") and original.endswith(".jsonl") else None


def _corrupt_digest(filename: str) -> str:
    original = _corrupt_original_filename(filename)
    if original is None:
        raise EventPathError("corrupt evidence filename is invalid")
    return filename[8:72]


def _replacement_processing_ref(
    ref: ProcessingRef,
    processing: ProcessingRef,
    handle: EventHandle,
) -> ProcessingRef:
    if ref.blackout_id != processing.blackout_id:
        return ref
    return ProcessingRef(
        ref.blackout_id,
        tuple(dict.fromkeys((*ref.segment_ids, handle.segment_id))),
        handle.path_token,
        "capture_damaged",
        handle.last_record_sha256,
    )


def _update_processing_refs(
    pending: Sequence[ProcessingRef],
    processing: ProcessingRef,
    handle: EventHandle,
) -> tuple[ProcessingRef, ...]:
    if not any(ref.blackout_id == processing.blackout_id for ref in pending):
        raise EventConflictError("corrupt processing event is no longer registered")
    return tuple(_replacement_processing_ref(ref, processing, handle) for ref in pending)


class JsonlEventStream:
    """Cohesive EventStream lane used by the transactional facade."""

    def __init__(
        self,
        events_path: Path,
        filesystem: JsonlFilesystem,
        registry: Callable[[], "JsonlWorkRegistry"],
        *,
        wall_clock: Callable[[], str],
        monotonic_clock_ns: Callable[[], int],
    ) -> None:
        self._events_path = events_path
        self._filesystem = filesystem
        self._registry = registry
        self._wall_clock = wall_clock
        self._monotonic_clock_ns = monotonic_clock_ns
        self._capacity = JsonlEventCapacity(events_path, filesystem, registry)

    def _reserve_segment_manifest(self, path_token: str, damaged_sha256: str | None = None) -> None:
        self._capacity.reserve_segment_manifest(path_token, damaged_sha256)

    def _damaged_hashes(self, blackout_id: str) -> tuple[str, ...]:
        return read_damaged_hashes(self._events_path, blackout_id)

    def _record_envelope(self, parts: _EnvelopeParts) -> dict[str, Any]:
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "record_type": parts.record_type,
            "provenance": parts.provenance,
            "event_kind": parts.event_kind,
            "blackout_id": parts.blackout_id,
            "segment_id": parts.segment_id,
            "seq": parts.seq,
            "boot_id": parts.boot_id,
            "wall_time_utc": parts.wall_time_utc,
            "monotonic_ns": parts.monotonic_ns,
            "prev_record_sha256": parts.prev_record_sha256,
            "payload": dict(parts.payload),
        }
        _validate_record_without_hash(envelope)
        return envelope

    def _repair_torn_tail(self, path: Path) -> bool:
        try:
            info = path.lstat()
        except OSError as exc:
            raise EventPersistenceError(
                f"cannot inspect event tail: {_bounded_error(exc)}"
            ) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise EventPathError("event tail path is not a regular file")
        if stat.S_IMODE(info.st_mode) & 0o222 == 0:
            self._capacity.validate_before_full_read(path)
            lines = _bounded_tail_lines(path, 1, MAX_LINE_BYTES)
            if not lines:
                raise EventCorruptionError("sealed event file is empty")
            _decode_record_line(lines[-1])
            return False
        fd = self._filesystem._open_existing(path, writable=True)
        try:
            size = os.fstat(fd).st_size
            if size == 0:
                return False
            self._capacity.validate_before_full_read(path)
            os.lseek(fd, 0, os.SEEK_SET)
            data = _read_exact_fd(fd, size)
            if data.endswith(b"\n"):
                _decode_record_line(data[data.rfind(b"\n", 0, -1) + 1 :])
                return False
            last_newline = data.rfind(b"\n")
            valid_bytes = last_newline + 1
            if valid_bytes:
                previous_start = data.rfind(b"\n", 0, last_newline) + 1
                _decode_record_line(data[previous_start:valid_bytes])
            os.ftruncate(fd, valid_bytes)
            self._filesystem._begin_durability_window()
            os.fdatasync(fd)
            self._filesystem.sync_storage_directory(self._events_path)
            self._filesystem._end_durability_window()
            return True
        except EventStoreError:
            raise
        except OSError as exc:
            self._filesystem._end_durability_window()
            self._filesystem._record_error(exc)
            raise EventPersistenceError(f"torn-tail repair failed: {_bounded_error(exc)}") from exc
        finally:
            os.close(fd)

    def _last_record(self, path: Path) -> _StoredRecord | None:
        return read_last_record(path)

    def _read_all_records(self, path: Path) -> tuple[_StoredRecord, ...]:
        return read_all_event_records(self._events_path, path)

    def _trusted_prefix(
        self,
        path: Path,
        blackout_id: str | None = None,
    ) -> tuple[_StoredRecord, ...]:
        return read_trusted_prefix(self._events_path, path, blackout_id)

    def _resolve_append_tail(
        self,
        handle: EventHandle,
        *,
        record_type: str = "observation",
    ) -> tuple[EventHandle, Path, _StoredRecord | None]:
        path = self._filesystem._event_path(handle.path_token)
        try:
            self._repair_torn_tail(path)
            last = self._last_record(path)
            continued = self._capacity.capacity_continuation(handle.blackout_id)
            if continued is not None:
                self._capacity.activate_capacity_continuation(continued)
                continued_path = self._filesystem._event_path(continued.path_token)
                if self._capacity.append_capacity_exceeded(
                    continued,
                    record_type=record_type,
                ):
                    raise _EventCapacityExceeded("event append exceeds its durable reserve")
                return continued, continued_path, self._last_record(continued_path)
            if self._capacity.append_capacity_exceeded(handle, record_type=record_type):
                self._continue_capture_after_capacity(handle, path, last)
                raise _EventCapacityExceeded("event append would exceed its durable bound")
            return handle, path, last
        except _EventCapacityExceeded:
            raise
        except EventCorruptionError:
            capture = self._registry()._read_registry().capture
            if not isinstance(capture, CapturingEventRef):
                raise
            continued = self._continue_capture_after_corruption(capture, path)
            continued_path = self._filesystem._event_path(continued.path_token)
            return continued, continued_path, self._last_record(continued_path)

    def _recovered_capture(
        self,
        handle: EventHandle,
        _last_boot_id: str,
    ) -> RecoveredCapture:
        projection = self.project(EventRef(handle.blackout_id, handle.path_token))
        physical = projection.observations[-1] if projection.observations else projection.start
        if physical is None:
            raise EventCorruptionError("active capture has no trusted physical observation")
        raw_observation = physical.payload.get("observation")
        payload = (
            physical.payload
            if physical.event_kind == "recharge"
            or physical.record_type == "observation"
            or not isinstance(raw_observation, Mapping)
            else raw_observation
        )
        start = projection.start
        first = start or physical
        return RecoveredCapture(
            handle,
            physical.boot_id,
            RecoveredObservation(
                physical.boot_id,
                physical.wall_time_utc,
                physical.monotonic_ns,
                payload,
            ),
            RecoveredObservation(
                first.boot_id,
                first.wall_time_utc,
                first.monotonic_ns,
                first.payload,
            ),
        )

    def _continue_capture_after_capacity(
        self,
        handle: EventHandle,
        path: Path,
        last: _StoredRecord | None,
    ) -> EventHandle:
        """Preserve a full raw segment and create one bounded terminal lane."""
        if last is None:
            raise EventCorruptionError("capacity-limited event has no durable tail")
        prepared = self._capacity.prepare_capacity_continuation(handle, path, last)
        continued = self._create_continuation_segment(
            blackout_id=handle.blackout_id,
            previous_segment_id=handle.segment_id,
            damaged_sha256=prepared.damaged_sha256,
            prefix=(last,),
            reason="event_size_limit",
        )
        return continued

    def _continue_capture_after_corruption(
        self,
        capture: CapturingEventRef,
        path: Path,
    ) -> EventHandle:
        preserved = self._preserve_corrupt_path(path)
        prefix = self._trusted_prefix(preserved, capture.blackout_id)
        registry = self._registry()._read_registry()
        current = registry.capture
        if not isinstance(current, CapturingEventRef) or current.blackout_id != capture.blackout_id:
            raise EventConflictError("corrupt capture is no longer active")
        handle = self._create_continuation_segment(
            blackout_id=capture.blackout_id,
            previous_segment_id=capture.segment_id,
            damaged_sha256=_corrupt_digest(preserved.name),
            prefix=prefix,
        )
        self._registry()._write_registry(
            WorkRegistry(
                CapturingEventRef(
                    handle.blackout_id,
                    handle.segment_id,
                    handle.path_token,
                ),
                registry.pending_processing,
            )
        )
        return handle

    def _continue_processing_after_corruption(
        self,
        processing: ProcessingRef,
        path: Path,
    ) -> EventHandle:
        preserved = self._preserve_corrupt_path(path)
        prefix = self._trusted_prefix(preserved, processing.blackout_id)
        handle = self._processing_recovery_segment(processing, preserved, prefix)
        registry = self._registry()._read_registry()
        updated = _update_processing_refs(registry.pending_processing, processing, handle)
        self._registry()._write_registry(WorkRegistry(registry.capture, updated))
        return handle

    def _processing_recovery_segment(
        self,
        processing: ProcessingRef,
        preserved: Path,
        prefix: Sequence[_StoredRecord],
    ) -> EventHandle:
        if prefix and prefix[-1].record_type == "end":
            return self._create_capture_damaged_terminal_segment(
                blackout_id=processing.blackout_id,
                prefix=prefix,
            )
        previous_segment_id = prefix[-1].segment_id if prefix else processing.segment_ids[-1]
        return self._create_continuation_segment(
            blackout_id=processing.blackout_id,
            previous_segment_id=previous_segment_id,
            damaged_sha256=_corrupt_digest(preserved.name),
            prefix=prefix,
        )

    def _create_continuation_segment(
        self,
        *,
        blackout_id: str,
        previous_segment_id: str,
        damaged_sha256: str,
        prefix: Sequence[_StoredRecord],
        reason: str = "capture_damaged",
    ) -> EventHandle:
        segment_id = uuid.uuid4().hex
        wall_time_utc = self._wall_clock()
        path_token = _continuation_event_filename(
            wall_time_utc,
            blackout_id,
            segment_id,
            ordinal=len(self._damaged_hashes(blackout_id)),
        )
        boot_id = prefix[-1].boot_id if prefix else "storage-recovery"
        envelope = self._record_envelope(
            _EnvelopeParts(
                "gap",
                "system",
                blackout_id,
                segment_id,
                0,
                boot_id,
                wall_time_utc,
                self._monotonic_clock_ns(),
                None,
                {
                    "reason": reason,
                    "damaged_segment_sha256": damaged_sha256,
                    "previous_segment_id": previous_segment_id,
                    "previous_segment_file_sha256": damaged_sha256,
                    "previous_final_record_sha256": prefix[-1].record_sha256 if prefix else None,
                },
                prefix[-1].event_kind if prefix else "blackout",
            )
        )
        line = canonical_record_line(envelope)
        gap = _decode_record_line(line)
        path = self._filesystem._event_path(path_token)
        fd = self._filesystem._create_regular_file(path, mode=0o600)
        try:
            self._filesystem._append_and_sync_fd(fd, line)
        finally:
            os.close(fd)
        self._filesystem.sync_storage_directory(self._events_path)
        return EventHandle(
            blackout_id,
            segment_id,
            path_token,
            1,
            gap.record_sha256,
            gap.event_kind,
        )

    def _create_capture_damaged_terminal_segment(
        self,
        *,
        blackout_id: str,
        prefix: Sequence[_StoredRecord],
    ) -> EventHandle:
        """Write an outcome-only continuation when the trusted history already ended."""
        if not prefix or prefix[-1].record_type != "end":
            raise EventCorruptionError("terminal continuation requires a trusted end record")
        segment_id = uuid.uuid4().hex
        wall_time_utc = self._wall_clock()
        path_token = _continuation_event_filename(
            wall_time_utc,
            blackout_id,
            segment_id,
            ordinal=len(self._damaged_hashes(blackout_id)),
        )
        envelope = self._record_envelope(
            _EnvelopeParts(
                "outcome",
                "derived",
                blackout_id,
                segment_id,
                0,
                prefix[-1].boot_id,
                wall_time_utc,
                self._monotonic_clock_ns(),
                None,
                self._capture_damaged_payload(blackout_id),
                prefix[-1].event_kind,
            )
        )
        line = canonical_record_line(envelope)
        outcome = _decode_record_line(line)
        path = self._filesystem._event_path(path_token)
        fd = self._filesystem._create_regular_file(path, mode=0o600)
        try:
            self._filesystem._append_and_sync_fd(fd, line)
        finally:
            os.close(fd)
        self._filesystem.sync_storage_directory(self._events_path)
        return EventHandle(
            blackout_id,
            segment_id,
            path_token,
            1,
            outcome.record_sha256,
            outcome.event_kind,
        )

    def _capture_damaged_payload(self, blackout_id: str) -> Mapping[str, JsonValue]:
        damaged = self._damaged_hashes(blackout_id)
        return {
            "disposition": "rejected",
            "evidence_class": "rejected",
            "comparison_available": False,
            "comparison_mode": "none",
            "ir_estimate_available": False,
            "commit_allowed": False,
            "reasons": ["capture_damaged"],
            "damaged_segment_hashes": list(damaged[:MAX_DAMAGED_HASHES]),
            "damaged_segment_overflow": max(0, len(damaged) - MAX_DAMAGED_HASHES),
        }

    def _preserve_corrupt_path(self, path: Path) -> Path:
        digest = _file_sha256(path)
        corrupt_path = self._events_path / f"corrupt-{digest}-{path.name}"
        if corrupt_path.exists():
            if _file_sha256(corrupt_path) != digest:
                raise EventConflictError("corrupt evidence destination has different bytes")
            return corrupt_path
        self._reserve_segment_manifest(path.name, digest)
        try:
            os.replace(path, corrupt_path)
            self._filesystem.sync_storage_directory(self._events_path)
        except OSError as exc:
            self._filesystem._record_error(exc)
            raise EventPersistenceError(
                f"cannot preserve corrupt event: {_bounded_error(exc)}"
            ) from exc
        return corrupt_path

    def project(self, ref: EventRef | EventHandle | SealedEventRef) -> EventProjection:
        """Project all trusted segment prefixes; never consult the summary index."""
        expected_blackout_id = ref.blackout_id
        sources = self._capacity.segment_sources(expected_blackout_id)
        for path, is_corrupt in sources:
            if not is_corrupt:
                self._repair_torn_tail(path)
        return read_project_event(
            self._events_path,
            expected_blackout_id,
            reserved_paths=self._capacity._reserved_paths(),
        )

    def _recover_corrupt_capture(
        self,
        capture: CapturingEventRef,
        path: Path,
    ) -> RecoveredCapture:
        continuation = self._continue_capture_after_corruption(capture, path)
        continuation_tail = self._last_record(self._filesystem._event_path(continuation.path_token))
        if continuation_tail is None:
            raise EventCorruptionError("capture continuation has no durable record")
        return self._recovered_capture(continuation, continuation_tail.boot_id)
