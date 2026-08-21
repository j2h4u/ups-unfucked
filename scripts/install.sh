#!/usr/bin/env bash
# UPS Battery Monitor Installation Script
# Installs systemd service and configures NUT dummy-ups for virtual UPS proxy
# Requires: root, Python 3, systemd, NUT daemon

set -euo pipefail

# === HELP MESSAGE (before root check) ===
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<EOF
UPS Battery Monitor Installation Script

Usage: sudo bash install.sh [OPTIONS]

Options:
    --help        Show this help message
    --dry-run     Show what would be done without making changes

This script:
1. Validates prerequisites (Python 3, systemd, NUT)
2. Installs systemd service unit
3. Configures NUT dummy-ups (idempotent)
4. Switches upsmon to virtual UPS (idempotent)
5. Installs the live NUT health MOTD script (UPS address templated)
6. Enables and starts the service
7. Verifies virtual UPS is readable by NUT

Must run as root (with sudo).
EOF
    exit 0
fi

# === ROOT CHECK ===
if [[ $EUID -ne 0 ]]; then
    echo "Error: This script must run as root (sudo)" >&2
    echo "Usage: sudo bash /path/to/install.sh" >&2
    exit 1
fi

DRY_RUN="no"
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN="yes"

# === UTILITY FUNCTIONS ===

log_info() {
    echo "[INFO] $*"
}

log_error() {
    echo "[ERROR] $*" >&2
}

log_ok() {
    echo "[✓] $*"
}

run_cmd() {
    if [[ "$DRY_RUN" == "yes" ]]; then
        echo "[DRY-RUN] Would run: $*"
    else
        "$@"
    fi
}

# === PREREQUISITE VALIDATION ===

log_info "Validating prerequisites..."

# Check Python 3
if ! command -v python3 &>/dev/null; then
    log_error "Python 3 not found. Install with: apt install python3"
    exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 13))'; then
    log_error "Python 3.13 or newer is required"
    exit 1
fi
log_ok "Python 3 found: $(python3 --version)"

# Check systemd
if ! command -v systemctl &>/dev/null; then
    log_error "systemd not found"
    exit 1
fi
log_ok "systemd found"

# Check NUT daemon is running
if [[ ! -d /run/nut ]]; then
    log_error "NUT daemon not running (/run/nut/ missing)"
    echo "  Start with: sudo systemctl start nut-server" >&2
    exit 1
fi
log_ok "NUT daemon running"

# Check systemd-python (optional, informational)
if python3 -c "import systemd.journal" 2>/dev/null; then
    log_ok "systemd-python installed (journald logging enabled)"
else
    log_info "Note: systemd-python not installed (will fallback to stderr logging)"
    log_info "  To enable journald: apt install python3-systemd"
fi

log_ok "All prerequisites met"

# === SCRIPT DIRECTORY DETECTION ===

# Find the repository root (parent of scripts/ directory)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
INSTALL_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)
UPS_NAME="cyberpower"
UPS_VIRTUAL_NAME="${UPS_NAME}-virtual"
RUNTIME_DIR="/run/ups-battery-monitor"
SERVICE_SRC="$REPO_ROOT/systemd/ups-battery-monitor.service"
SERVICE_DST="/etc/systemd/system/ups-battery-monitor.service"
VIRTUAL_DRIVER_SRC="$REPO_ROOT/systemd/nut-driver@${UPS_VIRTUAL_NAME}.service"
VIRTUAL_DRIVER_DST="/etc/systemd/system/nut-driver@${UPS_VIRTUAL_NAME}.service"
LEGACY_DROPIN_DIR="/etc/systemd/system/nut-driver@${UPS_VIRTUAL_NAME}.service.d"
LEGACY_DROPIN_DST="$LEGACY_DROPIN_DIR/nut-driver-virtual.conf"
STALE_MONITOR_DROPIN_DIR="/etc/systemd/system/ups-battery-monitor.service.d"
STALE_MONITOR_DROPIN_DST="$STALE_MONITOR_DROPIN_DIR/20-stable-worktree.conf"
TARGET_WANTS_LINK="/etc/systemd/system/nut-driver.target.wants/nut-driver@${UPS_VIRTUAL_NAME}.service"
MONITOR_WANTS_DIR="/etc/systemd/system/ups-battery-monitor.service.wants"
MONITOR_WANTS_LINK="$MONITOR_WANTS_DIR/nut-driver@${UPS_VIRTUAL_NAME}.service"
DUMMY_UPS_CONFIG="$REPO_ROOT/config/dummy-ups.conf"
NUT_CONFIG="/etc/nut/ups.conf"
UPSMON_CONF="/etc/nut/upsmon.conf"

