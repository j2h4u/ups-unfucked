"""Strict v3 terminal outcome codec tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.adapters.jsonl_v3_canonical import (
    EncodedV3Record,
    V3CodecError,
    V3RecordEnvelope,
    encode_v3_record,
)
from src.adapters.jsonl_v3_curve_assessment_codec import encode_curve_assessment_summary
from src.adapters.jsonl_v3_firmware_lb_assessment_codec import encode_firmware_lb_assessment_summary
from src.adapters.jsonl_v3_fragment_profile_codec import (
    decode_fragment_profile_records,
    encode_fragment_profiles,
)
from src.adapters.jsonl_v3_load_sag_assessment_codec import encode_load_sag_assessment_summary
from src.adapters.jsonl_v3_model_commit_receipt_codec import encode_model_commit_receipt
from src.adapters.jsonl_v3_terminal_outcome_codec import (
    MAX_OUTCOME_LINE_BYTES,
    TerminalOutcomeLinks,
    decode_terminal_outcome,
    encode_terminal_outcome,
    reconstruct_terminal_outcome,
    verify_terminal_outcome,
)
from src.adapters.jsonl_v3_terminal_tail_codec import encode_blackout_end, encode_endpoint_anchor
from src.domain.blackout_terminal import BlackoutTermination
from src.domain.curve_assessment import assess_curve
from src.domain.firmware_lb_assessment import assess_firmware_lb
from src.domain.fragment_policy import DEFAULT_DISCHARGE_FRAGMENT_POLICY
from src.domain.fragments import AnchorKind, build_discharge_fragment_profiles
from src.domain.load_sag_assessment import DEFAULT_LOAD_SAG_POLICY, assess_load_sag
from src.domain.reasons import InfrastructureReason
from src.domain.terminal_outcome import TerminalOutcome, TerminalOutcomeKind
from tests.adapters.test_jsonl_v3_load_sag_assessment_codec import _context
from tests.adapters.test_jsonl_v3_model_commit_receipt_codec import decision_record, receipt_record
from tests.adapters.test_jsonl_v3_terminal_tail_codec import _anchor, _end
from tests.domain.test_fragments import _many_profile_inputs

H1 = "1" * 64
H2 = "2" * 64

pytest_plugins = ("tests.domain.conftest",)


def assessed_outcome() -> TerminalOutcome:
    return TerminalOutcome(
        outcome_id="outcome-1",
        blackout_id="blackout-1",
        physical_episode_id="episode-1",
        battery_epoch_id="epoch-1",
        segment_id="segment-1",
        kind=TerminalOutcomeKind.ASSESSED,
        termination=BlackoutTermination.POWER_RESTORED,
        ended_at_utc=datetime(2026, 8, 18, tzinfo=timezone.utc),
        raw_record_count=96,
        raw_sample_count=257,
        blackout_end_hash="3" * 64,
        consumer_summary_hashes=("5" * 64, "6" * 64, "7" * 64),
        decision_record_hash="8" * 64,
        receipt_record_hash="9" * 64,
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


def linked_record(record_type: str, seq: int, previous: str | None = None):
    payload: dict[str, object] = {"record": record_type}
    if record_type in {
        "load_sag_assessment_summary",
        "curve_assessment_summary",
        "firmware_lb_assessment_summary",
    }:
        payload.update(
            {
                "blackout_id": "blackout-1",
                "physical_episode_id": "episode-1",
                "battery_epoch_id": "epoch-1",
            }
        )
    if record_type == "learning_decision":
        payload.update({"blackout_id": "blackout-1", "segment_id": "segment-1"})
    if record_type == "ir_model_commit_receipt":
        payload["receipt"] = {"blackout_id": "blackout-1"}
    segment_id = (
        "segment-1"
        if record_type == "load_sag_assessment_summary"
        else "summary"
        if record_type in {"curve_assessment_summary", "firmware_lb_assessment_summary"}
        else "segment-1"
    )
    if record_type == "load_sag_assessment_summary":
        payload["segment_id"] = segment_id
    return encode_v3_record(
        V3RecordEnvelope(
            3,
            record_type,
            "derived",
            "blackout-1",
            segment_id,
            seq,
            "boot-1",
            "2026-08-18T00:00:00.000000Z",
            seq,
            previous,
            payload,
        )
    )


def _relink_chain(records):
    result = [records[0]]
    for record in records[1:]:
        result.append(
            encode_v3_record(
                replace(
                    record.envelope,
                    prev_record_sha256=result[-1].record_sha256,
                    record_sha256=None,
                )
            )
        )
    return tuple(result)


def _assessed_link_chain():
    end = linked_record("blackout_end", 1)
    load = linked_record("load_sag_assessment_summary", 2, end.record_sha256)
    curve = linked_record("curve_assessment_summary", 3, load.record_sha256)
    firmware = linked_record("firmware_lb_assessment_summary", 4, curve.record_sha256)
    decision = linked_record("learning_decision", 5, firmware.record_sha256)
    receipt = linked_record("ir_model_commit_receipt", 6, decision.record_sha256)
    summaries = (load, curve, firmware)
    expected = replace(
        assessed_outcome(),
        blackout_end_hash=end.record_sha256,
        consumer_summary_hashes=tuple(item.record_sha256 for item in summaries),
        decision_record_hash=decision.record_sha256,
        receipt_record_hash=receipt.record_sha256,
    )
    return end, summaries, decision, receipt, expected


def _actual_prefix():
    anchors, slices, steps = _many_profile_inputs(2)
    profiles = build_discharge_fragment_profiles(
        anchors,
        slices,
        steps,
        DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision,
    )
    endpoint = encode_endpoint_anchor(
        replace(
            _anchor(),
            blackout_id="blackout-a",
            physical_episode_id="episode-a",
            segment_id="segment-a",
        ),
        seq=0,
    )
    end = encode_blackout_end(
        replace(
            _end(),
            blackout_id="blackout-a",
            physical_episode_id="episode-a",
            segment_id="segment-a",
            terminal_anchor_record_hash=endpoint.record_sha256,
        ),
        seq=1,
        previous_record_sha256=endpoint.record_sha256,
    )
    profile_records = decode_fragment_profile_records(
        record.line
        for record in encode_fragment_profiles(
            profiles,
            {item.slice_id: tuple(item.samples) for item in slices},
            seq=2,
            previous_record_sha256=end.record_sha256,
        )
    )
    raw_samples = {item.slice_id: tuple(item.samples) for item in slices}
    return endpoint, end, profiles, profile_records, slices, raw_samples


def _actual_summaries(profiles, profile_records, raw_samples, frozen_snapshot):
    load_results = tuple(
        assess_load_sag(profile, _context(profile), DEFAULT_LOAD_SAG_POLICY) for profile in profiles
    )
    load = encode_load_sag_assessment_summary(
        load_results,
        profile_records,
        raw_samples_by_slice=raw_samples,
        seq=2 + len(profile_records),
        previous_record_sha256=profile_records[-1].record_sha256,
    )
    curve_results = tuple(
        result for profile in profiles for result in assess_curve(profile, frozen_snapshot)
    )
    curve = encode_curve_assessment_summary(
        curve_results,
        profile_records,
        raw_samples_by_slice=raw_samples,
        seq=load.envelope.seq + 1,
        previous_record_sha256=load.record_sha256,
    )
    firmware_results = tuple(assess_firmware_lb(profile) for profile in profiles)
    firmware = encode_firmware_lb_assessment_summary(
        firmware_results,
        profile_records,
        raw_samples_by_slice=raw_samples,
        seq=curve.envelope.seq + 1,
        previous_record_sha256=curve.record_sha256,
    )
    return load, curve, firmware


def _actual_link_chain(frozen_snapshot):
    endpoint, end, profiles, profile_records, slices, raw_samples = _actual_prefix()
    load, curve, firmware = _actual_summaries(
        profiles, profile_records, raw_samples, frozen_snapshot
    )
    receipt_value = replace(
        receipt_record(),
        receipt=replace(receipt_record().receipt, blackout_id="blackout-a"),
    )
    decision = decision_record(
        receipt_value,
        blackout_id="blackout-a",
        segment_id="segment-a",
        seq=firmware.envelope.seq + 1,
        previous_record_sha256=firmware.record_sha256,
    )
    receipt = encode_model_commit_receipt(
        receipt_value,
        decision,
        seq=decision.envelope.seq + 1,
        previous_record_sha256=decision.record_sha256,
    )
    expected = replace(
        assessed_outcome(),
        blackout_id="blackout-a",
        physical_episode_id="episode-a",
        battery_epoch_id="epoch-a",
        segment_id="segment-a",
        blackout_end_hash=end.record_sha256,
        consumer_summary_hashes=(load.record_sha256, curve.record_sha256, firmware.record_sha256),
        decision_record_hash=decision.record_sha256,
        receipt_record_hash=receipt.record_sha256,
    )
    outcome = encode_terminal_outcome(
        expected,
        seq=receipt.envelope.seq + 1,
        previous_record_sha256=receipt.record_sha256,
    )
    links = TerminalOutcomeLinks(
        endpoint_anchor=endpoint,
        blackout_end=end,
        fragment_profile_records=profile_records,
        raw_samples_by_slice=raw_samples,
        load_sag_assessment_summary=load,
        curve_assessment_summary=curve,
        firmware_lb_assessment_summary=firmware,
        learning_decision=decision,
        ir_model_commit_receipt=receipt,
    )
    return (
        endpoint,
        end,
        profile_records,
        (load, curve, firmware),
        decision,
        receipt,
        expected,
        outcome,
        links,
    )


def _mutated_summary_chain(summaries, decision, receipt, wrong_index: int):
    wrong_segment = "summary" if wrong_index == 0 else "segment-1"
    mutated = list(summaries)
    mutated[wrong_index] = encode_v3_record(
        replace(mutated[wrong_index].envelope, segment_id=wrong_segment, record_sha256=None)
    )
    linked = _relink_chain((*mutated, decision, receipt))
    return linked[:3], linked[3], linked[4]


def test_assessed_outcome_is_canonical_and_reconstructable(frozen_snapshot) -> None:
    _, _, _, summaries, decision, receipt, expected, encoded, links = _actual_link_chain(
        frozen_snapshot
    )
    assert len(encoded.line) <= MAX_OUTCOME_LINE_BYTES
    assert decode_terminal_outcome(encoded.line).line == encoded.line
    assert reconstruct_terminal_outcome(encoded.line) == expected
    assert (
        verify_terminal_outcome(
            encoded,
            links=links,
            expected=expected,
        )
        == expected
    )


@pytest.mark.parametrize("wrong_index", (0, 1, 2))
def test_summary_segment_scope_matches_each_consumer_contract(
    wrong_index: int, frozen_snapshot
) -> None:
    endpoint, end, profiles, summaries, decision, receipt, expected, valid, links = (
        _actual_link_chain(frozen_snapshot)
    )
    assert (
        verify_terminal_outcome(
            valid,
            links=links,
        )
        == expected
    )

    changed_summaries, changed_decision, changed_receipt = _mutated_summary_chain(
        summaries, decision, receipt, wrong_index
    )
    changed = replace(
        expected,
        consumer_summary_hashes=(*(item.record_sha256 for item in changed_summaries),),
        decision_record_hash=changed_decision.record_sha256,
        receipt_record_hash=changed_receipt.record_sha256,
    )
    changed_record = encode_terminal_outcome(
        changed,
        seq=7,
        previous_record_sha256=changed_receipt.record_sha256,
    )
    with pytest.raises(V3CodecError, match="strict owned"):
        verify_terminal_outcome(
            changed_record,
            links=replace(
                links,
                load_sag_assessment_summary=changed_summaries[0],
                curve_assessment_summary=changed_summaries[1],
                firmware_lb_assessment_summary=changed_summaries[2],
                learning_decision=changed_decision,
                ir_model_commit_receipt=changed_receipt,
            ),
        )


def test_safe_shutdown_recorded_only_has_no_capacity_claim() -> None:
    value = assessed_outcome()
    censored = TerminalOutcome(
        outcome_id=value.outcome_id,
        blackout_id=value.blackout_id,
        physical_episode_id=value.physical_episode_id,
        battery_epoch_id=value.battery_epoch_id,
        segment_id=value.segment_id,
        kind=TerminalOutcomeKind.RECORDED_ONLY,
        termination=BlackoutTermination.SAFE_SHUTDOWN_RESTARTED,
        ended_at_utc=value.ended_at_utc,
        raw_record_count=value.raw_record_count,
        raw_sample_count=value.raw_sample_count,
        blackout_end_hash=value.blackout_end_hash,
        consumer_summary_hashes=value.consumer_summary_hashes,
        decision_record_hash=value.decision_record_hash,
        receipt_record_hash=None,
    )
    encoded = encode_terminal_outcome(censored)
    assert reconstruct_terminal_outcome(encoded.line) == censored
    assert "capacity" not in encoded.line.decode().lower()
    assert "runtime" not in encoded.line.decode().lower()
    assert "soh" not in encoded.line.decode().lower()


def test_infrastructure_refusal_contains_no_invented_results() -> None:
    value = assessed_outcome()
    refused = TerminalOutcome(
        outcome_id=value.outcome_id,
        blackout_id=value.blackout_id,
        physical_episode_id=value.physical_episode_id,
        battery_epoch_id=value.battery_epoch_id,
        segment_id=value.segment_id,
        kind=TerminalOutcomeKind.INFRASTRUCTURE_REFUSED,
        termination=BlackoutTermination.CAPTURE_DAMAGED,
        ended_at_utc=value.ended_at_utc,
        raw_record_count=0,
        raw_sample_count=0,
        blackout_end_hash=value.blackout_end_hash,
        consumer_summary_hashes=(),
        decision_record_hash=None,
        receipt_record_hash=None,
        infrastructure_reasons=(InfrastructureReason.CAPTURE_DAMAGED,),
    )
    assert reconstruct_terminal_outcome(encode_terminal_outcome(refused).line) == refused


def _zero_profile_infrastructure_chain() -> tuple[
    TerminalOutcome, EncodedV3Record, TerminalOutcomeLinks
]:
    endpoint = encode_endpoint_anchor(_anchor(AnchorKind.CORRUPTION), seq=0)
    end = encode_blackout_end(
        _end(
            termination=BlackoutTermination.CAPTURE_DAMAGED,
            terminal_anchor_record_hash=endpoint.record_sha256,
        ),
        seq=1,
        previous_record_sha256=endpoint.record_sha256,
    )
    outcome = replace(
        assessed_outcome(),
        kind=TerminalOutcomeKind.INFRASTRUCTURE_REFUSED,
        termination=BlackoutTermination.CAPTURE_DAMAGED,
        raw_record_count=0,
        raw_sample_count=0,
        blackout_end_hash=end.record_sha256,
        consumer_summary_hashes=(),
        decision_record_hash=None,
        receipt_record_hash=None,
        infrastructure_reasons=(InfrastructureReason.CAPTURE_DAMAGED,),
    )
    encoded = encode_terminal_outcome(
        outcome,
        seq=2,
        previous_record_sha256=end.record_sha256,
    )
    links = TerminalOutcomeLinks(
        endpoint_anchor=endpoint,
        blackout_end=end,
        fragment_profile_records=(),
    )
    return outcome, encoded, links


def test_zero_profile_infrastructure_refusal_verifies_continuous_chain() -> None:
    expected, encoded, links = _zero_profile_infrastructure_chain()

    assert verify_terminal_outcome(encoded, links=links, expected=expected) == expected


def test_capture_damaged_end_rejects_rehashed_power_restored_outcome() -> None:
    expected, _, links = _zero_profile_infrastructure_chain()
    blackout_end = links.blackout_end
    assert isinstance(blackout_end, EncodedV3Record)
    changed = replace(expected, termination=BlackoutTermination.POWER_RESTORED)
    encoded = encode_terminal_outcome(
        changed,
        seq=2,
        previous_record_sha256=blackout_end.record_sha256,
    )

    with pytest.raises(V3CodecError, match="termination differs"):
        verify_terminal_outcome(encoded, links=links)


def test_power_restored_end_rejects_capture_damaged_outcome() -> None:
    endpoint = encode_endpoint_anchor(_anchor(), seq=0)
    blackout_end = encode_blackout_end(
        _end(
            termination=BlackoutTermination.POWER_RESTORED,
            terminal_anchor_record_hash=endpoint.record_sha256,
        ),
        seq=1,
        previous_record_sha256=endpoint.record_sha256,
    )
    outcome = replace(
        assessed_outcome(),
        kind=TerminalOutcomeKind.INFRASTRUCTURE_REFUSED,
        termination=BlackoutTermination.CAPTURE_DAMAGED,
        raw_record_count=0,
        raw_sample_count=0,
        blackout_end_hash=blackout_end.record_sha256,
        consumer_summary_hashes=(),
        decision_record_hash=None,
        receipt_record_hash=None,
        infrastructure_reasons=(InfrastructureReason.CAPTURE_DAMAGED,),
    )
    encoded = encode_terminal_outcome(
        outcome,
        seq=2,
        previous_record_sha256=blackout_end.record_sha256,
    )
    links = TerminalOutcomeLinks(
        endpoint_anchor=endpoint,
        blackout_end=blackout_end,
        fragment_profile_records=(),
    )

    with pytest.raises(V3CodecError, match="termination differs"):
        verify_terminal_outcome(encoded, links=links)


def test_assessed_outcome_requires_profile_records(frozen_snapshot) -> None:
    _, _, _, _, _, _, expected, encoded, links = _actual_link_chain(frozen_snapshot)
    with pytest.raises(V3CodecError, match="complete profile records"):
        verify_terminal_outcome(
            encoded,
            links=replace(links, fragment_profile_records=(), raw_samples_by_slice=None),
            expected=expected,
        )


@pytest.mark.parametrize("field", ("summary", "decision", "receipt", "raw"))
def test_zero_profile_infrastructure_refusal_rejects_scientific_context(
    field: str, frozen_snapshot
) -> None:
    expected, encoded, links = _zero_profile_infrastructure_chain()
    _, _, _, summaries, decision, receipt, _, _, actual_links = _actual_link_chain(frozen_snapshot)
    if field == "summary":
        changed_links = replace(links, load_sag_assessment_summary=summaries[0])
    elif field == "decision":
        changed_links = replace(links, learning_decision=decision)
    elif field == "receipt":
        changed_links = replace(links, ir_model_commit_receipt=receipt)
    else:
        changed_links = replace(links, raw_samples_by_slice=actual_links.raw_samples_by_slice)

    with pytest.raises(V3CodecError, match="complete profile records"):
        verify_terminal_outcome(encoded, links=changed_links, expected=expected)


def test_zero_profile_refusal_with_raw_samples_is_rejected(frozen_snapshot) -> None:
    expected, encoded, links = _zero_profile_infrastructure_chain()
    _, _, _, _, _, _, _, _, actual_links = _actual_link_chain(frozen_snapshot)
    changed = replace(links, raw_samples_by_slice=actual_links.raw_samples_by_slice)

    with pytest.raises(V3CodecError, match="complete profile records"):
        verify_terminal_outcome(encoded, links=changed, expected=expected)


def test_zero_profile_infrastructure_refusal_requires_zero_raw_samples() -> None:
    _, encoded, links = _zero_profile_infrastructure_chain()
    blackout_end = links.blackout_end
    assert isinstance(blackout_end, EncodedV3Record)
    changed = replace(
        reconstruct_terminal_outcome(encoded),
        raw_sample_count=1,
    )
    record = encode_terminal_outcome(
        changed,
        seq=2,
        previous_record_sha256=blackout_end.record_sha256,
    )

    with pytest.raises(V3CodecError, match="complete profile records"):
        verify_terminal_outcome(record, links=links)


def test_zero_profile_infrastructure_refusal_requires_zero_raw_records() -> None:
    _, encoded, links = _zero_profile_infrastructure_chain()
    blackout_end = links.blackout_end
    assert isinstance(blackout_end, EncodedV3Record)
    changed = replace(
        reconstruct_terminal_outcome(encoded),
        raw_record_count=1,
    )
    record = encode_terminal_outcome(
        changed,
        seq=2,
        previous_record_sha256=blackout_end.record_sha256,
    )

    with pytest.raises(V3CodecError, match="complete profile records"):
        verify_terminal_outcome(record, links=links)


def test_outcome_verifier_rejects_wrong_type_and_ordered_namespace(frozen_snapshot) -> None:
    endpoint, end, profiles, summaries, decision, receipt, expected, outcome, links = (
        _actual_link_chain(frozen_snapshot)
    )
    wrong = linked_record(
        "ir_learning_decision", decision.envelope.seq, summaries[-1].record_sha256
    )
    with pytest.raises(V3CodecError, match="learning decision"):
        verify_terminal_outcome(
            outcome,
            links=replace(links, learning_decision=wrong, ir_model_commit_receipt=None),
        )
    with pytest.raises(V3CodecError):
        verify_terminal_outcome(
            outcome,
            links=replace(
                links,
                load_sag_assessment_summary=summaries[1],
                curve_assessment_summary=summaries[0],
                learning_decision=decision,
                ir_model_commit_receipt=receipt,
            ),
        )


@pytest.mark.parametrize("mutation", ("unknown", "schema", "hash", "scope", "nested"))
def test_outcome_rejects_strict_adversarial_payload(mutation: str) -> None:
    encoded = encode_terminal_outcome(assessed_outcome())
    value = json.loads(encoded.line)
    if mutation == "unknown":
        value["payload"]["unknown"] = True
    elif mutation == "schema":
        value["payload"]["schema"] = "terminal-outcome-v2"
    elif mutation == "hash":
        value["record_sha256"] = "e" * 64
    elif mutation == "scope":
        value["payload"]["blackout_id"] = "other"
    else:
        value["payload"]["learning_decision"] = {"disposition": "downward_commit"}
    mutated = rehash(encoded, value["payload"])
    if mutation == "hash":
        mutated = json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    with pytest.raises(V3CodecError):
        decode_terminal_outcome(mutated)
