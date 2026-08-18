import socket
from datetime import datetime, timezone
from typing import cast

import pytest

from src.adapters.nut_telemetry import observation_from_nut_reply
from src.adapters.raw_nut_telemetry import RawNutTelemetry, RawNUTTelemetryError
from src.domain.blackout_capture import CapturedTelemetry
from src.nut_client import StrictNUTEvidencePort


class StrictClient:
    def __init__(
        self,
        values: dict[str, float | str],
        tokens: dict[str, str],
        wire_lexemes: dict[str, str] | None = None,
    ) -> None:
        self.values = values
        self.tokens = tokens
        self.wire_lexemes = wire_lexemes or tokens
        self.calls = 0

    def get_ups_vars_with_evidence_strict(
        self,
    ) -> tuple[dict[str, float | str], dict[str, str], dict[str, str]]:
        self.calls += 1
        return self.values, self.tokens, self.wire_lexemes


def _reply() -> tuple[dict[str, float | str], dict[str, str], dict[str, str]]:
    values = {
        "ups.status": "OB DISCHRG",
        "battery.voltage": 12.30,
        "ups.load": 24.0,
        "input.voltage": 221.0,
        "device.note": 'quoted "value" \\ path',
    }
    tokens = {
        "device.note": 'quoted "value" \\ path',
        "input.voltage": "221.0",
        "battery.voltage": "12.30",
        "ups.status": "OB DISCHRG",
        "ups.load": "24",
    }
    wire_lexemes = {**tokens, "device.note": r"quoted \"value\" \\ path"}
    return values, tokens, wire_lexemes


def _adapter(client: StrictNUTEvidencePort) -> RawNutTelemetry:
    return RawNutTelemetry(
        client,
        boot_id="boot-a",
        wall_clock=lambda: datetime(2026, 8, 18, tzinfo=timezone.utc),
        monotonic_clock_ns=lambda: 123,
    )


def test_reads_one_reply_and_preserves_all_tokens_in_key_order() -> None:
    values, tokens, wire_lexemes = _reply()
    client = StrictClient(values, tokens, wire_lexemes)

    captured = _adapter(client).read()

    assert client.calls == 1
    assert isinstance(captured, CapturedTelemetry)
    assert [(item.key, item.token) for item in captured.raw_tokens] == [
        ("battery.voltage", "12.30"),
        ("device.note", 'quoted "value" \\ path'),
        ("input.voltage", "221.0"),
        ("ups.load", "24"),
        ("ups.status", "OB DISCHRG"),
    ]
    assert captured.observation == observation_from_nut_reply(
        values,
        tokens,
        boot_id="boot-a",
        wall_time_utc=datetime(2026, 8, 18, tzinfo=timezone.utc),
        monotonic_ns=123,
    )


@pytest.mark.parametrize("field", ("battery.voltage", "ups.load", "input.voltage"))
def test_preserves_raw_reply_when_optional_physical_field_is_absent(field: str) -> None:
    values, tokens, wire_lexemes = _reply()
    values.pop(field)
    tokens.pop(field)
    wire_lexemes.pop(field)

    captured = _adapter(StrictClient(values, tokens, wire_lexemes)).read()

    assert field not in {item.key for item in captured.raw_tokens}
    typed = {
        "battery.voltage": captured.observation.battery_voltage_v,
        "ups.load": captured.observation.load_percent,
        "input.voltage": captured.observation.input_voltage_v,
    }
    assert typed[field] is None


@pytest.mark.parametrize("field", ("battery.voltage", "ups.load", "input.voltage"))
def test_preserves_unavailable_optional_physical_token_and_typed_none(field: str) -> None:
    values, tokens, wire_lexemes = _reply()
    values[field] = "N/A"
    tokens[field] = "N/A"
    wire_lexemes[field] = "N/A"

    captured = _adapter(StrictClient(values, tokens, wire_lexemes)).read()

    assert next(item for item in captured.raw_tokens if item.key == field).token == "N/A"
    typed = {
        "battery.voltage": captured.observation.battery_voltage_v,
        "ups.load": captured.observation.load_percent,
        "input.voltage": captured.observation.input_voltage_v,
    }
    assert typed[field] is None


@pytest.mark.parametrize(
    ("values", "tokens", "reason"),
    [
        ({"ups.status": "OL"}, {}, "incomplete_reply"),
        ({}, {"ups.status": "OL"}, "incomplete_reply"),
        ({"ups.status": "OL"}, {"ups.status": 1}, "malformed_reply"),
    ],
)
def test_rejects_incomplete_or_malformed_reply(
    values: dict[str, object], tokens: dict[str, str], reason: str
) -> None:
    client = StrictClient(cast(dict[str, float | str], values), tokens)

    with pytest.raises(RawNUTTelemetryError) as raised:
        _adapter(client).read()

    assert raised.value.reason == reason
    assert client.calls == 1


def test_rejects_raw_map_above_sixteen_kibibytes() -> None:
    tokens = {f"vendor.key{index:04d}": "x" * 24 for index in range(800)}
    tokens["ups.status"] = "OL"
    values = dict(tokens)
    client = StrictClient(cast(dict[str, float | str], values), tokens)

    with pytest.raises(RawNUTTelemetryError, match="raw_map_oversize") as raised:
        _adapter(client).read()

    assert raised.value.reason == "raw_map_oversize"
    assert client.calls == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ups.status", "OL"),
        ("battery.voltage", 12.31),
        ("ups.load", 25.0),
        ("input.voltage", 222.0),
    ],
)
def test_rejects_typed_values_derived_from_different_raw_values(field: str, value: object) -> None:
    values, tokens, wire_lexemes = _reply()
    values[field] = cast(float | str, value)
    client = StrictClient(values, tokens, wire_lexemes)

    with pytest.raises(RawNUTTelemetryError, match="match|token"):
        _adapter(client).read()

    assert client.calls == 1


def test_rejects_logical_wire_mapping_mismatch() -> None:
    values, tokens, wire_lexemes = _reply()
    wire_lexemes["battery.voltage"] = "12.31"
    client = StrictClient(values, tokens, wire_lexemes)

    with pytest.raises(RawNUTTelemetryError, match="logical token"):
        _adapter(client).read()

    assert client.calls == 1


@pytest.mark.parametrize("failure", [socket.timeout("timed out"), ConnectionError("EOF")])
def test_preserves_conservative_transport_failures(failure: BaseException) -> None:
    class FailingClient:
        def get_ups_vars_with_evidence_strict(
            self,
        ) -> tuple[dict[str, float | str], dict[str, str], dict[str, str]]:
            raise failure

    with pytest.raises(type(failure), match=str(failure)):
        _adapter(FailingClient()).read()
