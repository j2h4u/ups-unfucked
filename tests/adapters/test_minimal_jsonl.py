import json
from pathlib import Path

import pytest

from src.adapters.jsonl_errors import EventCorruptionError, EventValidationError
from src.adapters.minimal_event_file import append, encode, read, sample
from src.adapters.minimal_jsonl import MinimalJsonlEventStore
from src.application.storage_values import EventRecord, EventStart


def _sample(at: str, status: str, *, pct: float | None = 50) -> dict[str, object]:
    return sample(at, 13.6, pct, 600, 19, 0 if status.startswith("OB") else 230, 230, status)


def _start(at: str, status: str = "OB DISCHRG", kind: str = "blackout") -> EventStart:
    return EventStart(
        "application-id",
        "application-segment",
        "boot",
        at,
        1,
        {
            "observation": {
                "wall_time_utc": at,
                "battery_voltage_v": 13.6,
                "battery_pct": 50,
                "runtime_s": 600,
                "load_percent": 19,
                "input_voltage_v": 0 if status.startswith("OB") else 230,
                "output_voltage_v": 230,
                "raw_status": status,
            }
        },
        kind,  # type: ignore[arg-type]
    )


def test_every_wire_line_is_an_eight_field_sample(tmp_path: Path) -> None:
    path = tmp_path / "events" / "telemetry.jsonl"
    append(path, _sample("2026-08-22T00:00:00Z", "OB DISCHRG"))
    append(path, _sample("2026-08-22T00:00:01Z", "OB DISCHRG"))

    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert all(
        set(json.loads(line))
        == {
            "at",
            "battery_v",
            "battery_pct",
            "runtime_s",
            "load_pct",
            "input_v",
            "output_v",
            "status",
        }
        for line in lines
    )
    assert [item.name for item in path.parent.iterdir()] == ["telemetry.jsonl"]


def test_headers_end_gaps_and_envelopes_are_not_wire_records() -> None:
    with pytest.raises(EventValidationError):
        encode({"event": "blackout", "started_at": "2026-08-22T00:00:00Z"})
    with pytest.raises(EventValidationError):
        encode({**_sample("2026-08-22T00:00:00Z", "OB"), "seq": 1})


def test_restart_reconstructs_active_blackout_without_metadata(tmp_path: Path) -> None:
    store = MinimalJsonlEventStore(tmp_path)
    handle = store.open(_start("2026-08-22T00:00:00Z"))
    store.append(
        handle,
        EventRecord(
            "observation",
            "boot",
            "2026-08-22T00:00:01Z",
            2,
            {
                "wall_time_utc": "2026-08-22T00:00:01Z",
                "battery_voltage_v": 13.5,
                "battery_pct": 49,
                "runtime_s": 590,
                "load_percent": 20,
                "input_voltage_v": 0,
                "output_voltage_v": 230,
                "raw_status": "OB DISCHRG",
            },
            "physical",
        ),
    )
    restarted = MinimalJsonlEventStore(tmp_path)

    recovered = restarted.recover_startup()
    assert recovered is not None
    assert recovered.last_observation.payload["raw_status"] == "OB DISCHRG"
    assert (tmp_path / "events" / "telemetry.jsonl").read_text().count("\n") == 2
    assert set(
        json.loads((tmp_path / "events" / "telemetry.jsonl").read_text().splitlines()[0])
    ) == {
        "at",
        "battery_v",
        "battery_pct",
        "runtime_s",
        "load_pct",
        "input_v",
        "output_v",
        "status",
    }


