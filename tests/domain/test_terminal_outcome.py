"""Terminal outcome reporting invariants."""

from datetime import datetime, timezone
from typing import Any, cast

import pytest

from src.domain.blackout_terminal import BlackoutTermination
from src.domain.reasons import InfrastructureReason
from src.domain.terminal_outcome import TerminalOutcome, TerminalOutcomeKind


def outcome(**overrides: object) -> TerminalOutcome:
    fields: dict[str, object] = {
        "outcome_id": "outcome-1",
        "blackout_id": "blackout-1",
        "physical_episode_id": "episode-1",
        "battery_epoch_id": "epoch-1",
        "segment_id": "segment-1",
        "kind": TerminalOutcomeKind.ASSESSED,
        "termination": BlackoutTermination.POWER_RESTORED,
        "ended_at_utc": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "raw_record_count": 4,
        "raw_sample_count": 3,
        "blackout_end_hash": "a" * 64,
        "consumer_summary_hashes": ("c" * 64, "d" * 64, "e" * 64),
        "decision_record_hash": "f" * 64,
        "receipt_record_hash": None,
    }
    fields.update(overrides)
    return TerminalOutcome(**cast(Any, fields))


def test_assessed_requires_three_summaries_and_decision() -> None:
    value = outcome()
    assert value.kind is TerminalOutcomeKind.ASSESSED
    with pytest.raises(ValueError):
        outcome(consumer_summary_hashes=())
    with pytest.raises(ValueError):
        outcome(decision_record_hash=None)


def test_infrastructure_refusal_cannot_fabricate_results() -> None:
    value = outcome(
        kind=TerminalOutcomeKind.INFRASTRUCTURE_REFUSED,
        consumer_summary_hashes=(),
        decision_record_hash=None,
        receipt_record_hash=None,
        infrastructure_reasons=(InfrastructureReason.CAPTURE_DAMAGED,),
    )
    assert value.infrastructure_reasons == (InfrastructureReason.CAPTURE_DAMAGED,)
    with pytest.raises(ValueError):
        outcome(
            kind=TerminalOutcomeKind.INFRASTRUCTURE_REFUSED,
            consumer_summary_hashes=(),
            decision_record_hash=None,
            receipt_record_hash=None,
        )


def test_receipt_implies_changed_ir_k_only() -> None:
    value = outcome(receipt_record_hash="1" * 64)
    assert value.receipt_record_hash == "1" * 64
    with pytest.raises(ValueError):
        outcome(
            kind=TerminalOutcomeKind.INFRASTRUCTURE_REFUSED,
            receipt_record_hash="1" * 64,
            consumer_summary_hashes=(),
            decision_record_hash=None,
            infrastructure_reasons=(InfrastructureReason.CAPTURE_DAMAGED,),
        )


def test_safe_shutdown_is_reported_as_censored_fact_not_capacity() -> None:
    value = outcome(
        termination=BlackoutTermination.SAFE_SHUTDOWN_RESTARTED,
        kind=TerminalOutcomeKind.ASSESSED,
    )
    assert value.raw_sample_count == 3
