"""Boundary invariants for v3 blackout terminal facts."""

from datetime import datetime, timezone
from typing import Any, cast

import pytest

from src.domain.blackout_terminal import (
    BlackoutEnd,
    BlackoutTermination,
    BudgetKind,
    ContinuationKind,
)
from src.domain.fragments import AnchorKind, AnchorProvenance, EndpointAnchor, ObservationOrigin

H = "a" * 64


def anchor(kind: AnchorKind = AnchorKind.POWER_RESTORED) -> EndpointAnchor:
    return EndpointAnchor(
        canonical_hash=H,
        kind=kind,
        provenance=(
            AnchorProvenance.MODELED
            if kind is AnchorKind.MODELED_SAFE_SHUTDOWN
            else AnchorProvenance.PHYSICAL
        ),
        boot_id="boot-1",
        wall_time_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        monotonic_ns=10,
        blackout_id="blackout-1",
        physical_episode_id="episode-1",
        segment_id="segment-1",
    )


def end(**overrides: object) -> BlackoutEnd:
    fields: dict[str, object] = {
        "blackout_id": "blackout-1",
        "physical_episode_id": "episode-1",
        "battery_epoch_id": "epoch-1",
        "segment_id": "segment-1",
        "termination": BlackoutTermination.POWER_RESTORED,
        "observation_origin": ObservationOrigin.NATURAL,
        "wall_time_utc": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "monotonic_ns": 10,
        "boot_id": "boot-1",
        "terminal_anchor_record_hash": H,
    }
    fields.update(overrides)
    return BlackoutEnd(**cast(Any, fields))


def test_power_restored_terminal_links_scope_and_time() -> None:
    value = end()
    assert value.termination is BlackoutTermination.POWER_RESTORED


def test_safe_shutdown_requires_modeled_anchor() -> None:
    value = end(
        termination=BlackoutTermination.SAFE_SHUTDOWN_RESTARTED,
        terminal_anchor_record_hash=H,
    )
    assert value.terminal_anchor_record_hash == H


@pytest.mark.parametrize(
    "changes",
    [
        {"budget_kind": BudgetKind.BYTES},
        {"continued_by": "next-blackout"},
        {"continuation_kind": ContinuationKind.REBOOT_GAP},
        {"termination": BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED},
    ],
)
def test_terminal_linkage_is_all_or_nothing(changes: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        end(**changes)


def test_size_rollover_requires_budget_and_successor() -> None:
    value = end(
        termination=BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED,
        terminal_anchor_record_hash=None,
        budget_kind=BudgetKind.BYTES,
        continued_by="blackout-2",
        continuation_kind=ContinuationKind.SIZE_ROLLOVER,
    )
    assert value.continued_by == "blackout-2"


def test_reboot_gap_requires_matching_successor_and_anchor_hash() -> None:
    value = end(
        termination=BlackoutTermination.CLOSED_RESTART_GAP,
        continued_by="blackout-2",
        continuation_kind=ContinuationKind.REBOOT_GAP,
    )
    assert value.terminal_anchor_record_hash == H
    with pytest.raises(ValueError, match="reboot-gap"):
        end(
            termination=BlackoutTermination.CLOSED_RESTART_GAP,
            terminal_anchor_record_hash=H,
        )


@pytest.mark.parametrize(
    "termination",
    (BlackoutTermination.CAPTURE_DAMAGED, BlackoutTermination.SERVICE_STOP),
)
def test_non_budget_terminal_matrix_requires_anchor_hash(termination: BlackoutTermination) -> None:
    with pytest.raises(ValueError, match="anchor"):
        end(termination=termination, terminal_anchor_record_hash=None)


def test_terminal_anchor_hash_is_strictly_canonical() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        end(terminal_anchor_record_hash="A" * 64)


def test_ids_and_utc_are_bounded() -> None:
    with pytest.raises(ValueError):
        end(blackout_id="x" * 129)
    with pytest.raises(ValueError, match="UTC"):
        end(wall_time_utc=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        end(boot_id="bad\nboot")