def test_end_command_keeps_its_observation_as_a_sample(tmp_path: Path) -> None:
    store = MinimalJsonlEventStore(tmp_path)
    handle = store.open(_start("2026-08-22T00:00:00Z"))
    store.append(
        handle,
        EventRecord(
            "end",
            "boot",
            "2026-08-22T00:00:10Z",
            2,
            {
                "termination": "power_restored",
                "observation": {
                    "wall_time_utc": "2026-08-22T00:00:10Z",
                    "battery_voltage_v": 13.5,
                    "battery_pct": 49,
                    "runtime_s": 590,
                    "load_percent": 20,
                    "input_voltage_v": 230,
                    "output_voltage_v": 230,
                    "raw_status": "OL CHRG",
                },
            },
            "physical",
        ),
    )
    lines = (tmp_path / "events" / "telemetry.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["status"] == "OL CHRG"
    assert "termination" not in json.loads(lines[-1])


def test_end_without_observation_does_not_invent_a_sample(tmp_path: Path) -> None:
    store = MinimalJsonlEventStore(tmp_path)
    handle = store.open(_start("2026-08-22T00:00:00Z"))
    path = tmp_path / "events" / "telemetry.jsonl"
    before = path.read_bytes()

    store.append(
        handle,
        EventRecord(
            "end",
            "boot",
            "2026-08-22T00:00:10Z",
            2,
            {"termination": "service_stop"},
            "physical",
        ),
    )

    assert path.read_bytes() == before


def test_projection_splits_two_blackouts_and_recharge_in_one_chronological_stream(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events" / "telemetry.jsonl"
    for at, status, pct in (
        ("2026-08-22T00:00:00Z", "OB DISCHRG", 50),
        ("2026-08-22T00:00:10Z", "OB DISCHRG", 49),
        ("2026-08-22T00:00:20Z", "OL CHRG", 49),
        ("2026-08-22T00:00:30Z", "OL CHRG", 70),
        ("2026-08-22T00:00:40Z", "OL", 100),
        ("2026-08-22T01:00:00Z", "OB DISCHRG", 100),
        ("2026-08-22T01:00:10Z", "OB DISCHRG", 99),
        ("2026-08-22T01:00:20Z", "OL CHRG", 99),
        ("2026-08-22T01:00:30Z", "OL", 100),
    ):
        append(path, _sample(at, status, pct=pct))

    store = MinimalJsonlEventStore(tmp_path)
    blackouts = store.sealed_event_projections("2026-08-22T00:00:00Z", "2026-08-22T02:00:00Z")
    recharges = store.sealed_event_projections(
        "2026-08-22T00:00:00Z", "2026-08-22T02:00:00Z", event_kind="recharge"
    )
    assert len(blackouts) == 2
    assert len(recharges) == 2
    assert [item.start.wall_time_utc for item in blackouts if item.start] == [
        "2026-08-22T00:00:00Z",
        "2026-08-22T01:00:00Z",
    ]
    assert all(
        item.outcome is not None and item.outcome.payload["disposition"] == "recorded_only"
        for item in (*blackouts, *recharges)
    )
    assert len(blackouts[0].observations) == 2
    assert len(recharges[0].observations) == 3  # includes the OL/100% transition sample


def test_plain_ol_recharge_and_transition_sample_are_reconstructed(tmp_path: Path) -> None:
    path = tmp_path / "events" / "telemetry.jsonl"
    append(path, _sample("2026-08-22T02:00:00Z", "OB DISCHRG", pct=40))
    append(path, _sample("2026-08-22T02:00:10Z", "OL", pct=40))
    append(path, _sample("2026-08-22T02:00:20Z", "OL", pct=100))

    store = MinimalJsonlEventStore(tmp_path)
    blackouts = store.sealed_event_projections("2026-08-22T02:00:00Z", "2026-08-22T03:00:00Z")
    recharges = store.sealed_event_projections(
        "2026-08-22T02:00:00Z", "2026-08-22T03:00:00Z", event_kind="recharge"
    )
    assert len(blackouts) == len(recharges) == 1
    assert [item.start.wall_time_utc for item in recharges if item.start] == [
        "2026-08-22T02:00:10Z"
    ]
    assert [item.wall_time_utc for item in recharges[0].observations] == [
        "2026-08-22T02:00:10Z",
        "2026-08-22T02:00:20Z",
    ]


def test_torn_or_non_sample_tail_fails_closed_without_rewriting_capture(tmp_path: Path) -> None:
    path = tmp_path / "events" / "telemetry.jsonl"
    append(path, _sample("2026-08-22T00:00:00Z", "OB DISCHRG"))
    with path.open("ab") as stream:
        stream.write(b'{"end":"2026-08-22T00:00:01Z"}\n')
    with pytest.raises(EventCorruptionError):
        read(path)
    assert path.read_bytes().count(b"\n") == 2
