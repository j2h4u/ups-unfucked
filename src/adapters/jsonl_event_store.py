"""Transactional JSONL event-store facade.

The facade owns transaction ordering and the single writer lock; cohesive
record, stream, registry, index, and filesystem lanes provide the internals.
"""

import fcntl
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

from src.adapters.jsonl_errors import (
    EventConflictError,
    EventCorruptionError,
    EventValidationError,
)
from src.adapters.jsonl_event_catalog import JsonlEventCatalog
from src.adapters.jsonl_event_stream import JsonlEventStream, _initial_event_filename
from src.adapters.jsonl_filesystem import JsonlFilesystem
from src.adapters.jsonl_health_report import storage_health
from src.adapters.jsonl_index import JsonlIndex, JsonlIndexPaths
from src.adapters.jsonl_outcome_commit import JsonlOutcomeResolver
from src.adapters.jsonl_record_codec import (
    MAX_PENDING_PROCESSING,
    MAX_REASON_BYTES,
    PROVENANCE_BY_RECORD_TYPE,
    _bounded_start_payload,
    _decode_record_line,
    _deduplicate_and_validate_chain,
    _EnvelopeParts,
    _json_mapping,
    _validate_short_ascii,
    _validate_uuid4_hex,
    _wall_time_utc,
    canonical_record_line,
)
from src.adapters.jsonl_report_outbox import ReportNotice
from src.adapters.jsonl_work_registry import JsonlWorkRegistry
from src.application.storage_values import (
    CaptureCloseReconciliation,
    CaptureCloseState,
    CapturingEventRef,
    EpochIndexScan,
    EpochIndexTail,
    EventHandle,
    EventProjection,
    EventRecord,
    EventRef,
    EventStart,
    EventSummary,
    PreparingCaptureRef,
    ProcessingRef,
    RecoveredCapture,
    ReportNoticeIdentity,
    SealedEventRef,
    StorageHealth,
    TerminalOutcomeRecord,
    WorkRegistry,
)
from src.domain.reasons import InfrastructureReason

FaultHook = Callable[[str], None]


