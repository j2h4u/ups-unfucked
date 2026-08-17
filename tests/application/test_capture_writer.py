from threading import Event, Thread

import pytest

from src.application.capture_writer import (
    LIFECYCLE_CAPACITY,
    OBSERVATION_CAPACITY,
    CaptureCommand,
    CaptureCommandKind,
    CaptureWriter,
)


def command(kind, label, calls):
    return CaptureCommand(kind, lambda: calls.append(label))


def test_start_command_precedes_observation_backlog() -> None:
    calls = []
    writer = CaptureWriter()
    writer.submit(command(CaptureCommandKind.OBSERVATION, "observation", calls))
    writer.submit(command(CaptureCommandKind.START, "start", calls))

    assert writer.drain_one()
    assert writer.drain_one()
    assert calls == ["start", "observation"]


def test_end_is_a_barrier_after_already_accepted_observations() -> None:
    calls = []
    writer = CaptureWriter()
    writer.submit(command(CaptureCommandKind.OBSERVATION, "observation", calls))
    writer.submit(command(CaptureCommandKind.END, "end", calls))

    assert writer.drain_one()
    assert writer.drain_one()
    assert calls == ["observation", "end"]


def test_end_does_not_pull_next_event_observation_across_barrier() -> None:
    calls = []
    writer = CaptureWriter()
    writer.submit(command(CaptureCommandKind.OBSERVATION, "old-observation", calls))
    writer.submit(command(CaptureCommandKind.END, "old-end", calls))
    writer.submit(command(CaptureCommandKind.START, "new-start", calls))
    writer.submit(command(CaptureCommandKind.OBSERVATION, "new-observation", calls))

    while writer.drain_one():
        pass

    assert calls == ["old-observation", "old-end", "new-start", "new-observation"]


def test_maintenance_waits_until_active_capture_scope_ends() -> None:
    calls = []
    writer = CaptureWriter()
    assert writer.submit(
        CaptureCommand(
            CaptureCommandKind.START,
            lambda: calls.append("start"),
            scope_id="blackout-a",
        )
    )
    assert writer.submit(
        CaptureCommand(
            CaptureCommandKind.MODEL_COMMIT,
            lambda: calls.append("maintenance"),
        )
    )

    assert writer.drain_one()
    assert calls == ["start"]
    assert writer.health().maintenance_queued == 1
    assert not writer.drain_one()

    assert writer.submit(
        CaptureCommand(
            CaptureCommandKind.END,
            lambda: calls.append("end"),
            scope_id="blackout-a",
        )
    )
    assert writer.drain_one()
    assert writer.drain_one()
    assert calls == ["start", "end", "maintenance"]


def test_failed_start_keeps_maintenance_behind_scope_recovery() -> None:
    calls = []
    writer = CaptureWriter()
    assert writer.submit(
        CaptureCommand(
            CaptureCommandKind.START,
            lambda: (_ for _ in ()).throw(OSError("start")),
            scope_id="blackout-a",
            recover_failure=lambda _exc: False,
        )
    )
    assert writer.submit(
        CaptureCommand(CaptureCommandKind.MODEL_COMMIT, lambda: calls.append("maintenance"))
    )
    assert writer.drain_one()
    assert not writer.drain_one()

    writer.clear_failed_scope("blackout-a")
    assert writer.submit(
        CaptureCommand(
            CaptureCommandKind.START, lambda: calls.append("recovered"), scope_id="blackout-a"
        )
    )
    assert writer.submit(
        CaptureCommand(CaptureCommandKind.END, lambda: calls.append("end"), scope_id="blackout-a")
    )
    while writer.drain_one():
        pass
    assert calls == ["recovered", "end", "maintenance"]


def test_failed_end_keeps_maintenance_behind_scope_recovery() -> None:
    calls = []
    writer = CaptureWriter()
    assert writer.submit(
        CaptureCommand(
            CaptureCommandKind.START, lambda: calls.append("start"), scope_id="blackout-a"
        )
    )
    assert writer.submit(
        CaptureCommand(
            CaptureCommandKind.END,
            lambda: (_ for _ in ()).throw(OSError("end")),
            scope_id="blackout-a",
            recover_failure=lambda _exc: False,
        )
    )
    assert writer.submit(
        CaptureCommand(CaptureCommandKind.MODEL_COMMIT, lambda: calls.append("maintenance"))
    )
    assert writer.drain_one()
    assert writer.drain_one()
    assert not writer.drain_one()

    writer.clear_failed_scope("blackout-a")
    assert writer.submit(
        CaptureCommand(
            CaptureCommandKind.END, lambda: calls.append("recovered-end"), scope_id="blackout-a"
        )
    )
    while writer.drain_one():
        pass
    assert calls == ["start", "recovered-end", "maintenance"]