if [[ -f /usr/lib/systemd/system/nut-driver@.service ]]; then
    NUT_DRIVER_TEMPLATE="/usr/lib/systemd/system/nut-driver@.service"
elif [[ -f /lib/systemd/system/nut-driver@.service ]]; then
    NUT_DRIVER_TEMPLATE="/lib/systemd/system/nut-driver@.service"
else
    NUT_DRIVER_TEMPLATE=""
fi

if [[ -f /usr/lib/systemd/system/nut-driver.target ]]; then
    NUT_DRIVER_TARGET_UNIT="/usr/lib/systemd/system/nut-driver.target"
elif [[ -f /lib/systemd/system/nut-driver.target ]]; then
    NUT_DRIVER_TARGET_UNIT="/lib/systemd/system/nut-driver.target"
else
    NUT_DRIVER_TARGET_UNIT=""
fi

if [[ -f /usr/lib/systemd/system/nut-server.service ]]; then
    NUT_SERVER_UNIT="/usr/lib/systemd/system/nut-server.service"
elif [[ -f /lib/systemd/system/nut-server.service ]]; then
    NUT_SERVER_UNIT="/lib/systemd/system/nut-server.service"
else
    NUT_SERVER_UNIT=""
fi

TRANSACTION_ROOT=""
TRANSACTION_ACTIVE="no"
MONITOR_WANTS_DIR_CREATED="no"
NUT_SERVER_ACTIVE_BEFORE="no"
NUT_MONITOR_ACTIVE_BEFORE="no"
MONITOR_ACTIVE_BEFORE="no"
NUT_DRIVER_ACTIVE_BEFORE="no"
MONITOR_ENABLED_BEFORE="unchanged"

if [[ -z "$INSTALL_HOME" || "$INSTALL_HOME" == "/" ]]; then
    log_error "Could not resolve a safe home directory for $RUN_USER"
    exit 1
fi

log_info "Repository root: $REPO_ROOT"
log_info "Service will run as user: $RUN_USER"

if [[ ! -f "$REPO_ROOT/systemd/ups-battery-monitor.service" ]]; then
    log_error "Service file not found at $REPO_ROOT/systemd/ups-battery-monitor.service"
    exit 1
fi

if [[ ! -f "$REPO_ROOT/config/dummy-ups.conf" ]]; then
    log_error "NUT config not found at $REPO_ROOT/config/dummy-ups.conf"
    exit 1
fi

if [[ ! -x "$(command -v systemd-analyze 2>/dev/null || true)" ]]; then
    log_error "systemd-analyze not found"
    exit 1
fi

if [[ -z "$NUT_DRIVER_TEMPLATE" ]]; then
    log_error "NUT driver template unit not found"
    exit 1
fi

if [[ -z "$NUT_DRIVER_TARGET_UNIT" || -z "$NUT_SERVER_UNIT" ]]; then
    log_error "NUT target/server unit files not found"
    exit 1
fi

if [[ ! -f "$VIRTUAL_DRIVER_SRC" ]]; then
    log_error "Virtual driver unit not found at $VIRTUAL_DRIVER_SRC"
    exit 1
fi

# === STAGED UNIT AND CONFIG TRANSACTION ===

assert_restore_target_safe() {
    local target="$1"
    if [[ -L "$target" ]]; then
        log_error "Refusing symlink target in installation transaction: $target"
        return 1
    fi
    if [[ -e "$target" && ! -f "$target" ]]; then
        log_error "Installation target is not a regular file: $target"
        return 1
    fi
}

backup_transaction_file() {
    local target="$1"
    local name="$2"
    assert_restore_target_safe "$target"
    if [[ -f "$target" ]]; then
        cp -a -- "$target" "$TRANSACTION_ROOT/$name"
    else
        : > "$TRANSACTION_ROOT/$name.absent"
    fi
}

validate_dropin_path() {
    local parent="$1"
    local target="$2"

    if [[ -L "$parent" ]]; then
        log_error "Refusing symlink drop-in parent: $parent"
        return 1
    fi
    if [[ -e "$parent" && ! -d "$parent" ]]; then
        log_error "Drop-in parent is not a directory: $parent"
        return 1
    fi
    assert_restore_target_safe "$target"
}

retire_stale_monitor_dropin() {
    validate_dropin_path "$STALE_MONITOR_DROPIN_DIR" "$STALE_MONITOR_DROPIN_DST"
    if [[ "$DRY_RUN" == "yes" ]]; then
        echo "[DRY-RUN] Would remove stale monitor drop-in: $STALE_MONITOR_DROPIN_DST"
        return
    fi
    if [[ -e "$STALE_MONITOR_DROPIN_DST" || -L "$STALE_MONITOR_DROPIN_DST" ]]; then
        rm -- "$STALE_MONITOR_DROPIN_DST"
    fi
    log_ok "Stale monitor drop-in retired: $STALE_MONITOR_DROPIN_DST"
}

