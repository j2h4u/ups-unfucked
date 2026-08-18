"""Strict permanent single-series summary tests for load-sag assessment."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from src.adapters.jsonl_v3_canonical import V3CodecError, decode_v3_record, encode_v3_record
from src.adapters.jsonl_v3_fragment_profile_codec import (
    decode_fragment_profile_records,
    encode_fragment_profiles,
)
from src.adapters.jsonl_v3_load_sag_assessment_codec import (
    LOAD_SAG_SUMMARY_MAX_LINE_BYTES,
    LOAD_SAG_SUMMARY_PROVENANCE,
    LOAD_SAG_SUMMARY_RECORD_TYPE,
    decode_load_sag_assessment_summary,
    encode_load_sag_assessment_summary,
    ordered_load_sag_results_sha256,
    source_profile_record_hashes_sha256,
)
from src.domain.fragment_policy import DEFAULT_DISCHARGE_FRAGMENT_POLICY
from src.domain.fragments import (
    DischargeFragmentProfile,
    ObservationOrigin,
)
from src.domain.load_sag_assessment import (
    DEFAULT_LOAD_SAG_POLICY,
    LoadSagAssessment,
    LoadSagAssessmentContext,
    assess_load_sag,
)
from tests.domain.test_load_sag_assessment import _slice, _step


def _context(profile: DischargeFragmentProfile) -> LoadSagAssessmentContext:
    return LoadSagAssessmentContext(
        battery_epoch_id=profile.slices[0].battery_epoch_id,
        policy_revision=DEFAULT_LOAD_SAG_POLICY.revision,
        evaluator_revision=DEFAULT_LOAD_SAG_POLICY.evaluator_revision,
        origin=profile.slices[0].origin,
        fragment_policy_revision=DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision,
    )


def _assessment_at(
    offset: int = 0,
    *,
    origin: ObservationOrigin = ObservationOrigin.NATURAL,
    uat: str | None = None,
) -> tuple[DischargeFragmentProfile, LoadSagAssessment]:
    parent = _slice(offset=offset, origin=origin, uat=uat)
    step = _step(parent, step_id=f"step-{offset}")
    profile = DischargeFragmentProfile(
        anchors=(),
        slices=(parent,),
        load_steps=(step,),
        policy_revision=DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision,
    )
    return profile, assess_load_sag(profile, _context(profile), DEFAULT_LOAD_SAG_POLICY)


def _series(
    count: int = 3,
) -> tuple[tuple[LoadSagAssessment, ...], tuple, dict[str, tuple]]:
    values = tuple(_assessment_at(index * 2) for index in range(count))
    profiles = tuple(
        replace(profile, ordinal=ordinal, record_count=count)
        for ordinal, (profile, _) in enumerate(values)
    )
    raw_samples = {
        item.slice_id: tuple(item.samples) for profile, _ in values for item in profile.slices
    }
    records = encode_fragment_profiles(profiles, raw_samples)
    return (
        tuple(item[1] for item in values),
        decode_fragment_profile_records(record.line for record in records),
        raw_samples,
    )


def _maximum_series() -> tuple[tuple[LoadSagAssessment, ...], tuple, dict[str, tuple]]:
    slices = tuple(_slice(offset=index * 2) for index in range(96))
    profiles = tuple(
        replace(
            DischargeFragmentProfile(
                anchors=(),
                slices=slices[start : start + 16],
                load_steps=(),
                policy_revision=DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision,
            ),
            ordinal=ordinal,
            record_count=6,
        )
        for ordinal, start in enumerate(range(0, 96, 16))
    )
    raw_samples = {item.slice_id: tuple(item.samples) for item in slices}
    records = decode_fragment_profile_records(
        record.line for record in encode_fragment_profiles(profiles, raw_samples)
    )
    results: list[LoadSagAssessment] = []
    for profile in profiles:
        assessment = assess_load_sag(profile, _context(profile), DEFAULT_LOAD_SAG_POLICY)
        for parent in profile.slices:
            results.append(
                replace(
                    assessment,
                    source_slice_hashes=(parent.slice_id,),
                    source_step_hashes=(),
                    admitted_steps=(),
                    refusals=(),
                    step_count=0,
                    step_overflow_count=0,
                    first_unprofiled_step_hash=None,
                )
            )
    return tuple(results), records, raw_samples


def _mutated_line(
    results: tuple[LoadSagAssessment, ...],
    profile_records: tuple,
    raw_samples_by_slice: dict[str, tuple],
    *,
    payload: dict[str, object] | None = None,
    **changes: object,
) -> bytes:
    encoded = encode_load_sag_assessment_summary(
        results,
        profile_records,
        raw_samples_by_slice=raw_samples_by_slice,
    )
    values = dict(encoded.envelope.payload)
    values.update(payload or {})
    mutated = replace(encoded.envelope, payload=values, record_sha256=None, **changes)
    return encode_v3_record(mutated).line


def test_summary_round_trip_recomputes_complete_ordered_series() -> None:
    results, profile_records, raw_samples = _series()
    encoded = encode_load_sag_assessment_summary(
        results,
        profile_records,
        raw_samples_by_slice=raw_samples,
        seq=7,
    )
    decoded = decode_load_sag_assessment_summary(
        encoded.line,
        results,
        profile_records,
        raw_samples_by_slice=raw_samples,
    )

    assert encoded.envelope.record_type == LOAD_SAG_SUMMARY_RECORD_TYPE
    assert encoded.envelope.provenance == LOAD_SAG_SUMMARY_PROVENANCE
    assert len(encoded.line) <= LOAD_SAG_SUMMARY_MAX_LINE_BYTES
    assert decoded.result_count == len(results)
    assert decoded.ordered_results_sha256 == ordered_load_sag_results_sha256(results)
    assert decoded.source_profile_record_hashes_sha256 == source_profile_record_hashes_sha256(
        profile_records, raw_samples_by_slice=raw_samples
    )
    assert (
        encoded.line
        == encode_load_sag_assessment_summary(
            results,
            profile_records,
            raw_samples_by_slice=raw_samples,
            seq=7,
        ).line
    )


def test_uat_summary_retains_diagnostic_scope() -> None:
    profile, assessment = _assessment_at(origin=ObservationOrigin.UAT, uat="uat-a")
    raw_samples = {item.slice_id: tuple(item.samples) for item in profile.slices}
    profile_records = decode_fragment_profile_records(
        record.line for record in encode_fragment_profiles((profile,), raw_samples)
    )
    encoded = encode_load_sag_assessment_summary(
        (assessment,),
        profile_records,
        raw_samples_by_slice=raw_samples,
        uat_intent_id="uat-a",
    )
    decoded = decode_load_sag_assessment_summary(
        encoded.line,
        (assessment,),
        profile_records,
        raw_samples_by_slice=raw_samples,
        uat_intent_id="uat-a",
    )

    assert decoded.observation_origin is ObservationOrigin.UAT
    assert decoded.uat_intent_id == "uat-a"


def test_changed_or_missing_concrete_result_is_rejected_by_recompute() -> None:
    results, profile_records, raw_samples = _series()
    encoded = encode_load_sag_assessment_summary(
        results,
        profile_records,
        raw_samples_by_slice=raw_samples,
    )
    changed = _assessment_at(8)[1]
    with pytest.raises(V3CodecError):
        decode_load_sag_assessment_summary(
            encoded.line,
            (results[0], changed, results[2]),
            profile_records,
            raw_samples_by_slice=raw_samples,
        )
    with pytest.raises(V3CodecError):
        decode_load_sag_assessment_summary(
            encoded.line,
            results[:-1],
            profile_records,
            raw_samples_by_slice=raw_samples,
        )
    with pytest.raises(V3CodecError):
        decode_load_sag_assessment_summary(
            encoded.line,
            results,
            profile_records[:-1],
            raw_samples_by_slice=raw_samples,
        )


def test_results_must_be_in_profile_physical_order() -> None:
    results, profile_records, raw_samples = _series()
    with pytest.raises(V3CodecError):
        encode_load_sag_assessment_summary(
            tuple(reversed(results)), profile_records, raw_samples_by_slice=raw_samples
        )


def test_result_and_profile_scope_mismatches_are_rejected() -> None:
    results, profile_records, raw_samples = _series()
    with pytest.raises(V3CodecError):
        encode_load_sag_assessment_summary(
            results, tuple(reversed(profile_records)), raw_samples_by_slice=raw_samples
        )
    with pytest.raises(V3CodecError):
        encode_load_sag_assessment_summary(
            (results[0], replace(results[1], source_segment_id="segment-other"), results[2]),
            profile_records,
            raw_samples_by_slice=raw_samples,
        )
    with pytest.raises(V3CodecError):
        encode_load_sag_assessment_summary(
            (
                results[0],
                replace(
                    results[1],
                    source_step_hashes=(results[2].source_step_hashes[0],),
                ),
                results[2],
            ),
            profile_records,
            raw_samples_by_slice=raw_samples,
        )
    profile = profile_records[0]
    payload = dict(profile.envelope.payload)
    payload["blackout_id"] = "blackout-other"
    changed = encode_v3_record(replace(profile.envelope, payload=payload, record_sha256=None))
    changed_records = (decode_v3_record(changed.line), *profile_records[1:])
    with pytest.raises(V3CodecError):
        encode_load_sag_assessment_summary(
            results, changed_records, raw_samples_by_slice=raw_samples
        )


def test_caller_supplied_profile_hashes_are_not_accepted() -> None:
    results, profile_records, raw_samples = _series()
    with pytest.raises(V3CodecError):
        encode_load_sag_assessment_summary(
            results,
            profile_records,
            raw_samples_by_slice=raw_samples,
            source_profile_record_hashes=("0" * 64,) * len(profile_records),
        )


@pytest.mark.parametrize(
    "mutator",
    (
        lambda results, records, raw: b'{"schema_version":2}\n',
        lambda results, records, raw: _mutated_line(
            results, records, raw, record_type="other_record"
        ),
        lambda results, records, raw: _mutated_line(results, records, raw, provenance="raw"),
        lambda results, records, raw: _mutated_line(
            results, records, raw, payload={"observation_origin": "future"}
        ),
        lambda results, records, raw: _mutated_line(
            results, records, raw, payload={"policy_revision": "load-sag-v2"}
        ),
        lambda results, records, raw: _mutated_line(
            results, records, raw, payload={"reason_overflow_count": 1}
        ),
        lambda results, records, raw: _mutated_line(
            results,
            records,
            raw,
            payload={"disposition_counts": {"admitted": 3, "diagnostic": 0}},
        ),
        lambda results, records, raw: _mutated_line(results, records, raw, blackout_id="other"),
    ),
)
def test_decode_rejects_schema_enum_policy_count_and_scope_mutations(mutator) -> None:
    results, profile_records, raw_samples = _series()
    with pytest.raises(V3CodecError):
        decode_load_sag_assessment_summary(
            mutator(results, profile_records, raw_samples),
            results,
            profile_records,
            raw_samples_by_slice=raw_samples,
        )


def test_decode_rejects_unknown_payload_field_and_noncanonical_line() -> None:
    results, profile_records, raw_samples = _series()
    encoded = encode_load_sag_assessment_summary(
        results,
        profile_records,
        raw_samples_by_slice=raw_samples,
    )
    value = json.loads(encoded.line)
    value["payload"]["unknown"] = True
    unknown = json.dumps(value, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    with pytest.raises(V3CodecError):
        decode_load_sag_assessment_summary(
            unknown,
            results,
            profile_records,
            raw_samples_by_slice=raw_samples,
        )

    with pytest.raises(V3CodecError):
        decode_load_sag_assessment_summary(
            encoded.line[:-1] + b" ",
            results,
            profile_records,
            raw_samples_by_slice=raw_samples,
        )


def test_maximum_256_result_summary_is_bounded_and_complete() -> None:
    results, profile_records, raw_samples = _maximum_series()
    encoded = encode_load_sag_assessment_summary(
        results,
        profile_records,
        raw_samples_by_slice=raw_samples,
    )
    decoded = decode_load_sag_assessment_summary(
        encoded.line,
        results,
        profile_records,
        raw_samples_by_slice=raw_samples,
    )

    assert decoded.result_count == 96
    assert len(profile_records) <= 128
    assert len(encoded.line) <= LOAD_SAG_SUMMARY_MAX_LINE_BYTES


def test_257_results_or_descriptors_are_rejected() -> None:
    results, profile_records, raw_samples = _maximum_series()
    extra_result = _assessment_at(512)[1]
    with pytest.raises(V3CodecError):
        encode_load_sag_assessment_summary(
            (*results, extra_result),
            profile_records,
            raw_samples_by_slice=raw_samples,
        )
    with pytest.raises(V3CodecError):
        encode_load_sag_assessment_summary(
            results,
            profile_records + profile_records[:1],
            raw_samples_by_slice=raw_samples,
        )


def test_natural_summary_rejects_uat_intent() -> None:
    results, profile_records, raw_samples = _series(1)
    with pytest.raises(V3CodecError):
        encode_load_sag_assessment_summary(
            results,
            profile_records,
            raw_samples_by_slice=raw_samples,
            uat_intent_id="unexpected",
        )