def test_writer_exposes_oldest_queue_age_and_max_busy_time() -> None:
    now = [0.0]
    writer = CaptureWriter(monotonic_clock=lambda: now[0])
    assert writer.submit(command(CaptureCommandKind.OBSERVATION, "observation", []))
    now[0] = 2.5
    assert writer.health().oldest_queue_age_s == pytest.approx(2.5)

    def slow_command() -> None:
        now[0] += 0.75

    assert writer.submit(CaptureCommand(CaptureCommandKind.OBSERVATION, slow_command))
    assert writer.drain_one()
    assert writer.drain_one()
    assert writer.health().max_busy_time_s == pytest.approx(0.75)


def test_observation_backlog_cannot_consume_reserved_lifecycle_slots() -> None:
    calls = []
    writer = CaptureWriter()
    for index in range(OBSERVATION_CAPACITY):
        assert writer.submit(command(CaptureCommandKind.OBSERVATION, str(index), calls))

    assert not writer.submit(command(CaptureCommandKind.OBSERVATION, "overflow", calls))
    for index in range(LIFECYCLE_CAPACITY):
        assert writer.submit(command(CaptureCommandKind.GAP, f"gap-{index}", calls))

    health = writer.health()
    assert not health.capture_available
    assert health.observation_overflow_count == 1
    assert health.lifecycle_queued == LIFECYCLE_CAPACITY


def test_failure_is_visible_and_does_not_stop_next_command() -> None:
    calls = []
    failures = []
    writer = CaptureWriter(on_failure=lambda queued, exc: failures.append((queued.kind, exc)))
    writer.submit(
        CaptureCommand(CaptureCommandKind.END, lambda: (_ for _ in ()).throw(OSError("disk")))
    )
    writer.submit(command(CaptureCommandKind.OBSERVATION, "after", calls))

    writer.drain_one()
    writer.drain_one()

    assert calls == ["after"]
    assert failures[0][0] == CaptureCommandKind.END
    assert writer.health().bounded_error == "OSError: disk"


def test_recovered_scoped_failure_keeps_later_lifecycle_command() -> None:
    calls = []
    writer = CaptureWriter()

    def record_durable_gap(_exc: Exception) -> bool:
        calls.append("durable-gap")
        return True

    writer.submit(
        CaptureCommand(
            CaptureCommandKind.OBSERVATION,
            lambda: (_ for _ in ()).throw(OSError("disk")),
            scope_id="blackout-a",
            recover_failure=record_durable_gap,
        )
    )
    writer.submit(
        CaptureCommand(
            CaptureCommandKind.END,
            lambda: calls.append("end"),
            scope_id="blackout-a",
        )
    )

    while writer.drain_one():
        pass

    assert calls == ["durable-gap", "end"]
    assert writer.health().discarded_command_count == 0


def test_unrecovered_scoped_failure_discards_only_same_event_commands() -> None:
    calls = []
    writer = CaptureWriter()
    writer.submit(
        CaptureCommand(
            CaptureCommandKind.OBSERVATION,
            lambda: (_ for _ in ()).throw(OSError("disk")),
            scope_id="blackout-a",
            recover_failure=lambda _exc: False,
        )
    )
    writer.submit(
        CaptureCommand(
            CaptureCommandKind.END,
            lambda: calls.append("old-end"),
            scope_id="blackout-a",
        )
    )
    writer.submit(
        CaptureCommand(
            CaptureCommandKind.START,
            lambda: calls.append("new-start"),
            scope_id="blackout-b",
        )
    )

    while writer.drain_one():
        pass

    assert calls == ["new-start"]
    assert writer.health().discarded_command_count == 1


def test_recovery_exception_is_operator_visible_and_scope_stays_failed() -> None:
    calls = []
    writer = CaptureWriter()

    def fail_recovery(_exc: Exception) -> bool:
        raise OSError("recovery disk unavailable")

    writer.submit(
        CaptureCommand(
            CaptureCommandKind.END,
            lambda: (_ for _ in ()).throw(OSError("primary disk unavailable")),
            scope_id="blackout-a",
            recover_failure=fail_recovery,
        )
    )

    assert writer.drain_one()
    assert not writer.submit(
        CaptureCommand(
            CaptureCommandKind.END,
            lambda: calls.append("unexpected-end"),
            scope_id="blackout-a",
        )
    )
    health = writer.health()
    assert not health.capture_available
    assert health.bounded_error == (
        "OSError: primary disk unavailable; "
        "terminal_recovery_failed OSError: recovery disk unavailable"
    )
    assert calls == []