# shellcheck disable=SC2317 # Called from the installation transaction and tests.
backup_transaction_link() {
    local target="$1"
    local name="$2"
    local link_target

    if [[ -L "$target" ]]; then
        link_target="$(readlink -- "$target")"
        if [[ -z "$link_target" ]]; then
            log_error "Could not read transaction symlink: $target"
            return 1
        fi
        printf '%s\n' "$link_target" > "$TRANSACTION_ROOT/$name.link"
    elif [[ -e "$target" ]]; then
        log_error "Expected a symlink or absent path, found another file: $target"
        return 1
    else
        : > "$TRANSACTION_ROOT/$name.absent"
    fi
}

# shellcheck disable=SC2317 # Called from the installation transaction and rollback.
remove_owned_link() {
    local target="$1"
    if [[ -L "$target" ]]; then
        rm -- "$target"
    elif [[ -e "$target" ]]; then
        log_error "Refusing to replace non-symlink path: $target"
        return 1
    fi
}

# shellcheck disable=SC2317 # Called from the EXIT trap through rollback_transaction.
restore_transaction_link() {
    local target="$1"
    local name="$2"
    local backup="$TRANSACTION_ROOT/$name.link"
    local absent="$TRANSACTION_ROOT/$name.absent"
    local link_target

    if [[ -f "$backup" ]]; then
        link_target="$(<"$backup")"
        if [[ -z "$link_target" ]]; then
            log_error "Empty transaction symlink backup for $target"
            return 1
        fi
        remove_owned_link "$target"
        mkdir --parents -- "$(dirname -- "$target")"
        ln -s -- "$link_target" "$target"
    elif [[ -f "$absent" ]]; then
        remove_owned_link "$target"
    else
        log_error "Missing transaction symlink backup for $target"
        return 1
    fi
}

render_staged_units() {
    local stage_root="$TRANSACTION_ROOT/staged"
    local stage_service="$stage_root/ups-battery-monitor.service"
    local stage_driver="$stage_root/nut-driver@${UPS_VIRTUAL_NAME}.service"
    local stage_target="$stage_root/nut-driver.target"
    local stage_server="$stage_root/nut-server.service"
    local stage_physical="$stage_root/nut-driver@.service"
    local stage_target_wants="$stage_root/nut-driver.target.wants"

    mkdir --parents -- "$stage_target_wants"
    sed -e "s|@RUN_USER@|$RUN_USER|g" \
        -e "s|@INSTALL_DIR@|$REPO_ROOT|g" \
        -e "s|@INSTALL_HOME@|$INSTALL_HOME|g" \
        "$SERVICE_SRC" > "$stage_service"
    cp -- "$VIRTUAL_DRIVER_SRC" "$stage_driver"
    cp -- "$NUT_DRIVER_TARGET_UNIT" "$stage_target"
    cp -- "$NUT_SERVER_UNIT" "$stage_server"
    cp -- "$NUT_DRIVER_TEMPLATE" "$stage_physical"
    ln -s -- "$stage_physical" "$stage_target_wants/nut-driver@cyberpower.service"
    chmod 600 -- "$stage_service" "$stage_driver" "$stage_target" \
        "$stage_server" "$stage_physical"

    if ! SYSTEMD_UNIT_PATH="$stage_root:/usr/lib/systemd/system:/lib/systemd/system" \
        systemd-analyze verify "$stage_service" "$stage_driver" "$stage_target" \
        "$stage_server" "$stage_physical"; then
        log_error "Rendered systemd units failed verification; no installation changes made"
        return 1
    fi
    STAGED_SERVICE="$stage_service"
    STAGED_DRIVER="$stage_driver"
    # The staged topology paths are consumed by tests and by the verify receipt.
    # shellcheck disable=SC2034
    STAGED_TARGET="$stage_target"
    # shellcheck disable=SC2034
    STAGED_SERVER="$stage_server"
}

