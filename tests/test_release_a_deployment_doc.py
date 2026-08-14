"""Contract and private-copy validation for the Release A deployment runbook."""

import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from src.model import KNOWN_STATE_KEYS, BatteryModel

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs/RELEASE-A-DEPLOYMENT.md"
RETAINED_BACKUP = Path("/home/j2h4u/.config/ups-battery-monitor.backup-20260815-3rb0H4/model.json")
LEGACY_KEYS = {
    "battery_install_date",
    "cumulative_on_battery_sec",
    "cycle_count",
    "discharge_events",
    "last_upscmd_status",
    "last_upscmd_timestamp",
    "last_upscmd_type",
    "lut",
    "new_battery_detected",
    "physics",
    "r_internal_history",
    "soh",
    "soh_history",
}
RELEASE_A_KEYS = {
    "capacity_estimates",
    "capacity_ah_measured",
    "battery_epoch_id",
    "new_battery_detected_timestamp",
}
LEGACY_GUARD = (
    "import json, sys; expected=set(sys.argv[1].split(',')); "
    "data=json.load(open(sys.argv[2])); assert set(data) == expected"
)


def _run_legacy_guard(path: Path) -> None:
    subprocess.run(
        ["python3", "-c", LEGACY_GUARD, ",".join(sorted(LEGACY_KEYS)), str(path)],
        check=True,
    )


def _jq_transform(source: Path, candidate: Path, epoch: str) -> None:
    expression = """{
      soh: .soh,
      soh_history: .soh_history,
      capacity_estimates: [],
      capacity_ah_measured: null,
      physics: .physics,
      lut: .lut,
      r_internal_history: .r_internal_history,
      battery_install_date: .battery_install_date,
      battery_epoch_id: $battery_epoch_id,
      cycle_count: .cycle_count,
      cumulative_on_battery_sec: .cumulative_on_battery_sec,
      new_battery_detected: .new_battery_detected,
      new_battery_detected_timestamp: null,
      discharge_events: .discharge_events,
      last_upscmd_timestamp: .last_upscmd_timestamp,
      last_upscmd_type: .last_upscmd_type,
      last_upscmd_status: .last_upscmd_status
    }"""
    with candidate.open("w") as output:
        subprocess.run(
            ["jq", "--arg", "battery_epoch_id", epoch, expression, str(source)],
            check=True,
            stdout=output,
        )
    candidate.chmod(0o600)


