#!/bin/bash
# MOTD module: UPS battery capacity estimation tracking
# Displays: capacity estimates with confidence, convergence progress, and convergence status
# Purpose: Show user convergence status toward stable capacity measurement with status badge
# Output: "Capacity: X.XAh (measured) vs Y.YAh (rated) · STATUS_BADGE · N/3 samples · NN% confidence"

set -o pipefail

# Color variables for status badges
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
DIM="\033[2m"
NC="\033[0m"  # No Color

# Path to model.json (same as monitor.py)
MODEL_FILE="${HOME}/.config/ups-battery-monitor/model.json"

# Only display if model.json exists
[[ ! -f "$MODEL_FILE" ]] && exit 0

# Read capacity estimates array
capacity_estimates=$(jq -r '.capacity_estimates // empty' "$MODEL_FILE" 2>/dev/null)

# No estimates yet - exit silently
[[ -z "$capacity_estimates" ]] && exit 0

# Count capacity measurements
sample_count=$(jq -r '.capacity_estimates | length' "$MODEL_FILE" 2>/dev/null || echo "0")

# Exit silently if no samples
[[ "$sample_count" -eq 0 ]] && exit 0

# Get latest capacity estimate and rated capacity
latest_ah=$(jq -r '.capacity_estimates[-1].ah_estimate' "$MODEL_FILE" 2>/dev/null)
rated_ah=7.2  # CyberPower UT850EG firmware rated capacity

# Compute convergence status and confidence via Python subprocess
# Returns: "status,sample_count,confidence_pct" (e.g., "locked,3,92")
convergence_data=$(python3 << 'PYTHON_EOF' 2>/dev/null || echo "unknown,0,0"
import json
import os
import math

try:
    with open(os.path.expanduser('~/.config/ups-battery-monitor/model.json')) as f:
        model = json.load(f)
except:
    print("unknown,0,0")
    exit(0)

estimates = model.get('capacity_estimates', [])
sample_count = len(estimates)

if not estimates or sample_count < 1:
    print("unknown,0,0")
    exit(0)

ah_values = [e['ah_estimate'] for e in estimates]
mean_ah = sum(ah_values) / len(ah_values)

# Compute convergence status and confidence
if sample_count < 3:
    # Less than 3 samples: measuring state
    confidence_score = 0
    status = "measuring"
else:
    # 3+ samples: calculate CoV and determine status
    if mean_ah > 0:
        variance = sum((x - mean_ah) ** 2 for x in ah_values) / sample_count
        std_ah = math.sqrt(variance)
        cov = std_ah / mean_ah
        confidence_score = max(0, min(100, int((1 - cov) * 100)))
        # Convergence locked if CoV < 0.10 and count >= 3
        status = "locked" if cov < 0.10 else "measuring"
    else:
        confidence_score = 0
        status = "unknown"

print(f"{status},{sample_count},{confidence_score}")
PYTHON_EOF
)

# Parse convergence data
IFS=',' read -r convergence_status sample_count_from_python confidence_pct <<< "$convergence_data"

# Use sample_count from jq (more reliable) but convergence status from Python
if [[ -z "$convergence_status" || "$convergence_status" == "unknown" ]]; then
    convergence_status="unknown"
    status_badge="? UNKNOWN"
    status_color="$DIM"
elif [[ "$convergence_status" == "locked" ]]; then
    status_badge="✓ LOCKED"
    status_color="$GREEN"
elif [[ "$convergence_status" == "measuring" ]]; then
    status_badge="⟳ MEASURING"
    status_color="$YELLOW"
else
    status_badge="? UNKNOWN"
    status_color="$DIM"
fi

# Format capacity line only if we have valid data
if [[ -n "$latest_ah" && "$latest_ah" != "null" ]]; then
    # Format: "Capacity: X.XAh (measured) vs Y.YAh (rated) · ✓ LOCKED · N/3 samples · NN% confidence"
    echo -e "  Capacity: ${latest_ah}Ah (measured) vs ${rated_ah}Ah (rated) · ${status_color}${status_badge}${NC} · ${sample_count}/3 samples · ${confidence_pct}% confidence"
fi

# Phase 13: Check for new battery detection flag
if [[ "$(jq -r '.new_battery_detected // false' "$MODEL_FILE")" == "true" ]]; then
    TIMESTAMP=$(jq -r '.new_battery_detected_timestamp // "unknown"' "$MODEL_FILE")
    echo "  ⚠️  Possible new battery detected (flagged at $TIMESTAMP)"
    echo "      Run: ups-battery-monitor --new-battery"
fi
