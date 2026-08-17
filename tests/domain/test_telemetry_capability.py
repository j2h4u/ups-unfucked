"""Stable capability signature and re-enable policy regressions."""

import pytest

from src.domain.telemetry_capability import (
    CAPABILITY_IDENTITY_POLICY_REVISION,
    CapabilityReply,
    CapabilitySignatureError,
    build_state_signatures,
    field_presence_mode,
    make_capability_reply,
    token_shape,
)


def _reply(
    *,
    status: str = "OL",
    voltage_token: str = "13.40",
    voltage_value: float | str | None = None,
    optional: bool = True,
) -> CapabilityReply:
    values: dict[str, float | str] = {
        "battery.voltage": float(voltage_token) if voltage_value is None else voltage_value,
        "ups.status": status,
    }
    tokens = {"battery.voltage": voltage_token, "ups.status": status}
    if optional:
        values["battery.charge"] = 100.0
        tokens["battery.charge"] = "100"
    return make_capability_reply(status, values, tokens)


def _signatures(*replies: CapabilityReply) -> tuple:
    return build_state_signatures(replies)


def _identity(signatures: tuple, field: str, status: str = "OL") -> tuple[object, ...] | None:
    state = next(item for item in signatures if item.status == status)
    value = dict(state.fields).get(field)
    if value is None:
        return None
    return (value.presence_mode, value.parsed_types, value.token_shapes)


def test_different_numeric_values_with_same_shape_match() -> None:
    previous = _signatures(*[_reply(voltage_token="13.40", voltage_value=13.4) for _ in range(30)])
    current = _signatures(*[_reply(voltage_token="12.70", voltage_value=12.7) for _ in range(60)])

    assert _identity(previous, "battery.voltage") == _identity(current, "battery.voltage")
    assert CAPABILITY_IDENTITY_POLICY_REVISION == "telemetry-capability-identity-v1"
    assert not hasattr(dict(previous[0].fields)["battery.voltage"], "tokens")


@pytest.mark.parametrize(
    ("previous_token", "current_token"),
    [("13.40", "13.4"), ("13.40", "N/A")],
)
def test_precision_or_type_change_mismatches(previous_token: str, current_token: str) -> None:
    previous_value: float | str = (
        float(previous_token) if previous_token != "N/A" else previous_token
    )
    current_value: float | str = float(current_token) if current_token != "N/A" else current_token
    previous = _signatures(_reply(voltage_token=previous_token, voltage_value=previous_value))
    current = _signatures(_reply(voltage_token=current_token, voltage_value=current_value))

    assert _identity(previous, "battery.voltage") != _identity(current, "battery.voltage")


def test_string_semantic_change_mismatches_without_storing_plain_value() -> None:
    previous = _signatures(
        make_capability_reply("OL", {"ups.alarm": "none"}, {"ups.alarm": "none"})
    )
    current = _signatures(
        make_capability_reply(
            "OL", {"ups.alarm": "replace battery"}, {"ups.alarm": "replace battery"}
        )
    )

    assert _identity(previous, "ups.alarm") != _identity(current, "ups.alarm")
    field = dict(previous[0].fields).get("ups.alarm")
    assert field is not None
    assert field.token_shapes[0].string_value_sha256 is not None
    assert "none" not in repr(field.token_shapes[0])


def test_presence_change_mismatches() -> None:
    previous = _signatures(_reply(optional=True))
    current = _signatures(_reply(optional=False))

    assert _identity(previous, "battery.charge") != _identity(current, "battery.charge")


def test_presence_policy_is_strict_and_complete() -> None:
    assert field_presence_mode(0, 60) == "absent"
    assert field_presence_mode(30, 60) == "intermittent"
    assert field_presence_mode(60, 60) == "always_present"
    with pytest.raises(CapabilitySignatureError, match="counts"):
        field_presence_mode(61, 60)


def test_intermittent_presence_mode_mismatches_even_when_counts_differ() -> None:
    previous = _signatures(_reply(optional=True), _reply(optional=False))
    current = _signatures(*[_reply(optional=True) for _ in range(60)])

    assert _identity(previous, "battery.charge") != _identity(current, "battery.charge")


def test_known_but_unobserved_state_is_unavailable_not_mismatch() -> None:
    previous = _signatures(_reply(status="OL"), _reply(status="OB"))
    current = _signatures(_reply(status="OL"))

    assert any(item.status == "OB" for item in previous)
    assert not any(item.status == "OB" for item in current)


def test_tampered_parsed_value_cannot_authorize() -> None:
    with pytest.raises(CapabilitySignatureError, match="disagrees"):
        _reply(voltage_token="13.40", voltage_value=99.0)


def test_status_token_mismatch_cannot_authorize() -> None:
    with pytest.raises(CapabilitySignatureError, match="status"):
        make_capability_reply("OL", {"ups.status": "OB"}, {"ups.status": "OB"})


def test_token_shape_records_scientific_quantisation_without_value() -> None:
    first = token_shape("1.20e-3")
    same_shape = token_shape("2.30e-3")
    changed_precision = token_shape("1.2e-3")

    assert first == same_shape
    assert first.quantization_exponent == -5
    assert first != changed_precision
