"""Durable terminal-outcome line construction for the JSONL store."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from src.adapters.jsonl_errors import EventConflictError
from src.adapters.jsonl_record_codec import (
    _decode_record_line,
    _EnvelopeParts,
    _json_mapping,
    _StoredRecord,
    canonical_record_line,
)
from src.application.storage_values import EventHandle, ProcessingRef, TerminalOutcomeRecord


@dataclass(frozen=True, slots=True)
class JsonlOutcomeResolver:
    """Resolve one caller outcome against the trusted durable tail."""

    filesystem: Any
    stream: Any

    def resolve(
        self,
        *,
        handle: EventHandle,
        outcome: TerminalOutcomeRecord,
        processing: ProcessingRef,
        records: tuple[_StoredRecord, ...],
        last: _StoredRecord,
    ) -> _StoredRecord:
        path = self.filesystem._event_path(handle.path_token)
        damaged = processing.frozen_stage == "capture_damaged"
        payload = _json_mapping(outcome.payload, "outcome payload")
        if outcome.event_kind != handle.event_kind:
            raise EventConflictError("terminal outcome event kind does not match event handle")
        if damaged:
            payload = self.stream._capture_damaged_payload(handle.blackout_id)
        boot_id = last.boot_id if damaged else outcome.boot_id
        wall_time = last.wall_time_utc if damaged else outcome.wall_time_utc
        monotonic_ns = last.monotonic_ns if damaged else outcome.monotonic_ns
        sequence = handle.next_seq
        segment_id = handle.segment_id
        if last.record_type == "outcome":
            sequence = last.seq
            segment_id = last.segment_id
        envelope = self.stream._record_envelope(
            _EnvelopeParts(
                "outcome",
                "derived",
                handle.blackout_id,
                segment_id,
                sequence,
                boot_id,
                wall_time,
                monotonic_ns,
                payload,
                handle.event_kind,
            )
        )
        line = canonical_record_line(envelope)
        expected = _decode_record_line(line)
        if last.record_type == "outcome":
            if last.canonical_line != line:
                raise EventConflictError("terminal outcome idempotency conflict")
            return last
        if last.seq != handle.next_seq - 1:
            raise EventConflictError("event handle does not match the durable tail")
        self.filesystem._trip("before_outcome_append")
        fd = self.filesystem._open_existing(path, writable=True)
        try:
            self.filesystem._append_and_sync_fd(fd, line)
        finally:
            os.close(fd)
        self.filesystem._trip("after_outcome_append")
        return expected
