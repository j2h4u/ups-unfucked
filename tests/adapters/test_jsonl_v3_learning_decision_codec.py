"""Strict v3 learning-decision codec tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256

import pytest

from src.adapters.jsonl_v3_canonical import V3CodecError, encode_v3_record
from src.adapters.jsonl_v3_learning_decision_codec import (
    LEARNING_DECISION_MAX_LINE_BYTES,
    LEARNING_DECISION_RECORD_TYPE,
    decode_learning_decision,
    decode_learning_decision_record,
    encode_learning_decision,
)
from src.domain.ir_learning_decision import IrLearningDecision, IrLearningDisposition
from src.domain.learning import ObservedLoadSagIncrease, evidence_set_id
from src.domain.reasons import LearningReason
from src.domain.values import ModelChange

START = datetime(2026, 8, 18, tzinfo=timezone.utc)
H1 = "a" * 64
H2 = "b" * 64


def _hashes(count: int = 2) -> tuple[str, ...]:
    return tuple(sorted(sha256(f"evidence-{index}".encode()).hexdigest() for index in range(count)))


def _downward(hashes: tuple[str, ...] = (H1, H2)) -> IrLearningDecision:
    return IrLearningDecision(
        disposition=IrLearningDisposition.DOWNWARD_COMMIT,
        evidence_hashes=hashes,
        evidence_set_id=evidence_set_id(hashes),
        change=ModelChange("ir_k_v_per_pp", 0.020, 0.018, 0.018, hashes, True),
    )


def _upward(hashes: tuple[str, ...] = (H1, H2)) -> IrLearningDecision:
    return IrLearningDecision(
        disposition=IrLearningDisposition.UPWARD_OBSERVATION,
        evidence_hashes=hashes,
        evidence_set_id=evidence_set_id(hashes),
        observed_load_sag_increase=ObservedLoadSagIncrease(
            "ir_k_v_per_pp", 0.020, 0.024, evidence_set_id(hashes), hashes
        ),
    )


def _refusal() -> IrLearningDecision:
    return IrLearningDecision(
        disposition=IrLearningDisposition.REFUSED,
        evidence_hashes=(),
        evidence_set_id=None,
        reasons=(LearningReason.COHORT_NOT_ELIGIBLE, LearningReason.COMMIT_RATE_LIMITED),
    )


def _encode(decision: IrLearningDecision):
    return encode_learning_decision(
        decision,
        blackout_id="blackout-1",
        segment_id="segment-1",
        boot_id="boot-1",
        wall_time_utc=START,
        monotonic_ns=10,
        seq=5,
    )


def _rehash(encoded, payload: dict[str, object], **changes: object) -> bytes:
    envelope = replace(encoded.envelope, payload=payload, record_sha256=None, **changes)
    return encode_v3_record(envelope).line


@pytest.mark.parametrize("decision", (_downward(), _upward(), _refusal()))
def test_decision_round_trip_is_canonical_and_domain_reconstructed(
    decision: IrLearningDecision,
) -> None:
    encoded = _encode(decision)
    decoded = decode_learning_decision(encoded.line)

    assert decoded == decision
    assert decode_learning_decision_record(encoded.line) == encoded
    assert encoded.line == _encode(decision).line
    assert encoded.envelope.record_type == LEARNING_DECISION_RECORD_TYPE
    assert len(encoded.line) <= LEARNING_DECISION_MAX_LINE_BYTES


def test_maximally_populated_actual_decision_stays_under_8k() -> None:
    hashes = _hashes(64)
    decision = _downward(hashes)
    decision = replace(decision, reasons=tuple(LearningReason)[:8])
    encoded = _encode(decision)

    assert len(decision.evidence_hashes) == 64
    assert len(decision.reasons) == 8
    assert len(encoded.line) <= LEARNING_DECISION_MAX_LINE_BYTES
    assert "evidence_hashes" not in encoded.envelope.payload["change"]
    assert decode_learning_decision(encoded.line) == decision


@pytest.mark.parametrize(
    "mutation",
    ("unknown", "schema", "enum", "hash", "scope", "change", "observation"),
)
def test_rehashed_semantic_mutations_are_rejected(mutation: str) -> None:
    decision = _downward()
    encoded = _encode(decision)
    payload = dict(encoded.envelope.payload)
    if mutation == "unknown":
        payload["unexpected"] = True
    elif mutation == "schema":
        payload["schema"] = "ir-learning-decision-v2"
    elif mutation == "enum":
        payload["disposition"] = "future"
    elif mutation == "hash":
        payload["evidence_set_id"] = "0" * 64
    elif mutation == "scope":
        payload["blackout_id"] = "other-blackout"
    elif mutation == "change":
        payload["change"] = {**payload["change"], "value_after": 0.025}  # type: ignore[index]
    else:
        payload["observed_load_sag_increase"] = {
            "parameter": "ir_k_v_per_pp",
            "value_before": 0.020,
            "measured_estimate": 0.024,
            "evidence_set_id": evidence_set_id((H1, H2)),
            "evidence_hashes": [H1, H2],
        }
    with pytest.raises(V3CodecError):
        decode_learning_decision(_rehash(encoded, payload))


def test_decode_rejects_unknown_nested_field_and_noncanonical_line() -> None:
    encoded = _encode(_downward())
    value = json.loads(encoded.line)
    value["payload"]["change"]["unexpected"] = True
    unknown = json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    with pytest.raises(V3CodecError):
        decode_learning_decision(unknown)
    pretty = json.dumps(json.loads(encoded.line), indent=2).encode() + b"\n"
    with pytest.raises(V3CodecError):
        decode_learning_decision(pretty)


def test_observation_nested_evidence_set_must_match_outer_hash_authority() -> None:
    encoded = _encode(_upward())
    payload = dict(encoded.envelope.payload)
    observation = dict(payload["observed_load_sag_increase"])  # type: ignore[arg-type]
    observation["evidence_set_id"] = "0" * 64
    payload["observed_load_sag_increase"] = observation
    with pytest.raises(V3CodecError):
        decode_learning_decision(_rehash(encoded, payload))


def test_decode_rejects_schema_two_scope_mismatch_and_line_overflow() -> None:
    encoded = _encode(_refusal())
    value = json.loads(encoded.line)
    with pytest.raises(V3CodecError):
        decode_learning_decision(json.dumps({**value, "schema_version": 2}).encode() + b"\n")
    with pytest.raises(V3CodecError):
        decode_learning_decision(
            _rehash(encoded, dict(encoded.envelope.payload), segment_id="other")
        )
    oversized = (
        encoded.line[:-1]
        + b" " * (LEARNING_DECISION_MAX_LINE_BYTES - len(encoded.line) + 1)
        + b"\n"
    )
    with pytest.raises(V3CodecError):
        decode_learning_decision(oversized)


def test_learning_decision_rejects_unknown_option_and_nonfinite_input() -> None:
    with pytest.raises(V3CodecError):
        encode_learning_decision(
            _refusal(),
            blackout_id="blackout-1",
            segment_id="segment-1",
            boot_id="boot-1",
            wall_time_utc=START,
            monotonic_ns=10,
            unexpected=True,
        )
    invalid_change = ModelChange("ir_k_v_per_pp", float("nan"), 0.018, 0.018, (H1, H2), True)
    with pytest.raises(ValueError):
        IrLearningDecision(
            IrLearningDisposition.DOWNWARD_COMMIT,
            (H1, H2),
            evidence_set_id((H1, H2)),
            change=invalid_change,
        )
