"""Strict v3 hash-linked terminal outcome codec."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from src.adapters.jsonl_v3_canonical import (
    EncodedV3Record,
    V3CodecError,
    V3RecordEnvelope,
    canonical_json_bytes,
    decode_v3_record,
    encode_v3_record,
)
from src.adapters.jsonl_v3_curve_assessment_codec import (
    decode_curve_assessment_summary_record,
)
from src.adapters.jsonl_v3_firmware_lb_assessment_codec import (
    decode_firmware_lb_assessment_summary_record,
)
from src.adapters.jsonl_v3_fragment_profile_codec import (
    decode_fragment_profile_records,
    reconstruct_fragment_profiles,
)
from src.adapters.jsonl_v3_learning_decision_codec import (
    decode_learning_decision_record,
)
from src.adapters.jsonl_v3_load_sag_assessment_codec import (
    decode_load_sag_assessment_summary_record,
)
from src.adapters.jsonl_v3_model_commit_receipt_codec import (
    decode_model_commit_receipt_record,
)
from src.adapters.jsonl_v3_terminal_tail_codec import (
    decode_blackout_end_record,
    decode_endpoint_anchor_record,
)
from src.domain.blackout_terminal import BlackoutTermination
from src.domain.fragments import CanonicalDischargeSample
from src.domain.reasons import InfrastructureReason
from src.domain.terminal_outcome import TerminalOutcome, TerminalOutcomeKind

OUTCOME_RECORD_TYPE = "terminal_outcome"
OUTCOME_PROVENANCE = "derived"
OUTCOME_SCHEMA = "terminal-outcome-v1"
MAX_OUTCOME_LINE_BYTES = 8 * 1024
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = frozenset(
    {
        "schema",
        "outcome_id",
        "blackout_id",
        "physical_episode_id",
        "battery_epoch_id",
        "segment_id",
        "kind",
        "termination",
        "ended_at_utc",
        "raw_record_count",
        "raw_sample_count",
        "blackout_end_hash",
        "consumer_summary_hashes",
        "decision_record_hash",
        "receipt_record_hash",
        "infrastructure_reasons",
    }
)


@dataclass(frozen=True, slots=True)
class TerminalOutcomeLinks:
    """The closed, ordered set of records linked by one terminal outcome."""

    blackout_end: EncodedV3Record | bytes
    fragment_profile_records: tuple[EncodedV3Record | bytes, ...]
    endpoint_anchor: EncodedV3Record | bytes | None = None
    raw_samples_by_slice: Mapping[str, tuple[CanonicalDischargeSample, ...]] | None = None
    load_sag_assessment_summary: EncodedV3Record | bytes | None = None
    curve_assessment_summary: EncodedV3Record | bytes | None = None
    firmware_lb_assessment_summary: EncodedV3Record | bytes | None = None
    learning_decision: EncodedV3Record | bytes | None = None
    ir_model_commit_receipt: EncodedV3Record | bytes | None = None


@dataclass(frozen=True, slots=True)
class _OutcomeLinks:
    endpoint_anchor: EncodedV3Record | None
    blackout_end: EncodedV3Record
    fragment_profile_records: tuple[EncodedV3Record, ...]
    load_sag_assessment_summary: EncodedV3Record | None
    curve_assessment_summary: EncodedV3Record | None
    firmware_lb_assessment_summary: EncodedV3Record | None
    learning_decision: EncodedV3Record | None
    ir_model_commit_receipt: EncodedV3Record | None

    @classmethod
    def from_records(cls, values: TerminalOutcomeLinks, outcome: TerminalOutcome) -> _OutcomeLinks:
        endpoint = _decode_owned(
            values.endpoint_anchor,
            decode_endpoint_anchor_record,
            "endpoint anchor",
        )
        end_line = _link_line(values.blackout_end, "blackout end")
        end = decode_blackout_end_record(end_line, terminal_anchor_record=endpoint)
        _validate_end_termination(outcome, end)
        if endpoint is None and end.envelope.payload.get("termination") != (
            BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED.value
        ):
            raise V3CodecError("non-budget blackout end requires an endpoint anchor record")
        profiles = _decode_profile_records(
            values.fragment_profile_records,
            values.raw_samples_by_slice,
            allow_empty=_empty_profile_context_allowed(outcome, values, end),
        )
        if values.raw_samples_by_slice is None and any(
            item is not None
            for item in (
                values.load_sag_assessment_summary,
                values.curve_assessment_summary,
                values.firmware_lb_assessment_summary,
            )
        ):
            raise V3CodecError("consumer summaries require raw profile replay context")
        raw_samples = values.raw_samples_by_slice or {}
        load = _decode_owned(
            values.load_sag_assessment_summary,
            lambda line: decode_load_sag_assessment_summary_record(
                line,
                profile_records=profiles,
                raw_samples_by_slice=raw_samples,
            ),
            "load-sag summary",
        )
        curve = _decode_owned(
            values.curve_assessment_summary,
            lambda line: decode_curve_assessment_summary_record(
                line,
                profile_records=profiles,
                raw_samples_by_slice=raw_samples,
            ),
            "curve summary",
        )
        firmware = _decode_owned(
            values.firmware_lb_assessment_summary,
            lambda line: decode_firmware_lb_assessment_summary_record(
                line,
                profile_records=profiles,
                raw_samples_by_slice=raw_samples,
            ),
            "firmware summary",
        )
        decision = _decode_owned(
            values.learning_decision,
            decode_learning_decision_record,
            "learning decision",
        )
        receipt = _decode_receipt(values.ir_model_commit_receipt, decision)
        return cls(
            endpoint,
            end,
            profiles,
            load,
            curve,
            firmware,
            decision,
            receipt,
        )


@dataclass(frozen=True, slots=True)
class _LinkContext:
    outcome: TerminalOutcome
    outcome_record: EncodedV3Record
    links: _OutcomeLinks


def encode_terminal_outcome(
    value: TerminalOutcome,
    *,
    seq: int = 0,
    previous_record_sha256: str | None = None,
) -> EncodedV3Record:
    if not isinstance(value, TerminalOutcome):
        raise TypeError("outcome codec requires TerminalOutcome")
    ended = _utc(value.ended_at_utc)
    record = encode_v3_record(
        V3RecordEnvelope(
            3,
            OUTCOME_RECORD_TYPE,
            OUTCOME_PROVENANCE,
            value.blackout_id,
            value.segment_id,
            seq,
            "terminal",
            ended,
            0,
            previous_record_sha256,
            _payload(value),
        )
    )
    if len(record.line) > MAX_OUTCOME_LINE_BYTES:
        raise V3CodecError("terminal outcome exceeds 8 KiB")
    return record


def decode_terminal_outcome(line: bytes) -> EncodedV3Record:
    if len(line) > MAX_OUTCOME_LINE_BYTES:
        raise V3CodecError("terminal outcome exceeds 8 KiB")
    record = decode_v3_record(line)
    if record.envelope.record_type != OUTCOME_RECORD_TYPE:
        raise V3CodecError("record is not a terminal outcome")
    if record.envelope.provenance != OUTCOME_PROVENANCE:
        raise V3CodecError("outcome provenance is not derived")
    payload = _mapping(record.envelope.payload, "outcome payload")
    _validate_payload(payload)
    if record.envelope.blackout_id != payload["blackout_id"]:
        raise V3CodecError("outcome blackout scope is not bound")
    if record.envelope.segment_id != payload["segment_id"]:
        raise V3CodecError("outcome segment scope is not bound")
    if record.envelope.wall_time_utc != payload["ended_at_utc"]:
        raise V3CodecError("outcome time scope is not bound")
    return record


def reconstruct_terminal_outcome(record: EncodedV3Record | bytes) -> TerminalOutcome:
    encoded = decode_terminal_outcome(record) if isinstance(record, bytes) else record
    if not isinstance(encoded, EncodedV3Record):
        raise TypeError("outcome record must be EncodedV3Record")
    return _domain_outcome(encoded.envelope.payload)


def verify_terminal_outcome(
    record: EncodedV3Record | bytes,
    *,
    links: TerminalOutcomeLinks,
    expected: TerminalOutcome | None = None,
) -> TerminalOutcome:
    """Verify exact links against decoded linked records supplied by the caller."""
    outcome_record = _decode_outcome_record(record)
    outcome = _domain_outcome(outcome_record.envelope.payload)
    decoded_links = _OutcomeLinks.from_records(links, outcome)
    _validate_link(_LinkContext(outcome, outcome_record, decoded_links))
    if expected is not None and outcome != expected:
        raise V3CodecError("outcome does not match expected domain facts")
    return outcome


def _validate_link(
    context: _LinkContext,
) -> None:
    outcome = context.outcome
    outcome_record = context.outcome_record
    links = context.links
    _validate_endpoint_scope(outcome_record, links.endpoint_anchor, links.blackout_end)
    _validate_blackout_scope(outcome_record, links.blackout_end, "blackout end")
    _validate_endpoint_segment_scope(outcome_record, links.blackout_end)
    _validate_end_termination(outcome, links.blackout_end)
    _validate_profile_scopes(outcome_record, links.fragment_profile_records)
    if links.blackout_end.record_sha256 != outcome.blackout_end_hash:
        raise V3CodecError("blackout end hash link differs")
    summaries = (
        links.load_sag_assessment_summary,
        links.curve_assessment_summary,
        links.firmware_lb_assessment_summary,
    )
    _validate_summary_links(
        outcome_record,
        outcome,
        summaries,
        links.fragment_profile_records,
    )
    _validate_optional_link(
        outcome_record,
        links.learning_decision,
        outcome.decision_record_hash,
        "learning decision",
    )
    if links.learning_decision is not None:
        _validate_learning_decision_scope(outcome_record, links.learning_decision)
    _validate_optional_link(
        outcome_record,
        links.ir_model_commit_receipt,
        outcome.receipt_record_hash,
        "IR model commit receipt",
    )
    if links.ir_model_commit_receipt is not None:
        _validate_receipt_scope(outcome_record, links.ir_model_commit_receipt)
    records = (
        *((links.endpoint_anchor,) if links.endpoint_anchor is not None else ()),
        links.blackout_end,
        *links.fragment_profile_records,
        *(item for item in summaries if item is not None),
        *((links.learning_decision,) if links.learning_decision is not None else ()),
        *((links.ir_model_commit_receipt,) if links.ir_model_commit_receipt is not None else ()),
        outcome_record,
    )
    _validate_owned_chain(records)


def _validate_optional_link(
    outcome_record: EncodedV3Record,
    linked: EncodedV3Record | None,
    expected_hash: str | None,
    name: str,
) -> None:
    if (linked is None) != (expected_hash is None):
        raise V3CodecError(f"{name} link presence differs")
    if linked is not None:
        _validate_blackout_scope(outcome_record, linked, name)
        if linked.record_sha256 != expected_hash:
            raise V3CodecError(f"{name} hash link differs")


def _validate_summary_links(
    outcome_record: EncodedV3Record,
    outcome: TerminalOutcome,
    summaries: tuple[EncodedV3Record | None, ...],
    profiles: tuple[EncodedV3Record, ...],
) -> None:
    expected_hashes = outcome.consumer_summary_hashes
    if not expected_hashes:
        if any(item is not None for item in summaries):
            raise V3CodecError("consumer summary links are not allowed for this outcome")
        return
    present = tuple(item for item in summaries if item is not None)
    if (
        len(present) != len(summaries)
        or tuple(item.record_sha256 for item in present) != expected_hashes
    ):
        raise V3CodecError("consumer summary links do not match exact order")
    load_sag, curve, firmware = present
    _validate_summary_link(
        outcome_record,
        outcome,
        load_sag,
        profiles,
        _validate_load_sag_segment_scope,
    )
    _validate_summary_link(
        outcome_record,
        outcome,
        curve,
        profiles,
        _validate_compact_summary_segment_scope,
    )
    _validate_summary_link(
        outcome_record,
        outcome,
        firmware,
        profiles,
        _validate_compact_summary_segment_scope,
    )


def _validate_summary_link(
    outcome_record: EncodedV3Record,
    outcome: TerminalOutcome,
    record: EncodedV3Record,
    profiles: tuple[EncodedV3Record, ...],
    segment_validator: Callable[[EncodedV3Record, EncodedV3Record, TerminalOutcome], None],
) -> None:
    _validate_blackout_scope(outcome_record, record, "consumer summary")
    segment_validator(outcome_record, record, outcome)
    _validate_summary_payload_scope(record, outcome)
    _validate_summary_profile_binding(record, profiles)


def _validate_owned_chain(
    records: tuple[EncodedV3Record, ...],
) -> None:
    for previous, current in zip(records, records[1:]):
        if current.envelope.seq != previous.envelope.seq + 1:
            raise V3CodecError("terminal outcome linked records are not sequential")
        if current.envelope.prev_record_sha256 != previous.record_sha256:
            raise V3CodecError("terminal outcome linked record chain is broken")


def _validate_blackout_scope(left: EncodedV3Record, right: EncodedV3Record, name: str) -> None:
    if left.envelope.blackout_id != right.envelope.blackout_id:
        raise V3CodecError(f"{name} blackout scope differs")


def _validate_exact_segment(
    outcome_record: EncodedV3Record,
    record: EncodedV3Record,
    expected: str,
    name: str,
) -> None:
    if record.envelope.segment_id != expected:
        raise V3CodecError(f"{name} segment scope differs")


def _validate_endpoint_segment_scope(
    outcome_record: EncodedV3Record, record: EncodedV3Record
) -> None:
    _validate_exact_segment(
        outcome_record,
        record,
        outcome_record.envelope.segment_id,
        "blackout end",
    )


def _validate_endpoint_scope(
    outcome_record: EncodedV3Record,
    endpoint: EncodedV3Record | None,
    blackout_end: EncodedV3Record,
) -> None:
    if endpoint is None:
        if blackout_end.envelope.payload.get("termination") != (
            BlackoutTermination.AGGREGATE_BUDGET_EXHAUSTED.value
        ):
            raise V3CodecError("non-budget blackout end requires an endpoint anchor record")
        return
    _validate_blackout_scope(outcome_record, endpoint, "endpoint anchor")
    payload = endpoint.envelope.payload
    if payload.get("physical_episode_id") != outcome_record.envelope.payload["physical_episode_id"]:
        raise V3CodecError("endpoint anchor physical-episode scope differs")
    if payload.get("segment_id") != outcome_record.envelope.segment_id:
        raise V3CodecError("endpoint anchor segment scope differs")


def _validate_end_termination(outcome: TerminalOutcome, blackout_end: EncodedV3Record) -> None:
    if blackout_end.envelope.payload.get("termination") != outcome.termination.value:
        raise V3CodecError("blackout end termination differs from outcome termination")


def _validate_profile_scopes(
    outcome_record: EncodedV3Record,
    records: tuple[EncodedV3Record, ...],
) -> None:
    expected = outcome_record.envelope.payload
    for record in records:
        payload = record.envelope.payload
        for key in ("blackout_id", "physical_episode_id", "battery_epoch_id"):
            if payload.get(key) != expected[key]:
                raise V3CodecError(f"fragment profile {key} scope differs")
        if record.envelope.segment_id != expected["segment_id"]:
            raise V3CodecError("fragment profile segment scope differs")


def _validate_load_sag_segment_scope(
    outcome_record: EncodedV3Record,
    record: EncodedV3Record,
    outcome: TerminalOutcome,
) -> None:
    _validate_exact_segment(outcome_record, record, outcome.segment_id, "load-sag summary")
    if record.envelope.payload.get("segment_id") != outcome.segment_id:
        raise V3CodecError("load-sag summary payload segment scope differs")


def _validate_compact_summary_segment_scope(
    _outcome_record: EncodedV3Record,
    record: EncodedV3Record,
    _outcome: TerminalOutcome,
) -> None:
    if record.envelope.segment_id != "summary":
        raise V3CodecError("compact summary segment namespace differs")


def _validate_learning_decision_scope(
    outcome_record: EncodedV3Record, record: EncodedV3Record
) -> None:
    _validate_exact_segment(
        outcome_record,
        record,
        outcome_record.envelope.segment_id,
        "learning decision",
    )
    payload = record.envelope.payload
    if payload.get("blackout_id") != outcome_record.envelope.blackout_id:
        raise V3CodecError("learning decision blackout scope differs")
    if payload.get("segment_id") != outcome_record.envelope.segment_id:
        raise V3CodecError("learning decision payload segment scope differs")


def _validate_receipt_scope(outcome_record: EncodedV3Record, record: EncodedV3Record) -> None:
    _validate_exact_segment(
        outcome_record,
        record,
        outcome_record.envelope.segment_id,
        "IR model commit receipt",
    )
    receipt = record.envelope.payload.get("receipt")
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("blackout_id") != outcome_record.envelope.blackout_id
    ):
        raise V3CodecError("commit receipt blackout scope differs")


def _validate_summary_payload_scope(record: EncodedV3Record, outcome: TerminalOutcome) -> None:
    payload = record.envelope.payload
    for key, expected in (
        ("blackout_id", outcome.blackout_id),
        ("physical_episode_id", outcome.physical_episode_id),
        ("battery_epoch_id", outcome.battery_epoch_id),
    ):
        if payload.get(key) != expected:
            raise V3CodecError("consumer summary scope differs")
    if "segment_id" in payload and payload["segment_id"] != outcome.segment_id:
        raise V3CodecError("consumer summary segment scope differs")


def _validate_summary_profile_binding(
    record: EncodedV3Record,
    profiles: tuple[EncodedV3Record, ...],
) -> None:
    if not profiles:
        raise V3CodecError("consumer summary requires complete profile records")
    payload = record.envelope.payload
    profile_payload = profiles[0].envelope.payload
    if payload.get("profile_series_id") != profile_payload.get("series_id"):
        raise V3CodecError("consumer summary profile series differs")
    hashes = [item.record_sha256 for item in profiles]
    expected_digests = {
        sha256(canonical_json_bytes({"hashes": hashes})).hexdigest(),
        sha256(canonical_json_bytes({"value": hashes})).hexdigest(),
    }
    if payload.get("source_profile_record_hashes_sha256") not in expected_digests:
        raise V3CodecError("consumer summary profile-record digest differs")
    if "source_first_profile_record_hash" in payload:
        if payload["source_first_profile_record_hash"] != hashes[0]:
            raise V3CodecError("consumer summary first profile record differs")
        if payload["source_last_profile_record_hash"] != hashes[-1]:
            raise V3CodecError("consumer summary last profile record differs")


def _decode_outcome_record(record: EncodedV3Record | bytes) -> EncodedV3Record:
    if isinstance(record, bytes):
        return decode_terminal_outcome(record)
    if not isinstance(record, EncodedV3Record):
        raise TypeError("terminal outcome must be an encoded canonical record")
    decoded = decode_terminal_outcome(record.line)
    if decoded != record:
        raise V3CodecError("terminal outcome is not its canonical decoded value")
    return decoded


def _link_line(value: EncodedV3Record | bytes, name: str) -> bytes:
    if isinstance(value, bytes):
        return value
    if not isinstance(value, EncodedV3Record):
        raise TypeError(f"{name} must be an encoded canonical record")
    decoded = decode_v3_record(value.line)
    if decoded != value:
        raise V3CodecError(f"{name} is not its canonical decoded value")
    return value.line


def _decode_owned(
    value: EncodedV3Record | bytes | None,
    decoder: Callable[[bytes], EncodedV3Record],
    name: str,
) -> EncodedV3Record | None:
    if value is None:
        return None
    try:
        record = decoder(_link_line(value, name))
    except (TypeError, ValueError) as exc:
        raise V3CodecError(f"{name} is not a strict owned record") from exc
    if isinstance(value, EncodedV3Record) and record != value:
        raise V3CodecError(f"{name} is not its canonical decoded value")
    return record


def _decode_receipt(
    value: EncodedV3Record | bytes | None,
    decision: EncodedV3Record | None,
) -> EncodedV3Record | None:
    if value is None:
        return None
    if decision is None:
        raise V3CodecError("commit receipt requires a strict learning decision")
    return _decode_owned(
        value,
        lambda line: decode_model_commit_receipt_record(line, decision),
        "IR model commit receipt",
    )


def _decode_profile_records(
    values: tuple[EncodedV3Record | bytes, ...],
    raw_samples_by_slice: Mapping[str, tuple[CanonicalDischargeSample, ...]] | None,
    *,
    allow_empty: bool = False,
) -> tuple[EncodedV3Record, ...]:
    if not values:
        if not allow_empty:
            raise V3CodecError("terminal outcome links require complete profile records")
        return ()
    lines = tuple(_link_line(value, "fragment profile") for value in values)
    decoded = decode_fragment_profile_records(lines)
    if any(
        isinstance(value, EncodedV3Record) and value != item
        for value, item in zip(values, decoded, strict=True)
    ):
        raise V3CodecError("fragment profile record is not its canonical decoded value")
    if raw_samples_by_slice is not None:
        try:
            reconstruct_fragment_profiles(decoded, raw_samples_by_slice)
        except (TypeError, ValueError) as exc:
            raise V3CodecError("fragment profile replay does not match raw samples") from exc
    return decoded


def _empty_profile_context_allowed(
    outcome: TerminalOutcome,
    values: TerminalOutcomeLinks,
    blackout_end: EncodedV3Record,
) -> bool:
    return (
        outcome.kind is TerminalOutcomeKind.INFRASTRUCTURE_REFUSED
        and outcome.termination is BlackoutTermination.CAPTURE_DAMAGED
        and blackout_end.envelope.payload.get("termination") == outcome.termination.value
        and outcome.raw_record_count == 0
        and outcome.raw_sample_count == 0
        and values.raw_samples_by_slice is None
        and not outcome.consumer_summary_hashes
        and outcome.decision_record_hash is None
        and outcome.receipt_record_hash is None
        and values.load_sag_assessment_summary is None
        and values.curve_assessment_summary is None
        and values.firmware_lb_assessment_summary is None
        and values.learning_decision is None
        and values.ir_model_commit_receipt is None
    )


def _payload(value: TerminalOutcome) -> dict[str, Any]:
    return {
        "schema": OUTCOME_SCHEMA,
        "outcome_id": value.outcome_id,
        "blackout_id": value.blackout_id,
        "physical_episode_id": value.physical_episode_id,
        "battery_epoch_id": value.battery_epoch_id,
        "segment_id": value.segment_id,
        "kind": value.kind.value,
        "termination": value.termination.value,
        "ended_at_utc": _utc(value.ended_at_utc),
        "raw_record_count": value.raw_record_count,
        "raw_sample_count": value.raw_sample_count,
        "blackout_end_hash": value.blackout_end_hash,
        "consumer_summary_hashes": list(value.consumer_summary_hashes),
        "decision_record_hash": value.decision_record_hash,
        "receipt_record_hash": value.receipt_record_hash,
        "infrastructure_reasons": [item.value for item in value.infrastructure_reasons],
    }


def _validate_payload(value: Mapping[str, Any]) -> None:
    if set(value) != _FIELDS or value["schema"] != OUTCOME_SCHEMA:
        raise V3CodecError("outcome payload fields or schema are invalid")
    _validate_identity(value)
    _validate_kind_and_termination(value)
    _validate_counts_and_hashes(value)
    _validate_kind_links(value)
    _validate_infrastructure_reasons(value)


def _validate_identity(value: Mapping[str, Any]) -> None:
    for key in (
        "outcome_id",
        "blackout_id",
        "physical_episode_id",
        "battery_epoch_id",
        "segment_id",
    ):
        _id(value[key], key)


def _validate_kind_and_termination(value: Mapping[str, Any]) -> None:
    if value["kind"] not in {item.value for item in TerminalOutcomeKind}:
        raise V3CodecError("unknown terminal outcome kind")
    if value["termination"] not in {item.value for item in BlackoutTermination}:
        raise V3CodecError("unknown terminal termination")
    _utc_parse(value["ended_at_utc"])


def _validate_counts_and_hashes(value: Mapping[str, Any]) -> None:
    _count(value["raw_record_count"], 1_000_000, "raw_record_count")
    _count(value["raw_sample_count"], 3_170, "raw_sample_count")
    _hash(value["blackout_end_hash"], "blackout_end_hash")
    _hashes(value["consumer_summary_hashes"], allow_empty=True)
    for key in ("decision_record_hash", "receipt_record_hash"):
        if value[key] is not None:
            _hash(value[key], key)


def _validate_kind_links(value: Mapping[str, Any]) -> None:
    if value["kind"] == TerminalOutcomeKind.ASSESSED.value:
        if len(value["consumer_summary_hashes"]) != 3 or value["decision_record_hash"] is None:
            raise V3CodecError("assessed outcome links are incomplete")
    if value["kind"] == TerminalOutcomeKind.INFRASTRUCTURE_REFUSED.value:
        if (
            value["consumer_summary_hashes"]
            or value["decision_record_hash"]
            or value["receipt_record_hash"]
        ):
            raise V3CodecError("infrastructure refusal contains scientific links")
    if (
        value["receipt_record_hash"] is not None
        and value["kind"] != TerminalOutcomeKind.ASSESSED.value
    ):
        raise V3CodecError("receipt link requires assessed outcome")


def _validate_infrastructure_reasons(value: Mapping[str, Any]) -> None:
    if not isinstance(value["infrastructure_reasons"], list) or any(
        item not in {reason.value for reason in InfrastructureReason}
        for item in value["infrastructure_reasons"]
    ):
        raise V3CodecError("infrastructure reasons are invalid")


def _domain_outcome(value: Mapping[str, Any]) -> TerminalOutcome:
    return TerminalOutcome(
        outcome_id=value["outcome_id"],
        blackout_id=value["blackout_id"],
        physical_episode_id=value["physical_episode_id"],
        battery_epoch_id=value["battery_epoch_id"],
        segment_id=value["segment_id"],
        kind=TerminalOutcomeKind(value["kind"]),
        termination=BlackoutTermination(value["termination"]),
        ended_at_utc=_utc_parse(value["ended_at_utc"]),
        raw_record_count=value["raw_record_count"],
        raw_sample_count=value["raw_sample_count"],
        blackout_end_hash=value["blackout_end_hash"],
        consumer_summary_hashes=tuple(value["consumer_summary_hashes"]),
        decision_record_hash=value["decision_record_hash"],
        receipt_record_hash=value["receipt_record_hash"],
        infrastructure_reasons=tuple(
            InfrastructureReason(item) for item in value["infrastructure_reasons"]
        ),
    )


def _utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise V3CodecError("outcome time must be UTC")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utc_parse(value: Any) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value
    ):
        raise V3CodecError("outcome time is not canonical UTC")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise V3CodecError(f"{name} must be an object")
    return value


def _id(value: Any, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode()) > 128
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise V3CodecError(f"{name} is not a bounded ID")


def _count(value: Any, maximum: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise V3CodecError(f"{name} is outside its bound")


def _hash(value: Any, name: str) -> None:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise V3CodecError(f"{name} must be lowercase SHA-256")


def _hashes(value: Any, *, allow_empty: bool = False) -> None:
    if not isinstance(value, list) or (not allow_empty and not value) or len(value) > 3:
        raise V3CodecError("consumer summary hashes are not bounded")
    for item in value:
        _hash(item, "consumer summary hash")
