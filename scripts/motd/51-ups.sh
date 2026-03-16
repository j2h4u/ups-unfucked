#!/bin/bash
# MOTD module: UPS battery capacity estimation tracking
# Displays: capacity estimates with confidence, convergence progress
# Purpose: Show user convergence status toward stable capacity measurement
# Output: "Capacity: X.XAh (measured) vs Y.YAh (rated), Z/3 deep discharges, NN% confidence"

set -o pipefail

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

# Compute confidence (CoV-based) from all estimates via Python for accuracy
confidence_percent=$(python3 << 'PYTHON_EOF' 2>/dev/null || echo "0"
import json
import os

try:
    with open(os.path.expanduser('~/.config/ups-battery-monitor/model.json')) as f:
        model = json.load(f)
except:
    print(0)
    exit(0)

estimates = model.get('capacity_estimates', [])
if not estimates or len(estimates) < 2:
    print(0)
else:
    ah_values = [e['ah_estimate'] for e in estimates]
    if len(ah_values) < 3:
        # < 3 measurements: confidence is 0.0 by design
        print(0)
    else:
        # >= 3 measurements: confidence = 1 - CoV
        mean_ah = sum(ah_values) / len(ah_values)
        if mean_ah > 0:
            variance = sum((x - mean_ah) ** 2 for x in ah_values) / len(ah_values)
            std_ah = variance ** 0.5
            cov = std_ah / mean_ah
            confidence = max(0, min(100, int((1 - cov) * 100)))
            print(confidence)
        else:
            print(0)
PYTHON_EOF
)

# Format capacity line only if we have valid data
if [[ -n "$latest_ah" && "$latest_ah" != "null" ]]; then
    # Format: "Capacity: 7.2Ah (measured) vs 7.2Ah (rated), 2/3 deep discharges, 45% confidence"
    echo "  Capacity: ${latest_ah}Ah (measured) vs ${rated_ah}Ah (rated), ${sample_count}/3 deep discharges, ${confidence_percent}% confidence"
fi
