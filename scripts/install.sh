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
5. Installs MOTD health script, patches existing 51-ups.sh
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

DRIVER_SRC="$REPO_ROOT/systemd/ups-virtual-driver.service"
DRIVER_DST="/etc/systemd/system/ups-virtual-driver.service"

if [[ "$DRY_RUN" == "yes" ]]; then
    echo "[DRY-RUN] Would install: $SERVICE_SRC -> $SERVICE_DST"
    echo "[DRY-RUN] Would install: $DRIVER_SRC -> $DRIVER_DST"
else
    cp "$SERVICE_SRC" "$SERVICE_DST"
    chmod 644 "$SERVICE_DST"
    log_ok "Service file installed to $SERVICE_DST"
    cp "$DRIVER_SRC" "$DRIVER_DST"
    chmod 644 "$DRIVER_DST"
    log_ok "Driver oneshot installed to $DRIVER_DST"
fi

log_info "Reloading systemd daemon..."
run_cmd systemctl daemon-reload
log_ok "systemd daemon reloaded"

# === NUT DUMMY-UPS CONFIG MERGE (IDEMPOTENT) ===

log_info "Configuring NUT dummy-ups..."

NUT_CONFIG="/etc/nut/ups.conf"
DUMMY_UPS_CONFIG="$REPO_ROOT/config/dummy-ups.conf"

NUT_NEW_PORT="port = /run/ups-battery-monitor/ups-virtual.dev"
if grep -q "$NUT_NEW_PORT" "$NUT_CONFIG" 2>/dev/null; then
    log_ok "Dummy-ups already configured correctly in $NUT_CONFIG (skipped)"
elif grep -q "cyberpower-virtual" "$NUT_CONFIG" 2>/dev/null; then
    log_info "Updating dummy-ups port path in $NUT_CONFIG (migrating from /dev/shm)..."
    if [[ "$DRY_RUN" == "yes" ]]; then
        echo "[DRY-RUN] Would update [cyberpower-virtual] port in $NUT_CONFIG"
    else
        python3 -c "
import re, sys
content = open('$NUT_CONFIG').read()
content = re.sub(r'\n\[cyberpower-virtual\].*?(?=\n\[|\Z)', '', content, flags=re.DOTALL)
content = content.rstrip('\n') + '\n'
content += open('$DUMMY_UPS_CONFIG').read()
open('$NUT_CONFIG', 'w').write(content)
"
        log_ok "Updated [cyberpower-virtual] port path in $NUT_CONFIG"
    fi
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

MOTD_DIR="$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)/scripts/motd"

# Install new health script
HEALTH_SRC="$REPO_ROOT/scripts/motd/51-ups-health.sh"
HEALTH_DST="$MOTD_DIR/51-ups-health.sh"

if [[ -d "$MOTD_DIR" ]]; then
    if [[ "$DRY_RUN" == "yes" ]]; then
        echo "[DRY-RUN] Would copy $HEALTH_SRC -> $HEALTH_DST"
    else
        cp "$HEALTH_SRC" "$HEALTH_DST"
        chmod +x "$HEALTH_DST"
        log_ok "MOTD health script installed to $HEALTH_DST"
    fi

    # Patch existing 51-ups.sh to use virtual UPS
    UPS_MOTD="$MOTD_DIR/51-ups.sh"
    if [[ -f "$UPS_MOTD" ]]; then
        if grep -q "cyberpower-virtual@localhost" "$UPS_MOTD" 2>/dev/null; then
            log_ok "51-ups.sh already uses cyberpower-virtual (skipped)"
        elif grep -q "cyberpower@localhost" "$UPS_MOTD" 2>/dev/null; then
            if [[ "$DRY_RUN" == "yes" ]]; then
                echo "[DRY-RUN] Would patch 51-ups.sh: cyberpower → cyberpower-virtual"
            else
                sed -i 's/cyberpower@localhost/cyberpower-virtual@localhost/' "$UPS_MOTD"
                log_ok "51-ups.sh patched: cyberpower → cyberpower-virtual"
            fi
        fi
    fi
else
    log_info "MOTD directory $MOTD_DIR not found (skipping MOTD installation)"
fi

# === SERVICE ENABLEMENT AND STARTUP ===

log_info "Enabling and starting ups-battery-monitor service..."

run_cmd systemctl enable ups-battery-monitor
run_cmd systemctl enable ups-virtual-driver
log_ok "Services enabled (will auto-start on boot)"

run_cmd systemctl restart ups-battery-monitor
log_ok "Monitor service (re)started"

# Driver oneshot will wait for device file, then start dummy-ups
run_cmd systemctl restart ups-virtual-driver
log_ok "Virtual UPS driver (re)started"

# === POST-INSTALL VERIFICATION ===

if [[ "$DRY_RUN" == "yes" ]]; then
    log_info "[DRY-RUN] Skipping verification (would check virtual UPS in real run)"
    exit 0
fi

log_info "Verifying installation..."

# Wait for virtual UPS device file
log_info "Waiting for virtual UPS device (/run/ups-battery-monitor/ups-virtual.dev)..."
TIMEOUT=10
COUNTER=0
while [[ ! -f /run/ups-battery-monitor/ups-virtual.dev && $COUNTER -lt $TIMEOUT ]]; do
    sleep 1
    COUNTER=$((COUNTER + 1))
done

if [[ -f /run/ups-battery-monitor/ups-virtual.dev ]]; then
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
