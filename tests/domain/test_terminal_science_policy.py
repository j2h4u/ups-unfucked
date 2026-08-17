"""Closed terminal authorization for scientific blackout evidence."""

import pytest

from src.domain.evidence import terminal_science_policy
from src.domain.values import TerminationFact


@pytest.mark.parametrize(
    ("raw", "termination", "authorizes"),
    (
        ("power_restored", TerminationFact.POWER_RESTORED, True),
        ("service_stop", TerminationFact.SERVICE_STOP, False),
        ("closed_restart_gap", TerminationFact.CLOSED_RESTART_GAP, False),
        ("capture_damaged", TerminationFact.CAPTURE_DAMAGED, False),
        ("future_terminal", None, False),
        (None, None, False),
    ),
)
def test_terminal_policy_only_authorizes_power_restored(
    raw: object,
    termination: TerminationFact | None,
    authorizes: bool,
) -> None:
    policy = terminal_science_policy(raw)

    assert policy.termination is termination
    assert policy.authorizes_science is authorizes
