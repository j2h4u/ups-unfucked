"""Current-algorithm decline reporting over bounded sealed raw evidence."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest

from src.application import decline_reporting
from src.application.decline_reporting import decline_statuses
from src.application.errors import StoragePortCorruption
from src.application.storage_values import EventProjection, EventSummary, ProjectedEventRecord
from src.domain.decline import FirmwareReserveSample, LoadSagTrendSample, LongPartialSample
from src.domain.reasons import DeclineReason
from src.domain.values import DeclineVerdict, PhysicalObservation

NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)


class _ProjectionStore:
    def __init__(self, projections: dict[str, EventProjection]) -> None:
        self._projections = projections

    def project(self, event_ref):
        return self._projections[event_ref.blackout_id]


class _CorruptProjectionStore:
    def project(self, _event_ref):
        raise StoragePortCorruption("corrupt sealed event")


def _record(
    blackout_id: str,
    *,
    seq: int,
    record_type: str,
    payload: dict,
) -> ProjectedEventRecord:
    raw_monotonic = payload.get("monotonic_ns")
    monotonic_ns = (
        raw_monotonic
        if isinstance(raw_monotonic, int)
        else 650_000_000_000
        if record_type == "ir_estimate"
        else 0
    )
    return ProjectedEventRecord(
        record_type=record_type,
        provenance="derived" if record_type in {"ir_estimate", "outcome"} else "physical",
        blackout_id=blackout_id,
        segment_id=f"segment-{blackout_id}",
        seq=seq,
        boot_id="boot-a",
        wall_time_utc=(NOW + timedelta(seconds=monotonic_ns / 1_000_000_000))
        .isoformat()
        .replace("+00:00", "Z"),
        monotonic_ns=monotonic_ns,
        payload=payload,
    )


def _raw(
    second: int,
    *,
    quantum: float | None = 0.01,
    input_voltage_v: float = 0.0,
) -> dict:
    return {
        "boot_id": "boot-a",
        "monotonic_ns": second * 1_000_000_000,
        "wall_time_utc": (NOW + timedelta(seconds=second)).isoformat().replace("+00:00", "Z"),
        "raw_status": "OB DISCHRG",
        "battery_voltage_raw": "13.00",
        "battery_voltage_v": 13.0,
        "voltage_token_quantum_v": quantum,
        "load_percent": 20.0,
        "input_voltage_v": input_voltage_v,
    }


def _observation(
    second: int,
    *,
    boot_id: str = "boot-a",
    raw_status: str = "OB DISCHRG",
    battery_voltage_v: float | None = 13.4,
    load_percent: float | None = 20.0,
) -> PhysicalObservation:
    return PhysicalObservation(
        boot_id=boot_id,
        monotonic_ns=second * 1_000_000_000,
        wall_time_utc=NOW + timedelta(seconds=second),
        raw_status=raw_status,
        battery_voltage_raw=None if battery_voltage_v is None else f"{battery_voltage_v:.2f}",
        battery_voltage_v=battery_voltage_v,
        voltage_token_quantum_v=0.01,
        load_percent=load_percent,
        input_voltage_v=0.0,
    )


def _projection(
    blackout_id: str,
    *,
    malformed_quantum: bool,
    evidence_class: str = "qualifying",
    firmware_tail: bool | int = False,
    start_input_voltage_v: float = 0.0,
) -> EventProjection:
    start = _record(
        blackout_id,
        seq=0,
        record_type="start",
        payload={
            "battery_epoch_id": "epoch-a",
            "charge_readiness": {"ready": True},
            "observation": _raw(
                0,
                quantum=None if malformed_quantum else 0.01,
                input_voltage_v=start_input_voltage_v,
            ),
        },
    )
    calibration_at = firmware_tail if type(firmware_tail) is int else None
    observation_seconds = range(1, 111) if firmware_tail else range(5, 651, 5)
    observations = []
    for index, second in enumerate(observation_seconds, start=1):
        payload = _raw(second)
        if firmware_tail and calibration_at == second:
            payload["raw_status"] = "OB CAL"
        elif firmware_tail and second == 100:
            payload["raw_status"] = "OB DISCHRG LB"
        elif firmware_tail and second > 100:
            payload["boot_id"] = "boot-b"
            payload["monotonic_ns"] = (second - 101) * 1_000_000_000
            payload["raw_status"] = "COMMFAULT"
            payload["load_percent"] = None
        observations.append(
            _record(
                blackout_id,
                seq=index,
                record_type="observation",
                payload=payload,
            )
        )
    observations = tuple(observations)
    end = _record(
        blackout_id,
        seq=len(observations) + 1,
        record_type="end",
        payload={"termination": "power_restored"},
    )
    stored_estimate = _record(
        blackout_id,
        seq=len(observations) + 2,
        record_type="ir_estimate",
        payload={"quality": "qualifying", "k_settled_v_per_pp": 9.9},
    )
    outcome = _record(
        blackout_id,
        seq=len(observations) + 3,
        record_type="outcome",
        payload={
            "evidence_class": evidence_class,
            "decline_evidence_eligible": evidence_class == "qualifying",
        },
    )
    records = (start, *observations, end, stored_estimate, outcome)
    return EventProjection(
        start,
        observations,
        (),
        end,
        (stored_estimate,),
        outcome,
        (records,),
        records,
    )


def _with_termination(projection: EventProjection, termination: str) -> EventProjection:
    assert projection.end is not None
    end = replace(projection.end, payload={"termination": termination})
    records = tuple(end if record.record_type == "end" else record for record in projection.records)
    return replace(projection, end=end, records=records)


def _summary(
    blackout_id: str,
    day: int,
    *,
    evidence_class: str = "qualifying",
    termination: str | None = "power_restored",
) -> EventSummary:
    return EventSummary(
        blackout_id,
        f"event-{blackout_id}.jsonl",
        (NOW + timedelta(days=day)).isoformat().replace("+00:00", "Z"),
        None,
        termination,
        evidence_class,
        "recorded_only",
        650.0,
        131,
        "epoch-a",
        False,
        True,
        None,
    )


def test_decline_recomputes_raw_and_isolates_metric_specific_malformed_input() -> None:
    ids = tuple(f"{index + 1:032x}" for index in range(6))
    projections = {
        blackout_id: _projection(blackout_id, malformed_quantum=index == 0)
        for index, blackout_id in enumerate(ids)
    }

    load_sag, firmware, long_partial = decline_statuses(
        cast(Any, _ProjectionStore(projections)),
        tuple(_summary(blackout_id, index) for index, blackout_id in enumerate(ids)),
    )

    assert load_sag.verdict == DeclineVerdict.INSUFFICIENT_COMPARABLE_EVIDENCE
    assert firmware.verdict == DeclineVerdict.INSUFFICIENT_COMPARABLE_EVIDENCE
    assert long_partial.verdict == DeclineVerdict.STABLE_WITHIN_OBSERVED_EVIDENCE
    assert long_partial.event_ids == ids


def test_decline_surfaces_storage_corruption_distinctly_from_insufficient_history() -> None:
    statuses = decline_statuses(
        cast(Any, _CorruptProjectionStore()),
        (_summary("1" * 32, 0),),
    )

    assert tuple(status.metric for status in statuses) == (
        "load_sag_trend",
        "firmware_lb_reserve_proxy",
        "long_partial_curve",
    )
    assert all(
        status.reasons.values == (DeclineReason.EVIDENCE_STORAGE_CORRUPT,) for status in statuses
    )


def test_decline_selects_latest_six_per_metric_without_cross_metric_crowding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = tuple(f"{index + 1:032x}" for index in range(33))
    evidence = {}
    for index, blackout_id in enumerate(ids):
        started = NOW + timedelta(days=index)
        firmware = (
            (
                FirmwareReserveSample(
                    blackout_id,
                    started,
                    "epoch-a",
                    True,
                    13.4,
                    20.0,
                    0.0,
                    1.0,
                    1.0,
                    100.0,
                ),
            )
            if index < 6
            else ()
        )
        load_sag = (
            (LoadSagTrendSample(blackout_id, started, "epoch-a", index, 0.01),)
            if index >= 6
            else ()
        )
        long_partial = (
            (
                LongPartialSample(
                    blackout_id,
                    started,
                    "epoch-a",
                    True,
                    650.0,
                    13.4,
                    20.0,
                    0.0,
                    1.0,
                    1.0,
                    13.0,
                ),
            )
            if index >= 6
            else ()
        )
        evidence[blackout_id] = decline_reporting._EventDeclineEvidence(
            load_sag,
            firmware,
            long_partial,
        )

    monkeypatch.setattr(
        decline_reporting,
        "_event_decline_evidence",
        lambda _store, summary: evidence[summary.blackout_id],
    )
    statuses = decline_statuses(
        cast(Any, object()),
        tuple(_summary(blackout_id, index) for index, blackout_id in enumerate(ids)),
    )

    assert statuses[0].event_ids == ids[-6:]
    assert statuses[1].event_ids == ids[:6]
    assert statuses[2].event_ids == ids[-6:]


@pytest.mark.parametrize(
    "evidence_class",
    (
        "operational_only",
        "rejected",
    ),
)
def test_decline_recomputes_current_raw_evidence_for_nonqualifying_events(
    evidence_class: str,
) -> None:
    ids = tuple(f"{index + 1:032x}" for index in range(6))
    projections = {
        blackout_id: _projection(
            blackout_id,
            malformed_quantum=False,
            evidence_class=evidence_class,
        )
        for blackout_id in ids
    }
    summaries = tuple(
        _summary(
            blackout_id,
            index,
            evidence_class=evidence_class,
        )
        for index, blackout_id in enumerate(ids)
    )

    statuses = decline_statuses(cast(Any, _ProjectionStore(projections)), summaries)

    assert statuses[0].verdict == DeclineVerdict.INSUFFICIENT_COMPARABLE_EVIDENCE
    assert statuses[1].verdict == DeclineVerdict.INSUFFICIENT_COMPARABLE_EVIDENCE
    assert statuses[2].verdict == DeclineVerdict.STABLE_WITHIN_OBSERVED_EVIDENCE


def test_public_decline_statuses_recomputes_firmware_for_operational_event() -> None:
    ids = tuple(f"{index + 1:032x}" for index in range(6))
    projections = {
        blackout_id: _projection(
            blackout_id,
            malformed_quantum=False,
            evidence_class="operational_only",
            firmware_tail=True,
        )
        for blackout_id in ids
    }
    summaries = tuple(
        _summary(
            blackout_id,
            index,
            evidence_class="operational_only",
        )
        for index, blackout_id in enumerate(ids)
    )

    load_sag, firmware, long_partial = decline_statuses(
        cast(Any, _ProjectionStore(projections)), summaries
    )

    assert load_sag.verdict == DeclineVerdict.INSUFFICIENT_COMPARABLE_EVIDENCE
    assert firmware.verdict == DeclineVerdict.STABLE_WITHIN_OBSERVED_EVIDENCE
    assert firmware.event_ids == ids
    assert long_partial.verdict == DeclineVerdict.INSUFFICIENT_COMPARABLE_EVIDENCE


@pytest.mark.parametrize("termination", ("service_stop", "closed_restart_gap"))
def test_decline_excludes_non_restored_terminal_events_from_every_cohort(
    termination: str,
) -> None:
    ids = tuple(f"{index + 1:032x}" for index in range(6))
    projections = {
        blackout_id: _with_termination(
            _projection(blackout_id, malformed_quantum=False),
            termination,
        )
        for blackout_id in ids
    }
    summaries = tuple(
        _summary(blackout_id, index, termination=termination)
        for index, blackout_id in enumerate(ids)
    )

    statuses = decline_statuses(cast(Any, _ProjectionStore(projections)), summaries)

    assert all(
        status.verdict == DeclineVerdict.INSUFFICIENT_COMPARABLE_EVIDENCE for status in statuses
    )
    assert all(status.event_ids == () for status in statuses)


def test_public_decline_statuses_keeps_load_sag_before_bad_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = tuple(f"{index + 1:032x}" for index in range(6))
    projections = {
        blackout_id: _projection(
            blackout_id,
            malformed_quantum=False,
            evidence_class="operational_only",
            firmware_tail=True,
        )
        for blackout_id in ids
    }

    def load_sag(_context, observations):
        assert len(observations) == 101
        return (LoadSagTrendSample(_context.blackout_id, _context.started_utc, "epoch-a", 1, 0.01),)

    monkeypatch.setattr(decline_reporting, "_load_sag_samples", load_sag)
    summaries = tuple(
        _summary(blackout_id, index, evidence_class="operational_only")
        for index, blackout_id in enumerate(ids)
    )

    statuses = decline_statuses(cast(Any, _ProjectionStore(projections)), summaries)

    assert statuses[0].verdict == DeclineVerdict.STABLE_WITHIN_OBSERVED_EVIDENCE
    assert statuses[0].event_ids == ids


def test_public_decline_statuses_rejects_calibration_firmware_prefix() -> None:
    ids = tuple(f"{index + 1:032x}" for index in range(6))
    projections = {
        blackout_id: _projection(
            blackout_id,
            malformed_quantum=False,
            evidence_class="operational_only",
            firmware_tail=90,
        )
        for blackout_id in ids
    }
    summaries = tuple(
        _summary(blackout_id, index, evidence_class="operational_only")
        for index, blackout_id in enumerate(ids)
    )

    statuses = decline_statuses(cast(Any, _ProjectionStore(projections)), summaries)

    assert statuses[1].verdict == DeclineVerdict.INSUFFICIENT_COMPARABLE_EVIDENCE


@pytest.mark.parametrize(
    ("firmware_tail", "start_input_voltage_v"),
    ((30, 0.0), (True, 230.0)),
)
def test_firmware_decline_rejects_unnatural_provenance_before_origin(
    firmware_tail: bool | int,
    start_input_voltage_v: float,
) -> None:
    ids = tuple(f"{index + 1:032x}" for index in range(6))
    projections = {
        blackout_id: _projection(
            blackout_id,
            malformed_quantum=False,
            evidence_class="operational_only",
            firmware_tail=firmware_tail,
            start_input_voltage_v=start_input_voltage_v,
        )
        for blackout_id in ids
    }

    statuses = decline_statuses(
        cast(Any, _ProjectionStore(projections)),
        tuple(
            _summary(blackout_id, index, evidence_class="operational_only")
            for index, blackout_id in enumerate(ids)
        ),
    )

    assert statuses[1].verdict == DeclineVerdict.INSUFFICIENT_COMPARABLE_EVIDENCE


def test_calibration_event_does_not_poison_six_later_natural_firmware_events() -> None:
    ids = tuple(f"{index + 1:032x}" for index in range(7))
    projections = {
        blackout_id: _projection(
            blackout_id,
            malformed_quantum=False,
            evidence_class="operational_only",
            firmware_tail=90 if index == 0 else True,
        )
        for index, blackout_id in enumerate(ids)
    }

    statuses = decline_statuses(
        cast(Any, _ProjectionStore(projections)),
        tuple(
            _summary(blackout_id, index, evidence_class="operational_only")
            for index, blackout_id in enumerate(ids)
        ),
    )

    assert statuses[1].verdict == DeclineVerdict.STABLE_WITHIN_OBSERVED_EVIDENCE
    assert statuses[1].event_ids == ids[1:]


def test_decline_uses_current_raw_evidence_when_terminal_class_disagrees() -> None:
    ids = tuple(f"{index + 1:032x}" for index in range(6))
    projections = {
        blackout_id: _projection(
            blackout_id,
            malformed_quantum=False,
            evidence_class="operational_only" if index == 0 else "qualifying",
        )
        for index, blackout_id in enumerate(ids)
    }

    statuses = decline_statuses(
        cast(Any, _ProjectionStore(projections)),
        tuple(_summary(blackout_id, index) for index, blackout_id in enumerate(ids)),
    )

    assert statuses[0].verdict == DeclineVerdict.INSUFFICIENT_COMPARABLE_EVIDENCE
    assert statuses[1].verdict == DeclineVerdict.INSUFFICIENT_COMPARABLE_EVIDENCE
    assert statuses[2].verdict == DeclineVerdict.STABLE_WITHIN_OBSERVED_EVIDENCE


def test_firmware_sample_uses_trusted_origin_through_first_raw_lb() -> None:
    observations = tuple(
        _observation(second, raw_status="OB DISCHRG LB" if second == 100 else "OB DISCHRG")
        for second in range(111)
    )
    context = decline_reporting._EventContext("event-a", "segment-a", NOW, "epoch-a")

    sample = decline_reporting._firmware_sample(context, True, observations)

    assert sample is not None
    assert sample.ready_at_start is True
    assert sample.start_voltage_v == 13.4
    assert sample.mean_load_percent == 20.0
    assert sample.load_stddev_percent == 0.0
    assert sample.coverage_ratio == 1.0
    assert sample.max_gap_s == 1.0
    assert sample.reserve_proxy_pp_s == 500.0


def test_firmware_sample_ignores_post_lb_gap_after_trusted_prefix() -> None:
    def observation_at(second: int) -> PhysicalObservation:
        item = _observation(
            second,
            boot_id="boot-b" if second > 100 else "boot-a",
            raw_status=(
                "OB DISCHRG LB" if second == 100 else "COMMFAULT" if second > 100 else "OB DISCHRG"
            ),
            load_percent=None if second > 100 else 20.0,
        )
        if second <= 100:
            return item
        return replace(item, monotonic_ns=(second - 101) * 1_000_000_000)

    observations = tuple(observation_at(second) for second in range(111))
    context = decline_reporting._EventContext("event-a", "segment-a", NOW, "epoch-a")

    sample = decline_reporting._firmware_sample(context, True, observations)

    assert sample is not None
    assert sample.reserve_proxy_pp_s == 500.0


def test_firmware_provenance_rejects_reboot_before_first_lb_by_record_order() -> None:
    before_reboot = tuple(_observation(second) for second in range(51))
    after_reboot = tuple(
        replace(
            _observation(
                51 + second,
                boot_id="boot-b",
                raw_status="OB DISCHRG LB" if second == 100 else "OB DISCHRG",
            ),
            monotonic_ns=second * 1_000_000_000,
        )
        for second in range(101)
    )

    assert not decline_reporting._decline_policy.natural_prefix((*before_reboot, *after_reboot))


@pytest.mark.parametrize(("gap_seq", "eligible"), ((50, False), (105, True)))
def test_firmware_provenance_uses_gap_position_relative_to_first_lb(
    gap_seq: int,
    eligible: bool,
) -> None:
    ids = tuple(f"{index + 1:032x}" for index in range(6))
    projections = {}
    for blackout_id in ids:
        projection = _projection(
            blackout_id,
            malformed_quantum=False,
            evidence_class="operational_only",
            firmware_tail=True,
        )
        gap = _record(
            blackout_id,
            seq=gap_seq,
            record_type="gap",
            payload={"reason": "test_gap"},
        )
        projections[blackout_id] = replace(projection, gaps=(gap,))

    statuses = decline_statuses(
        cast(Any, _ProjectionStore(projections)),
        tuple(
            _summary(blackout_id, index, evidence_class="operational_only")
            for index, blackout_id in enumerate(ids)
        ),
    )

    assert (statuses[1].verdict == DeclineVerdict.STABLE_WITHIN_OBSERVED_EVIDENCE) is eligible


def test_gap_before_policy_selected_lb_is_not_hidden_by_early_pre_origin_lb() -> None:
    ids = tuple(f"{index + 1:032x}" for index in range(6))
    projections = {}
    for blackout_id in ids:
        projection = _projection(
            blackout_id,
            malformed_quantum=False,
            evidence_class="operational_only",
            firmware_tail=True,
        )
        records = []
        for record in projection.observations:
            if record.seq == 30:
                payload = dict(record.payload)
                payload["raw_status"] = "OB DISCHRG LB"
                record = replace(record, payload=payload)
            records.append(record)
        gap = _record(
            blackout_id,
            seq=50,
            record_type="gap",
            payload={"reason": "test_gap"},
        )
        projections[blackout_id] = replace(
            projection,
            observations=tuple(records),
            gaps=(gap,),
        )

    statuses = decline_statuses(
        cast(Any, _ProjectionStore(projections)),
        tuple(
            _summary(blackout_id, index, evidence_class="operational_only")
            for index, blackout_id in enumerate(ids)
        ),
    )

    assert statuses[1].verdict == DeclineVerdict.INSUFFICIENT_COMPARABLE_EVIDENCE


def test_firmware_sample_ignores_lb_before_evaluation_origin() -> None:
    observations = tuple(
        _observation(second, raw_status="OB DISCHRG LB" if second == 30 else "OB DISCHRG")
        for second in range(111)
    )
    context = decline_reporting._EventContext("event-a", "segment-a", NOW, "epoch-a")

    assert decline_reporting._firmware_sample(context, True, observations) is None


def test_firmware_sample_refuses_missing_load_inside_trusted_prefix() -> None:
    observations = tuple(
        _observation(
            second,
            raw_status="OB DISCHRG LB" if second == 100 else "OB DISCHRG",
            load_percent=None if second == 91 else 20.0,
        )
        for second in range(111)
    )
    context = decline_reporting._EventContext("event-a", "segment-a", NOW, "epoch-a")

    assert decline_reporting._firmware_sample(context, True, observations) is None


def test_firmware_sample_refuses_without_a_complete_origin_window() -> None:
    context = decline_reporting._EventContext("event-a", "segment-a", NOW, "epoch-a")
    observations = tuple(
        _observation(second, raw_status="OB DISCHRG LB" if second == 30 else "OB DISCHRG")
        for second in range(31)
    )

    assert decline_reporting._firmware_sample(context, True, observations) is None


def test_integrated_load_accepts_only_increasing_same_boot_edges() -> None:
    observations = (
        _observation(0, load_percent=10.0),
        _observation(1, load_percent=20.0),
        _observation(2, boot_id="boot-b", load_percent=30.0),
        replace(_observation(2, boot_id="boot-b"), load_percent=40.0),
        _observation(3, boot_id="boot-b", load_percent=None),
        _observation(4, boot_id="boot-b", load_percent=50.0),
        _observation(5, boot_id="boot-b", load_percent=70.0),
    )

    assert decline_reporting._integrated_load(observations) == 75.0


@pytest.mark.parametrize(
    ("mutation", "stable"),
    (
        ("valid", True),
        ("gap_at_limit", True),
        ("gap_above_limit", False),
        ("boot_change", False),
        ("duplicate_time", False),
        ("missing_load", False),
        ("missing_voltage", False),
        ("unstable_load", False),
    ),
)
def test_stable_origin_window_rejects_each_invalid_axis(mutation: str, stable: bool) -> None:
    observations = tuple(_observation(second) for second in range(31))
    if mutation in {"gap_at_limit", "gap_above_limit"}:
        offset_ns = 1_500_000_000 if mutation == "gap_at_limit" else 1_501_000_000
        observations = tuple(
            replace(item, monotonic_ns=item.monotonic_ns + (offset_ns if index >= 16 else 0))
            for index, item in enumerate(observations)
        )
    elif mutation == "boot_change":
        observations = tuple(
            replace(item, boot_id="boot-b") if index == 16 else item
            for index, item in enumerate(observations)
        )
    elif mutation == "duplicate_time":
        observations = tuple(
            replace(item, monotonic_ns=observations[15].monotonic_ns) if index == 16 else item
            for index, item in enumerate(observations)
        )
    elif mutation == "missing_load":
        observations = tuple(
            replace(item, load_percent=None) if index == 16 else item
            for index, item in enumerate(observations)
        )
    elif mutation == "missing_voltage":
        observations = tuple(
            replace(item, battery_voltage_v=None) if index == 16 else item
            for index, item in enumerate(observations)
        )
    elif mutation == "unstable_load":
        observations = tuple(
            replace(item, load_percent=15.0 if index % 2 == 0 else 25.0)
            for index, item in enumerate(observations)
        )

    assert decline_reporting._stable_origin_window(observations) is stable


def test_empty_origin_window_is_not_stable() -> None:
    assert decline_reporting._stable_origin_window(()) is False