def test_release_a_runbook_has_protected_fish_conversion_contract():
    text = RUNBOOK.read_text()
    for required in (
        "upsc cyberpower@localhost ups.status",
        "active_event_id",
        "journal_healthy",
        "sha256sum",
        "set epoch_id (python3 -c 'import uuid; print(uuid.uuid4())')",
        "chmod 0600",
        "capacity_estimates: []",
        "capacity_ah_measured: null",
        "physics: .physics",
        "lut: .lut",
        "r_internal_history: .r_internal_history",
        "soh: .soh",
        "soh_history: .soh_history",
        "battery_epoch_id: $battery_epoch_id",
        "new_battery_detected_timestamp: null",
        "BatteryModel",
        'mv -- "$candidate" "$model"',
        "under 30 seconds",
        "TimeoutStartSec=0",
        "retained backup",
        'mktemp -p "$state_dir" model.json.release-a.XXXXXX',
        'mktemp -p "$state_dir" ups-battery-monitor.new-unit.XXXXXX',
        'mktemp -p "$state_dir" ups-battery-monitor.rollback-unit.XXXXXX',
        "git worktree add --detach",
        "c6c5980",
        "release-a-20260815",
        "set release_a_commit",
        "refs/tags/release-a-20260815^{commit}",
        'git -C "$repo" diff --quiet "$release_a_commit" --',
        "untracked_runtime",
        "@INSTALL_DIR@",
        "@INSTALL_HOME@",
        "set legacy_keys (string join ','",
        'expected=set(sys.argv[1].split(","))',
        'cd "$repo"; or exit 1',
        'sudo install -o root -g root -m 0644 "$new_unit_candidate"',
        'sudo install -o root -g root -m 0644 "$rollback_unit_candidate"',
        "daemon-reload",
        "stop_and_require_inactive",
        "systemctl show -p ActiveState --value",
        "--no-block",
        "for second in (seq 1 10)",
        "bounded_start",
        "bounded_start 10",
        "bounded_start 30",
        "exactly inactive or failed",
        "recover_before_replace",
        "restore_after_replace",
        "source model changed during preflight",
        "atomic model replacement failed",
        "new service failed to start",
        "CRITICAL: protection is not restored",
        "RuntimeDirectory",
        "virtual UPS",
        "shutdown",
        "<30s",
        "TimeoutStartSec=30",
    ):
        assert required in text
    assert "DischargeJournal" not in text
    assert "journal.replay" not in text
    assert "model.json.release-a-candidate" not in text
    assert "--prepare-only" not in text
    assert "--restore-previous" not in text
    assert "systemd-analyze verify" not in text
    for key in LEGACY_KEYS:
        assert key in text
    worktree = text.index("git worktree add --detach")
    render = text.index("@INSTALL_DIR@")
    stop = text.index("stop_and_require_inactive; or begin; recover_before_replace")
    install = text.index('sudo install -o root -g root -m 0644 "$new_unit_candidate"')
    start = text.index("bounded_start 10", install)
    assert worktree < render < stop < install < start
    assert "sudo systemctl start ups-battery-monitor; or" not in text
    restore = text.index("function restore_after_replace")
    restore_stop = text.index("stop_and_require_inactive", restore)
    restore_stage = text.index("set restore_candidate", restore)
    restore_mv = text.index('mv -- "$restore_candidate" "$model"', restore)
    assert restore_stop < restore_stage < restore_mv
    assert text.index('expected=set(sys.argv[1].split(","))') < text.index(
        "jq --arg battery_epoch_id"
    )
    assert text.index('expected=set(sys.argv[1].split(","))') < stop
    assert text.index('cd "$repo"; or exit 1') < text.index("from src.model import BatteryModel")
    conversion_block = text.split("jq --arg battery_epoch_id", 1)[1].split("```", 1)[0]
    assert "//" not in conversion_block


def test_documented_transform_validates_private_copy_of_retained_backup(tmp_path: Path):
    if not RETAINED_BACKUP.is_file():
        pytest.skip(f"retained host backup unavailable: {RETAINED_BACKUP}")
    if shutil.which("jq") is None:
        pytest.skip("jq unavailable")

    source = tmp_path / "model.json"
    candidate = tmp_path / "candidate.json"
    shutil.copy2(RETAINED_BACKUP, source)
    source_before = source.read_bytes()
    epoch = str(uuid.uuid4())
    _jq_transform(source, candidate, epoch)

    data = json.loads(candidate.read_text())
    assert set(data) == KNOWN_STATE_KEYS
    assert data["battery_epoch_id"] == epoch
    assert data["capacity_estimates"] == []
    assert data["capacity_ah_measured"] is None
    assert data["new_battery_detected_timestamp"] is None
    BatteryModel(candidate)
    assert candidate.stat().st_mode & 0o777 == 0o600
    assert source.read_bytes() == source_before


def test_legacy_guard_accepts_exact_copy_and_rejects_schema_changes(tmp_path: Path):
    if not RETAINED_BACKUP.is_file():
        pytest.skip(f"retained host backup unavailable: {RETAINED_BACKUP}")

    valid = tmp_path / "valid-model.json"
    shutil.copy2(RETAINED_BACKUP, valid)
    _run_legacy_guard(valid)

    original = json.loads(valid.read_text())
    for key in sorted(RELEASE_A_KEYS):
        candidate = tmp_path / f"reject-{key}.json"
        data = original | {key: None}
        candidate.write_text(json.dumps(data))
        with pytest.raises(subprocess.CalledProcessError):
            _run_legacy_guard(candidate)

    missing = tmp_path / "reject-missing.json"
    data = original.copy()
    data.pop("soh")
    missing.write_text(json.dumps(data))
    with pytest.raises(subprocess.CalledProcessError):
        _run_legacy_guard(missing)

    unexpected = tmp_path / "reject-unexpected.json"
    data = original | {"unexpected": None}
    unexpected.write_text(json.dumps(data))
    with pytest.raises(subprocess.CalledProcessError):
        _run_legacy_guard(unexpected)
