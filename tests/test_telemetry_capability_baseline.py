"""Offline regression tests for the read-only Slice-0 capability baseline."""

import fcntl
import json
import os
from pathlib import Path
from typing import Any

import pytest

from src.adapters import telemetry_capability_baseline as baseline_module
from src.adapters.telemetry_capability_baseline import (
    ARTIFACT_FILENAME,
    MAX_BASELINE_BYTES,
    OWNER_ONLY_MODE,
    BaselinePublicationDurabilityError,
    BaselineRefusal,
    CollectionTiming,
    TelemetryCapabilityError,
    build_baseline,
    canonical_json_bytes,
    load_baseline,
    record_baseline,
    validate_baseline,
    verify_baseline,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "slice0"
FAST_TIMING = CollectionTiming(poll_interval_seconds=0.0, sleep=lambda _seconds: None)


def _fixture_snapshot(name: str) -> tuple[dict[str, float | str], dict[str, str]]:
    lines = (FIXTURE_ROOT / name).read_text(encoding="utf-8").splitlines()
    assert lines[0] == "BEGIN LIST VAR cyberpower"
    assert lines[-1] == "END LIST VAR cyberpower"
    values: dict[str, float | str] = {}
    tokens: dict[str, str] = {}
    for line in lines[1:-1]:
        prefix = "VAR cyberpower "
        assert line.startswith(prefix)
        key, raw = line[len(prefix) :].split(' "', maxsplit=1)
        token = raw[:-1]
        try:
            value: float | str = float(token)
        except ValueError:
            value = token
        values[key] = value
        tokens[key] = token
    return values, tokens


class FakeNUT:
    """Deterministic LIST VAR-only port; it exposes no command method."""

    def __init__(self, snapshots: list[tuple[dict[str, float | str], dict[str, str]]]):
        self.snapshots = snapshots
        self.calls = 0

    def get_ups_vars_with_tokens_strict(self) -> tuple[dict[str, float | str], dict[str, str]]:
        if self.calls >= len(self.snapshots):
            raise AssertionError("producer requested more than the prepared replies")
        self.calls += 1
        values, tokens = self.snapshots[self.calls - 1]
        return dict(values), dict(tokens)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class LatencyNUT(FakeNUT):
    def __init__(
        self, snapshots: list[tuple[dict[str, float | str], dict[str, str]]], clock: FakeClock
    ):
        super().__init__(snapshots)
        self.clock = clock

    def get_ups_vars_with_tokens_strict(self) -> tuple[dict[str, float | str], dict[str, str]]:
        reply = super().get_ups_vars_with_tokens_strict()
        self.clock.now += 0.25
        return reply


class OverrunningNUT(FakeNUT):
    def __init__(
        self, snapshots: list[tuple[dict[str, float | str], dict[str, str]]], clock: FakeClock
    ):
        super().__init__(snapshots)
        self.clock = clock

    def get_ups_vars_with_tokens_strict(self) -> tuple[dict[str, float | str], dict[str, str]]:
        reply = super().get_ups_vars_with_tokens_strict()
        self.clock.now += 1.25
        return reply


def _snapshots(
    *, identity_change_at: int | None = None
) -> list[tuple[dict[str, float | str], dict[str, str]]]:
    ol_values, ol_tokens = _fixture_snapshot("list-var-ol.txt")
    ob_values, ob_tokens = _fixture_snapshot("list-var-ob.txt")
    ob_values.pop("input.voltage", None)
    ob_tokens.pop("input.voltage", None)
    snapshots = [(dict(ol_values), dict(ol_tokens)) for _ in range(30)] + [
        (dict(ob_values), dict(ob_tokens)) for _ in range(30)
    ]
    if identity_change_at is not None:
        values, tokens = snapshots[identity_change_at]
        changed_values = dict(values)
        changed_tokens = dict(tokens)
        changed_values["device.serial"] = "CHANGED"
        changed_tokens["device.serial"] = "CHANGED"
        snapshots[identity_change_at] = changed_values, changed_tokens
    return snapshots


def test_builds_exactly_sixty_replies_and_scopes_optional_fields() -> None:
    client = FakeNUT(_snapshots())

    artifact = build_baseline(client, timing=FAST_TIMING)

    assert client.calls == 60
    assert artifact["reply_count"] == 60
    assert len(artifact["replies"]) == 60
    assert artifact["observed_ups_status"] == ["OB", "OL"]
    assert artifact["identity"] == {
        "nut_driver_name": "usbhid-ups",
        "nut_driver_version": "2.8.1",
        "ups_firmware": None,
        "ups_model": "UT850EG",
        "ups_serial": "ABC123456",
    }
    assert artifact["replies"][0]["tokens"]["battery.voltage"] == "13.40"
    assert artifact["identity_source_keys"]["ups_firmware"] is None
    assert artifact["replies"][0]["tokens"]["driver.version.data"] == "CyberPower HID 0.6"
    assert "driver.version.data" not in artifact["identity_source_keys"].values()
    ob_voltage = artifact["state_scoped_signatures"]["OB"]["fields"]["input.voltage"]
    ol_voltage = artifact["state_scoped_signatures"]["OL"]["fields"]["input.voltage"]
    assert "tokens" not in artifact["state_scoped_signatures"]["OL"]["fields"]["battery.voltage"]
    assert artifact["state_scoped_signatures"]["OL"]["fields"]["battery.voltage"]["token_shapes"]
    assert (
        artifact["state_scoped_signatures"]["OL"]["fields"]["battery.voltage"]["presence_mode"]
        == "always_present"
    )
    assert ob_voltage["present_count"] == 0
    assert ob_voltage["missing_count"] == 30
    assert ol_voltage["present_count"] == 30


def test_collection_timing_is_injected_between_consecutive_replies() -> None:
    clock = FakeClock()
    timing = CollectionTiming(
        poll_interval_seconds=1.0, sleep=clock.sleep, monotonic=clock.monotonic
    )

    build_baseline(FakeNUT(_snapshots()), timing=timing)

    assert clock.sleeps == [1.0] * 59


def test_collection_timing_aligns_deadlines_after_reply_latency() -> None:
    clock = FakeClock()
    timing = CollectionTiming(
        poll_interval_seconds=1.0, sleep=clock.sleep, monotonic=clock.monotonic
    )

    build_baseline(LatencyNUT(_snapshots(), clock), timing=timing)

    assert clock.sleeps == pytest.approx([0.75] * 59)


def test_collection_timing_runs_overdue_polls_immediately_without_negative_sleep() -> None:
    clock = FakeClock()
    timing = CollectionTiming(
        poll_interval_seconds=1.0, sleep=clock.sleep, monotonic=clock.monotonic
    )

    build_baseline(OverrunningNUT(_snapshots(), clock), timing=timing)

    assert clock.sleeps == []


def test_record_is_canonical_owner_only_and_no_clobber(tmp_path: Path) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    artifact = record_baseline(FakeNUT(_snapshots()), destination, timing=FAST_TIMING)

    raw = destination.read_bytes()
    assert destination.stat().st_mode & 0o777 == OWNER_ONLY_MODE
    assert raw == canonical_json_bytes(artifact) + b"\n"
    assert load_baseline(destination) == artifact
    with pytest.raises(FileExistsError, match="no-clobber"):
        record_baseline(FakeNUT(_snapshots()), destination, timing=FAST_TIMING)


def test_oversized_publication_is_refused_before_any_destination_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    monkeypatch.setattr(baseline_module, "MAX_BASELINE_BYTES", 32)

    with pytest.raises(BaselineRefusal, match="publication size limit"):
        record_baseline(FakeNUT(_snapshots()), destination, timing=FAST_TIMING)

    assert not destination.exists()
    assert not tuple(tmp_path.glob(f".{ARTIFACT_FILENAME}.tmp-*"))


def test_identity_change_refuses_before_publishing(tmp_path: Path) -> None:
    destination = tmp_path / ARTIFACT_FILENAME

    with pytest.raises(TelemetryCapabilityError, match="identity changed"):
        record_baseline(FakeNUT(_snapshots(identity_change_at=20)), destination, timing=FAST_TIMING)
    assert not destination.exists()


def test_optional_firmware_appearance_is_an_identity_change(tmp_path: Path) -> None:
    snapshots = _snapshots()
    values, tokens = snapshots[20]
    values["ups.firmware"] = "BF1"
    tokens["ups.firmware"] = "BF1"
    destination = tmp_path / ARTIFACT_FILENAME

    with pytest.raises(TelemetryCapabilityError, match="identity changed"):
        record_baseline(FakeNUT(snapshots), destination, timing=FAST_TIMING)
    assert not destination.exists()


def test_incomplete_reply_refuses_without_partial_artifact(tmp_path: Path) -> None:
    snapshots = _snapshots()
    snapshots[11] = ({"ups.status": "OL"}, {"ups.status": "OL"})
    destination = tmp_path / ARTIFACT_FILENAME

    with pytest.raises(TelemetryCapabilityError, match="mandatory identity"):
        record_baseline(FakeNUT(snapshots), destination, timing=FAST_TIMING)
    assert not destination.exists()


def test_tampered_parsed_value_refuses_without_partial_artifact(tmp_path: Path) -> None:
    snapshots = _snapshots()
    values, _tokens = snapshots[0]
    values["battery.voltage"] = 99.0
    destination = tmp_path / ARTIFACT_FILENAME

    with pytest.raises(TelemetryCapabilityError, match="disagree with raw tokens"):
        record_baseline(FakeNUT(snapshots), destination, timing=FAST_TIMING)
    assert not destination.exists()


def test_symlink_destination_is_refused(tmp_path: Path) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    target = tmp_path / "elsewhere"
    target.write_text("do not replace", encoding="utf-8")
    destination.symlink_to(target)

    with pytest.raises(TelemetryCapabilityError, match="symlink"):
        record_baseline(FakeNUT(_snapshots()), destination, timing=FAST_TIMING)
    assert target.read_text(encoding="utf-8") == "do not replace"


def test_private_parent_mode_is_required(tmp_path: Path) -> None:
    tmp_path.chmod(0o755)
    destination = tmp_path / ARTIFACT_FILENAME

    try:
        with pytest.raises(TelemetryCapabilityError, match="parent ownership or mode"):
            record_baseline(FakeNUT(_snapshots()), destination, timing=FAST_TIMING)
    finally:
        tmp_path.chmod(0o700)


def test_publish_handles_short_os_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    real_write = baseline_module.os.write

    def short_write(fd: int, data: bytes) -> int:
        return real_write(fd, data[:7])

    monkeypatch.setattr(baseline_module.os, "write", short_write)
    record_baseline(FakeNUT(_snapshots()), destination, timing=FAST_TIMING)

    assert load_baseline(destination)["reply_count"] == 60


def test_directory_fsync_failure_reports_published_artifact_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / ARTIFACT_FILENAME

    def fail_directory_fsync(_path: Path) -> None:
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(baseline_module, "_fsync_directory", fail_directory_fsync)
    with pytest.raises(
        BaselinePublicationDurabilityError,
        match="artifact was published but directory durability is unconfirmed",
    ):
        record_baseline(FakeNUT(_snapshots()), destination, timing=FAST_TIMING)

    assert destination.is_file()
    assert not tuple(tmp_path.glob(f".{ARTIFACT_FILENAME}.tmp-*"))
    assert load_baseline(destination)["reply_count"] == 60


def test_concurrent_owner_lock_refuses_second_run(tmp_path: Path) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    lock_fd = baseline_module._acquire_lock(destination)
    try:
        with pytest.raises(TelemetryCapabilityError, match="another baseline run"):
            record_baseline(FakeNUT(_snapshots()), destination, timing=FAST_TIMING)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_lock_permission_open_error_is_not_reported_as_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    real_open = baseline_module.os.open

    def deny_lock_open(path: Path, flags: int, mode: int = 0o777) -> int:
        if str(path).endswith(f".{ARTIFACT_FILENAME}.lock"):
            raise PermissionError(13, "permission denied")
        return real_open(path, flags, mode)

    monkeypatch.setattr(baseline_module.os, "open", deny_lock_open)
    with pytest.raises(BaselineRefusal, match="cannot be opened") as refusal:
        baseline_module._acquire_lock(destination)
    assert "another baseline run" not in str(refusal.value)


def test_lock_flock_permission_error_is_not_reported_as_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    real_flock = baseline_module.fcntl.flock

    def deny_flock(fd: int, operation: int) -> None:
        if operation & fcntl.LOCK_NB:
            raise PermissionError(13, "permission denied")
        real_flock(fd, operation)

    monkeypatch.setattr(baseline_module.fcntl, "flock", deny_flock)
    with pytest.raises(BaselineRefusal, match="cannot be acquired") as refusal:
        baseline_module._acquire_lock(destination)
    assert "another baseline run" not in str(refusal.value)


def test_persistent_lock_is_owner_only_after_open_regardless_of_umask(tmp_path: Path) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    lock_path = tmp_path / f".{ARTIFACT_FILENAME}.lock"
    lock_path.touch(mode=0o666)
    os.chmod(lock_path, 0o644)
    previous_umask = os.umask(0o000)
    try:
        lock_fd = baseline_module._acquire_lock(destination)
    finally:
        os.umask(previous_umask)
    try:
        assert lock_path.stat().st_mode & 0o777 == OWNER_ONLY_MODE
        assert os.fstat(lock_fd).st_mode & 0o777 == OWNER_ONLY_MODE
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_validator_rejects_canonical_identity_tampering(tmp_path: Path) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    record_baseline(FakeNUT(_snapshots()), destination, timing=FAST_TIMING)
    payload: dict[str, Any] = json.loads(destination.read_text(encoding="utf-8"))
    payload["identity"]["ups_serial"] = "OTHER"
    destination.write_bytes(canonical_json_bytes(payload) + b"\n")
    os.chmod(destination, OWNER_ONLY_MODE)

    with pytest.raises(TelemetryCapabilityError, match="identity changed"):
        load_baseline(destination)
    with pytest.raises(TelemetryCapabilityError, match="identity changed"):
        validate_baseline(payload)


def test_validator_rejects_disagreeing_identity_alias(tmp_path: Path) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    record_baseline(FakeNUT(_snapshots()), destination, timing=FAST_TIMING)
    payload: dict[str, Any] = json.loads(destination.read_text(encoding="utf-8"))
    payload["raw_keys"].append("ups.model")
    payload["raw_keys"].sort()
    for reply in payload["replies"]:
        reply["tokens"]["ups.model"] = "DIFFERENT"
    destination.write_bytes(canonical_json_bytes(payload) + b"\n")
    os.chmod(destination, OWNER_ONLY_MODE)

    with pytest.raises(TelemetryCapabilityError, match="aliases disagree"):
        load_baseline(destination)


def test_load_refuses_noncanonical_and_oversized_artifacts(tmp_path: Path) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    artifact = record_baseline(FakeNUT(_snapshots()), destination, timing=FAST_TIMING)
    destination.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    os.chmod(destination, OWNER_ONLY_MODE)

    with pytest.raises(TelemetryCapabilityError, match="not canonical"):
        load_baseline(destination)

    destination.write_bytes(b"x" * (MAX_BASELINE_BYTES + 1))
    with pytest.raises(TelemetryCapabilityError, match="trusted bound"):
        load_baseline(destination)


def test_validator_rejects_bool_port_and_extra_state_signature_key() -> None:
    artifact = build_baseline(FakeNUT(_snapshots()), timing=FAST_TIMING)
    artifact["endpoint"]["port"] = True
    with pytest.raises(TelemetryCapabilityError, match="port"):
        validate_baseline(artifact)

    artifact = build_baseline(FakeNUT(_snapshots()), timing=FAST_TIMING)
    artifact["state_scoped_signatures"]["OL"]["extra"] = "not schema"
    with pytest.raises(TelemetryCapabilityError, match="state signature shape"):
        validate_baseline(artifact)


@pytest.mark.parametrize("field", ["observed_ups_status", "raw_keys"])
def test_validator_rederives_status_and_raw_key_sets(field: str) -> None:
    artifact = build_baseline(FakeNUT(_snapshots()), timing=FAST_TIMING)
    artifact[field].append("ZZ" if field == "observed_ups_status" else "zz.phantom")
    artifact[field].sort()

    with pytest.raises(TelemetryCapabilityError, match="not derived from replies"):
        validate_baseline(artifact)


def test_verify_refuses_while_record_holds_exclusive_lock(tmp_path: Path) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    lock_fd = baseline_module._acquire_lock(destination)
    try:
        client = FakeNUT([_snapshots()[0]])
        with pytest.raises(TelemetryCapabilityError, match="another baseline run"):
            verify_baseline(destination, client)
        assert client.calls == 0
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_stale_unrelated_temp_is_not_cleaned_by_rerun(tmp_path: Path) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    stale = tmp_path / f".{ARTIFACT_FILENAME}.tmp-interrupted"
    stale.write_bytes(b"interrupted")
    baseline_module._publish_no_clobber(destination, b"published")

    assert destination.read_bytes() == b"published"
    assert stale.read_bytes() == b"interrupted"


def test_temp_name_collision_has_distinct_diagnostic(tmp_path: Path) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    data = b"payload"
    temporary = tmp_path / f".{ARTIFACT_FILENAME}.tmp-{os.getpid()}-{id(data)}"
    temporary.write_bytes(b"unrelated")

    with pytest.raises(TelemetryCapabilityError, match="temporary path already exists"):
        baseline_module._publish_no_clobber(destination, data)
    assert not destination.exists()
    assert temporary.read_bytes() == b"unrelated"


def test_read_rejects_growth_after_initial_size_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    test_limit = 32
    monkeypatch.setattr(baseline_module, "MAX_BASELINE_BYTES", test_limit)
    destination.write_bytes(b"x" * (test_limit - 1))
    os.chmod(destination, OWNER_ONLY_MODE)
    writer_fd = os.open(destination, os.O_WRONLY | os.O_APPEND)
    real_read = baseline_module.os.read
    read_calls = 0

    def grow_after_first_read(fd: int, size: int) -> bytes:
        nonlocal read_calls
        chunk = real_read(fd, size)
        read_calls += 1
        if read_calls == 1:
            os.write(writer_fd, b"xy")
        return chunk

    monkeypatch.setattr(baseline_module.os, "read", grow_after_first_read)
    try:
        with pytest.raises(TelemetryCapabilityError, match="trusted bound"):
            load_baseline(destination)
    finally:
        os.close(writer_fd)


def test_unexpected_programmer_exception_from_client_is_not_translated() -> None:
    class BuggyClient:
        def get_ups_vars_with_tokens_strict(self) -> tuple[dict[str, float | str], dict[str, str]]:
            raise RuntimeError("programmer bug")

    with pytest.raises(RuntimeError, match="programmer bug"):
        build_baseline(BuggyClient(), timing=FAST_TIMING)


def test_destination_race_has_friendly_no_clobber_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    data = b"payload"
    real_link = baseline_module.os.link

    def race_link(source: Path, target: Path, *, follow_symlinks: bool = True) -> None:
        target.write_bytes(b"raced")
        os.chmod(target, OWNER_ONLY_MODE)
        real_link(source, target, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(baseline_module.os, "link", race_link)
    with pytest.raises(FileExistsError, match="destination exists and no-clobber"):
        baseline_module._publish_no_clobber(destination, data)
    assert destination.read_bytes() == b"raced"
    assert not tuple(tmp_path.glob(f".{ARTIFACT_FILENAME}.tmp-*"))


def test_post_link_temp_cleanup_failure_keeps_artifact_loadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    real_unlink = Path.unlink

    def fail_temp_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.name.startswith(f".{ARTIFACT_FILENAME}.tmp-"):
            raise OSError("simulated cleanup failure")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_temp_unlink)
    artifact = record_baseline(FakeNUT(_snapshots()), destination, timing=FAST_TIMING)

    assert load_baseline(destination) == artifact


def test_verify_requires_current_identity_match(tmp_path: Path) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    record_baseline(FakeNUT(_snapshots()), destination, timing=FAST_TIMING)

    verify_baseline(destination, FakeNUT([_snapshots()[0]]))
    changed = _snapshots()[0]
    changed_values, changed_tokens = changed
    changed_values["device.model"] = "OTHER"
    changed_tokens["device.model"] = "OTHER"
    with pytest.raises(TelemetryCapabilityError, match="does not match baseline"):
        verify_baseline(destination, FakeNUT([changed]))


def test_verify_rejects_firmware_appearing_after_absent_baseline(tmp_path: Path) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    record_baseline(FakeNUT(_snapshots()), destination, timing=FAST_TIMING)
    values, tokens = _snapshots()[0]
    values["ups.firmware"] = "BF1"
    tokens["ups.firmware"] = "BF1"

    with pytest.raises(TelemetryCapabilityError, match="does not match baseline"):
        verify_baseline(destination, FakeNUT([(values, tokens)]))


def test_verify_requires_configured_endpoint_match(tmp_path: Path) -> None:
    destination = tmp_path / ARTIFACT_FILENAME
    record_baseline(FakeNUT(_snapshots()), destination, timing=FAST_TIMING)

    with pytest.raises(TelemetryCapabilityError, match="endpoint"):
        verify_baseline(destination, FakeNUT([_snapshots()[0]]), port=9999)
