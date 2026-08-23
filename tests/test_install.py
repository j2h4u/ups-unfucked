"""Targeted tests for install.sh's private battery-state handling."""

import shlex
import subprocess
from pathlib import Path

INSTALL = Path(__file__).parents[1] / "scripts" / "install.sh"


def test_installer_requires_runtime_python_313_before_mutations() -> None:
    source = INSTALL.read_text()
    version_check = "sys.version_info < (3, 13)"
    assert version_check in source
    assert source.index(version_check) < source.index("ensure_private_state() {")


def _private_state_helper() -> str:
    source = INSTALL.read_text()
    start = source.index("ensure_private_state() {")
    end = source.index("\nTRANSACTION_ROOT=", start)
    return source[start:end]


def _transaction_helpers() -> str:
    source = INSTALL.read_text()
    start = source.index("assert_restore_target_safe() {")
    end = source.index("\ntrap finish_transaction EXIT", start)
    return source[start:end]


def _render_helper() -> str:
    source = INSTALL.read_text()
    start = source.index("render_staged_units() {")
    end = source.index("\n# shellcheck disable=SC2317", start)
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
REPO_ROOT={shlex.quote(str(INSTALL.parents[1]))}
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

    result = _run_helper(home, dry_run=True)

    assert result.returncode == 0
    assert "Would ensure private state directory" in result.stdout


def test_install_ensures_private_state_and_repairs_modes(tmp_path):
    home = tmp_path / "home"
    state = home / ".local" / "state" / "ups-battery-monitor"
    state.mkdir(parents=True)
    state.chmod(0o755)

    result = _run_helper(home, dry_run=False)

    assert result.returncode == 0
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / "model.json").is_file()


def test_install_stops_active_service_before_mutating_private_state(tmp_path):
    home = tmp_path / "home"
    events = tmp_path / "events.log"

    result = _run_helper(home, dry_run=False, service_active=True, event_log=events)

    assert result.returncode == 0
    assert events.read_text().splitlines() == ["is-active", "stop", "mkdir"]
    state = home / ".local" / "state" / "ups-battery-monitor"
    assert (state / "model.json").is_file()


def test_fresh_install_creates_flat_xdg_state_layout(tmp_path):
    home = tmp_path / "home"

    result = _run_helper(home, dry_run=False)

    assert result.returncode == 0
    state = home / ".local" / "state" / "ups-battery-monitor"
    assert (state / "model.json").is_file()
    assert not (state / "events").exists()


def test_install_migrates_legacy_state_without_copy_or_backup(tmp_path):
    home = tmp_path / "home"
    legacy = home / ".config" / "ups-battery-monitor"
    events = legacy / "events"
    events.mkdir(parents=True)
    (legacy / "model.json").write_text('{"model":1}\n')
    (events / "telemetry.jsonl").write_text('{"at":"old"}\n')
    (events / "history.jsonl").write_text('{"kind":"old"}\n')

    result = _run_helper(home, dry_run=False)

    assert result.returncode == 0, result.stderr
    state = home / ".local" / "state" / "ups-battery-monitor"
    assert (state / "model.json").read_text() == '{"model":1}\n'
    assert (state / "telemetry.jsonl").read_text() == '{"at":"old"}\n'
    assert (state / "history.jsonl").read_text() == '{"kind":"old"}\n'
    assert not legacy.exists()


def test_install_refuses_ambiguous_state_migration(tmp_path):
    home = tmp_path / "home"
    legacy = home / ".config" / "ups-battery-monitor"
    target = home / ".local" / "state" / "ups-battery-monitor"
    legacy.mkdir(parents=True)
    target.mkdir(parents=True)
    (legacy / "model.json").write_text("old\n")
    (target / "model.json").write_text("new\n")

    result = _run_helper(home, dry_run=False)

    assert result.returncode != 0
    assert "Both legacy and target battery state exist: model.json" in result.stderr
    assert (legacy / "model.json").read_text() == "old\n"
    assert (target / "model.json").read_text() == "new\n"