snapshot_runtime_state() {
    if [[ "$DRY_RUN" == "yes" ]]; then
        return
    fi
    NUT_SERVER_ACTIVE_BEFORE="no"
    NUT_MONITOR_ACTIVE_BEFORE="no"
    MONITOR_ACTIVE_BEFORE="no"
    NUT_DRIVER_ACTIVE_BEFORE="no"
    if systemctl is-active --quiet nut-server 2>/dev/null; then
        NUT_SERVER_ACTIVE_BEFORE="yes"
    fi
    if systemctl is-active --quiet nut-monitor 2>/dev/null; then
        NUT_MONITOR_ACTIVE_BEFORE="yes"
    fi
    if systemctl is-active --quiet ups-battery-monitor 2>/dev/null; then
        MONITOR_ACTIVE_BEFORE="yes"
    fi
    if systemctl is-active --quiet "nut-driver@${UPS_VIRTUAL_NAME}" 2>/dev/null; then
        NUT_DRIVER_ACTIVE_BEFORE="yes"
    fi

    local enabled_state
    enabled_state="$(systemctl is-enabled ups-battery-monitor 2>/dev/null || true)"
    case "$enabled_state" in
        enabled|enabled-runtime|disabled|not-found)
            MONITOR_ENABLED_BEFORE="$enabled_state"
            ;;
        *)
            log_error "Could not snapshot ups-battery-monitor enabled state: $enabled_state"
            return 1
            ;;
    esac
}

# shellcheck disable=SC2317 # These helpers are reached indirectly by the EXIT trap.
restore_transaction_file() {
    local target="$1"
    local name="$2"
    local backup="$TRANSACTION_ROOT/$name"
    local absent="$TRANSACTION_ROOT/$name.absent"

    if [[ -f "$backup" ]]; then
        local restored
        restored="$(mktemp "$(dirname "$target")/.$(basename "$target").rollback.XXXXXX")"
        cp -a -- "$backup" "$restored"
        mv -- "$restored" "$target"
    elif [[ -f "$absent" ]]; then
        if [[ -e "$target" || -L "$target" ]]; then
            rm -f -- "$target"
        fi
    else
        log_error "Missing transaction backup for $target"
        return 1
    fi
}

# shellcheck disable=SC2317 # Called from the EXIT trap through rollback_transaction.
restore_enabled_state() {
    case "$MONITOR_ENABLED_BEFORE" in
        enabled)
            systemctl enable ups-battery-monitor >/dev/null 2>&1
            ;;
        enabled-runtime)
            systemctl disable ups-battery-monitor >/dev/null 2>&1 &&
                systemctl enable --runtime ups-battery-monitor >/dev/null 2>&1
            ;;
        disabled|not-found)
            systemctl disable ups-battery-monitor >/dev/null 2>&1
            ;;
        unchanged)
            return 0
            ;;
        *)
            log_error "Unknown saved enabled state: $MONITOR_ENABLED_BEFORE"
            return 1
            ;;
    esac
}

# shellcheck disable=SC2317 # Called from the EXIT trap through rollback_transaction.
restore_active_state() {
    local restore_status=0
    local server_ready="yes"
    local monitor_ready="yes"

    if [[ "$NUT_SERVER_ACTIVE_BEFORE" == "yes" ]]; then
        if ! systemctl restart nut-server >/dev/null 2>&1; then
            log_error "Rollback could not restart previously active nut-server"
            restore_status=1
            server_ready="no"
        fi
    fi
    if [[ "$MONITOR_ACTIVE_BEFORE" == "yes" ]]; then
        if [[ "$server_ready" == "yes" ]]; then
            if ! systemctl restart ups-battery-monitor >/dev/null 2>&1; then
                log_error "Rollback could not restart previously active ups-battery-monitor"
                restore_status=1
                monitor_ready="no"
            fi
        else
            log_error "Rollback skipped ups-battery-monitor because nut-server restore failed"
            restore_status=1
            monitor_ready="no"
        fi
    fi
    if [[ "$NUT_DRIVER_ACTIVE_BEFORE" == "yes" ]]; then
        if [[ "$monitor_ready" == "yes" ]]; then
            if ! systemctl restart "nut-driver@${UPS_VIRTUAL_NAME}" >/dev/null 2>&1; then
                log_error "Rollback could not restart previously active virtual NUT driver"
                restore_status=1
            fi
        else
            log_error "Rollback skipped the virtual NUT driver because monitor restore failed"
            restore_status=1
        fi
    fi
    if [[ "$NUT_MONITOR_ACTIVE_BEFORE" == "yes" ]]; then
        if [[ "$server_ready" == "yes" ]]; then
            if ! systemctl restart nut-monitor >/dev/null 2>&1; then
                log_error "Rollback could not restart previously active nut-monitor"
                restore_status=1
            fi
        else
            log_error "Rollback skipped nut-monitor because nut-server restore failed"
            restore_status=1
        fi
    fi
    return "$restore_status"
}

