#!/bin/bash
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
5. Installs MOTD scripts (install dir templated in for the bundled CLI)
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

# === SERVICE FILE INSTALLATION ===

log_info "Installing systemd service file..."

SERVICE_SRC="$REPO_ROOT/systemd/ups-battery-monitor.service"
SERVICE_DST="/etc/systemd/system/ups-battery-monitor.service"

DROPIN_SRC="$REPO_ROOT/systemd/nut-driver-virtual.conf"
DROPIN_DIR="/etc/systemd/system/nut-driver@${UPS_VIRTUAL_NAME}.service.d"

if [[ "$DRY_RUN" == "yes" ]]; then
    echo "[DRY-RUN] Would install: $SERVICE_SRC -> $SERVICE_DST (User=$RUN_USER, Dir=$REPO_ROOT)"
    echo "[DRY-RUN] Would install: $DROPIN_SRC -> $DROPIN_DIR/"
else
    sed -e "s|@RUN_USER@|$RUN_USER|g" \
        -e "s|@INSTALL_DIR@|$REPO_ROOT|g" \
        -e "s|@INSTALL_HOME@|$INSTALL_HOME|g" \
        "$SERVICE_SRC" > "$SERVICE_DST"
    chmod 644 "$SERVICE_DST"
    log_ok "Service file installed to $SERVICE_DST (User=$RUN_USER, Dir=$REPO_ROOT)"
    mkdir -p "$DROPIN_DIR"
    cp "$DROPIN_SRC" "$DROPIN_DIR/"
    chmod 644 "$DROPIN_DIR/$(basename "$DROPIN_SRC")"
    log_ok "NUT driver drop-in installed to $DROPIN_DIR/"

    # Remove legacy oneshot service if present
    if [[ -f /etc/systemd/system/ups-virtual-driver.service ]]; then
        systemctl disable --now ups-virtual-driver 2>/dev/null || true
        rm -f /etc/systemd/system/ups-virtual-driver.service
        log_ok "Removed legacy ups-virtual-driver.service"
    fi
fi

log_info "Reloading systemd daemon..."
run_cmd systemctl daemon-reload
log_ok "systemd daemon reloaded"

# === NUT DUMMY-UPS CONFIG MERGE (IDEMPOTENT) ===

log_info "Configuring NUT dummy-ups..."

NUT_CONFIG="/etc/nut/ups.conf"
DUMMY_UPS_CONFIG="$REPO_ROOT/config/dummy-ups.conf"

EXPECTED_PORT="port = ${RUNTIME_DIR}/ups-virtual.dev"
if grep -qF "$EXPECTED_PORT" "$NUT_CONFIG" 2>/dev/null && ! grep -q "dummy-once" "$NUT_CONFIG" 2>/dev/null; then
    log_ok "Dummy-ups already configured correctly in $NUT_CONFIG (skipped)"
else
    # Remove any stale [cyberpower-virtual] block before appending fresh config
    if grep -q "cyberpower-virtual" "$NUT_CONFIG" 2>/dev/null; then
        log_info "Removing stale [cyberpower-virtual] block from $NUT_CONFIG..."
        if [[ "$DRY_RUN" != "yes" ]]; then
            python3 -c '
import re, sys
path = sys.argv[1]
content = open(path).read()
content = re.sub(r"\n*# [^\n]*[Vv]irtual UPS[^\n]*\n", "\n", content)
content = re.sub(r"\n\[cyberpower-virtual\].*?(?=\n\[|\Z)", "", content, flags=re.DOTALL)
open(path, "w").write(content.rstrip("\n") + "\n")
' "$NUT_CONFIG"
        fi
    fi
    if [[ "$DRY_RUN" == "yes" ]]; then
        echo "[DRY-RUN] Would append config from $DUMMY_UPS_CONFIG to $NUT_CONFIG"
    else
        printf '\n' >> "$NUT_CONFIG"
        cat "$DUMMY_UPS_CONFIG" >> "$NUT_CONFIG"
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

UPSMON_CONF="/etc/nut/upsmon.conf"

if grep -q "cyberpower-virtual@localhost" "$UPSMON_CONF" 2>/dev/null; then
    log_ok "upsmon already points to cyberpower-virtual (skipped)"
elif grep -q "cyberpower@localhost" "$UPSMON_CONF" 2>/dev/null; then
    log_info "Switching upsmon from cyberpower to cyberpower-virtual..."
    if [[ "$DRY_RUN" == "yes" ]]; then
        echo "[DRY-RUN] Would sed 's/cyberpower@localhost/cyberpower-virtual@localhost/' in $UPSMON_CONF"
    else
        sed -i.bak 's/cyberpower@localhost/cyberpower-virtual@localhost/' "$UPSMON_CONF"
        log_ok "upsmon.conf updated: cyberpower → cyberpower-virtual"
        systemctl restart nut-monitor
        log_ok "nut-monitor restarted with new config"
    fi
else
    log_info "No cyberpower entry found in $UPSMON_CONF (manual config may be needed)"
fi

# === MOTD SCRIPTS ===
# All three modules read prepared data from `python3 -m src.motd_status`, so the
# install dir (REPO_ROOT) is templated into each via @INSTALL_DIR@ at deploy time —
# the same mechanism used for the systemd unit above. No jq dependency.

MOTD_DIR="$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)/scripts/motd"

if [[ -d "$MOTD_DIR" ]]; then
    for motd_name in 51-ups.sh 51-ups-health.sh; do
        motd_src="$REPO_ROOT/scripts/motd/$motd_name"
        motd_dst="$MOTD_DIR/$motd_name"
        if [[ "$DRY_RUN" == "yes" ]]; then
            echo "[DRY-RUN] Would install (templated) $motd_src -> $motd_dst"
        else
            sed -e "s|@INSTALL_DIR@|$REPO_ROOT|g" \
                -e "s|@UPS_NUT_ADDRESS@|${UPS_VIRTUAL_NAME}@localhost|g" \
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

run_cmd systemctl enable ups-battery-monitor
log_ok "Service enabled (will auto-start on boot)"

run_cmd systemctl restart ups-battery-monitor
log_ok "Monitor service (re)started"

# NUT driver drop-in handles dependency — restart dummy-ups after daemon is ready
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

# === SUCCESS ===

log_info ""
log_info "=== Installation Complete ==="
log_info ""
log_info "Next steps:"
log_info "  1. Verify UPS status: upsc ${UPS_VIRTUAL_NAME}@localhost | head"
log_info "  2. View daemon logs: journalctl -u ups-battery-monitor -f"
log_info "  3. Check MOTD: bash ~/scripts/motd/51-ups-health.sh"
log_info ""
log_info "Optional: Configure upsmon for automated shutdown (see CONTEXT.md)"
log_info ""

exit 0
