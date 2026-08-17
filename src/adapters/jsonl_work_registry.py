"""Canonical serialization of the bounded active-work registry."""

import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from src.adapters.jsonl_errors import (
    EventConflictError,
    EventCorruptionError,
    EventPathError,
    EventPersistenceError,
    EventValidationError,
    ProcessingBacklogFullError,
)
from src.adapters.jsonl_filesystem import JsonlFilesystem
from src.adapters.jsonl_record_codec import (
    MAX_LINE_BYTES,
    MAX_PENDING_PROCESSING,
    MAX_REASON_BYTES,
    MAX_REGISTRY_BYTES,
    MAX_SEGMENT_REFS,
    JsonValue,
    _bounded_error,
    _decode_record_line,
    _EnvelopeParts,
    _is_sha256,
    _require_uuid4_hex,
    _StoredRecord,
    _strict_json_loads,
    _validate_path_token,
    _validate_short_ascii,
    _validate_uuid4_hex,
    canonical_json_bytes,
    canonical_record_line,
)

if TYPE_CHECKING:
    from src.adapters.jsonl_event_stream import JsonlEventStream
    from src.adapters.jsonl_index import JsonlIndex
from src.application.storage_values import (
    CaptureRef,
    CapturingEventRef,
    EventHandle,
    EventRecord,
    EventRef,
    PreparingCaptureRef,
    ProcessingRef,
    SealedEventRef,
    WorkRegistry,
)


def _decode_registry(raw: bytes) -> WorkRegistry:
    try:
        obj = _strict_json_loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EventCorruptionError("active registry is not strict JSON") from exc
    if not isinstance(obj, dict) or set(obj) != {"capture", "pending_processing"}:
        raise EventCorruptionError("active registry fields do not match schema")
    if canonical_json_bytes(obj) + b"\n" != raw:
        raise EventCorruptionError("active registry is not canonical JSON")
    capture = _decode_capture_ref(obj["capture"])
    pending = _decode_pending_registry(obj["pending_processing"])
    return WorkRegistry(capture, pending)


def _decode_pending_registry(value: Any) -> tuple[ProcessingRef, ...]:
    if not isinstance(value, list) or len(value) > MAX_PENDING_PROCESSING:
        raise EventCorruptionError("pending processing registry is invalid or unbounded")
    pending = tuple(_decode_processing_ref(item) for item in value)
    if len({ref.blackout_id for ref in pending}) != len(pending):
        raise EventCorruptionError("pending processing contains duplicate blackout IDs")
    return pending


def _capture_ref_dict(ref: CaptureRef | None) -> JsonValue:
    if ref is None:
        return None
    if isinstance(ref, PreparingCaptureRef):
        return {
            "tag": ref.tag,
            "blackout_id": ref.blackout_id,
            "segment_id": ref.segment_id,
            "path_token": ref.path_token,
            "canonical_start_record_utf8": ref.canonical_start_record_utf8,
        }
    return {
        "tag": ref.tag,
        "blackout_id": ref.blackout_id,
        "segment_id": ref.segment_id,
        "path_token": ref.path_token,
    }


def _processing_ref_dict(ref: ProcessingRef) -> dict[str, JsonValue]:
    return {
        "tag": ref.tag,
        "blackout_id": ref.blackout_id,
        "segment_ids": list(ref.segment_ids),
        "final_path_token": ref.final_path_token,
        "frozen_stage": ref.frozen_stage,
        "last_record_hash": ref.last_record_hash,
    }