def test_start_refuses_race_with_in_progress_manual_drain() -> None:
    entered = Event()
    release = Event()
    writer = CaptureWriter()

    def blocking_write() -> None:
        entered.set()
        assert release.wait(timeout=1.0)

    writer.submit(CaptureCommand(CaptureCommandKind.START, blocking_write))
    draining = Thread(target=writer.drain_one)
    draining.start()
    assert entered.wait(timeout=1.0)
    try:
        with pytest.raises(RuntimeError, match="manual capture drain"):
            writer.start()
    finally:
        release.set()
        draining.join(timeout=1.0)

    assert not draining.is_alive()


def test_background_writer_drains_on_stop() -> None:
    calls = []
    writer = CaptureWriter()
    writer.start()
    writer.submit(command(CaptureCommandKind.START, "start", calls))
    writer.stop(drain=True)
    assert calls == ["start"]
    assert not writer.submit(command(CaptureCommandKind.OBSERVATION, "orphan", calls))


def test_background_writer_waits_for_capture_end_before_maintenance() -> None:
    maintenance_done = Event()
    writer = CaptureWriter()
    assert writer.submit(
        CaptureCommand(CaptureCommandKind.START, lambda: None, scope_id="blackout-a")
    )
    assert writer.drain_one()

    writer.start()
    try:
        assert writer.submit(CaptureCommand(CaptureCommandKind.MODEL_COMMIT, maintenance_done.set))
        assert not maintenance_done.wait(timeout=0.05)

        assert writer.submit(
            CaptureCommand(CaptureCommandKind.END, lambda: None, scope_id="blackout-a")
        )
        assert maintenance_done.wait(timeout=1.0)
    finally:
        writer.stop(drain=True)


def test_shutdown_boundary_waits_for_a_full_lifecycle_lane_to_drain() -> None:
    entered = Event()
    release = Event()
    writer = CaptureWriter()

    def blocked_start() -> None:
        entered.set()
        assert release.wait(timeout=1.0)

    writer.start()
    assert writer.submit(CaptureCommand(CaptureCommandKind.START, blocked_start))
    assert entered.wait(timeout=1.0)
    for index in range(LIFECYCLE_CAPACITY):
        assert writer.submit(command(CaptureCommandKind.GAP, f"gap-{index}", []))
    assert not writer.submit(command(CaptureCommandKind.END, "overflow", []))

    release.set()
    assert writer.wait_for_lifecycle_capacity(1.0)
    assert writer.submit(command(CaptureCommandKind.END, "shutdown", []))
    writer.stop(drain=True)


def test_stop_without_drain_reports_discarded_work_as_capture_failure() -> None:
    calls = []
    writer = CaptureWriter()
    writer.submit(command(CaptureCommandKind.START, "start", calls))
    writer.submit(command(CaptureCommandKind.OBSERVATION, "observation", calls))

    writer.stop(drain=False)

    health = writer.health()
    assert calls == []
    assert not health.capture_available
    assert health.discarded_command_count == 2
    assert health.bounded_error == "capture_writer_stopped_without_drain"


def test_manual_drain_is_forbidden_while_background_thread_runs() -> None:
    writer = CaptureWriter()
    writer.start()
    try:
        with pytest.raises(RuntimeError, match="cannot drain manually"):
            writer.drain_one()
    finally:
        writer.stop()


def test_sticky_timeout_blocks_model_work_while_inflight_writer_finishes() -> None:
    entered = Event()
    release = Event()
    model_calls: list[str] = []
    writer = CaptureWriter()

    def blocked_capture() -> None:
        entered.set()
        assert release.wait(timeout=1.0)

    writer.start()
    assert writer.submit(
        CaptureCommand(
            CaptureCommandKind.START,
            blocked_capture,
            scope_id="blackout-a",
        )
    )
    assert entered.wait(timeout=1.0)
    writer.mark_capture_unhealthy(
        "sticky recovery deadline exceeded",
        scope_id="blackout-a",
    )

    assert not writer.submit(
        CaptureCommand(
            CaptureCommandKind.MODEL_COMMIT,
            lambda: model_calls.append("model"),
        )
    )
    release.set()
    writer.stop(drain=True)

    assert model_calls == []
    health = writer.health()
    assert not health.capture_available
    assert health.last_failure_kind == "sticky_recovery_timeout"
    assert health.bounded_error == "sticky recovery deadline exceeded"
