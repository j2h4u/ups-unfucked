"""Cross-consumer proof for one censored, modeled-safe-shutdown event."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from src.adapters.jsonl_v3_fragment_profile_codec import (
    decode_fragment_profile_records,
    encode_fragment_profiles,
    reconstruct_fragment_profiles,
)
from src.domain.blackout_terminal import BlackoutTermination
from src.domain.curve_assessment import CurveDisposition, assess_curve
from src.domain.firmware_lb_assessment import (
    FirmwareLbDisposition,
    assess_firmware_lb,
)
from src.domain.fragment_policy import DEFAULT_DISCHARGE_FRAGMENT_POLICY
from src.domain.fragments import (
    AnchorKind,
    AnchorProvenance,
    CanonicalDischargeSample,
    DischargeFragmentProfile,
    DischargeSlice,
    EndpointAnchor,
    ObservationOrigin,
    ReadinessProvenance,
    StartReadinessContext,
)
from src.domain.load_sag_assessment import (
    DEFAULT_LOAD_SAG_POLICY,
    LoadSagDisposition,
    assess_load_sag,
)
from src.domain.terminal_outcome import TerminalOutcome, TerminalOutcomeKind
from tests.adapters.test_jsonl_v3_load_sag_assessment_codec import _context


def _censored_profile(observation_factory) -> DischargeFragmentProfile:
    observations = tuple(
        observation_factory(
            index,
            voltage_v=13.2 - 0.3 * index / 120,
            raw_status="OB DISCHRG LB" if index == 100 else "OB DISCHRG",
        )
        for index in range(121)
    )
    samples = tuple(
        CanonicalDischargeSample(
            index,
            hashlib.sha256(f"safe-shutdown-sample-{index}".encode()).hexdigest(),
            observation,
        )
        for index, observation in enumerate(observations)
    )
    terminal = EndpointAnchor(
        hashlib.sha256(b"modeled-safe-shutdown").hexdigest(),
        AnchorKind.MODELED_SAFE_SHUTDOWN,
        AnchorProvenance.MODELED,
        "boot-a",
        observations[-1].wall_time_utc,
        observations[-1].monotonic_ns,
        samples[-1].canonical_hash,
        "blackout-a",
        "episode-a",
        "segment-a",
    )
    slice_value = DischargeSlice(
        samples=samples,
        blackout_id="blackout-a",
        physical_episode_id="episode-a",
        battery_epoch_id="epoch-a",
        segment_id="segment-a",
        origin=ObservationOrigin.NATURAL,
        policy_revision=DEFAULT_DISCHARGE_FRAGMENT_POLICY.revision,
        end_anchor=terminal,
        readiness_context=StartReadinessContext(
            False,
            "partial-start",
            ReadinessProvenance.OPERATIONAL,
        ),
    )
    return DischargeFragmentProfile(
        (terminal,),
        (slice_value,),
        (),
        slice_value.policy_revision,
    )


def test_same_decoded_censored_profile_has_independent_consumer_outcomes(
    observation_factory, frozen_snapshot
) -> None:
    profile = _censored_profile(observation_factory)
    raw_samples_by_slice = {profile.slices[0].slice_id: profile.slices[0].samples}
    records = encode_fragment_profiles((profile,), raw_samples_by_slice)
    decoded_records = decode_fragment_profile_records(record.line for record in records)
    (profile,) = reconstruct_fragment_profiles(decoded_records, raw_samples_by_slice)
    assert any(
        record.envelope.payload["chunk_kind"] == "anchor_chunk" for record in decoded_records
    )

    load = assess_load_sag(profile, _context(profile), DEFAULT_LOAD_SAG_POLICY)
    curve = assess_curve(profile, frozen_snapshot)[0]
    firmware = assess_firmware_lb(profile)

    assert load.disposition is LoadSagDisposition.REFUSED
    assert curve.disposition is CurveDisposition.ADMITTED
    assert firmware.disposition is FirmwareLbDisposition.DIAGNOSTIC_ONLY
    assert firmware.comparable is False

    forbidden_claim_names = ("capacity", "runtime", "soh", "empty_battery", "full_battery")
    consumer_text = " ".join(repr(value).lower() for value in (load, curve, firmware))
    assert not any(name in consumer_text for name in forbidden_claim_names)

    summary_hashes = tuple(
        hashlib.sha256(repr(value).encode()).hexdigest() for value in (load, curve, firmware)
    )
    outcome = TerminalOutcome(
        outcome_id="outcome-safe-shutdown",
        blackout_id="blackout-a",
        physical_episode_id="episode-a",
        battery_epoch_id="epoch-a",
        segment_id="segment-a",
        kind=TerminalOutcomeKind.ASSESSED,
        termination=BlackoutTermination.SAFE_SHUTDOWN_RESTARTED,
        ended_at_utc=datetime(2026, 8, 16, tzinfo=timezone.utc),
        raw_record_count=len(decoded_records),
        raw_sample_count=121,
        blackout_end_hash=hashlib.sha256(b"blackout-end").hexdigest(),
        consumer_summary_hashes=summary_hashes,
        decision_record_hash=hashlib.sha256(b"learning-decision").hexdigest(),
        receipt_record_hash=None,
    )
    assert outcome.kind is TerminalOutcomeKind.ASSESSED
    assert outcome.termination is BlackoutTermination.SAFE_SHUTDOWN_RESTARTED