# shellcheck disable=SC2317 # Called from the EXIT trap through finish_transaction.
rollback_transaction() {
    local rollback_status=0
    log_error "Installation failed; restoring exact pre-install unit and NUT files"
    set +e
    if [[ "$DRY_RUN" != "yes" ]]; then
        systemctl stop "nut-driver@${UPS_VIRTUAL_NAME}" ups-battery-monitor nut-monitor nut-server \
            >/dev/null 2>&1 || rollback_status=1
    fi
    restore_transaction_file "$SERVICE_DST" service || rollback_status=1
    restore_transaction_file "$VIRTUAL_DRIVER_DST" virtual-driver || rollback_status=1
    restore_transaction_file "$LEGACY_DROPIN_DST" legacy-dropin || rollback_status=1
    if [[ -n "${STALE_MONITOR_DROPIN_DST:-}" ]]; then
        restore_transaction_file "$STALE_MONITOR_DROPIN_DST" stale-monitor-dropin || rollback_status=1
    fi
    restore_transaction_file "$NUT_CONFIG" nut-config || rollback_status=1
    restore_transaction_file "$UPSMON_CONF" upsmon || rollback_status=1
    restore_transaction_link "$TARGET_WANTS_LINK" target-wants || rollback_status=1
    restore_transaction_link "$MONITOR_WANTS_LINK" monitor-wants || rollback_status=1
    if [[ "$MONITOR_WANTS_DIR_CREATED" == "yes" && -e "$MONITOR_WANTS_DIR" ]]; then
        rmdir -- "$MONITOR_WANTS_DIR" 2>/dev/null || rollback_status=1
    fi
    if [[ "$DRY_RUN" != "yes" ]]; then
        systemctl daemon-reload >/dev/null 2>&1 || rollback_status=1
        restore_enabled_state || rollback_status=1
        restore_active_state || rollback_status=1
    fi
    set -e
    TRANSACTION_ACTIVE="no"
    if [[ "$rollback_status" -ne 0 ]]; then
        log_error "Rollback was incomplete; inspect the installation targets before retrying"
    else
        log_ok "Installation targets restored exactly"
    fi
    return "$rollback_status"
}

# shellcheck disable=SC2317 # Called from the EXIT trap through finish_transaction.
cleanup_transaction() {
    if [[ -z "$TRANSACTION_ROOT" || ! -d "$TRANSACTION_ROOT" || -L "$TRANSACTION_ROOT" ]]; then
        return
    fi
    case "$TRANSACTION_ROOT" in
        "${TMPDIR:-/tmp}"/ups-battery-monitor-install.*) ;;
        *) log_error "Refusing to clean an unexpected transaction path: $TRANSACTION_ROOT"; return ;;
    esac
    find -P "$TRANSACTION_ROOT" -depth -type f -delete
    find -P "$TRANSACTION_ROOT" -depth -type l -delete
    find -P "$TRANSACTION_ROOT" -depth -type d -empty -delete
}

# shellcheck disable=SC2317 # Registered as an EXIT trap below.
finish_transaction() {
    local status=$?
    if [[ "$status" -ne 0 && "$TRANSACTION_ACTIVE" == "yes" ]]; then
        rollback_transaction || true
    fi
    cleanup_transaction
    exit "$status"
}

trap finish_transaction EXIT

# === PRIVATE BATTERY STATE ===
# The daemon writes one strict model and per-blackout JSONL files below this
# directory. Refuse symlinks before mutation. A Release-A global journal, when
# present, is archived byte-for-byte but never created or opened by the candidate.

