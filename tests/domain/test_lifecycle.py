import pytest

from src.domain.lifecycle import classify_physical_observation
from src.domain.values import BlackoutKind


@pytest.mark.parametrize(
    ("status", "input_voltage_v", "expected"),
    [
        ("OB DISCHRG", 236.0, BlackoutKind.BLACKOUT_REAL),
        ("OB DISCHRG", 0.0, BlackoutKind.BLACKOUT_REAL),
        ("CAL DISCHRG", 236.0, BlackoutKind.BLACKOUT_TEST),
        ("CAL DISCHRG", 0.0, BlackoutKind.BLACKOUT_TEST),
        ("OB CAL DISCHRG", 236.0, BlackoutKind.BLACKOUT_TEST),
        ("OL", 236.0, BlackoutKind.ONLINE),
        ("", 236.0, BlackoutKind.UNKNOWN),
    ],
)
def test_classification_uses_status_flags_and_not_input_voltage(
    observation_factory, status, input_voltage_v, expected
) -> None:
    observation = observation_factory(
        0.0,
        raw_status=status,
        input_voltage_v=input_voltage_v,
    )

    assert classify_physical_observation(observation) == expected
    assert observation.input_voltage_v == input_voltage_v
