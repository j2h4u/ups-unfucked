"""Strict v3 hash-linked model commit receipt codec."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from src.adapters.jsonl_v3_canonical import (
    EncodedV3Record,
    V3CodecError,
    V3RecordEnvelope,
    decode_v3_record,
    encode_v3_record,
)
from src.adapters.jsonl_v3_learning_decision_codec import (
    LEARNING_DECISION_RECORD_TYPE,
    decode_learning_decision,
)
from src.domain.ir_learning_decision import (
    IrLearningDecision,
    IrLearningDisposition,
    IrModelCommitReceiptRecord,
)
from src.domain.values import ModelChange, ModelCommitReceipt

RECEIPT_RECORD_TYPE = "ir_model_commit_receipt"
RECEIPT_PROVENANCE = "derived"
RECEIPT_SCHEMA = "ir-model-commit-receipt-v1"
MAX_RECEIPT_LINE_BYTES = 8 * 1024
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_ORACLE_RE = re.compile(r"sampled_safety_regression_grid:[1-9][0-9]{0,7}\Z")
_PAYLOAD_FIELDS = frozenset({"schema", "decision_record_hash", "receipt"})
_RECEIPT_FIELDS = frozenset(
    {
        "blackout_id",
        "parameter",
        "value_before",
        "measured_estimate",
        "value_after",
        "model_hash_before",
        "model_hash_after",
        "scientific_fingerprint_before",
        "scientific_fingerprint_after",
        "evidence_set_id",
        "consumed_step_hashes",
        "reference_reparameterization",
        "safety_oracle",
    }
)


def encode_model_commit_receipt(
    value: IrModelCommitReceiptRecord,
    decision_record: EncodedV3Record,
    *,
    seq: int = 0,
    previous_record_sha256: str | None = None,
) -> EncodedV3Record:
    """Encode only the receipt and an externally addressed decision record."""
    if not isinstance(value, IrModelCommitReceiptRecord):
        raise TypeError("receipt codec requires IrModelCommitReceiptRecord")
    _validate_decision_record(decision_record)
    if decode_learning_decision(decision_record.line) != value.decision:
        raise V3CodecError("receipt decision differs from linked decision record")
    payload = {
        "schema": RECEIPT_SCHEMA,
        "decision_record_hash": decision_record.record_sha256,
        "receipt": _receipt_payload(value.receipt),
    }
    record = encode_v3_record(
        V3RecordEnvelope(
            3,
            RECEIPT_RECORD_TYPE,
            RECEIPT_PROVENANCE,
            decision_record.envelope.blackout_id,
            decision_record.envelope.segment_id,
            seq,
            "derived",
            decision_record.envelope.wall_time_utc,
            decision_record.envelope.monotonic_ns,
            previous_record_sha256,
            payload,
        )
    )
    if len(record.line) > MAX_RECEIPT_LINE_BYTES:
        raise V3CodecError("model commit receipt exceeds 8 KiB")
    return record


def decode_model_commit_receipt(line: bytes) -> EncodedV3Record:
    """Decode the receipt payload without pretending to decode its decision link."""
    if len(line) > MAX_RECEIPT_LINE_BYTES:
        raise V3CodecError("model commit receipt exceeds 8 KiB")
    record = decode_v3_record(line)
    if record.envelope.record_type != RECEIPT_RECORD_TYPE:
        raise V3CodecError("record is not an IR model commit receipt")
    if record.envelope.provenance != RECEIPT_PROVENANCE:
        raise V3CodecError("receipt provenance is not derived")
    payload = _mapping(record.envelope.payload, "receipt payload")
    if set(payload) != _PAYLOAD_FIELDS or payload["schema"] != RECEIPT_SCHEMA:
        raise V3CodecError("receipt payload fields or schema are invalid")
    _hash(payload["decision_record_hash"], "decision record hash")
    receipt = _mapping(payload["receipt"], "receipt")
    _validate_receipt(receipt)
    if record.envelope.blackout_id != receipt["blackout_id"]:
        raise V3CodecError("receipt envelope scope is not bound")
    return record


def decode_model_commit_receipt_record(
    line: bytes,
    decision_record: EncodedV3Record,
) -> EncodedV3Record:
    """Strictly decode a receipt and bind it to the actual decision record."""
    record = decode_model_commit_receipt(line)
    decision = decode_learning_decision(decision_record.line)
    verify_model_commit_receipt(record, decision_record, decision)
    return record


def reconstruct_model_commit_receipt(
    record: EncodedV3Record | bytes,
    decision_record: EncodedV3Record,
    decision: IrLearningDecision,
) -> IrModelCommitReceiptRecord:
    """Reconstruct the wrapper only after validating the supplied decision link."""
    encoded = decode_model_commit_receipt(record) if isinstance(record, bytes) else record
    if not isinstance(decision, IrLearningDecision):
        raise TypeError("decoded decision is required for receipt reconstruction")
    payload = encoded.envelope.payload
    receipt = _receipt_from_payload(payload["receipt"])
    return _linked_record(encoded, decision_record, decision, receipt)


def verify_model_commit_receipt(
    record: EncodedV3Record | bytes,
    decision_record: EncodedV3Record,
    decision: IrLearningDecision,
    expected: IrModelCommitReceiptRecord | None = None,
) -> IrModelCommitReceiptRecord:
    """Validate decision hash, receipt fields, and optional expected wrapper."""
    actual = reconstruct_model_commit_receipt(record, decision_record, decision)
    if expected is not None and actual != expected:
        raise V3CodecError("receipt replay does not match supplied wrapper")
    return actual


def _linked_record(
    encoded: EncodedV3Record,
    decision_record: EncodedV3Record,
    decision: IrLearningDecision,
    receipt: ModelCommitReceipt,
) -> IrModelCommitReceiptRecord:
    _validate_decision_record(decision_record)
    if decode_learning_decision(decision_record.line) != decision:
        raise V3CodecError("supplied decision differs from linked decision record")
    link = encoded.envelope.payload["decision_record_hash"]
    if link != decision_record.record_sha256:
        raise V3CodecError("receipt decision link does not match decoded decision record")
    if decision.disposition is not IrLearningDisposition.DOWNWARD_COMMIT:
        raise V3CodecError("receipt decision is not downward commit")
    change = decision.change
    if change is None:
        raise V3CodecError("receipt decision has no model change")
    if receipt.blackout_id != encoded.envelope.blackout_id:
        raise V3CodecError("receipt blackout scope differs from envelope")
    if encoded.envelope.blackout_id != decision_record.envelope.blackout_id:
        raise V3CodecError("receipt and decision blackout scopes differ")
    if encoded.envelope.segment_id != decision_record.envelope.segment_id:
        raise V3CodecError("receipt and decision segment scopes differ")
    if not _receipt_matches_decision(receipt, decision, change):
        raise V3CodecError("receipt does not match supplied decision")
    return IrModelCommitReceiptRecord(decision, receipt)


def _validate_decision_record(value: EncodedV3Record) -> None:
    if not isinstance(value, EncodedV3Record):
        raise TypeError("encoded learning decision record is required")
    if value.envelope.record_type != LEARNING_DECISION_RECORD_TYPE:
        raise V3CodecError("receipt link must target a learning decision")
    if value.envelope.provenance != RECEIPT_PROVENANCE:
        raise V3CodecError("learning decision link must be derived")


def _receipt_matches_decision(
    receipt: ModelCommitReceipt,
    decision: IrLearningDecision,
    change: ModelChange,
) -> bool:
    return (
        receipt.parameter == change.parameter
        and receipt.value_before == change.value_before
        and receipt.measured_estimate == change.measured_estimate
        and receipt.value_after == change.value_after
        and receipt.evidence_set_id == decision.evidence_set_id
        and receipt.consumed_step_hashes == change.evidence_hashes
    )


def _receipt_payload(value: ModelCommitReceipt) -> dict[str, Any]:
    return {
        "blackout_id": value.blackout_id,
        "parameter": value.parameter,
        "value_before": value.value_before,
        "measured_estimate": value.measured_estimate,
        "value_after": value.value_after,
        "model_hash_before": value.model_hash_before,
        "model_hash_after": value.model_hash_after,
        "scientific_fingerprint_before": value.scientific_fingerprint_before,
        "scientific_fingerprint_after": value.scientific_fingerprint_after,
        "evidence_set_id": value.evidence_set_id,
        "consumed_step_hashes": list(value.consumed_step_hashes),
        "reference_reparameterization": value.reference_reparameterization,
        "safety_oracle": value.safety_oracle,
    }


def _receipt_from_payload(value: Mapping[str, Any]) -> ModelCommitReceipt:
    return ModelCommitReceipt(
        blackout_id=value["blackout_id"],
        parameter=value["parameter"],
        value_before=value["value_before"],
        measured_estimate=value["measured_estimate"],
        value_after=value["value_after"],
        model_hash_before=value["model_hash_before"],
        model_hash_after=value["model_hash_after"],
        scientific_fingerprint_before=value["scientific_fingerprint_before"],
        scientific_fingerprint_after=value["scientific_fingerprint_after"],
        evidence_set_id=value["evidence_set_id"],
        consumed_step_hashes=tuple(value["consumed_step_hashes"]),
        reference_reparameterization=value["reference_reparameterization"],
        safety_oracle=value["safety_oracle"],
    )


def _validate_receipt(value: Mapping[str, Any]) -> None:
    if set(value) != _RECEIPT_FIELDS:
        raise V3CodecError("receipt fields are not exact")
    if value["parameter"] != "ir_k_v_per_pp" or value["reference_reparameterization"] is not False:
        raise V3CodecError("receipt parameter or reference flag is invalid")
    if not isinstance(value["blackout_id"], str) or not value["blackout_id"]:
        raise V3CodecError("receipt blackout ID is invalid")
    for key in (
        "model_hash_before",
        "model_hash_after",
        "scientific_fingerprint_before",
        "scientific_fingerprint_after",
        "evidence_set_id",
    ):
        _hash(value[key], key)
    _hashes(value["consumed_step_hashes"])
    if (
        not isinstance(value["safety_oracle"], str)
        or _ORACLE_RE.fullmatch(value["safety_oracle"]) is None
    ):
        raise V3CodecError("safety oracle is not canonical")


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise V3CodecError(f"{name} must be an object")
    return value


def _hash(value: Any, name: str) -> None:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise V3CodecError(f"{name} must be lowercase SHA-256")


def _hashes(value: Any) -> None:
    if not isinstance(value, list) or not value or len(value) > 64:
        raise V3CodecError("receipt evidence hashes are invalid")
    if any(_HASH_RE.fullmatch(item or "") is None for item in value):
        raise V3CodecError("receipt evidence hashes are invalid")
    if value != sorted(value) or len(set(value)) != len(value):
        raise V3CodecError("receipt evidence hashes are invalid")