ensure_private_state() {
    # consts
    local -r config_dir="$INSTALL_HOME/.config"
    local -r state_dir="$config_dir/ups-battery-monitor"
    local -r events_dir="$state_dir/events"
    local -r model="$state_dir/model.json"
    local -r legacy_journal="$state_dir/discharge-events-v1.jsonl"

    # vars
    local path

    # code
    for path in "$INSTALL_HOME" "$config_dir" "$state_dir" "$events_dir" "$model" "$legacy_journal"; do
        if [[ -L "$path" ]]; then
            log_error "Refusing symlink in battery state path: $path"
            exit 1
        fi
    done

    if [[ -e "$config_dir" && ! -d "$config_dir" ]]; then
        log_error "Battery config parent is not a directory: $config_dir"
        exit 1
    fi
    if [[ -e "$state_dir" && ! -d "$state_dir" ]]; then
        log_error "Battery state path is not a directory: $state_dir"
        exit 1
    fi
    if [[ -e "$events_dir" && ! -d "$events_dir" ]]; then
        log_error "Per-blackout event path is not a directory: $events_dir"
        exit 1
    fi
    if [[ -e "$model" && ! -f "$model" ]]; then
        log_error "Battery model is not a regular file: $model"
        exit 1
    fi
    if [[ -e "$legacy_journal" && ! -f "$legacy_journal" ]]; then
        log_error "Archived legacy journal is not a regular file: $legacy_journal"
        exit 1
    fi

    # Stop an existing writer before changing any shared private state. The
    # final service restart below starts it again after installation completes.
    if systemctl is-active --quiet ups-battery-monitor 2>/dev/null; then
        run_cmd systemctl stop ups-battery-monitor
        if [[ "$DRY_RUN" == "yes" ]]; then
            log_info "[DRY-RUN] Existing monitor service would be stopped before private state changes"
        else
            log_ok "Monitor service stopped before private state changes"
        fi
    else
        log_info "Monitor service is not active; no stop needed before private state changes"
    fi

    if [[ "$DRY_RUN" == "yes" ]]; then
        echo "[DRY-RUN] Would ensure private state directory: $state_dir (owner=$RUN_USER, mode=0700)"
        echo "[DRY-RUN] Would ensure per-blackout event directory: $events_dir (owner=$RUN_USER, mode=0700)"
        if [[ ! -e "$model" ]]; then
            echo "[DRY-RUN] Would explicitly provision strict target model: $model (owner=$RUN_USER, mode=0600)"
        fi
        if [[ -e "$legacy_journal" ]]; then
            echo "[DRY-RUN] Would preserve archived legacy journal byte-for-byte: $legacy_journal"
        fi
        return
    fi

    mkdir --parents -- "$state_dir" "$events_dir"
    chmod 700 -- "$state_dir" "$events_dir"
    chown --no-dereference "$RUN_USER:$RUN_USER" "$state_dir" "$events_dir"

    if [[ ! -e "$model" ]]; then
        PYTHONPATH="$REPO_ROOT" python3 -c '
import sys
from pathlib import Path

from src.adapters.model_owner import ModelOwner
from src.application.safety_oracle import no_later_lb_oracle

ModelOwner(
    Path(sys.argv[1]),
    safety_oracle=no_later_lb_oracle,
    create_if_missing=True,
)
' "$model"
    fi
    chmod 600 -- "$model"
    chown --no-dereference "$RUN_USER:$RUN_USER" "$model"
    if [[ -e "$legacy_journal" ]]; then
        chmod 600 -- "$legacy_journal"
        chown --no-dereference "$RUN_USER:$RUN_USER" "$legacy_journal"
        log_ok "Archived legacy journal preserved byte-for-byte: $legacy_journal"
    fi
    log_ok "Private target model and per-blackout event state ready: $state_dir"
}

TRANSACTION_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/ups-battery-monitor-install.XXXXXX")"
chmod 700 -- "$TRANSACTION_ROOT"
backup_transaction_file "$SERVICE_DST" service
backup_transaction_file "$VIRTUAL_DRIVER_DST" virtual-driver
backup_transaction_file "$LEGACY_DROPIN_DST" legacy-dropin
validate_dropin_path "$STALE_MONITOR_DROPIN_DIR" "$STALE_MONITOR_DROPIN_DST"
backup_transaction_file "$STALE_MONITOR_DROPIN_DST" stale-monitor-dropin
backup_transaction_file "$NUT_CONFIG" nut-config
backup_transaction_file "$UPSMON_CONF" upsmon
backup_transaction_link "$TARGET_WANTS_LINK" target-wants
backup_transaction_link "$MONITOR_WANTS_LINK" monitor-wants
if [[ -L "$LEGACY_DROPIN_DIR" || ( -e "$LEGACY_DROPIN_DIR" && ! -d "$LEGACY_DROPIN_DIR" ) ]]; then
    log_error "Legacy drop-in parent is not a directory: $LEGACY_DROPIN_DIR"
    exit 1
fi
if [[ -L "$MONITOR_WANTS_DIR" || ( -e "$MONITOR_WANTS_DIR" && ! -d "$MONITOR_WANTS_DIR" ) ]]; then
    log_error "Monitor wants parent is not a directory: $MONITOR_WANTS_DIR"
    exit 1
fi
if [[ ! -e "$MONITOR_WANTS_DIR" ]]; then
    MONITOR_WANTS_DIR_CREATED="yes"
fi
render_staged_units
snapshot_runtime_state
TRANSACTION_ACTIVE="yes"

ensure_private_state

# === SERVICE FILE INSTALLATION ===

log_info "Installing systemd service file..."

if [[ "$DRY_RUN" == "yes" ]]; then
    echo "[DRY-RUN] Would install: $SERVICE_SRC -> $SERVICE_DST (User=$RUN_USER, Dir=$REPO_ROOT)"
    echo "[DRY-RUN] Would install: $VIRTUAL_DRIVER_SRC -> $VIRTUAL_DRIVER_DST"
    echo "[DRY-RUN] Would remove target membership: $TARGET_WANTS_LINK"
    echo "[DRY-RUN] Would enable virtual driver under: $MONITOR_WANTS_LINK"
