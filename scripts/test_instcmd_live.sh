#!/bin/bash
set -euo pipefail

# Live validation: Send INSTCMD test.battery.start.quick/deep to real UT850EG
# Requires: NUT upsd running locally, CyberPower UPS configured
# Purpose: Validate RFC 9271 INSTCMD protocol works on target hardware

# Script usage
usage() {
    cat <<'EOF'
test_instcmd_live.sh — Live validation of NUT INSTCMD protocol on UT850EG

USAGE:
  test_instcmd_live.sh [OPTIONS]

OPTIONS:
  --help              Show this help message
  --ups <name>        UPS name in NUT (default: cyberpower)
  --quick             Send test.battery.start.quick (default)
  --deep              Send test.battery.start.deep
  --timeout <sec>     Wait timeout for test result in seconds (default: 30)

EXAMPLES:
  # Send quick test and monitor for 30 seconds
  test_instcmd_live.sh --quick

  # Send deep test to custom UPS, wait 60 seconds
  test_instcmd_live.sh --ups myups --deep --timeout 60

PROTOCOL:
  Sends INSTCMD via NUT upscmd CLI tool, then polls test.result variable
  to confirm test actually started. Timeout is graceful (test may still run).

REQUIREMENTS:
  - upscmd and upsc CLI tools (from nut package)
  - NUT upsd daemon running
  - UPS configured in upsd
EOF
}

# Error handling
die() {
    echo "ERROR: $1" >&2
    exit 1
}

# Defaults
ups_name="cyberpower"
test_cmd="test.battery.start.quick"
timeout_sec=30

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help)
            usage
            exit 0
            ;;
        --ups)
            ups_name="$2"
            shift 2
            ;;
        --quick)
            test_cmd="test.battery.start.quick"
            shift
            ;;
        --deep)
            test_cmd="test.battery.start.deep"
            shift
            ;;
        --timeout)
            timeout_sec="$2"
            shift 2
            ;;
        *)
            die "Unknown option: $1"
            ;;
    esac
done

# Pre-flight checks
echo "=== Pre-flight Checks ==="

# Check upscmd CLI
if ! command -v upscmd &> /dev/null; then
    die "upscmd not found in PATH. Install NUT package: sudo apt-get install nut"
fi
echo "✓ upscmd CLI found"

# Check upsc CLI
if ! command -v upsc &> /dev/null; then
    die "upsc not found in PATH. Install NUT package: sudo apt-get install nut"
fi
echo "✓ upsc CLI found"

# Check NUT upsd is running and responding
if ! upsc "$ups_name" battery.charge &>/dev/null; then
    die "NUT upsd not responding for UPS '$ups_name'. Check: upsc $ups_name"
fi
echo "✓ NUT upsd responding for UPS '$ups_name'"

echo ""
echo "=== Sending INSTCMD ==="

# Capture pre-dispatch test.result to detect changes
pre_test_result=$(upsc "$ups_name" test.result 2>/dev/null || echo "UNKNOWN")
echo "Pre-dispatch test.result: $pre_test_result"

# Send INSTCMD via upscmd (simulating daemon behavior)
echo "Sending: upscmd -u upsmon $ups_name $test_cmd"
response=$(upscmd -u upsmon "$ups_name" "$test_cmd" 2>&1 || true)
echo "Response: $response"

# Check for success indicators
if echo "$response" | grep -qi "succeeded\|Instant command succeeded"; then
    echo "✓ INSTCMD dispatch successful (upscmd OK)"
else
    die "INSTCMD dispatch failed. Response: $response"
fi

echo ""
echo "=== Monitoring Test Progress (${timeout_sec}s timeout) ==="

# Poll test.result variable post-dispatch
start_time=$(date +%s)
deadline=$((start_time + timeout_sec))
poll_interval=2

while [[ $(date +%s) -lt $deadline ]]; do
    test_result=$(upsc "$ups_name" test.result 2>/dev/null || echo "UNKNOWN")
    echo "[$(($(date +%s) - start_time))s] test.result: $test_result"

    # Check if test started (test.result changed from pre-dispatch value)
    if [[ "$test_result" != "$pre_test_result" ]] && [[ "$test_result" != "UNKNOWN" ]]; then
        if [[ "$test_result" == *"In Progress"* ]] || [[ "$test_result" == *"Completed"* ]]; then
            echo "✓ Test started and actively running"
            break
        fi
    fi

    sleep "$poll_interval"
done

echo ""
echo "=== Summary ==="
final_test_result=$(upsc "$ups_name" test.result 2>/dev/null || echo "UNKNOWN")
echo "Final test.result: $final_test_result"

if [[ "$final_test_result" != "$pre_test_result" ]] && [[ "$final_test_result" != "UNKNOWN" ]]; then
    echo "✓ Test execution confirmed (result changed from pre-dispatch state)"
else
    echo "⚠ Test result unchanged after dispatch"
    echo "  Note: For deep tests, this timeout (${timeout_sec}s) may be insufficient."
    echo "  Check later: upsc $ups_name test.result"
fi

echo ""
echo "=== Live Validation Complete ==="
exit 0
