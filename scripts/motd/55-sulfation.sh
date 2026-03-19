#!/bin/bash
# MOTD module: Display sulfation status and next test countdown
# Reads from health.json updated by daemon every 10s

# Path to health.json (matches daemon's write location)
# Can be overridden via HEALTH_FILE environment variable for testing
HEALTH_FILE="${HEALTH_FILE:-/run/ups-battery-monitor/ups-health.json}"

# Exit cleanly if health.json not found or invalid
if [[ ! -f "$HEALTH_FILE" ]]; then
    exit 0
fi

# Parse JSON safely with jq
sulfation=$(jq -r '.sulfation_score // "null"' "$HEALTH_FILE" 2>/dev/null)
next_test=$(jq -r '.next_test_timestamp // null' "$HEALTH_FILE" 2>/dev/null)
# Exit if jq failed or sulfation not available
if [[ "$sulfation" == "null" || "$sulfation" == "" ]]; then
    exit 0
fi

# Convert sulfation score [0-1.0] to percentage [0-100]
score_pct=$(printf "%.0f" "$(echo "$sulfation * 100" | bc -l)")

# Calculate days until next test
if [[ "$next_test" != "null" && "$next_test" != "" ]]; then
    now=$(date +%s)
    next_epoch=$(date -d "$next_test" +%s 2>/dev/null) || next_epoch=""
    if [[ -z "$next_epoch" ]]; then
        test_str="unknown"
    elif days_until=$(( (next_epoch - now) / 86400 )); [[ $days_until -lt 0 ]]; then
        test_str="overdue"
    elif [[ $days_until -eq 0 ]]; then
        test_str="today"
    else
        test_str="in ${days_until}d"
    fi
else
    test_str="none scheduled"
fi

# Format output
output="Battery health: Sulfation ${score_pct}% · Next test ${test_str}"

echo "$output"
exit 0
