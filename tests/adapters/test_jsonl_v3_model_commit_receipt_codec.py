"""Strict v3 model-commit receipt codec tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.adapters.jsonl_v3_canonical import V3CodecError, V3RecordEnvelope, encode_v3_record
from src.adapters.jsonl_v3_learning_decision_codec import encode_learning_decision
from src.adapters.jsonl_v3_model_commit_receipt_codec import (
    MAX_RECEIPT_LINE_BYTES,
    decode_model_commit_receipt,
    decode_model_commit_receipt_record,
    encode_model_commit_receipt,
    reconstruct_model_commit_receipt,
    verify_model_commit_receipt,
)
from src.domain.ir_learning_decision import (
    IrLearningDecision,
    IrLearningDisposition,
    IrModelCommitReceiptRecord,
)
from src.domain.learning import evidence_set_id
from src.domain.values import ModelChange, ModelCommitReceipt

HASHES = tuple(f"{index:064x}" for index in range(1, 65))


def receipt_record() -> IrModelCommitReceiptRecord:
    change = ModelChange("ir_k_v_per_pp", 0.020, 0.018, 0.018, HASHES[:2], True)
    decision = IrLearningDecision(
        IrLearningDisposition.DOWNWARD_COMMIT,
        HASHES[:2],
        evidence_set_id(HASHES[:2]),
        change=change,
    )
    receipt = ModelCommitReceipt(
        blackout_id="blackout-1",
        parameter="ir_k_v_per_pp",
        value_before=0.020,
        measured_estimate=0.018,
        value_after=0.018,
        model_hash_before="a" * 64,
        model_hash_after="b" * 64,
        scientific_fingerprint_before="c" * 64,
        scientific_fingerprint_after="d" * 64,
        evidence_set_id=evidence_set_id(HASHES[:2]),
        consumed_step_hashes=HASHES[:2],
        reference_reparameterization=False,
        safety_oracle="sampled_safety_regression_grid:1",
    )
    return IrModelCommitReceiptRecord(decision, receipt)


def decision_record(
    value: IrModelCommitReceiptRecord | None = None,
    *,
    blackout_id: str = "blackout-1",
    segment_id: str = "segment-1",
    seq: int = 0,
    previous_record_sha256: str | None = None,
):
    decision = (value or receipt_record()).decision
    return encode_learning_decision(
        decision,
        blackout_id=blackout_id,
        segment_id=segment_id,
        boot_id="boot-1",
        wall_time_utc=datetime(2026, 8, 18, tzinfo=timezone.utc),
        monotonic_ns=10,
        seq=seq,
        previous_record_sha256=previous_record_sha256,
    )


def rehash(record, payload: dict[str, object]) -> bytes:
    envelope = record.envelope
    return encode_v3_record(
        V3RecordEnvelope(
            3,
            envelope.record_type,
            envelope.provenance,
            envelope.blackout_id,
            envelope.segment_id,
            envelope.seq,
            envelope.boot_id,
            envelope.wall_time_utc,
            envelope.monotonic_ns,
            envelope.prev_record_sha256,
            payload,
        )
    ).line


def test_receipt_is_canonical_and_reconstructable() -> None:
    expected = receipt_record()
    linked = decision_record(expected)
    encoded = encode_model_commit_receipt(expected, linked)
    assert len(encoded.line) <= MAX_RECEIPT_LINE_BYTES
    assert decode_model_commit_receipt(encoded.line).line == encoded.line
    assert decode_model_commit_receipt_record(encoded.line, linked) == encoded
    assert reconstruct_model_commit_receipt(encoded.line, linked, expected.decision) == expected
    assert verify_model_commit_receipt(encoded, linked, expected.decision, expected) == expected


@pytest.mark.parametrize("mutation", ("unknown", "schema", "hash", "scope", "parameter"))
def test_receipt_rejects_strict_adversarial_payload(mutation: str) -> None:
    encoded = encode_model_commit_receipt(receipt_record(), decision_record())
    value = json.loads(encoded.line)
    if mutation == "unknown":
        value["payload"]["unknown"] = True
    elif mutation == "schema":
        value["payload"]["schema"] = "schema-2"
    elif mutation == "hash":
        value["record_sha256"] = "e" * 64
    elif mutation == "scope":
        value["blackout_id"] = "other"
    else:
        value["payload"]["receipt"]["parameter"] = "soh"
    mutated = rehash(encoded, value["payload"])
    if mutation == "hash":
        mutated = json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    if mutation == "scope":
        mutated_value = json.loads(mutated)
        mutated_value["blackout_id"] = "other"
        mutated = json.dumps(mutated_value, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    with pytest.raises(V3CodecError):
        decode_model_commit_receipt(mutated)


def test_maximum_legal_hash_budget_remains_under_8k() -> None:
    value = receipt_record()
    change = ModelChange("ir_k_v_per_pp", 0.020, 0.018, 0.018, HASHES, True)
    decision = IrLearningDecision(
        IrLearningDisposition.DOWNWARD_COMMIT,
        HASHES,
        evidence_set_id(HASHES),
        change=change,
    )
    maximal_receipt = ModelCommitReceipt(
        blackout_id=value.receipt.blackout_id,
        parameter="ir_k_v_per_pp",
        value_before=0.020,
        measured_estimate=0.018,
        value_after=0.018,
        model_hash_before="a" * 64,
        model_hash_after="b" * 64,
        scientific_fingerprint_before="c" * 64,
        scientific_fingerprint_after="d" * 64,
        evidence_set_id=evidence_set_id(HASHES),
        consumed_step_hashes=HASHES,
        reference_reparameterization=False,
        safety_oracle="sampled_safety_regression_grid:99999999",
    )
    encoded = encode_model_commit_receipt(
        IrModelCommitReceiptRecord(decision, maximal_receipt),
        decision_record(IrModelCommitReceiptRecord(decision, maximal_receipt)),
    )
    assert len(encoded.line) <= MAX_RECEIPT_LINE_BYTES
