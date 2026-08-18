"""Actual-byte proof for the v3 derived and terminal tail reservation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from src.adapters.jsonl_v3_canonical import EncodedV3Record, V3CodecError
from src.adapters.jsonl_v3_fragment_profile_codec import (
    PROFILE_RECORD_TYPE,
    profile_descriptor_count,
)
from src.domain.fragment_policy import DerivedTailBudget


@dataclass(frozen=True, slots=True)
class TailBudgetProof:
    """Measured bytes and cardinalities for one legal tail construction."""

    derived_record_count: int
    derived_bytes: int
    descriptor_count: int
    terminal_bytes: int
    total_bytes: int
    derived_limit_bytes: int
    total_limit_bytes: int
    margin_bytes: int


def prove_tail_budget(
    derived_records: Sequence[EncodedV3Record],
    terminal_records: Sequence[EncodedV3Record],
    *,
    budget: DerivedTailBudget = DerivedTailBudget(),
) -> TailBudgetProof:
    """Prove limits from actual encoded lines, never from estimated payload sizes."""
    derived = tuple(derived_records)
    terminal = tuple(terminal_records)
    if any(not isinstance(item, EncodedV3Record) for item in (*derived, *terminal)):
        raise TypeError("tail budget requires encoded v3 records")
    if len(derived) > budget.max_derived_records:
        raise V3CodecError("derived record budget exceeded")
    profile_records = tuple(
        item for item in derived if item.envelope.record_type == PROFILE_RECORD_TYPE
    )
    descriptor_count = profile_descriptor_count(profile_records) if profile_records else 0
    if descriptor_count > budget.max_compact_descriptors:
        raise V3CodecError("compact descriptor budget exceeded")
    derived_bytes = sum(len(item.line) for item in derived)
    terminal_bytes = sum(len(item.line) for item in terminal)
    if any(len(item.line) > budget.max_derived_record_bytes for item in derived):
        raise V3CodecError("derived record exceeds 8 KiB")
    if any(len(item.line) > budget.max_derived_record_bytes for item in terminal):
        raise V3CodecError("terminal record exceeds 8 KiB")
    if derived_bytes > budget.max_record_total_bytes:
        raise V3CodecError("derived byte budget exceeded")
    total = derived_bytes + terminal_bytes
    if total > budget.max_total_bytes:
        raise V3CodecError("full tail budget exceeded")
    return TailBudgetProof(
        derived_record_count=len(derived),
        derived_bytes=derived_bytes,
        descriptor_count=descriptor_count,
        terminal_bytes=terminal_bytes,
        total_bytes=total,
        derived_limit_bytes=budget.max_record_total_bytes,
        total_limit_bytes=budget.max_total_bytes,
        margin_bytes=budget.max_total_bytes - total,
    )
