"""Actual-object byte and chain proofs for the v3 derived tail."""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from src.adapters.jsonl_v3_canonical import EncodedV3Record
from src.adapters.jsonl_v3_curve_assessment_codec import (
    decode_curve_assessment_summary,
    encode_curve_assessment_summary,
)
from src.adapters.jsonl_v3_firmware_lb_assessment_codec import (
    decode_firmware_lb_assessment_summary,
    encode_firmware_lb_assessment_summary,
)
from src.adapters.jsonl_v3_fragment_profile_codec import (
    decode_fragment_profile_records,
    encode_fragment_profiles,
    profile_descriptor_count,
)
from src.adapters.jsonl_v3_learning_decision_codec import decode_learning_decision
from src.adapters.jsonl_v3_load_sag_assessment_codec import (
    decode_load_sag_assessment_summary,
    encode_load_sag_assessment_summary,
)
from src.adapters.jsonl_v3_model_commit_receipt_codec import (
    encode_model_commit_receipt,
    verify_model_commit_receipt,
)
from src.adapters.jsonl_v3_tail_budget import prove_tail_budget
from src.adapters.jsonl_v3_terminal_outcome_codec import (
    TerminalOutcomeLinks,
    encode_terminal_outcome,
    reconstruct_terminal_outcome,
    verify_terminal_outcome,
)
from src.adapters.jsonl_v3_terminal_tail_codec import (
    decode_blackout_end,
    decode_endpoint_anchor,
    encode_blackout_end,
    encode_endpoint_anchor,
)
from src.domain.curve_assessment import assess_curve
from src.domain.firmware_lb_assessment import assess_firmware_lb
from src.domain.fragments import build_discharge_fragment_profiles
from src.domain.load_sag_assessment import DEFAULT_LOAD_SAG_POLICY, assess_load_sag
from tests.adapters.test_jsonl_v3_load_sag_assessment_codec import _context
from tests.adapters.test_jsonl_v3_model_commit_receipt_codec import (
    decision_record,
    receipt_record,
)
from tests.adapters.test_jsonl_v3_terminal_outcome_codec import assessed_outcome
from tests.adapters.test_jsonl_v3_terminal_tail_codec import _anchor, _end
from tests.domain.test_fragments import HASHES, _many_profile_inputs

pytest_plugins = ("tests.domain.conftest",)


@dataclass(frozen=True)
class _TailFacts:
    end: EncodedV3Record
    summaries: tuple[EncodedV3Record, EncodedV3Record, EncodedV3Record]
    decision: EncodedV3Record
    receipt: EncodedV3Record | None


def _profile_objects():
    anchors, slices, steps = _many_profile_inputs(64)
    extra = tuple(
        replace(anchor, canonical_hash=HASHES[700 + index]) for index, anchor in enumerate(anchors)
    )
    profiles = build_discharge_fragment_profiles(
        (*anchors, *extra), slices, steps, slices[0].policy_revision
    )
    raw = {item.slice_id: item.samples for profile in profiles for item in profile.slices}
    records = encode_fragment_profiles(profiles, raw)
    assert profile_descriptor_count(records) == 256
    return profiles, records


def _summary_objects(profiles, profile_records, frozen_snapshot, *, seq, previous):
    raw_samples_by_slice = {
        item.slice_id: tuple(item.samples) for profile in profiles for item in profile.slices
    }
    curve_results = tuple(
        result for profile in profiles for result in assess_curve(profile, frozen_snapshot)
    )
    firmware_results = tuple(assess_firmware_lb(profile) for profile in profiles)
    decoded_profiles = decode_fragment_profile_records(record.line for record in profile_records)
    load_results = tuple(
        assess_load_sag(profile, _context(profile), DEFAULT_LOAD_SAG_POLICY) for profile in profiles
    )
    load = encode_load_sag_assessment_summary(
        load_results,
        decoded_profiles,
        raw_samples_by_slice=raw_samples_by_slice,
        seq=seq,
        previous_record_sha256=previous,
    )
    curve = encode_curve_assessment_summary(
        curve_results,
        profile_records,
        raw_samples_by_slice=raw_samples_by_slice,
        seq=seq + 1,
        previous_record_sha256=load.record_sha256,
    )
    firmware = encode_firmware_lb_assessment_summary(
        firmware_results,
        profile_records,
        raw_samples_by_slice=raw_samples_by_slice,
        seq=seq + 2,
        previous_record_sha256=curve.record_sha256,
    )
    decode_load_sag_assessment_summary(
        load.line,
        load_results,
        decoded_profiles,
        raw_samples_by_slice=raw_samples_by_slice,
        policy=DEFAULT_LOAD_SAG_POLICY,
    )
    decode_curve_assessment_summary(curve.line)
    decode_firmware_lb_assessment_summary(firmware.line)
    return load, curve, firmware


