#!/bin/bash
# MOTD module: UPS battery health status
# Displays: status icon, charge%, runtime, load%, SoH%, replacement date
# Colors: green (healthy), yellow (warning), red (critical)

set -o pipefail

# Source color definitions (should exist from 51-ups.sh or similar)
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

# Path to model.json (same as monitor.py)
MODEL_FILE="${HOME}/.config/ups-battery-monitor/model.json"

# Path to health endpoint (tmpfs, written every poll)
HEALTH_FILE="/run/ups-battery-monitor/ups-health.json"

# Read virtual UPS metrics (Phase 3 infrastructure)
# Format: `upsc` returns key: value pairs
ups_data=$(upsc cyberpower-virtual@localhost 2>/dev/null) || exit 0

# Parse fields
ups_status=$(echo "$ups_data" | grep "^ups.status:" | cut -d' ' -f2-)
charge=$(echo "$ups_data" | grep "^battery.charge:" | cut -d' ' -f2 | cut -d'.' -f1)
runtime=$(echo "$ups_data" | grep "^battery.runtime:" | cut -d' ' -f2)
load=$(echo "$ups_data" | grep "^ups.load:" | cut -d' ' -f2 | cut -d'.' -f1)

# Read SoH and replacement date from model.json (using jq)
soh="?"
replacement_date=""

if [[ -f "$MODEL_FILE" ]]; then
    soh=$(jq -r '.soh // empty' "$MODEL_FILE" 2>/dev/null)

    # Try to read replacement_date from soh_history last entry's prediction
    # (This would be stored by monitor.py; for now, compute from history)
    if [[ -n "$soh" && "$soh" != "?" ]]; then
        replacement_date=$(jq -r '.replacement_due // empty' "$MODEL_FILE" 2>/dev/null)
    fi
fi

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
if [[ -n "$soh" && "$soh" != "?" && "$soh" =~ ^[0-9]*\.?[0-9]+$ ]]; then
    soh_pct=$(printf "%.0f" "$(awk -v soh="$soh" 'BEGIN {printf "%.0f\n", soh * 100}')")
    soh_fmt="${soh_pct}%"

    # Color based on health
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

# Replacement date suffix (only shown when prediction exists)
repl_suffix=""
if [[ -n "$replacement_date" ]]; then
    repl_color="$DIM"
    # Color red if within 3 months
    if [[ "$replacement_date" =~ ^[0-9]{4}-[0-9]{2} ]]; then
        current_date=$(date +%s)
        repl_date_sec=$(date -d "$replacement_date" +%s 2>/dev/null || echo "$current_date")
        days_diff=$(( ($repl_date_sec - $current_date) / 86400 ))
        if [[ $days_diff -le 90 ]]; then
            repl_color="$RED"
        fi
    fi
    repl_suffix=" ${DIM}·${NC} replace by ${repl_color}${replacement_date}${NC}"
fi

# Output single line
echo -e "  ${st_color}${icon}${NC} UPS: ${st_label}${NC} ${DIM}·${NC} charge ${charge}% ${DIM}·${NC} runtime ${rt_fmt} ${DIM}·${NC} load ${load}% ${DIM}·${NC} health ${soh_color}${soh_fmt}${NC}${repl_suffix}"
