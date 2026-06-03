#!/bin/bash
# MOTD module: Display sulfation status and next test countdown
# Data comes from the bundled CLI (python3 -m src.motd_status) — no jq, no embedded
# JSON parsing. @INSTALL_DIR@ is filled in by scripts/install.sh; override with
# UPS_PKG_DIR for local testing from a repo checkout.

UPS_PKG_DIR="${UPS_PKG_DIR:-@INSTALL_DIR@}"

# Fetch prepared MOTD fields; exit silently if the package/data isn't available
# (MOTD runs on every login — it must never print errors when the monitor is down).
motd=$(PYTHONPATH="$UPS_PKG_DIR" python3 -m src.motd_status 2>/dev/null) || exit 0

declare -A M
while IFS='=' read -r key value; do
    M["$key"]="$value"
done <<< "$motd"

# No sulfation data yet → nothing to show
[[ -z "${M[sulfation_pct]}" ]] && exit 0

score_pct="${M[sulfation_pct]}"
next_test="${M[next_test_timestamp]}"

# Calculate days until next test
if [[ -n "$next_test" ]]; then
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

echo "Battery health: Sulfation ${score_pct}% · Next test ${test_str}"
exit 0
