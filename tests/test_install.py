"""Targeted tests for install.sh's private battery-state handling."""

import shlex
import subprocess
from pathlib import Path

INSTALL = Path(__file__).parents[1] / "scripts" / "install.sh"


def _private_state_helper() -> str:
    source = INSTALL.read_text()
    start = source.index("ensure_private_state() {")
    end = source.index("\nensure_private_state\n", start)
    return source[start:end]


def _run_helper(
    home: Path,
    *,
    dry_run: bool,
    service_active: bool = False,
    event_log: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    event_path = event_log or home.parent / "events.log"
    command = f"""
set -euo pipefail
DRY_RUN={"yes" if dry_run else "no"}
INSTALL_HOME={shlex.quote(str(home))}
RUN_USER=$(id -un)
EVENT_LOG={shlex.quote(str(event_path))}
SERVICE_ACTIVE={"yes" if service_active else "no"}
log_error() {{ echo "[ERROR] $*" >&2; }}
log_ok() {{ echo "[OK] $*"; }}
log_info() {{ echo "[INFO] $*"; }}
run_cmd() {{
    if [[ "$DRY_RUN" == "yes" ]]; then
        echo "[DRY-RUN] Would run: $*"
    else
        "$@"
    fi
}}
systemctl() {{
    case "$1" in
        is-active)
            printf 'is-active\\n' >> "$EVENT_LOG"
            [[ "$SERVICE_ACTIVE" == "yes" ]]
            ;;
        stop)
            printf 'stop\\n' >> "$EVENT_LOG"
            SERVICE_ACTIVE=no
            ;;
        *)
            return 1
            ;;
    esac
}}
mkdir() {{
    printf 'mkdir\\n' >> "$EVENT_LOG"
    command mkdir "$@"
}}
{_private_state_helper()}
ensure_private_state
"""
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)


def test_install_dry_run_does_not_create_or_modify_state(tmp_path):
    home = tmp_path / "home"
    state = home / ".config" / "ups-battery-monitor"
    state.mkdir(parents=True)
    journal = state / "discharge-events-v1.jsonl"
    journal.write_text("original\n")
    journal.chmod(0o640)

    result = _run_helper(home, dry_run=True)

    assert result.returncode == 0
    assert journal.read_text() == "original\n"
    assert journal.stat().st_mode & 0o777 == 0o640
    assert "Would preserve and secure existing journal" in result.stdout


def test_install_preserves_existing_journal_and_repairs_modes(tmp_path):
    home = tmp_path / "home"
    state = home / ".config" / "ups-battery-monitor"
    state.mkdir(parents=True)
    journal = state / "discharge-events-v1.jsonl"
    journal.write_text("start\n")
    journal.chmod(0o640)
    state.chmod(0o755)

    result = _run_helper(home, dry_run=False)

    assert result.returncode == 0
    assert journal.read_text() == "start\n"
    assert state.stat().st_mode & 0o777 == 0o700
    assert journal.stat().st_mode & 0o777 == 0o600


def test_install_refuses_symlink_journal_without_following_it(tmp_path):
    home = tmp_path / "home"
    state = home / ".config" / "ups-battery-monitor"
    state.mkdir(parents=True)
    target = tmp_path / "outside.jsonl"
    target.write_text("protected\n")
    (state / "discharge-events-v1.jsonl").symlink_to(target)

    result = _run_helper(home, dry_run=False)

    assert result.returncode != 0
    assert "Refusing symlink" in result.stderr
    assert target.read_text() == "protected\n"


def test_install_stops_active_service_before_mutating_private_state(tmp_path):
    home = tmp_path / "home"
    events = tmp_path / "events.log"

    result = _run_helper(home, dry_run=False, service_active=True, event_log=events)

    assert result.returncode == 0
    assert events.read_text().splitlines() == ["is-active", "stop", "mkdir"]
    assert (home / ".config" / "ups-battery-monitor" / "discharge-events-v1.jsonl").exists()
