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
4. Enables and starts the service
5. Verifies virtual UPS is readable by NUT

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

DRY_RUN="${1:---dry-run}" && [[ "$1" == "--dry-run" ]] && DRY_RUN="yes" || DRY_RUN="no"

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

log_info "Repository root: $REPO_ROOT"

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

if [[ "$DRY_RUN" == "yes" ]]; then
    echo "[DRY-RUN] Would install: $SERVICE_SRC -> $SERVICE_DST"
else
    cp "$SERVICE_SRC" "$SERVICE_DST"
    chmod 644 "$SERVICE_DST"
    log_ok "Service file installed to $SERVICE_DST"
fi

log_info "Reloading systemd daemon..."
run_cmd systemctl daemon-reload
log_ok "systemd daemon reloaded"

# === NUT DUMMY-UPS CONFIG MERGE (IDEMPOTENT) ===

log_info "Configuring NUT dummy-ups..."

NUT_CONFIG="/etc/nut/ups.conf"
DUMMY_UPS_CONFIG="$REPO_ROOT/config/dummy-ups.conf"

# Idempotent guard: check if already configured
if grep -q "cyberpower-virtual" "$NUT_CONFIG" 2>/dev/null; then
    log_ok "Dummy-ups already configured in $NUT_CONFIG (skipped)"
else
    if [[ "$DRY_RUN" == "yes" ]]; then
        echo "[DRY-RUN] Would append config from $DUMMY_UPS_CONFIG to $NUT_CONFIG"
    else
        cat "$DUMMY_UPS_CONFIG" >> "$NUT_CONFIG"
        log_ok "Dummy-ups config appended to $NUT_CONFIG"
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

# === SERVICE ENABLEMENT AND STARTUP ===

log_info "Enabling and starting ups-battery-monitor service..."

run_cmd systemctl enable ups-battery-monitor
log_ok "Service enabled (will auto-start on boot)"

run_cmd systemctl start ups-battery-monitor
log_ok "Service started"

# === POST-INSTALL VERIFICATION ===

if [[ "$DRY_RUN" == "yes" ]]; then
    log_info "[DRY-RUN] Skipping verification (would check virtual UPS in real run)"
    exit 0
fi

log_info "Verifying installation..."

# Wait for virtual UPS device file
log_info "Waiting for virtual UPS device (/dev/shm/ups-virtual.dev)..."
TIMEOUT=10
COUNTER=0
while [[ ! -f /dev/shm/ups-virtual.dev && $COUNTER -lt $TIMEOUT ]]; do
    sleep 1
    COUNTER=$((COUNTER + 1))
done

if [[ -f /dev/shm/ups-virtual.dev ]]; then
    log_ok "Virtual UPS device created"
else
    log_error "Virtual UPS device not created after $TIMEOUT seconds"
    log_error "Daemon logs:"
    journalctl -u ups-battery-monitor -n 20 --no-pager >&2
    exit 1
fi

# Test virtual UPS readability
log_info "Testing NUT access to virtual UPS..."
if upsc cyberpower-virtual@localhost >/dev/null 2>&1; then
    log_ok "NUT dummy-ups readable (cyberpower-virtual@localhost)"
else
    log_error "Failed to read cyberpower-virtual via upsc"
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
log_info "  1. Verify UPS status: upsc cyberpower-virtual@localhost | head"
log_info "  2. View daemon logs: journalctl -u ups-battery-monitor -f"
log_info "  3. Check MOTD: bash ~/scripts/motd/51-ups-health.sh"
log_info ""
log_info "Optional: Configure upsmon for automated shutdown (see CONTEXT.md)"
log_info ""

exit 0