def test_install_renders_and_verifies_units_before_mutation(tmp_path):
    stage = tmp_path / "transaction"
    fixture_units = tmp_path / "systemd"
    fixture_units.mkdir()
    target = fixture_units / "nut-driver.target"
    server = fixture_units / "nut-server.service"
    driver = fixture_units / "nut-driver@.service"
    target.write_text("[Unit]\nDescription=NUT drivers\n")
    server.write_text(
        "[Unit]\nDescription=NUT server\n[Service]\nType=oneshot\nExecStart=/bin/true\n"
    )
    driver.write_text(
        "[Unit]\nDescription=NUT driver %i\n"
        "[Service]\nType=oneshot\nExecStart=/bin/true\nRemainAfterExit=yes\n"
        "[Install]\nWantedBy=nut-driver.target\n"
    )
    command = f"""
set -euo pipefail
REPO_ROOT={shlex.quote(str(INSTALL.parents[1]))}
SERVICE_SRC="$REPO_ROOT/systemd/ups-battery-monitor.service"
VIRTUAL_DRIVER_SRC="$REPO_ROOT/systemd/nut-driver@cyberpower-virtual.service"
NUT_DRIVER_TARGET_UNIT={shlex.quote(str(target))}
NUT_SERVER_UNIT={shlex.quote(str(server))}
NUT_DRIVER_TEMPLATE={shlex.quote(str(driver))}
TRANSACTION_ROOT={shlex.quote(str(stage))}
RUN_USER=j2h4u
INSTALL_HOME=/home/j2h4u
UPS_VIRTUAL_NAME=cyberpower-virtual
log_error() {{ echo "$*" >&2; }}
mkdir --parents -- "$TRANSACTION_ROOT"
{_render_helper()}
render_staged_units
test -s "$STAGED_SERVICE"
test -s "$STAGED_DRIVER"
test -s "$STAGED_TARGET"
test -s "$STAGED_SERVER"
test ! -e "$TRANSACTION_ROOT/staged/nut-driver.target.wants/nut-driver@cyberpower-virtual.service"
grep -q 'WantedBy=ups-battery-monitor.service' "$STAGED_DRIVER"
! grep -q '@[A-Z_]*@' "$STAGED_SERVICE"
ROOT_FIXTURE="$TRANSACTION_ROOT/root"
mkdir --parents -- "$ROOT_FIXTURE/etc/systemd/system"
cp -- "$STAGED_DRIVER" "$ROOT_FIXTURE/etc/systemd/system/"
systemctl --root="$ROOT_FIXTURE" enable nut-driver@cyberpower-virtual.service
test -L "$ROOT_FIXTURE/etc/systemd/system/ups-battery-monitor.service.wants/nut-driver@cyberpower-virtual.service"
test ! -e "$ROOT_FIXTURE/etc/systemd/system/nut-driver.target.wants/nut-driver@cyberpower-virtual.service"
"""
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_install_transaction_restores_exact_unit_and_nut_files(tmp_path):
    service = tmp_path / "ups-battery-monitor.service"
    virtual_driver = tmp_path / "nut-driver@cyberpower-virtual.service"
    nut_config = tmp_path / "ups.conf"
    upsmon = tmp_path / "upsmon.conf"
    for path, contents in (
        (service, "old service\n"),
        (virtual_driver, "old exact driver\n"),
        (nut_config, "old NUT\n"),
        (upsmon, "old upsmon\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        path.chmod(0o640)
    transaction = tmp_path / "ups-battery-monitor-install.test"
    transaction.mkdir()
    target_wants = tmp_path / "nut-driver.target.wants" / "nut-driver@cyberpower-virtual.service"
    monitor_wants = (
        tmp_path / "ups-battery-monitor.service.wants" / "nut-driver@cyberpower-virtual.service"
    )
    target_wants.parent.mkdir()
    monitor_wants.parent.mkdir()
    target_wants.symlink_to("/usr/lib/systemd/system/nut-driver@.service")
    monitor_wants.symlink_to("../nut-driver@cyberpower-virtual.service")
    command = f"""
set -euo pipefail
DRY_RUN=yes
TRANSACTION_ROOT={shlex.quote(str(transaction))}
SERVICE_DST={shlex.quote(str(service))}
VIRTUAL_DRIVER_DST={shlex.quote(str(virtual_driver))}
NUT_CONFIG={shlex.quote(str(nut_config))}
UPSMON_CONF={shlex.quote(str(upsmon))}
TARGET_WANTS_LINK={shlex.quote(str(target_wants))}
MONITOR_WANTS_LINK={shlex.quote(str(monitor_wants))}
MONITOR_WANTS_DIR={shlex.quote(str(monitor_wants.parent))}
MONITOR_WANTS_DIR_CREATED=no
log_error() {{ echo "$*" >&2; }}
log_ok() {{ echo "$*"; }}
{_transaction_helpers()}
backup_transaction_file "$SERVICE_DST" service
backup_transaction_file "$VIRTUAL_DRIVER_DST" virtual-driver
backup_transaction_file "$NUT_CONFIG" nut-config
backup_transaction_file "$UPSMON_CONF" upsmon
backup_transaction_link "$TARGET_WANTS_LINK" target-wants
backup_transaction_link "$MONITOR_WANTS_LINK" monitor-wants
printf 'new service\n' > "$SERVICE_DST"
printf 'new exact driver\n' > "$VIRTUAL_DRIVER_DST"
printf 'new NUT\n' > "$NUT_CONFIG"
printf 'new upsmon\n' > "$UPSMON_CONF"
rm -- "$TARGET_WANTS_LINK" "$MONITOR_WANTS_LINK"
rollback_transaction
cmp -- "$TRANSACTION_ROOT/service" "$SERVICE_DST"
cmp -- "$TRANSACTION_ROOT/virtual-driver" "$VIRTUAL_DRIVER_DST"
cmp -- "$TRANSACTION_ROOT/nut-config" "$NUT_CONFIG"
cmp -- "$TRANSACTION_ROOT/upsmon" "$UPSMON_CONF"
test -L "$TARGET_WANTS_LINK"
test "$(readlink -- "$TARGET_WANTS_LINK")" = /usr/lib/systemd/system/nut-driver@.service
test -L "$MONITOR_WANTS_LINK"
test "$(readlink -- "$MONITOR_WANTS_LINK")" = ../nut-driver@cyberpower-virtual.service
test "$(stat -c %a "$SERVICE_DST")" = 640
"""
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def _runtime_state_test_command(tmp_path: Path, *, fail_unit: str = "") -> str:
    event_log = tmp_path / "runtime-events.log"
    return f"""
set -euo pipefail
DRY_RUN=no
UPS_VIRTUAL_NAME=cyberpower-virtual
EVENT_LOG={shlex.quote(str(event_log))}
FAIL_UNIT={shlex.quote(fail_unit)}
ACTIVE_NUT_SERVER=yes
ACTIVE_NUT_MONITOR=yes
ACTIVE_MONITOR=yes
ACTIVE_DRIVER=yes
ENABLED_STATE=enabled
log_error() {{ echo "$*" >&2; }}
systemctl() {{
    case "$1" in
        is-active)
            case "$3" in
                nut-server) [[ "$ACTIVE_NUT_SERVER" == yes ]] ;;
                nut-monitor) [[ "$ACTIVE_NUT_MONITOR" == yes ]] ;;
                ups-battery-monitor) [[ "$ACTIVE_MONITOR" == yes ]] ;;
                nut-driver@cyberpower-virtual) [[ "$ACTIVE_DRIVER" == yes ]] ;;
                *) return 1 ;;
            esac
            ;;
        is-enabled)
            printf '%s\\n' "$ENABLED_STATE"
            ;;
        restart)
            printf 'restart %s\\n' "$2" >> "$EVENT_LOG"
            [[ -z "$FAIL_UNIT" || "$2" != "$FAIL_UNIT" ]]
            ;;
        enable|disable)
            printf '%s\\n' "$1" >> "$EVENT_LOG"
            ;;
        stop|daemon-reload)
            printf '%s\\n' "$1" >> "$EVENT_LOG"
            ;;
        *) return 1 ;;
    esac
}}
{_transaction_helpers()}
snapshot_runtime_state
"""


def test_install_snapshots_and_restores_only_previously_active_units(tmp_path):
    command = (
        _runtime_state_test_command(tmp_path)
        + """
test "$NUT_SERVER_ACTIVE_BEFORE" = yes
test "$NUT_MONITOR_ACTIVE_BEFORE" = yes
test "$MONITOR_ACTIVE_BEFORE" = yes
test "$NUT_DRIVER_ACTIVE_BEFORE" = yes
test "$MONITOR_ENABLED_BEFORE" = enabled
restore_enabled_state
restore_active_state
grep -Fx 'restart nut-server' "$EVENT_LOG"
grep -Fx 'restart ups-battery-monitor' "$EVENT_LOG"
grep -Fx 'restart nut-driver@cyberpower-virtual' "$EVENT_LOG"
grep -Fx 'restart nut-monitor' "$EVENT_LOG"
"""
    )
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_install_rollback_leaves_previously_inactive_units_inactive(tmp_path):
    command = (
        _runtime_state_test_command(tmp_path)
        + """
ACTIVE_NUT_SERVER=no
ACTIVE_NUT_MONITOR=no
ACTIVE_MONITOR=no
ACTIVE_DRIVER=no
ENABLED_STATE=disabled
snapshot_runtime_state
test "$MONITOR_ENABLED_BEFORE" = disabled
restore_enabled_state
restore_active_state
grep -Fx disable "$EVENT_LOG"
! grep -q '^restart ' "$EVENT_LOG"
"""
    )
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_install_rollback_removes_partial_enable_from_fresh_install(tmp_path):
    command = (
        _runtime_state_test_command(tmp_path)
        + """
ENABLED_STATE=not-found
snapshot_runtime_state
test "$MONITOR_ENABLED_BEFORE" = not-found
restore_enabled_state
grep -Fx disable "$EVENT_LOG"
"""
    )
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_install_rollback_restores_runtime_only_enablement(tmp_path):
    command = (
        _runtime_state_test_command(tmp_path)
        + """
ENABLED_STATE=enabled-runtime
snapshot_runtime_state
restore_enabled_state
test "$(sed -n '1p' "$EVENT_LOG")" = disable
test "$(sed -n '2p' "$EVENT_LOG")" = enable
"""
    )
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_install_rollback_surfaces_partial_runtime_restore_failure(tmp_path):
    command = (
        _runtime_state_test_command(tmp_path, fail_unit="nut-driver@cyberpower-virtual")
        + f"""
TRANSACTION_ROOT={shlex.quote(str(tmp_path / "rollback"))}
SERVICE_DST="$TRANSACTION_ROOT/service.target"
VIRTUAL_DRIVER_DST="$TRANSACTION_ROOT/virtual-driver.target"
NUT_CONFIG="$TRANSACTION_ROOT/ups.target"
UPSMON_CONF="$TRANSACTION_ROOT/upsmon.target"
TARGET_WANTS_LINK="$TRANSACTION_ROOT/target-wants.link"
MONITOR_WANTS_LINK="$TRANSACTION_ROOT/monitor-wants.link"
MONITOR_WANTS_DIR="$TRANSACTION_ROOT/monitor-wants.d"
MONITOR_WANTS_DIR_CREATED=no
mkdir --parents -- "$TRANSACTION_ROOT" "$MONITOR_WANTS_DIR"
printf 'old\\n' > "$SERVICE_DST"
printf 'old\\n' > "$VIRTUAL_DRIVER_DST"
printf 'old\\n' > "$NUT_CONFIG"
printf 'old\\n' > "$UPSMON_CONF"
backup_transaction_file "$SERVICE_DST" service
backup_transaction_file "$VIRTUAL_DRIVER_DST" virtual-driver
backup_transaction_file "$NUT_CONFIG" nut-config
backup_transaction_file "$UPSMON_CONF" upsmon
backup_transaction_link "$TARGET_WANTS_LINK" target-wants
backup_transaction_link "$MONITOR_WANTS_LINK" monitor-wants
if rollback_transaction; then
    echo 'expected rollback failure' >&2
    exit 1
fi
"""
    )
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert "previously active virtual NUT driver" in result.stderr
    assert "Rollback was incomplete" in result.stderr


def test_install_defers_enable_until_runtime_verification():
    source = INSTALL.read_text()

    verification = source.index("# Check daemon is running")
    enablement = source.rindex("run_cmd systemctl enable ups-battery-monitor")

    assert enablement > verification
