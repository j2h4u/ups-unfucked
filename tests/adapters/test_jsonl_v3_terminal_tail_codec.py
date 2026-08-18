"""Strict v3 terminal-anchor and blackout-end codec tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.adapters.jsonl_v3_canonical import EncodedV3Record, V3CodecError, encode_v3_record
from src.adapters.jsonl_v3_terminal_tail_codec import (
    BLACKOUT_END_MAX_LINE_BYTES,
    BLACKOUT_END_RECORD_TYPE,
    ENDPOINT_ANCHOR_MAX_LINE_BYTES,
    ENDPOINT_ANCHOR_RECORD_TYPE,
    decode_blackout_end,
    decode_blackout_end_record,
    decode_endpoint_anchor,
    decode_endpoint_anchor_record,
    encode_blackout_end,
    encode_endpoint_anchor,
)
from src.domain.blackout_terminal import (
    BlackoutEnd,
    BlackoutTermination,
    BudgetKind,
    ContinuationKind,
)
from src.domain.fragments import AnchorKind, AnchorProvenance, EndpointAnchor, ObservationOrigin

START = datetime(2026, 8, 18, tzinfo=timezone.utc)
H1 = "a" * 64
H2 = "b" * 64


def _anchor(kind: AnchorKind = AnchorKind.POWER_RESTORED) -> EndpointAnchor:
    return EndpointAnchor(
        canonical_hash=H1,
        kind=kind,
        provenance=(
            AnchorProvenance.MODELED
            if kind is AnchorKind.MODELED_SAFE_SHUTDOWN
            else AnchorProvenance.PHYSICAL
            if kind is AnchorKind.POWER_RESTORED
            else AnchorProvenance.OPERATIONAL
        ),
        boot_id="boot-1",
        wall_time_utc=START,
        monotonic_ns=10,
        source_sample_hash=H2,
        blackout_id="blackout-1",
        physical_episode_id="episode-1",
        segment_id="segment-1",
    )


def _end(**changes: object) -> BlackoutEnd:
    fields: dict[str, object] = {
        "blackout_id": "blackout-1",
        "physical_episode_id": "episode-1",
        "battery_epoch_id": "epoch-1",
        "segment_id": "segment-1",
        "termination": BlackoutTermination.POWER_RESTORED,
        "terminal_anchor_record_hash": H1,
        "observation_origin": ObservationOrigin.NATURAL,
        "wall_time_utc": START,
        "monotonic_ns": 10,
        "boot_id": "boot-1",
    }
    fields.update(changes)
    return BlackoutEnd(**fields)  # type: ignore[arg-type]


def _linked_end(
    *,
    anchor_kind: AnchorKind = AnchorKind.POWER_RESTORED,
    **changes: object,
) -> tuple[EncodedV3Record, BlackoutEnd, EncodedV3Record]:
    anchor = _anchor(anchor_kind)
    endpoint = encode_endpoint_anchor(anchor, seq=8)
    value = _end(
        terminal_anchor_record_hash=endpoint.record_sha256,
        **changes,
    )
    encoded = encode_blackout_end(
        value,
        seq=9,
        previous_record_sha256=endpoint.record_sha256,
    )
    return endpoint, value, encoded


def _rehash(encoded, payload: dict[str, object], **changes: object) -> bytes:
    envelope = replace(encoded.envelope, payload=payload, record_sha256=None, **changes)
    return encode_v3_record(envelope).line


def test_terminal_anchor_round_trip_is_byte_identical_and_scoped() -> None:
    encoded = encode_endpoint_anchor(_anchor(), seq=4)
    decoded = decode_endpoint_anchor(encoded.line)

    assert decoded == _anchor()
    assert encoded.line == encode_endpoint_anchor(_anchor(), seq=4).line
    assert encoded.envelope.record_type == ENDPOINT_ANCHOR_RECORD_TYPE
    assert encoded.envelope.provenance == AnchorProvenance.PHYSICAL.value
    assert len(encoded.line) <= ENDPOINT_ANCHOR_MAX_LINE_BYTES


def test_strict_link_entrypoints_return_canonical_records() -> None:
    endpoint, _, encoded = _linked_end()
    endpoint_record = encode_endpoint_anchor(_anchor(), seq=8)
    assert decode_endpoint_anchor_record(endpoint_record.line) == endpoint_record
    assert (
        decode_blackout_end_record(
            encoded.line,
            terminal_anchor_record=endpoint,
        )
        == encoded
    )


def test_safe_shutdown_is_a_modeled_terminal_anchor() -> None:
    anchor = _anchor(AnchorKind.MODELED_SAFE_SHUTDOWN)
    encoded = encode_endpoint_anchor(anchor)
    assert decode_endpoint_anchor(encoded.line) == anchor
    assert encoded.envelope.provenance == AnchorProvenance.MODELED.value


def test_intermediate_anchor_is_rejected_by_terminal_tail() -> None:
    intermediate = EndpointAnchor(
        H1,
        AnchorKind.TRANSFER_TO_BATTERY,
        AnchorProvenance.PHYSICAL,
        "boot-1",
        START,
        10,
        H2,
        "blackout-1",
        "episode-1",
        "segment-1",
    )
    with pytest.raises(V3CodecError):
        encode_endpoint_anchor(intermediate)


@pytest.mark.parametrize(
    "mutation",
    ("unknown", "schema", "role", "kind", "scope", "hash"),
)
def test_anchor_decode_rejects_rehashed_semantic_mutations(mutation: str) -> None:
    encoded = encode_endpoint_anchor(_anchor())
    payload = dict(encoded.envelope.payload)
    if mutation == "unknown":
        payload["unexpected"] = True
    elif mutation == "schema":
        payload["schema"] = "endpoint-anchor-v2"
    elif mutation == "role":
        payload["anchor_role"] = "intermediate"
    elif mutation == "kind":
        payload["kind"] = AnchorKind.TRANSFER_TO_BATTERY.value
    elif mutation == "scope":
        payload["blackout_id"] = "other-blackout"
    else:
        payload["canonical_hash"] = "A" * 64
    with pytest.raises(V3CodecError):
        decode_endpoint_anchor(_rehash(encoded, payload))


def test_anchor_decode_rejects_schema_two_noncanonical_utc_and_oversize() -> None:
    encoded = encode_endpoint_anchor(_anchor())
    value = json.loads(encoded.line)
    with pytest.raises(V3CodecError):
        decode_endpoint_anchor(json.dumps({**value, "schema_version": 2}).encode() + b"\n")
    with pytest.raises(V3CodecError):
        decode_endpoint_anchor(
            json.dumps({**value, "wall_time_utc": "2026-08-18T00:00:00+00:00"}).encode() + b"\n"
        )
    oversized = (
        encoded.line[:-1] + b" " * (ENDPOINT_ANCHOR_MAX_LINE_BYTES - len(encoded.line) + 1) + b"\n"
    )
    with pytest.raises(V3CodecError):
        decode_endpoint_anchor(oversized)


def test_blackout_end_round_trip_preserves_terminal_and_origin_scope() -> None:
    endpoint, value, encoded = _linked_end()
    decoded = decode_blackout_end(encoded.line, terminal_anchor_record=endpoint)

    assert decoded == value
    assert (
        encoded.line
        == encode_blackout_end(
            value,
            seq=9,
            previous_record_sha256=endpoint.record_sha256,
        ).line
    )
    assert encoded.envelope.record_type == BLACKOUT_END_RECORD_TYPE
    assert len(encoded.line) <= BLACKOUT_END_MAX_LINE_BYTES


def test_rollover_and_reboot_end_links_are_domain_reconstructable() -> None:
    rollover = _end(
        termination=BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED,
        terminal_anchor_record_hash=None,
        budget_kind=BudgetKind.BYTES,
        continued_by="blackout-2",
        continuation_kind=ContinuationKind.SIZE_ROLLOVER,
    )
    endpoint, reboot, encoded_reboot = _linked_end(
        anchor_kind=AnchorKind.BOOT_BOUNDARY,
        termination=BlackoutTermination.CLOSED_RESTART_GAP,
        continued_by="blackout-2",
        continuation_kind=ContinuationKind.REBOOT_GAP,
    )

    assert decode_blackout_end(encode_blackout_end(rollover).line) == rollover
    assert decode_blackout_end(encoded_reboot.line, terminal_anchor_record=endpoint) == reboot


def test_uat_blackout_end_retains_intent() -> None:
    endpoint, value, encoded = _linked_end(
        observation_origin=ObservationOrigin.UAT,
        uat_intent_id="uat-1",
    )
    assert decode_blackout_end(encoded.line, terminal_anchor_record=endpoint) == value


@pytest.mark.parametrize("mutation", ("unknown", "schema", "anchor", "scope", "termination"))
def test_blackout_end_decode_rejects_rehashed_semantic_mutations(mutation: str) -> None:
    endpoint, _, encoded = _linked_end()
    payload = dict(encoded.envelope.payload)
    if mutation == "unknown":
        payload["unexpected"] = True
    elif mutation == "schema":
        payload["schema"] = "blackout-end-v2"
    elif mutation == "anchor":
        payload["terminal_anchor_record_hash"] = "A" * 64
    elif mutation == "scope":
        payload["physical_episode_id"] = "other-episode"
    else:
        payload["termination"] = BlackoutTermination.SERVICE_STOP.value
    with pytest.raises(V3CodecError):
        decode_blackout_end(
            _rehash(encoded, payload),
            terminal_anchor_record=endpoint,
        )


def test_blackout_end_rejects_envelope_scope_and_oversize() -> None:
    endpoint, _, encoded = _linked_end()
    with pytest.raises(V3CodecError):
        decode_blackout_end(
            _rehash(encoded, dict(encoded.envelope.payload), blackout_id="other"),
            terminal_anchor_record=endpoint,
        )
    oversized = (
        encoded.line[:-1] + b" " * (BLACKOUT_END_MAX_LINE_BYTES - len(encoded.line) + 1) + b"\n"
    )
    with pytest.raises(V3CodecError):
        decode_blackout_end(oversized)


def test_terminal_payload_envelope_shape_is_exact() -> None:
    encoded = encode_endpoint_anchor(_anchor())
    assert set(encoded.envelope.payload) == {
        "schema",
        "anchor_role",
        "canonical_hash",
        "kind",
        "provenance",
        "boot_id",
        "wall_time_utc",
        "monotonic_ns",
        "source_sample_hash",
        "blackout_id",
        "physical_episode_id",
        "segment_id",
    }


def test_blackout_end_links_a_separate_endpoint_record_by_hash() -> None:
    endpoint = encode_endpoint_anchor(_anchor(), seq=3)
    value = _end(terminal_anchor_record_hash=endpoint.record_sha256)
    encoded = encode_blackout_end(value, seq=4, previous_record_sha256=endpoint.record_sha256)
    assert decode_blackout_end(encoded.line, terminal_anchor_record=endpoint) == value
    other_endpoint = encode_endpoint_anchor(_anchor(), seq=99)
    with pytest.raises(V3CodecError):
        decode_blackout_end(encoded.line, terminal_anchor_record=other_endpoint)


def test_blackout_end_rejects_anchor_canonical_hash_and_anchor_only_placeholder() -> None:
    endpoint, _, encoded = _linked_end()
    anchor_hash_value = replace(
        _end(),
        terminal_anchor_record_hash=_anchor().canonical_hash,
    )
    anchor_hash_record = encode_blackout_end(
        anchor_hash_value,
        seq=9,
        previous_record_sha256=endpoint.record_sha256,
    )
    with pytest.raises(V3CodecError):
        decode_blackout_end(anchor_hash_record.line, terminal_anchor_record=endpoint)
    with pytest.raises(V3CodecError):
        decode_blackout_end(encoded.line, terminal_anchor=_anchor())


def test_blackout_end_rejects_broken_endpoint_chain() -> None:
    endpoint, value, _ = _linked_end()
    broken = encode_blackout_end(value, seq=9, previous_record_sha256=H2)
    with pytest.raises(V3CodecError):
        decode_blackout_end(broken.line, terminal_anchor_record=endpoint)
