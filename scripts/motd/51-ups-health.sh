#!/bin/bash
# MOTD module: UPS battery health status
# Displays: status icon, charge%, runtime, load%, and health percentage
# Colors: green (healthy), yellow (warning), red (critical)
#
# Live UPS metrics come from NUT (upsc) — no jq or bundled CLI dependency.

set -o pipefail

# Source optional shared color definitions when present.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/colors.sh" ]]; then
    source "$SCRIPT_DIR/colors.sh"
else
    # Fallback color definitions
    RED='\033[0;31m'
    YELLOW='\033[1;33m'
    GREEN='\033[0;32m'
    DIM='\033[2m'
    NC='\033[0m'  # No color
fi

# Virtual UPS NUT address; @UPS_NUT_ADDRESS@ is filled in by scripts/install.sh
# (from UPS_VIRTUAL_NAME). Override with UPS_NUT_ADDRESS for local testing.
UPS_NUT_ADDRESS="${UPS_NUT_ADDRESS:-@UPS_NUT_ADDRESS@}"

# Read virtual UPS metrics; exit silently if daemon or NUT is not running.
# MOTD runs on every login — it must not print errors when the monitor is down.
ups_data=$(upsc "$UPS_NUT_ADDRESS" 2>/dev/null) || exit 0

# Parse live fields from NUT
ups_status=$(echo "$ups_data" | grep "^ups.status:" | cut -d' ' -f2-)
charge=$(echo "$ups_data" | grep "^battery.charge:" | cut -d' ' -f2 | cut -d'.' -f1)
runtime=$(echo "$ups_data" | grep "^battery.runtime:" | cut -d' ' -f2)
load=$(echo "$ups_data" | grep "^ups.load:" | cut -d' ' -f2 | cut -d'.' -f1)
soh_pct=$(echo "$ups_data" | grep "^battery.health:" | cut -d' ' -f2 | cut -d'.' -f1)

# Format runtime: convert seconds to minutes/hours
if [[ -n "$runtime" && "$runtime" -gt 0 ]] 2>/dev/null; then
    hours=$((runtime / 3600))
    mins=$(( (runtime % 3600) / 60 ))
    if [[ $hours -gt 0 ]]; then
        rt_fmt="${hours}h${mins}m"
    else
        rt_fmt="${mins}m"
    fi
else
    rt_fmt="?"
fi

# Format and color SoH as percentage
if [[ -n "$soh_pct" ]]; then
    soh_fmt="${soh_pct}%"
    if [[ "$soh_pct" -ge 80 ]]; then
        soh_color="$GREEN"
    elif [[ "$soh_pct" -ge 60 ]]; then
        soh_color="$YELLOW"
    else
        soh_color="$RED"
    fi
else
    soh_fmt="?"
    soh_color="$DIM"
fi

# Status icon and color
if [[ "$ups_status" == *"OB"* ]]; then
    st_color="$YELLOW"
    st_label="On Battery"
    icon="⚡"
elif [[ "$ups_status" == *"OL"* ]]; then
    st_color="$GREEN"
    st_label="Online"
    icon="✓"
else
    st_color="$DIM"
    st_label="$ups_status"
    icon="?"
fi

# Output single line
printf '%b\n' "  ${st_color}${icon}${NC} UPS: ${st_label}${NC} ${DIM}·${NC} charge ${charge}% ${DIM}·${NC} runtime ${rt_fmt} ${DIM}·${NC} load ${load}% ${DIM}·${NC} health ${soh_color}${soh_fmt}${NC}"
