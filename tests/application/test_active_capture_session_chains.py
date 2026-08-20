from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.application.blackout_storage_values import (
    BlackoutCaptureCursor,
    BlackoutCaptureOpened,
    BlackoutChainKind,
    BlackoutProcessingRef,
    BlackoutProcessingStage,
    BlackoutRef,
    RecoveredCaptureWork,
)
from src.application.v3_active_capture_session import V3ActiveCaptureSession, _validate_advance
from src.domain.blackout_capture import (
    CapturedTelemetry,
    DischargeSample,
    DischargeSampleIdentity,
    RawNutToken,
)
from src.domain.blackout_terminal import (
    BlackoutEnd,
    BlackoutTermination,
    BudgetKind,
    ContinuationKind,
)
from src.domain.fragments import AnchorKind, AnchorProvenance, EndpointAnchor, ObservationOrigin
from src.domain.values import PhysicalObservation

H0 = "0" * 64
H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)
REF = BlackoutRef("blackout-1", "segment-1")


def cursor(
    chain: BlackoutChainKind, sequence: int | None, digest: str | None
) -> BlackoutCaptureCursor:
    return BlackoutCaptureCursor("blackout-1", "segment-1", chain, sequence, digest)


def anchor(kind: AnchorKind, source: str) -> EndpointAnchor:
    return EndpointAnchor(
        H1,
        kind,
        AnchorProvenance.MODELED
        if kind is AnchorKind.MODELED_SAFE_SHUTDOWN
        else AnchorProvenance.PHYSICAL,
        "boot-1",
        NOW,
        10,
        source,
        "blackout-1",
        "episode-1",
        "segment-1",
    )


def end(termination: BlackoutTermination, anchor_hash: str | None) -> BlackoutEnd:
    return BlackoutEnd(
        "blackout-1",
        "episode-1",
        "epoch-1",
        "segment-1",
        termination,
        ObservationOrigin.NATURAL,
        NOW,
        10,
        "boot-1",
        terminal_anchor_record_hash=anchor_hash,
        budget_kind=BudgetKind.BYTES
        if termination is BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED
        else None,
        continued_by="blackout-2"
        if termination is BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED
        else None,
        continuation_kind=(
            ContinuationKind.SIZE_ROLLOVER
            if termination is BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED
            else None
        ),
    )


def sample() -> DischargeSample:
    observation = PhysicalObservation(
        "boot-1", 10, NOW, "OB DISCHRG", "12.30", 12.3, 0.01, 20.0, 0.0
    )
    captured = CapturedTelemetry(
        observation,
        (
            RawNutToken("battery.voltage", "12.30", "12.30"),
            RawNutToken("input.voltage", "0.00", "0.00"),
            RawNutToken("ups.load", "20.00", "20.00"),
            RawNutToken("ups.status", "OB DISCHRG", "OB DISCHRG"),
        ),
    )
    return DischargeSample.from_telemetry(
        2,
        captured,
        DischargeSampleIdentity(
            "blackout-1", "episode-1", "epoch-1", "segment-1", ObservationOrigin.NATURAL
        ),
    )


class Store:
    def __init__(self) -> None:
        self.calls: list[tuple[str, BlackoutCaptureCursor]] = []
        self.fail = False

    def append_sample(self, ref, cursor, sample):
        self.calls.append(("sample", cursor))
        if self.fail:
            raise RuntimeError("injected")
        return cursor.__class__(
            ref.blackout_id, ref.segment_id, cursor.chain, cursor.next_sequence + 1, H2
        )

    def append_gap(self, ref, cursor, gap):
        self.calls.append(("gap", cursor))
        return cursor.__class__(
            ref.blackout_id, ref.segment_id, cursor.chain, cursor.next_sequence + 1, H2
        )

    def append_anchor(self, ref, cursor, value):
        self.calls.append(("anchor", cursor))
        chain = (
            BlackoutChainKind.TERMINAL
            if value.kind is AnchorKind.MODELED_SAFE_SHUTDOWN
            else cursor.chain
        )
        digest = (
            H1 if chain is BlackoutChainKind.TERMINAL and cursor.last_record_sha256 is None else H3
        )
        sequence = (
            1
            if chain is BlackoutChainKind.TERMINAL and cursor.chain is BlackoutChainKind.PHYSICAL
            else cursor.next_sequence + 1
        )
        return cursor.__class__(ref.blackout_id, ref.segment_id, chain, sequence, digest)

    def close(self, ref, cursor, value):
        self.calls.append(("close", cursor))
        return BlackoutProcessingRef(ref, BlackoutProcessingStage.PROCESSING, H3, "policy-v1")


def test_modelled_marker_sample_and_closing_anchor_keep_two_chains() -> None:
    store = Store()
    session = V3ActiveCaptureSession(store)  # type: ignore[arg-type]
    session.attach(BlackoutCaptureOpened(REF, cursor(BlackoutChainKind.PHYSICAL, 1, H0)))
    session.append_anchor(anchor(AnchorKind.MODELED_SAFE_SHUTDOWN, H0))
    session.append_sample(sample())
    session.append_anchor(anchor(AnchorKind.POWER_RESTORED, H2))
    assert [item[1].chain for item in store.calls] == [
        BlackoutChainKind.PHYSICAL,
        BlackoutChainKind.PHYSICAL,
        BlackoutChainKind.TERMINAL,
    ]
    assert session.active and session.terminal_cursor is not None
    session.close(end(BlackoutTermination.POWER_RESTORED, H3))
    assert not session.active