def _decode_capture_ref(value: Any) -> CaptureRef | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise EventCorruptionError("capture registry value is not an object")
    tag = value.get("tag")
    common = {"tag", "blackout_id", "segment_id", "path_token"}
    expected = common | ({"canonical_start_record_utf8"} if tag == "preparing" else set())
    if set(value) != expected:
        raise EventCorruptionError("capture registry fields do not match its tag")
    blackout_id = _require_uuid4_hex(value.get("blackout_id"), "blackout_id")
    segment_id = _require_uuid4_hex(value.get("segment_id"), "segment_id")
    path_token = value.get("path_token")
    if not isinstance(path_token, str):
        raise EventCorruptionError("capture path token is invalid")
    _validate_path_token(path_token)
    if tag == "preparing":
        frozen = value.get("canonical_start_record_utf8")
        if not isinstance(frozen, str) or len(frozen.encode("utf-8")) > MAX_LINE_BYTES:
            raise EventCorruptionError("preparing start bytes are invalid or unbounded")
        return PreparingCaptureRef(blackout_id, segment_id, path_token, frozen)
    if tag == "capturing":
        return CapturingEventRef(blackout_id, segment_id, path_token)
    raise EventCorruptionError("unknown capture registry tag")


def _decode_processing_ref(value: Any) -> ProcessingRef:
    required = {
        "tag",
        "blackout_id",
        "segment_ids",
        "final_path_token",
        "frozen_stage",
        "last_record_hash",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("tag") != "processing":
        raise EventCorruptionError("processing registry fields do not match schema")
    blackout_id = value["blackout_id"]
    _validate_uuid4_hex(blackout_id, "blackout_id")
    segment_ids_raw = value["segment_ids"]
    if (
        not isinstance(segment_ids_raw, list)
        or not segment_ids_raw
        or len(segment_ids_raw) > MAX_SEGMENT_REFS
    ):
        raise EventCorruptionError("processing segment IDs are invalid or unbounded")
    segment_ids: list[str] = []
    for segment_id in segment_ids_raw:
        _validate_uuid4_hex(segment_id, "segment_id")
        segment_ids.append(segment_id)
    path_token = value["final_path_token"]
    if not isinstance(path_token, str):
        raise EventCorruptionError("processing path token is invalid")
    _validate_path_token(path_token)
    frozen_stage = value["frozen_stage"]
    last_hash = value["last_record_hash"]
    if not isinstance(frozen_stage, str):
        raise EventCorruptionError("processing frozen stage is invalid")
    _validate_short_ascii(frozen_stage, "frozen_stage", MAX_REASON_BYTES)
    if not isinstance(last_hash, str) or not _is_sha256(last_hash):
        raise EventCorruptionError("processing last record hash is invalid")
    return ProcessingRef(
        blackout_id,
        tuple(segment_ids),
        path_token,
        frozen_stage,
        last_hash,
    )


class JsonlWorkRegistry:
    """Cohesive WorkRegistry lane used by the transactional facade."""

    def __init__(
        self,
        registry_path: Path,
        events_path: Path,
        filesystem: JsonlFilesystem,
        stream: Callable[[], "JsonlEventStream"],
        index: Callable[[], "JsonlIndex"],
    ) -> None:
        self._registry_path = registry_path
        self._events_path = events_path
        self._filesystem = filesystem
        self._stream = stream
        self._index = index

    def _recover_preparing(
        self,
        preparing: PreparingCaptureRef,
        pending: tuple[ProcessingRef, ...],
    ) -> CapturingEventRef:
        start_line = preparing.canonical_start_record_utf8.encode("utf-8")
        start = _decode_record_line(start_line)
        if (
            start.record_type != "start"
            or start.seq != 0
            or start.blackout_id != preparing.blackout_id
            or start.segment_id != preparing.segment_id
        ):
            raise EventCorruptionError("preparing ref contains a mismatched start record")
        path = self._filesystem._event_path(preparing.path_token)
        if not path.exists() and not path.is_symlink():
            fd = self._filesystem._create_regular_file(path, mode=0o600)
            try:
                self._filesystem._append_and_sync_fd(fd, start_line)
            finally:
                os.close(fd)
            self._filesystem.sync_storage_directory(self._events_path)
        else:
            self._stream()._repair_torn_tail(path)
            existing = path.read_bytes()
            if existing == b"":
                fd = self._filesystem._open_existing(path, writable=True)
                try:
                    self._filesystem._append_and_sync_fd(fd, start_line)
                finally:
                    os.close(fd)
            elif existing != start_line:
                raise EventConflictError("preparing event file is not the frozen start bytes")
            else:
                fd = self._filesystem._open_existing(path, writable=False)
                try:
                    os.fdatasync(fd)
                except OSError as exc:
                    self._filesystem._record_error(exc)
                    raise EventPersistenceError(
                        f"cannot recover frozen start durability: {_bounded_error(exc)}"
                    ) from exc
                finally:
                    os.close(fd)
        capturing = CapturingEventRef(
            preparing.blackout_id,
            preparing.segment_id,
            preparing.path_token,
        )
        self._write_registry(WorkRegistry(capturing, pending))
        return capturing

    def _processing_ref(self, blackout_id: str) -> ProcessingRef:
        for ref in self._read_registry().pending_processing:
            if ref.blackout_id == blackout_id:
                return ref
        raise EventConflictError("event is not pending processing")

    def _handle_from_processing_ref(self, ref: ProcessingRef) -> EventHandle:
        path = self._filesystem._event_path(ref.final_path_token)
        self._stream()._repair_torn_tail(path)
        tail = self._stream()._last_record(path)
        if tail is None:
            raise EventCorruptionError("processing event has no durable record")
        return EventHandle(
            ref.blackout_id,
            tail.segment_id,
            ref.final_path_token,
            tail.seq + 1,
            tail.record_sha256,
        )

    def _move_capture_to_processing(self, handle: EventHandle, last_hash: str) -> None:
        registry = self._read_registry()
        capture = registry.capture
        if not isinstance(capture, CapturingEventRef) or capture.blackout_id != handle.blackout_id:
            matching = [
                ref for ref in registry.pending_processing if ref.blackout_id == handle.blackout_id
            ]
            if matching and matching[0].last_record_hash == last_hash:
                return
            raise EventConflictError("end transition does not match active capture")
        projection = self._stream().project(EventRef(handle.blackout_id, handle.path_token))
        segment_ids = tuple(dict.fromkeys(record.segment_id for record in projection.records))
        if not segment_ids:
            segment_ids = (handle.segment_id,)
        processing = ProcessingRef(
            handle.blackout_id,
            segment_ids,
            handle.path_token,
            "end_durable",
            last_hash,
        )
        self._write_registry(WorkRegistry(None, (*registry.pending_processing, processing)))

    def _reject_processing_backlog_full(
        self,
        handle: EventHandle,
        end_record: EventRecord,
    ) -> None:
        envelope = self._stream()._record_envelope(
            _EnvelopeParts(
                "outcome",
                "derived",
                handle.blackout_id,
                handle.segment_id,
                handle.next_seq,
                end_record.boot_id,
                end_record.wall_time_utc,
                end_record.monotonic_ns,
                handle.last_record_sha256,
                {
                    "disposition": "rejected",
                    "evidence_class": "rejected",
                    "comparison_available": False,
                    "comparison_mode": "none",
                    "ir_estimate_available": False,
                    "reasons": ["processing_backlog_full"],
                },
            )
        )
        line = canonical_record_line(envelope)
        outcome = _decode_record_line(line)
        fd = self._filesystem._open_existing(
            self._filesystem._event_path(handle.path_token), writable=True
        )
        try:
            self._filesystem._append_and_sync_fd(fd, line)
        finally:
            os.close(fd)
        sealed_handle = EventHandle(
            handle.blackout_id,
            handle.segment_id,
            handle.path_token,
            handle.next_seq + 1,
            outcome.record_sha256,
        )
        self._finish_sealed_projection(sealed_handle, outcome)
        raise ProcessingBacklogFullError(
            "event durably rejected because pending processing FIFO is full"
        )

    def _require_handle_registered(self, handle: EventHandle) -> None:
        registry = self._read_registry()
        capture = registry.capture
        if (
            isinstance(capture, CapturingEventRef)
            and capture.blackout_id == handle.blackout_id
            and capture.segment_id == handle.segment_id
            and capture.path_token == handle.path_token
        ):
            return
        if any(
            ref.blackout_id == handle.blackout_id and ref.final_path_token == handle.path_token
            for ref in registry.pending_processing
        ):
            return
        raise EventConflictError("event handle is not registered")

    def _read_registry(self) -> WorkRegistry:
        return _decode_registry(self._read_registry_bytes())

    def _read_registry_bytes(self) -> bytes:
        try:
            info = self._registry_path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise EventPathError("active registry is not a regular file")
            if info.st_size > MAX_REGISTRY_BYTES:
                raise EventCorruptionError("active registry exceeds 128 KiB")
            raw = self._registry_path.read_bytes()
        except FileNotFoundError as exc:
            raise EventPersistenceError("active registry is missing") from exc
        except OSError as exc:
            raise EventPersistenceError(
                f"cannot read active registry: {_bounded_error(exc)}"
            ) from exc
        return raw

    def _write_registry(self, registry: WorkRegistry) -> None:
        obj = {
            "capture": _capture_ref_dict(registry.capture),
            "pending_processing": [
                _processing_ref_dict(ref) for ref in registry.pending_processing
            ],
        }
        encoded = canonical_json_bytes(obj) + b"\n"
        if len(encoded) > MAX_REGISTRY_BYTES:
            raise EventValidationError("active registry exceeds 128 KiB")
        self._filesystem.atomic_replace(self._registry_path, encoded, mode=0o600)

    def _remove_processing_or_capture_ref(self, *, blackout_id: str) -> None:
        registry = self._read_registry()
        pending = tuple(
            ref for ref in registry.pending_processing if ref.blackout_id != blackout_id
        )
        removed_pending = len(pending) != len(registry.pending_processing)
        capture = registry.capture
        if capture is not None and capture.blackout_id == blackout_id:
            capture = None
        elif not removed_pending:
            # The caller has just completed the durable, idempotent summary
            # append.  With no registry reference left, this is crash replay;
            # scanning a growing index would only make seal unbounded.
            return
        self._write_registry(WorkRegistry(capture, pending))

    def _finish_end_transition(
        self,
        handle: EventHandle,
        record: EventRecord,
        path: Path,
    ) -> EventHandle:
        self._filesystem._trip("after_end_append")
        if len(self._read_registry().pending_processing) >= MAX_PENDING_PROCESSING:
            self._reject_processing_backlog_full(handle, record)
        try:
            self._move_capture_to_processing(handle, handle.last_record_sha256)
        except EventCorruptionError:
            capture = self._read_registry().capture
            if not isinstance(capture, CapturingEventRef):
                raise
            handle = self._stream()._continue_capture_after_corruption(capture, path)
            self._move_capture_to_processing(handle, handle.last_record_sha256)
        self._filesystem._trip("after_end_registry_transition")
        return handle

    def _finish_sealed_projection(
        self,
        handle: EventHandle,
        outcome: _StoredRecord,
    ) -> SealedEventRef:
        self._filesystem._trip("before_event_chmod")
        sources = self._stream()._capacity.segment_sources(handle.blackout_id)
        for segment_path, _retained_corrupt in sources:
            fd = self._filesystem._open_existing(segment_path, writable=False)
            try:
                os.fchmod(fd, 0o400)
                os.fdatasync(fd)
            except OSError as exc:
                self._filesystem._record_error(exc)
                raise EventPersistenceError(f"cannot seal event: {_bounded_error(exc)}") from exc
            finally:
                os.close(fd)
        self._filesystem.sync_storage_directory(self._events_path)
        self._filesystem._trip("after_event_chmod")
        projection = self._stream().project(EventRef(handle.blackout_id, handle.path_token))
        if projection.outcome is None or projection.outcome.record_sha256 != outcome.record_sha256:
            raise EventConflictError("sealed outcome does not match projected outcome")
        summary = self._index()._summary_for(handle.path_token, projection)
        self._filesystem._trip("before_summary_append")
        self._index()._commit_summary(summary, projection)
        self._filesystem._trip("after_summary_append")
        self._remove_processing_or_capture_ref(
            blackout_id=handle.blackout_id,
        )
        self._filesystem._trip("after_registry_remove")
        segment_ids = tuple(dict.fromkeys(record.segment_id for record in projection.records))
        return SealedEventRef(
            handle.blackout_id,
            segment_ids,
            handle.path_token,
            outcome.record_sha256,
        )