class JsonlEventStore:
    """Sole transactional owner of per-event JSONL and writer exclusivity."""

    def __init__(
        self,
        model_data_dir: str | Path,
        *,
        writer_lock_fd: int | None = None,
        fault_hook: FaultHook | None = None,
        wall_clock: Callable[[], str] | None = None,
        monotonic_clock_ns: Callable[[], int] | None = None,
    ) -> None:
        root = Path(model_data_dir)
        events_path = root / "events"
        catalog_path = events_path / "event-catalog.jsonl"
        registry_path = events_path / "active.json"
        wall_clock_fn = wall_clock or _wall_time_utc
        monotonic_clock_fn = monotonic_clock_ns or time.monotonic_ns
        self._events_path = events_path
        self._owned_lock_fd: int | None = None
        self._filesystem = JsonlFilesystem(
            root,
            fault_hook=fault_hook,
            monotonic_clock_ns=monotonic_clock_fn,
        )
        self._catalog = JsonlEventCatalog(catalog_path, self._filesystem)
        stream_ref: JsonlEventStream | None = None
        registry_ref: JsonlWorkRegistry | None = None
        index_ref: JsonlIndex | None = None

        def stream_dependency() -> JsonlEventStream:
            assert stream_ref is not None
            return stream_ref

        def registry_dependency() -> JsonlWorkRegistry:
            assert registry_ref is not None
            return registry_ref

        def index_dependency() -> JsonlIndex:
            assert index_ref is not None
            return index_ref

        self._stream = JsonlEventStream(
            events_path,
            self._filesystem,
            registry_dependency,
            wall_clock=wall_clock_fn,
            monotonic_clock_ns=monotonic_clock_fn,
        )
        self._outcome_resolver = JsonlOutcomeResolver(self._filesystem, self._stream)

        def reserve_event_path(path_token: str) -> None:
            self._catalog.reserve(path_token)
            self._stream._reserve_segment_manifest(path_token)

        self._filesystem._attach_catalog_reserver(reserve_event_path)
        self._registry = JsonlWorkRegistry(
            registry_path,
            events_path,
            self._filesystem,
            stream_dependency,
            index_dependency,
        )
        self._index = JsonlIndex(
            JsonlIndexPaths(
                events_path=events_path,
                index_path=events_path / "index.jsonl",
                cursor_path=events_path / "index-rebuild.cursor.json",
                rebuild_path=events_path / "index.rebuild.in-progress.jsonl",
                merge_path=events_path / "index.rebuild.merged.jsonl",
                delta_path=events_path / "index-rebuild.delta.jsonl",
                catalog_cursor_path=events_path / "index-rebuild.catalog.cursor.json",
                wall_clock=wall_clock_fn,
            ),
            self._filesystem,
            stream_dependency,
            registry_dependency,
            self._catalog,
        )
        self.report_outbox = _EventReportOutbox(self._index)
        self.maintenance = _EventMaintenance(self._index)
        stream_ref = self._stream
        registry_ref = self._registry
        index_ref = self._index
        self._filesystem._ensure_layout()
        if writer_lock_fd is None:
            self._owned_lock_fd = self._filesystem._acquire_writer_lock()
        else:
            self._filesystem._validate_lock_fd(writer_lock_fd)
        if not registry_path.exists():
            self._registry._write_registry(WorkRegistry(None, ()))
        self._index._recover_append_intent()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        """Release only a writer lock acquired by this adapter."""
        if self._owned_lock_fd is None:
            return
        fd = self._owned_lock_fd
        self._owned_lock_fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def work_registry(self) -> WorkRegistry:
        """Read only the bounded registry; no event history is opened."""
        return self._registry._read_registry()

    def reconcile_damaged_close(
        self,
        blackout_id: str,
        current_handle: EventHandle | None,
    ) -> CaptureCloseReconciliation:
        """Return the durable tail for an idempotent capture-damage retry."""
        registry = self._registry._read_registry()
        path_token = current_handle.path_token if current_handle is not None else None
        capture = registry.capture
        if capture is not None and capture.blackout_id == blackout_id:
            path_token = capture.path_token
        else:
            for pending in registry.pending_processing:
                if pending.blackout_id == blackout_id:
                    path_token = pending.final_path_token
                    break
        if path_token is None:
            return CaptureCloseReconciliation(CaptureCloseState.UNKNOWN, current_handle)
        projection = self._stream.project(EventRef(blackout_id, path_token))
        if not projection.records:
            return CaptureCloseReconciliation(CaptureCloseState.UNKNOWN, current_handle)
        tail = projection.records[-1]
        handle = EventHandle(
            blackout_id,
            tail.segment_id,
            path_token,
            tail.seq + 1,
            tail.record_sha256,
        )
        state = CaptureCloseState.ACTIVE
        if projection.outcome is not None:
            state = CaptureCloseState.OUTCOME
        elif projection.end is not None:
            state = CaptureCloseState.END
        return CaptureCloseReconciliation(state, handle)

    def acknowledge_capture_recovery(self) -> None:
        """Clear the current storage error only after durable reconciliation."""
        self._filesystem._clear_error()

    def project(self, ref: EventRef | EventHandle | SealedEventRef) -> EventProjection:
        return self._stream.project(ref)

    def index_tail(self, limit: int) -> tuple[EventSummary, ...]:
        return self._index.index_tail(limit)

    def index_tail_for_epoch(self, battery_epoch_id: str, limit: int) -> EpochIndexTail:
        return self._index.index_tail_for_epoch(battery_epoch_id, limit)

    def index_scan_for_decline_epoch(self, battery_epoch_id: str) -> EpochIndexScan:
        return self._index.index_scan_for_decline_epoch(battery_epoch_id)

    def storage_health(
        self,
        *,
        queued_observations: int | None = None,
        consumed_step_budget_remaining: int | None = None,
    ) -> StorageHealth:
        return storage_health(
            self._index,
            queued_observations=queued_observations,
            consumed_step_budget_remaining=consumed_step_budget_remaining,
        )

    def open(self, start: EventStart) -> EventHandle:
        """Create one event using registry-first preparing -> capturing order."""
        registry = self._registry._read_registry()
        if registry.capture is not None:
            raise EventConflictError("a blackout capture is already active")
        _validate_uuid4_hex(start.blackout_id, "blackout_id")
        _validate_uuid4_hex(start.segment_id, "segment_id")
        path_token = _initial_event_filename(start.wall_time_utc, start.blackout_id)
        payload = _bounded_start_payload(_json_mapping(start.payload, "start payload"))
        envelope = self._stream._record_envelope(
            _EnvelopeParts(
                "start",
                "physical",
                start.blackout_id,
                start.segment_id,
                0,
                start.boot_id,
                start.wall_time_utc,
                start.monotonic_ns,
                None,
                payload,
            )
        )
        start_line = canonical_record_line(envelope)
        start_record = _decode_record_line(start_line)
        preparing = PreparingCaptureRef(
            blackout_id=start.blackout_id,
            segment_id=start.segment_id,
            path_token=path_token,
            canonical_start_record_utf8=start_line.decode("utf-8"),
        )
        self._filesystem._trip("before_registry_prepare")
        self._registry._write_registry(WorkRegistry(preparing, registry.pending_processing))
        self._filesystem._trip("after_registry_prepare")
        path = self._filesystem._event_path(path_token)
        fd = self._filesystem._create_regular_file(path, mode=0o600)
        try:
            self._filesystem._trip("after_event_create")
            self._filesystem._append_and_sync_fd(fd, start_line)
            self._filesystem._trip("after_start_append")
        finally:
            os.close(fd)
        self._filesystem.sync_storage_directory(self._events_path)
        capturing = CapturingEventRef(start.blackout_id, start.segment_id, path_token)
        self._registry._write_registry(WorkRegistry(capturing, registry.pending_processing))
        self._filesystem._trip("after_registry_capturing")
        return EventHandle(
            start.blackout_id,
            start.segment_id,
            path_token,
            1,
            start_record.record_sha256,
        )

    def recover_startup(self) -> RecoveredCapture | None:
        """Recover only preparing/capturing state and its bounded tail."""
        registry = self._registry._read_registry()
        capture = registry.capture
        if capture is None:
            return None
        if isinstance(capture, PreparingCaptureRef):
            capture = self._registry._recover_preparing(capture, registry.pending_processing)
            registry = WorkRegistry(capture, registry.pending_processing)
        path = self._filesystem._event_path(capture.path_token)
        try:
            self._stream._repair_torn_tail(path)
            tail = self._stream._last_record(path)
        except EventCorruptionError:
            return self._stream._recover_corrupt_capture(capture, path)
        if tail is None:
            raise EventCorruptionError("capturing event has no durable start")
        if tail.blackout_id != capture.blackout_id or tail.segment_id != capture.segment_id:
            raise EventConflictError("capture registry and event tail identify different events")
        handle = EventHandle(
            capture.blackout_id,
            capture.segment_id,
            capture.path_token,
            tail.seq + 1,
            tail.record_sha256,
        )
        if tail.record_type == "end":
            if len(registry.pending_processing) >= MAX_PENDING_PROCESSING:
                self._registry._reject_processing_backlog_full(
                    handle,
                    EventRecord(
                        "end",
                        tail.boot_id,
                        tail.wall_time_utc,
                        tail.monotonic_ns,
                        tail.payload,
                        "physical",
                    ),
                )
            try:
                self._registry._move_capture_to_processing(handle, tail.record_sha256)
            except EventCorruptionError:
                continuation = self._stream._continue_capture_after_corruption(capture, path)
                self._registry._move_capture_to_processing(
                    continuation,
                    continuation.last_record_sha256,
                )
            return None
        if tail.record_type == "outcome":
            self._registry._finish_sealed_projection(handle, tail)
            return None
        return self._resume_active_capture(capture, path, handle, tail.boot_id)

    def _resume_active_capture(
        self,
        capture: CapturingEventRef,
        path: Path,
        handle: EventHandle,
        last_boot_id: str,
    ) -> RecoveredCapture:
        try:
            return self._stream._recovered_capture(handle, last_boot_id)
        except EventCorruptionError:
            return self._stream._recover_corrupt_capture(capture, path)

    def append(self, handle: EventHandle, record: EventRecord) -> EventHandle:
        """Append and fdatasync one complete record before acknowledging it."""
        if record.record_type == "outcome":
            raise EventValidationError("terminal outcome must be written through seal()")
        expected_provenance = PROVENANCE_BY_RECORD_TYPE.get(record.record_type)
        if expected_provenance is None:
            raise EventValidationError(f"unknown record type: {record.record_type!r}")
        if record.provenance != expected_provenance:
            raise EventValidationError("record provenance does not match its record type")
        self._registry._require_handle_registered(handle)
        handle, path, last = self._stream._resolve_append_tail(
            handle,
            record_type=record.record_type,
        )
        if last is None:
            raise EventCorruptionError("event file is empty")
        envelope = self._stream._record_envelope(
            _EnvelopeParts(
                record.record_type,
                record.provenance,
                handle.blackout_id,
                handle.segment_id,
                handle.next_seq,
                record.boot_id,
                record.wall_time_utc,
                record.monotonic_ns,
                handle.last_record_sha256,
                _json_mapping(record.payload, "record payload"),
            )
        )
        line = canonical_record_line(envelope)
        candidate = _decode_record_line(line)
        if last.seq == handle.next_seq:
            if last.canonical_line != line:
                raise EventConflictError(
                    "same event sequence already has different canonical bytes"
                )
            return EventHandle(
                handle.blackout_id,
                handle.segment_id,
                handle.path_token,
                handle.next_seq + 1,
                last.record_sha256,
            )
        if last.seq != handle.next_seq - 1 or last.record_sha256 != handle.last_record_sha256:
            raise EventConflictError("event handle does not match the durable tail")
        fd = self._filesystem._open_existing(path, writable=True)
        try:
            self._filesystem._append_and_sync_fd(fd, line)
        finally:
            os.close(fd)
        next_handle = EventHandle(
            handle.blackout_id,
            handle.segment_id,
            handle.path_token,
            handle.next_seq + 1,
            candidate.record_sha256,
        )
        if record.record_type == "end":
            next_handle = self._registry._finish_end_transition(next_handle, record, path)
        return next_handle

    def checkpoint_processing(self, handle: EventHandle, frozen_stage: str) -> None:
        """Persist a bounded close-stage transition without rewriting per observation."""
        _validate_short_ascii(frozen_stage, "frozen_stage", MAX_REASON_BYTES)
        registry = self._registry._read_registry()
        updated: list[ProcessingRef] = []
        matched = False
        for ref in registry.pending_processing:
            if ref.blackout_id == handle.blackout_id:
                matched = True
                updated.append(
                    ProcessingRef(
                        ref.blackout_id,
                        ref.segment_ids,
                        ref.final_path_token,
                        frozen_stage,
                        handle.last_record_sha256,
                    )
                )
            else:
                updated.append(ref)
        if not matched:
            raise EventConflictError("event is not pending processing")
        self._registry._write_registry(WorkRegistry(registry.capture, tuple(updated)))

    def seal(self, handle: EventHandle, outcome: TerminalOutcomeRecord) -> SealedEventRef:
        """Durably write outcome, seal, project summary, then remove processing ref."""
        processing = self._registry._processing_ref(handle.blackout_id)
        if processing.final_path_token != handle.path_token:
            handle = self._registry._handle_from_processing_ref(processing)
        path = self._filesystem._event_path(handle.path_token)
        records = ()
        try:
            self._stream._repair_torn_tail(path)
            records = _deduplicate_and_validate_chain(self._stream._read_all_records(path))
            last = records[-1] if records else None
        except EventCorruptionError:
            handle = self._stream._continue_processing_after_corruption(processing, path)
            processing = self._registry._processing_ref(handle.blackout_id)
            path = self._filesystem._event_path(handle.path_token)
            records = _deduplicate_and_validate_chain(self._stream._read_all_records(path))
            last = records[-1] if records else None
        if last is None:
            raise EventCorruptionError("event file is empty")
        if last.record_type == "outcome" and processing.frozen_stage == "capture_damaged":
            if last.payload != self._stream._capture_damaged_payload(handle.blackout_id):
                raise EventConflictError("capture-damaged outcome idempotency conflict")
            sealed_handle = EventHandle(
                handle.blackout_id,
                handle.segment_id,
                handle.path_token,
                last.seq + 1,
                last.record_sha256,
            )
            return self._registry._finish_sealed_projection(sealed_handle, last)
        durable_outcome = self._outcome_resolver.resolve(
            handle=handle,
            outcome=outcome,
            processing=processing,
            records=records,
            last=last,
        )
        sealed_handle = EventHandle(
            handle.blackout_id,
            handle.segment_id,
            handle.path_token,
            durable_outcome.seq + 1,
            durable_outcome.record_sha256,
        )
        return self._registry._finish_sealed_projection(sealed_handle, durable_outcome)

    def reject_processing(
        self,
        processing: ProcessingRef,
        reason: InfrastructureReason,
    ) -> SealedEventRef:
        """Seal deterministic assessment corruption without manufacturing science."""
        if reason is not InfrastructureReason.CAPTURE_DAMAGED:
            raise EventValidationError("unsupported processing rejection reason")
        current = self._registry._processing_ref(processing.blackout_id)
        handle = self._registry._handle_from_processing_ref(current)
        self.checkpoint_processing(handle, InfrastructureReason.CAPTURE_DAMAGED.value)
        return self.seal(
            handle,
            TerminalOutcomeRecord("adapter", "1970-01-01T00:00:00Z", 0, {}),
        )


