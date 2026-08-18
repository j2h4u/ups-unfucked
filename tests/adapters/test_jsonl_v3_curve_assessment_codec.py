"""Permanent curve assessment summary codec tests."""

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
from src.adapters.jsonl_v3_curve_assessment_codec import (
    CURVE_SUMMARY_RECORD_TYPE,
    MAX_CURVE_SUMMARY_LINE_BYTES,
    decode_curve_assessment_summary,
    encode_curve_assessment_summary,
    verify_curve_assessment_summary,
)
from src.adapters.jsonl_v3_fragment_profile_codec import encode_fragment_profiles
from src.domain.curve_assessment import assess_curve
from src.domain.fragments import build_discharge_fragment_profiles
from tests.domain.test_fragments import _many_profile_inputs

pytest_plugins = ("tests.domain.conftest",)


def _assessment_chain(frozen_snapshot, count=1):
    anchors, slices, steps = _many_profile_inputs(count)
    profiles = build_discharge_fragment_profiles(anchors, slices, steps, slices[0].policy_revision)
    raw_samples = {
        item.slice_id: tuple(item.samples) for profile in profiles for item in profile.slices
    }
    records = encode_fragment_profiles(profiles, raw_samples)
    assessments = tuple(
        assessment for profile in profiles for assessment in assess_curve(profile, frozen_snapshot)
    )
    return assessments, records, raw_samples


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


def test_summary_is_canonical_deterministic_and_replayable(observation_factory, frozen_snapshot):
    assessments, records, raw_samples = _assessment_chain(frozen_snapshot, 3)
    encoded = encode_curve_assessment_summary(
        assessments, records, raw_samples_by_slice=raw_samples
    )
    assert encoded.envelope.record_type == CURVE_SUMMARY_RECORD_TYPE
    assert encoded.envelope.payload["result_count"] == 3
    assert decode_curve_assessment_summary(encoded.line).line == encoded.line
    verify_curve_assessment_summary(encoded, assessments, records, raw_samples_by_slice=raw_samples)
    with pytest.raises(V3CodecError):
        encode_curve_assessment_summary(
            tuple(reversed(assessments)), records, raw_samples_by_slice=raw_samples
        )


def test_mutated_or_missing_result_is_rejected_by_replay(observation_factory, frozen_snapshot):
    assessments, records, raw_samples = _assessment_chain(frozen_snapshot, 2)
    encoded = encode_curve_assessment_summary(
        assessments, records, raw_samples_by_slice=raw_samples
    )
    mutated = replace(assessments[0], policy_revision="other-policy")
    with pytest.raises(ValueError):
        verify_curve_assessment_summary(
            encoded, (mutated, assessments[1]), records, raw_samples_by_slice=raw_samples
        )
    with pytest.raises(ValueError):
        verify_curve_assessment_summary(
            encoded, (assessments[0],), records, raw_samples_by_slice=raw_samples
        )


def test_max_256_result_summary_is_compact(observation_factory, frozen_snapshot):
    assessments, records, raw_samples = _assessment_chain(frozen_snapshot, 72)
    encoded = encode_curve_assessment_summary(
        assessments, records, raw_samples_by_slice=raw_samples
    )
    assert len(encoded.line) < MAX_CURVE_SUMMARY_LINE_BYTES
    verify_curve_assessment_summary(encoded, assessments, records, raw_samples_by_slice=raw_samples)


@pytest.mark.parametrize("mutation", ("unknown", "schema", "counts", "hash"))
def test_summary_rejects_strict_adversarial_payload(observation_factory, frozen_snapshot, mutation):
    assessments, records, raw_samples = _assessment_chain(frozen_snapshot)
    encoded = encode_curve_assessment_summary(
        assessments, records, raw_samples_by_slice=raw_samples
    )
    payload = dict(encoded.envelope.payload)
    if mutation == "unknown":
        payload["unexpected"] = True
        raw = _rehash(encoded, payload)
    elif mutation == "schema":
        payload["schema"] = "curve-assessment-summary-v2"
        raw = _rehash(encoded, payload)
    elif mutation == "counts":
        payload["disposition_counts"] = dict(payload["disposition_counts"], admitted=2)
        raw = _rehash(encoded, payload)
    else:
        payload["ordered_results_sha256"] = "0" * 64
        raw = _rehash(encoded, payload)
    if mutation == "hash":
        with pytest.raises(ValueError):
            verify_curve_assessment_summary(
                decode_curve_assessment_summary(raw),
                assessments,
                records,
                raw_samples_by_slice=raw_samples,
            )
    else:
        with pytest.raises(ValueError):
            decode_curve_assessment_summary(raw)


def test_noncanonical_and_oversize_lines_are_rejected(observation_factory, frozen_snapshot):
    assessments, records, raw_samples = _assessment_chain(frozen_snapshot)
    encoded = encode_curve_assessment_summary(
        assessments, records, raw_samples_by_slice=raw_samples
    )
    noncanonical = json.dumps(json.loads(encoded.line), indent=2).encode() + b"\n"
    with pytest.raises(ValueError):
        decode_curve_assessment_summary(noncanonical)
    with pytest.raises(ValueError):
        decode_curve_assessment_summary(encoded.line + b"x" * MAX_CURVE_SUMMARY_LINE_BYTES)


def test_profile_links_are_derived_and_scope_bound(frozen_snapshot):
    assessments, records, raw_samples = _assessment_chain(frozen_snapshot, 2)
    with pytest.raises(ValueError):
        encode_curve_assessment_summary(
            assessments,
            cast(tuple[EncodedV3Record, ...], (records[0].record_sha256,)),
            raw_samples_by_slice=raw_samples,
        )
    with pytest.raises(ValueError):
        verify_curve_assessment_summary(
            encode_curve_assessment_summary(assessments, records, raw_samples_by_slice=raw_samples),
            (replace(assessments[0], profile_series_id="0" * 64),) + assessments[1:],
            records,
            raw_samples_by_slice=raw_samples,
        )