def _maximum_objects(observation_factory, frozen_snapshot, *, include_receipt=True):
    del observation_factory
    endpoint, end = _terminal_prefix()
    profiles, _ = _profile_objects()
    raw_samples_by_slice = {
        item.slice_id: tuple(item.samples) for profile in profiles for item in profile.slices
    }
    profile_records = encode_fragment_profiles(
        profiles,
        raw_samples_by_slice,
        seq=2,
        previous_record_sha256=end.record_sha256,
    )
    load, curve, firmware = _summary_objects(
        profiles,
        profile_records,
        frozen_snapshot,
        seq=2 + len(profile_records),
        previous=profile_records[-1].record_sha256,
    )
    linked_decision, receipt = _learning_tail(
        firmware,
        seq=2 + len(profile_records) + 3,
        include_receipt=include_receipt,
    )
    facts = _TailFacts(end, (load, curve, firmware), linked_decision, receipt)
    outcome = _outcome_tail(facts)
    verify_terminal_outcome(
        outcome,
        links=TerminalOutcomeLinks(
            blackout_end=end,
            endpoint_anchor=endpoint,
            fragment_profile_records=tuple(profile_records),
            raw_samples_by_slice=raw_samples_by_slice,
            load_sag_assessment_summary=load,
            curve_assessment_summary=curve,
            firmware_lb_assessment_summary=firmware,
            learning_decision=linked_decision,
            ir_model_commit_receipt=receipt,
        ),
        expected=reconstruct_terminal_outcome(outcome.line),
    )
    derived = (
        *profile_records,
        load,
        curve,
        firmware,
        linked_decision,
        *((receipt,) if receipt else ()),
    )
    return derived, (endpoint, end, outcome)


def _terminal_prefix():
    anchor = replace(
        _anchor(),
        blackout_id="blackout-a",
        physical_episode_id="episode-a",
        segment_id="segment-a",
    )
    endpoint = encode_endpoint_anchor(anchor, seq=0)
    end = encode_blackout_end(
        replace(
            _end(),
            terminal_anchor_record_hash=endpoint.record_sha256,
            blackout_id="blackout-a",
            physical_episode_id="episode-a",
            segment_id="segment-a",
        ),
        seq=1,
        previous_record_sha256=endpoint.record_sha256,
    )
    decode_endpoint_anchor(endpoint.line)
    decode_blackout_end(end.line, terminal_anchor_record=endpoint)
    return endpoint, end


def _learning_tail(firmware, *, seq, include_receipt):
    receipt_value = receipt_record()
    receipt_value = replace(
        receipt_value,
        receipt=replace(receipt_value.receipt, blackout_id="blackout-a"),
    )
    linked_decision = decision_record(
        receipt_value,
        blackout_id="blackout-a",
        segment_id="segment-a",
        seq=seq,
        previous_record_sha256=firmware.record_sha256,
    )
    receipt = (
        encode_model_commit_receipt(
            receipt_value,
            linked_decision,
            seq=seq + 1,
            previous_record_sha256=linked_decision.record_sha256,
        )
        if include_receipt
        else None
    )
    if receipt is not None:
        verify_model_commit_receipt(
            receipt,
            linked_decision,
            decode_learning_decision(linked_decision.line),
            receipt_value,
        )
    return linked_decision, receipt


def _outcome_tail(facts: _TailFacts):
    previous = facts.receipt.record_sha256 if facts.receipt else facts.decision.record_sha256
    sequence = facts.decision.envelope.seq + (2 if facts.receipt else 1)
    return encode_terminal_outcome(
        replace(
            assessed_outcome(),
            blackout_id="blackout-a",
            physical_episode_id="episode-a",
            battery_epoch_id="epoch-a",
            segment_id="segment-a",
            blackout_end_hash=facts.end.record_sha256,
            consumer_summary_hashes=(*(item.record_sha256 for item in facts.summaries),),
            decision_record_hash=facts.decision.record_sha256,
            receipt_record_hash=(facts.receipt.record_sha256 if facts.receipt else None),
        ),
        seq=sequence,
        previous_record_sha256=previous,
    )


def _assert_continuous_chain(derived, terminal) -> None:
    chain = (*terminal[:2], *derived, terminal[2])
    assert chain[0].envelope.seq == 0
    assert chain[0].envelope.prev_record_sha256 is None
    for previous, current in zip(chain, chain[1:]):
        assert current.envelope.seq == previous.envelope.seq + 1
        assert current.envelope.prev_record_sha256 == previous.record_sha256
        assert current.envelope.blackout_id == "blackout-a"
    assert len(chain) <= 104


def test_actual_maximum_profile_chain_and_tail_have_margin(
    observation_factory, frozen_snapshot
) -> None:
    derived, terminal = _maximum_objects(observation_factory, frozen_snapshot)
    _assert_continuous_chain(derived, terminal)
    proof = prove_tail_budget(derived, terminal)
    assert len(derived) == 93
    assert sum(item.envelope.record_type == "fragment_profile" for item in derived) == 88
    assert len(terminal) == 3
    assert proof.derived_record_count == 93
    assert proof.derived_record_count <= 128
    assert proof.descriptor_count == 256
    assert proof.total_bytes <= 2 * 1024 * 1024
    assert proof.margin_bytes > 0
    assert all(len(record.line) <= 8 * 1024 for record in (*derived, *terminal))
    assert max(len(record.line) for record in (*derived, *terminal)) == 8175


def test_receipt_absent_tail_rebuilds_valid_chain(observation_factory, frozen_snapshot) -> None:
    derived, terminal = _maximum_objects(
        observation_factory,
        frozen_snapshot,
        include_receipt=False,
    )
    _assert_continuous_chain(derived, terminal)
    proof = prove_tail_budget(derived, terminal)
    assert len(derived) == 92
    assert proof.descriptor_count == 256
    assert terminal[-1].envelope.payload["receipt_record_hash"] is None


def test_budget_rejects_129_derived_records(observation_factory, frozen_snapshot) -> None:
    derived, terminal = _maximum_objects(observation_factory, frozen_snapshot)
    with pytest.raises(ValueError, match="derived record budget"):
        prove_tail_budget(derived * 5, terminal)


def test_budget_derives_descriptor_count_and_requires_actual_records() -> None:
    with pytest.raises(TypeError):
        prove_tail_budget(("not-a-record",), ())  # type: ignore[arg-type]