class _EventReportOutbox:
    """Narrow report-delivery adapter separate from the event-store facade."""

    def __init__(self, index: JsonlIndex) -> None:
        self._index = index

    def report_outbox_pending(self, limit: int) -> tuple[ReportNoticeIdentity, ...]:
        return tuple(
            ReportNoticeIdentity(item.blackout_id, item.segment_filename, item.summary_sha256)
            for item in self._index.report_outbox_pending(limit)
        )

    def acknowledge_report_notice(self, notice: ReportNoticeIdentity) -> None:
        item = self._index.report_outbox_head()
        if item is not None and _notice_matches(item, notice):
            self._index.acknowledge_report_notice(item)
            return
        raise EventConflictError("report outbox notice is not pending")


def _notice_matches(item: ReportNotice, notice: ReportNoticeIdentity) -> bool:
    return item.identity == (
        notice.blackout_id,
        notice.segment_filename,
        notice.summary_sha256,
    )


class _EventMaintenance:
    """Narrow bounded index-maintenance adapter."""

    def __init__(self, index: JsonlIndex) -> None:
        self._index = index

    def storage_health(
        self,
        *,
        queued_observations: int | None = None,
        consumed_step_budget_remaining: int | None = None,
    ) -> StorageHealth:
        return storage_health(
            self._index,
            queued_observations=queued_observations,
            consumed_step_budget_remaining=consumed_step_budget_remaining,
        )

    def begin_index_rebuild(self) -> str:
        return self._index._begin_index_rebuild()

    def rebuild_index_tick(
        self,
        *,
        max_files: int,
        max_bytes: int,
        max_wall_s: float = 0.20,
    ) -> bool:
        return self._index._rebuild_index_tick(
            max_files=max_files,
            max_bytes=max_bytes,
            max_wall_s=max_wall_s,
        )

    def promote_index_rebuild(self) -> None:
        self._index._promote_index_rebuild()