else
    install --owner=root --group=root --mode=0644 -- "$STAGED_SERVICE" "$SERVICE_DST"
    log_ok "Service file installed to $SERVICE_DST (User=$RUN_USER, Dir=$REPO_ROOT)"
    install --owner=root --group=root --mode=0644 -- "$STAGED_DRIVER" "$VIRTUAL_DRIVER_DST"
    log_ok "Exact virtual NUT driver installed to $VIRTUAL_DRIVER_DST"
    remove_owned_link "$TARGET_WANTS_LINK"
    if [[ -L "$LEGACY_DROPIN_DST" || ( -e "$LEGACY_DROPIN_DST" && ! -f "$LEGACY_DROPIN_DST" ) ]]; then
        log_error "Legacy NUT drop-in is not a regular file: $LEGACY_DROPIN_DST"
        exit 1
    fi
    rm -f -- "$LEGACY_DROPIN_DST"
    log_ok "Legacy enumerator-owned virtual driver drop-in retired"
fi

retire_stale_monitor_dropin

log_info "Reloading systemd daemon..."
run_cmd systemctl daemon-reload
log_ok "systemd daemon reloaded"

log_info "Enabling the exact virtual driver under the monitor service..."
if [[ "$DRY_RUN" != "yes" ]]; then
    remove_owned_link "$MONITOR_WANTS_LINK"
fi
run_cmd systemctl enable "nut-driver@${UPS_VIRTUAL_NAME}"
log_ok "Virtual NUT driver enabled via $MONITOR_WANTS_DIR"

# === NUT DUMMY-UPS CONFIG MERGE (IDEMPOTENT) ===

log_info "Configuring NUT dummy-ups..."

EXPECTED_PORT="port = ${RUNTIME_DIR}/ups-virtual.dev"
if grep -qF "$EXPECTED_PORT" "$NUT_CONFIG" 2>/dev/null && ! grep -q "dummy-once" "$NUT_CONFIG" 2>/dev/null; then
    log_ok "Dummy-ups already configured correctly in $NUT_CONFIG (skipped)"
else
    if [[ "$DRY_RUN" == "yes" ]]; then
        echo "[DRY-RUN] Would replace the cyberpower-virtual block in $NUT_CONFIG"
    else
        NUT_CONFIG_CANDIDATE="$TRANSACTION_ROOT/ups.conf.candidate"
        if [[ -f "$NUT_CONFIG" ]]; then
            cp -a -- "$NUT_CONFIG" "$NUT_CONFIG_CANDIDATE"
        else
            : > "$NUT_CONFIG_CANDIDATE"
            chmod 600 -- "$NUT_CONFIG_CANDIDATE"
        fi
        # Remove any stale [cyberpower-virtual] block before appending fresh config.
        if grep -q "cyberpower-virtual" "$NUT_CONFIG_CANDIDATE" 2>/dev/null; then
            log_info "Removing stale [cyberpower-virtual] block from $NUT_CONFIG..."
            python3 -c '
import re, sys
path = sys.argv[1]
content = open(path).read()
content = re.sub(r"\n*# [^\n]*[Vv]irtual UPS[^\n]*\n", "\n", content)
content = re.sub(r"\n\[cyberpower-virtual\].*?(?=\n\[|\Z)", "", content, flags=re.DOTALL)
open(path, "w").write(content.rstrip("\n") + "\n")
' "$NUT_CONFIG_CANDIDATE"
        fi
        printf '\n' >> "$NUT_CONFIG_CANDIDATE"
        cat "$DUMMY_UPS_CONFIG" >> "$NUT_CONFIG_CANDIDATE"
        mv -- "$NUT_CONFIG_CANDIDATE" "$NUT_CONFIG"
        log_ok "Dummy-ups config written to $NUT_CONFIG"
    fi
fi

# === NUT SERVICE RESTART ===

log_info "Restarting NUT services..."

run_cmd systemctl restart nut-server
log_ok "nut-server restarted"

if systemctl is-active --quiet nut-monitor 2>/dev/null; then
    run_cmd systemctl restart nut-monitor
    log_ok "nut-monitor restarted"
fi

# Give services time to settle
sleep 2
log_ok "Services settled"

# === UPSMON SWITCHOVER TO VIRTUAL UPS ===

if grep -q "cyberpower-virtual@localhost" "$UPSMON_CONF" 2>/dev/null; then
    log_ok "upsmon already points to cyberpower-virtual (skipped)"
