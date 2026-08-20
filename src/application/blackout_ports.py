"""Least-authority application ports for v3 blackout capture and recovery."""

from __future__ import annotations

from typing import Protocol

from src.application.blackout_storage_values import (
    MAX_EVIDENCE_PAGE_RECORDS,
    MAX_RECOVERY_PAGE_SIZE,
    BlackoutCaptureCursor,
    BlackoutCaptureOpened,
    BlackoutProcessingRef,
    BlackoutRecoveryCursor,
    BlackoutRecoveryPage,
    BlackoutRef,
    BlackoutSummaryPage,
    BlackoutTailBatch,
    RawEvidencePage,
)
from src.domain.blackout_capture import (
    BlackoutStart,
    DischargeGap,
    DischargeSample,
    FrozenModelCapture,
)
from src.domain.blackout_terminal import BlackoutEnd, BudgetKind
from src.domain.fragments import EndpointAnchor


class BlackoutCaptureStorePort(Protocol):
    """Only the capture lane may append typed physical records."""

    def open(self, start: BlackoutStart) -> BlackoutCaptureOpened: ...

    def append_sample(
        self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, sample: DischargeSample
    ) -> BlackoutCaptureCursor: ...

    def append_gap(
        self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, gap: DischargeGap
    ) -> BlackoutCaptureCursor: ...

    def append_anchor(
        self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, anchor: EndpointAnchor
    ) -> BlackoutCaptureCursor: ...

    def rollover(
        self,
        ref: BlackoutRef,
        cursor: BlackoutCaptureCursor,
        *,
        budget_kind: BudgetKind = BudgetKind.BYTES,
    ) -> BlackoutCaptureOpened: ...

    def close(
        self, ref: BlackoutRef, cursor: BlackoutCaptureCursor, end: BlackoutEnd
    ) -> BlackoutProcessingRef: ...

    def recover(
        self,
        cursor: BlackoutRecoveryCursor | None = None,
        *,
        limit: int = MAX_RECOVERY_PAGE_SIZE,
    ) -> BlackoutRecoveryPage: ...


class BlackoutEvidencePort(Protocol):
    """Read-only bounded physical evidence access for domain consumers."""

    def page(
        self,
        ref: BlackoutRef,
        cursor: BlackoutCaptureCursor | None = None,
        *,
        limit: int = MAX_EVIDENCE_PAGE_RECORDS,
    ) -> RawEvidencePage: ...


class BlackoutTailStorePort(Protocol):
    """Append one closed derived-tail command after physical capture closes."""

    def append_tail(self, ref: BlackoutRef, batch: BlackoutTailBatch) -> BlackoutProcessingRef: ...

    def mark_processed(self, processing: BlackoutProcessingRef) -> BlackoutProcessingRef: ...


class BlackoutModelCapturePort(Protocol):
    """Atomic read-only model capture for a durable blackout START."""

    def capture(self) -> FrozenModelCapture: ...


class BlackoutHistoryPort(Protocol):
    """Rebuildable history projection queries, never raw-evidence authority."""

    def page_summaries(
        self, *, cursor: str | None = None, limit: int = 100
    ) -> BlackoutSummaryPage: ...

    def project(self, ref: BlackoutRef) -> BlackoutSummaryPage: ...


__all__ = [
    "BlackoutCaptureStorePort",
    "BlackoutEvidencePort",
    "BlackoutHistoryPort",
    "BlackoutModelCapturePort",
    "BlackoutTailStorePort",
]
