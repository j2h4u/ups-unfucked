#!/bin/bash
# MOTD module: UPS battery capacity estimation tracking
# Displays: latest measured capacity, convergence badge, sample progress, confidence
# Output: "Capacity: X.XAh (measured) vs Y.YAh (rated) · STATUS_BADGE · N/3 samples · NN% confidence"
#
# All data (capacity, convergence status, confidence, new-battery flag) comes from
# the bundled CLI (python3 -m src.motd_status) — no jq, no embedded convergence math.
# @INSTALL_DIR@ is filled in by scripts/install.sh; override with UPS_PKG_DIR locally.

set -o pipefail

GREEN="\033[0;32m"
YELLOW="\033[1;33m"
DIM="\033[2m"
NC="\033[0m"  # No Color

UPS_PKG_DIR="${UPS_PKG_DIR:-@INSTALL_DIR@}"

# Fetch prepared MOTD fields; exit silently if the package/data isn't available.
motd=$(PYTHONPATH="$UPS_PKG_DIR" python3 -m src.motd_status 2>/dev/null) || exit 0

declare -A M
while IFS='=' read -r key value; do
    M["$key"]="$value"
done <<< "$motd"

# No capacity samples yet → exit silently
[[ "${M[capacity_samples]:-0}" -eq 0 ]] 2>/dev/null && exit 0

case "${M[capacity_status]}" in
    locked)    status_badge="✓ LOCKED";    status_color="$GREEN" ;;
    measuring) status_badge="⟳ MEASURING"; status_color="$YELLOW" ;;
    *)         status_badge="? UNKNOWN";   status_color="$DIM" ;;
esac

# Format capacity line only when a measurement exists
latest_ah="${M[capacity_measured_ah]}"
if [[ -n "$latest_ah" ]]; then
    echo -e "  Capacity: ${latest_ah}Ah (measured) vs ${M[capacity_rated_ah]}Ah (rated) · ${status_color}${status_badge}${NC} · ${M[capacity_samples]}/3 samples · ${M[capacity_confidence_pct]}% confidence"
fi

# New battery detection flag
if [[ "${M[new_battery_detected]}" == "true" ]]; then
    echo "  ⚠️  Possible new battery detected (flagged at ${M[new_battery_timestamp]:-unknown})"
    echo "      Run: ups-battery-monitor --new-battery"
fi