def test_recovery_and_anchorless_budget_close_use_expected_cursor() -> None:
    store = Store()
    session = V3ActiveCaptureSession(store)  # type: ignore[arg-type]
    session.attach_recovered(
        RecoveredCaptureWork(
            REF,
            cursor(BlackoutChainKind.PHYSICAL, 2, H2),
            cursor(BlackoutChainKind.TERMINAL, 1, H1),
        )
    )
    session.close(end(BlackoutTermination.POWER_RESTORED, H1))
    assert store.calls[-1][1].chain is BlackoutChainKind.TERMINAL

    session.attach_recovered(
        RecoveredCaptureWork(
            REF,
            cursor(BlackoutChainKind.PHYSICAL, 2, H2),
            cursor(BlackoutChainKind.TERMINAL, 1, H1),
        )
    )
    session.close(end(BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED, None))
    assert store.calls[-1][1] == cursor(BlackoutChainKind.TERMINAL, 1, H1)

    budget_session = V3ActiveCaptureSession(store)  # type: ignore[arg-type]
    budget_session.attach(BlackoutCaptureOpened(REF, cursor(BlackoutChainKind.PHYSICAL, 2, H2)))
    budget_session.close(end(BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED, None))
    assert store.calls[-1][1].chain is BlackoutChainKind.PHYSICAL


def test_modeled_marker_then_physical_sample_budget_closes_on_terminal_cursor() -> None:
    store = Store()
    session = V3ActiveCaptureSession(store)  # type: ignore[arg-type]
    session.attach(BlackoutCaptureOpened(REF, cursor(BlackoutChainKind.PHYSICAL, 1, H0)))
    session.append_anchor(anchor(AnchorKind.MODELED_SAFE_SHUTDOWN, H0))
    session.append_sample(sample())

    session.close(end(BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED, None))

    assert store.calls[-1][0] == "close"
    assert store.calls[-1][1] == cursor(BlackoutChainKind.TERMINAL, 1, H3)


def test_normal_close_requires_terminal_cursor_and_wrong_returns_leave_state() -> None:
    store = Store()
    session = V3ActiveCaptureSession(store)  # type: ignore[arg-type]
    session.attach(BlackoutCaptureOpened(REF, cursor(BlackoutChainKind.PHYSICAL, 1, H0)))
    with pytest.raises(RuntimeError):
        session.close(end(BlackoutTermination.POWER_RESTORED, H1))
    assert session.active
    store.fail = True
    sample_value = sample()
    with pytest.raises(RuntimeError):
        session.append_sample(sample_value)
    assert session.active and session.physical_cursor is not None
    assert session.physical_cursor.last_record_sha256 == H0


def test_public_exhaustion_rejects_before_store_call() -> None:
    store = Store()
    session = V3ActiveCaptureSession(store)  # type: ignore[arg-type]
    session.attach(BlackoutCaptureOpened(REF, cursor(BlackoutChainKind.PHYSICAL, None, H0)))
    with pytest.raises(ValueError, match="exhausted"):
        session.append_sample(sample())
    assert store.calls == []


def test_normal_close_rejects_exhausted_terminal_cursor_before_store_call() -> None:
    store = Store()
    session = V3ActiveCaptureSession(store)  # type: ignore[arg-type]
    session.attach_recovered(
        RecoveredCaptureWork(
            REF,
            cursor(BlackoutChainKind.PHYSICAL, 2, H2),
            cursor(BlackoutChainKind.TERMINAL, None, H3),
        )
    )
    with pytest.raises(ValueError, match="exhausted"):
        session.close(end(BlackoutTermination.POWER_RESTORED, H3))
    assert session.active
    assert store.calls == []


@pytest.mark.parametrize(
    ("returned", "message"),
    (
        (object(), "invalid processing ref"),
        (
            BlackoutProcessingRef(
                BlackoutRef("other", "segment-1"),
                BlackoutProcessingStage.PROCESSING,
                H3,
                "policy-v1",
            ),
            "different capture",
        ),
        (
            BlackoutProcessingRef(REF, BlackoutProcessingStage.TAIL, H3, "policy-v1"),
            "non-processing",
        ),
    ),
)
def test_close_validates_processing_handoff_before_clearing(returned: object, message: str) -> None:
    class ResultStore(Store):
        def close(self, ref, cursor, value):
            self.calls.append(("close", cursor))
            return returned

    store = ResultStore()
    session = V3ActiveCaptureSession(store)  # type: ignore[arg-type]
    session.attach_recovered(
        RecoveredCaptureWork(
            REF,
            cursor(BlackoutChainKind.PHYSICAL, 2, H2),
            cursor(BlackoutChainKind.TERMINAL, 1, H3),
        )
    )
    with pytest.raises(ValueError, match=message):
        session.close(end(BlackoutTermination.POWER_RESTORED, H3))
    assert session.active


@pytest.mark.parametrize(
    ("previous", "returned", "expected"),
    ((3196, 3197, 3197), (3197, None, None)),
)
def test_cursor_advancement_has_explicit_exhaustion(
    previous: int, returned: int | None, expected: int | None
) -> None:
    _validate_advance(
        cursor(BlackoutChainKind.PHYSICAL, previous, H0),
        cursor(BlackoutChainKind.PHYSICAL, returned, H1),
        BlackoutChainKind.PHYSICAL,
    )
    assert expected in {returned, 3197}
    with pytest.raises(ValueError, match="exhausted"):
        _validate_advance(
            cursor(BlackoutChainKind.PHYSICAL, None, H0),
            cursor(BlackoutChainKind.PHYSICAL, None, H1),
            BlackoutChainKind.PHYSICAL,
        )