elif grep -q "cyberpower@localhost" "$UPSMON_CONF" 2>/dev/null; then
    log_info "Switching upsmon from cyberpower to cyberpower-virtual..."
    if [[ "$DRY_RUN" == "yes" ]]; then
        echo "[DRY-RUN] Would sed 's/cyberpower@localhost/cyberpower-virtual@localhost/' in $UPSMON_CONF"
    else
        UPSMON_CANDIDATE="$TRANSACTION_ROOT/upsmon.conf.candidate"
        cp -a -- "$UPSMON_CONF" "$UPSMON_CANDIDATE"
        sed -i 's/cyberpower@localhost/cyberpower-virtual@localhost/' \
            "$UPSMON_CANDIDATE"
        mv -- "$UPSMON_CANDIDATE" "$UPSMON_CONF"
        log_ok "upsmon.conf updated: cyberpower → cyberpower-virtual"
        systemctl restart nut-monitor
        log_ok "nut-monitor restarted with new config"
    fi
else
    log_info "No cyberpower entry found in $UPSMON_CONF (manual config may be needed)"
fi

# === MOTD SCRIPT ===
# The live health module reads NUT directly; only its UPS address is templated
# into the installed copy, using the same mechanism as the systemd unit above.

MOTD_DIR="$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)/scripts/motd"

if [[ -d "$MOTD_DIR" ]]; then
    for motd_name in 51-ups-health.sh; do
        motd_src="$REPO_ROOT/scripts/motd/$motd_name"
        motd_dst="$MOTD_DIR/$motd_name"
        if [[ "$DRY_RUN" == "yes" ]]; then
            echo "[DRY-RUN] Would install (templated) $motd_src -> $motd_dst"
        else
            sed -e "s|@UPS_NUT_ADDRESS@|${UPS_VIRTUAL_NAME}@localhost|g" \
                "$motd_src" > "$motd_dst"
            chmod +x "$motd_dst"
            log_ok "MOTD script installed to $motd_dst"
        fi
    done
else
    log_info "MOTD directory $MOTD_DIR not found (skipping MOTD installation)"
fi

# === SERVICE ENABLEMENT AND STARTUP ===

log_info "Enabling and starting ups-battery-monitor service..."

run_cmd systemctl restart ups-battery-monitor
log_ok "Monitor service (re)started"

# Restart the exact virtual driver after NUT has reloaded its configuration.
run_cmd systemctl restart "nut-driver@${UPS_VIRTUAL_NAME}"
log_ok "NUT dummy-ups driver (re)started"

# === POST-INSTALL VERIFICATION ===

if [[ "$DRY_RUN" == "yes" ]]; then
    log_info "[DRY-RUN] Skipping verification (would check virtual UPS in real run)"
    exit 0
fi

log_info "Verifying installation..."

# Wait for virtual UPS device file
log_info "Waiting for virtual UPS device (${RUNTIME_DIR}/ups-virtual.dev)..."
TIMEOUT=10
COUNTER=0
while [[ ! -f "${RUNTIME_DIR}/ups-virtual.dev" && $COUNTER -lt $TIMEOUT ]]; do
    sleep 1
    COUNTER=$((COUNTER + 1))
done

if [[ -f "${RUNTIME_DIR}/ups-virtual.dev" ]]; then
    log_ok "Virtual UPS device created"
else
    log_error "Virtual UPS device not created after $TIMEOUT seconds"
    log_error "Daemon logs:"
    journalctl -u ups-battery-monitor -n 20 --no-pager >&2
    exit 1
fi

# Test virtual UPS readability
log_info "Testing NUT access to virtual UPS..."
if upsc "${UPS_VIRTUAL_NAME}@localhost" >/dev/null 2>&1; then
    log_ok "NUT dummy-ups readable (${UPS_VIRTUAL_NAME}@localhost)"
else
    log_error "Failed to read ${UPS_VIRTUAL_NAME} via upsc"
    log_error "Daemon logs:"
    journalctl -u ups-battery-monitor -n 20 --no-pager >&2
    exit 1
fi

# Check daemon is running
if systemctl is-active --quiet ups-battery-monitor; then
    log_ok "Daemon running"
else
    log_error "Daemon not running"
    log_error "Daemon logs:"
    journalctl -u ups-battery-monitor -n 20 --no-pager >&2
    exit 1
fi

# Enable only after all staged files and runtime verification have succeeded;
# rollback still restores the pre-install enabled state if this command fails.
run_cmd systemctl enable ups-battery-monitor
log_ok "Service enabled (will auto-start on boot)"

# === SUCCESS ===

log_info ""
log_info "=== Installation Complete ==="
log_info ""
log_info "Next steps:"
log_info "  1. Verify UPS status: upsc ${UPS_VIRTUAL_NAME}@localhost | head"
log_info "  2. View daemon logs: journalctl -u ups-battery-monitor -f"
log_info "  3. Check live UPS MOTD: bash ~/scripts/motd/51-ups-health.sh"
log_info ""
log_info "Optional: Configure upsmon for automated shutdown (see CONTEXT.md)"
log_info ""

exit 0
