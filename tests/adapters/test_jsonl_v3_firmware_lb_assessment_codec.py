"""Permanent firmware-LB assessment summary codec tests."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import cast

import pytest

from src.adapters.jsonl_v3_canonical import (
    EncodedV3Record,
    V3CodecError,
    V3RecordEnvelope,
    encode_v3_record,
)
from src.adapters.jsonl_v3_firmware_lb_assessment_codec import (
    FIRMWARE_LB_SUMMARY_RECORD_TYPE,
    MAX_FIRMWARE_LB_SUMMARY_LINE_BYTES,
    decode_firmware_lb_assessment_summary,
    encode_firmware_lb_assessment_summary,
    verify_firmware_lb_assessment_summary,
)
from src.adapters.jsonl_v3_fragment_profile_codec import encode_fragment_profiles
from src.domain.firmware_lb_assessment import assess_firmware_lb
from src.domain.fragments import build_discharge_fragment_profiles
from tests.domain.test_fragments import _many_profile_inputs

pytest_plugins = ("tests.domain.conftest",)


def _assessment_chain(slice_count=1):
    anchors, slices, steps = _many_profile_inputs(slice_count)
    profiles = build_discharge_fragment_profiles(anchors, slices, steps, slices[0].policy_revision)
    raw_samples = {
        item.slice_id: tuple(item.samples) for profile in profiles for item in profile.slices
    }
    records = encode_fragment_profiles(profiles, raw_samples)
    return tuple(assess_firmware_lb(profile) for profile in profiles), records, raw_samples


def _rehash(encoded, payload):
    envelope = encoded.envelope
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


def test_summary_is_canonical_deterministic_and_replayable(observation_factory):
    assessments, records, raw_samples = _assessment_chain(33)
    encoded = encode_firmware_lb_assessment_summary(
        assessments, records, raw_samples_by_slice=raw_samples
    )
    assert encoded.envelope.record_type == FIRMWARE_LB_SUMMARY_RECORD_TYPE
    assert encoded.envelope.payload["result_count"] == 3
    assert decode_firmware_lb_assessment_summary(encoded.line).line == encoded.line
    verify_firmware_lb_assessment_summary(
        encoded, assessments, records, raw_samples_by_slice=raw_samples
    )
    with pytest.raises(V3CodecError):
        encode_firmware_lb_assessment_summary(
            tuple(reversed(assessments)), records, raw_samples_by_slice=raw_samples
        )


def test_mutated_or_missing_result_is_rejected_by_replay(observation_factory):
    assessments, records, raw_samples = _assessment_chain(17)
    encoded = encode_firmware_lb_assessment_summary(
        assessments, records, raw_samples_by_slice=raw_samples
    )
    mutated = replace(assessments[0], policy_revision="other-policy")
    with pytest.raises(ValueError):
        verify_firmware_lb_assessment_summary(
            encoded, (mutated, assessments[1]), records, raw_samples_by_slice=raw_samples
        )
    with pytest.raises(ValueError):
        verify_firmware_lb_assessment_summary(
            encoded, (assessments[0],), records, raw_samples_by_slice=raw_samples
        )


def test_max_profile_chain_summary_is_compact(observation_factory):
    assessments, records, raw_samples = _assessment_chain(72)
    encoded = encode_firmware_lb_assessment_summary(
        assessments, records, raw_samples_by_slice=raw_samples
    )
    assert len(encoded.line) < MAX_FIRMWARE_LB_SUMMARY_LINE_BYTES
    verify_firmware_lb_assessment_summary(
        encoded, assessments, records, raw_samples_by_slice=raw_samples
    )


@pytest.mark.parametrize("mutation", ("unknown", "schema", "counts", "hash"))
def test_summary_rejects_strict_adversarial_payload(observation_factory, mutation):
    assessments, records, raw_samples = _assessment_chain()
    encoded = encode_firmware_lb_assessment_summary(
        assessments, records, raw_samples_by_slice=raw_samples
    )
    payload = dict(encoded.envelope.payload)
    if mutation == "unknown":
        payload["unexpected"] = True
        raw = _rehash(encoded, payload)
    elif mutation == "schema":
        payload["schema"] = "firmware-lb-assessment-summary-v2"
        raw = _rehash(encoded, payload)
    elif mutation == "counts":
        payload["disposition_counts"] = dict(payload["disposition_counts"], comparable=2)
        raw = _rehash(encoded, payload)
    else:
        payload["ordered_results_sha256"] = "0" * 64
        raw = _rehash(encoded, payload)
    if mutation == "hash":
        with pytest.raises(ValueError):
            verify_firmware_lb_assessment_summary(
                decode_firmware_lb_assessment_summary(raw),
                assessments,
                records,
                raw_samples_by_slice=raw_samples,
            )
    else:
        with pytest.raises(ValueError):
            decode_firmware_lb_assessment_summary(raw)


def test_noncanonical_and_oversize_lines_are_rejected(observation_factory):
    assessments, records, raw_samples = _assessment_chain()
    encoded = encode_firmware_lb_assessment_summary(
        assessments, records, raw_samples_by_slice=raw_samples
    )
    noncanonical = json.dumps(json.loads(encoded.line), indent=2).encode() + b"\n"
    with pytest.raises(ValueError):
        decode_firmware_lb_assessment_summary(noncanonical)
    with pytest.raises(ValueError):
        decode_firmware_lb_assessment_summary(
            encoded.line + b"x" * MAX_FIRMWARE_LB_SUMMARY_LINE_BYTES
        )


def test_profile_links_are_derived_and_scope_bound():
    assessments, records, raw_samples = _assessment_chain(17)
    with pytest.raises(ValueError):
        encode_firmware_lb_assessment_summary(
            assessments,
            cast(tuple[EncodedV3Record, ...], (records[0].record_sha256,)),
            raw_samples_by_slice=raw_samples,
        )
    with pytest.raises(ValueError):
        verify_firmware_lb_assessment_summary(
            encode_firmware_lb_assessment_summary(
                assessments, records, raw_samples_by_slice=raw_samples
            ),
            (replace(assessments[0], profile_series_id="0" * 64),) + assessments[1:],
            records,
            raw_samples_by_slice=raw_samples,
        )


@pytest.mark.parametrize("mutation", ("missing", "extra", "reordered"))
def test_assessment_slice_ids_must_equal_complete_logical_profile(
    mutation: str,
) -> None:
    assessments, records, raw_samples = _assessment_chain(17)
    original = assessments[0]
    if mutation == "missing":
        changed_ids = original.slice_ids[:-1]
    elif mutation == "extra":
        changed_ids = (*original.slice_ids, "f" * 64)
    else:
        changed_ids = tuple(reversed(original.slice_ids))
    changed = replace(original, slice_ids=changed_ids)
    values = (changed, *assessments[1:])
    with pytest.raises(ValueError, match="canonical profile order"):
        encode_firmware_lb_assessment_summary(values, records, raw_samples_by_slice=raw_samples)
