#!/usr/bin/env bash
set -uo pipefail

usage() {
    cat <<'EOF'
Usage: test_instcmd_live.sh [--ups NAME] [--timeout SECONDS] [--quick]

Run one authenticated quick UPS self-test and briefly observe both NUT UPS
identities plus the shared telemetry.jsonl stream. The NUT password is read
once from the terminal and is never printed or saved.
EOF
}

die() {
    local -r message="$1"
    printf 'ERROR: %s\n' "$message" >&2
    exit 1
}

require_integer() {
    local -r value="$1" name="$2"
    [[ "$value" =~ ^[0-9]+$ && "$value" -gt 0 ]] || die "$name must be a positive integer"
}

ups_snapshot() {
    local -r ups="$1"
    local output

    output=$(upsc "$ups" 2>/dev/null) || return 1
    awk -F': ' '
        $1 == "ups.status" { status=$2 }
        $1 == "battery.charge" { charge=$2 }
        $1 == "battery.runtime" { runtime=$2 }
        END {
            if (status == "") status="?"
            if (charge == "") charge="?"
            if (runtime == "") runtime="?"
            printf "status=%s charge=%s%% runtime=%ss", status, charge, runtime
        }
    ' <<<"$output"
}

telemetry_size() {
    local -r telemetry="$1"
    local bytes lines

    if [[ -f "$telemetry" ]]; then
        bytes=$(stat --format='%s' "$telemetry") || return 1
        lines=$(wc --lines <"$telemetry") || return 1
        printf '%s bytes/%s lines' "$bytes" "${lines//[[:space:]]/}"
    else
        printf 'absent'
    fi
}

main() {
    local ups_name='cyberpower'
    local timeout_sec=30
    local telemetry="${XDG_CONFIG_HOME:-$HOME/.config}/ups-battery-monitor/events/telemetry.jsonl"
    local option
    local physical virtual after
    local start_time now elapsed
    local telemetry_before telemetry_after
    local -i telemetry_grew=0

    while [[ $# -gt 0 ]]; do
        option="$1"
        case "$option" in
            --help)
                usage
                return 0
                ;;
            --ups)
                [[ $# -ge 2 ]] || die '--ups requires a value'
                ups_name="$2"
                shift 2
                ;;
            --timeout)
                [[ $# -ge 2 ]] || die '--timeout requires a value'
                timeout_sec="$2"
                shift 2
                ;;
            --quick)
                shift
                ;;
            --deep)
                die 'only test.battery.start.quick is allowed'
                ;;
            *)
                die "unknown option: $option"
                ;;
        esac
    done
    require_integer "$timeout_sec" '--timeout'

    command -v upscmd >/dev/null 2>&1 || die 'upscmd not found in PATH'
    command -v upsc >/dev/null 2>&1 || die 'upsc not found in PATH'

    printf 'Preflight (%s and cyberpower-virtual)\n' "$ups_name"
    physical=$(ups_snapshot "$ups_name") || die "NUT is not responding for $ups_name"
    virtual=$(ups_snapshot 'cyberpower-virtual') || die 'NUT is not responding for cyberpower-virtual'
    printf '  %s: %s\n  cyberpower-virtual: %s\n' "$ups_name" "$physical" "$virtual"
    telemetry_before=$(telemetry_size "$telemetry") || die "cannot inspect $telemetry"
    printf '  telemetry: %s (%s)\n' "$telemetry" "$telemetry_before"

    [[ -t 0 ]] || die 'NUT password requires an interactive terminal'

    printf 'Starting quick self-test on %s...\n' "$ups_name"
    if ! upscmd -u upsmon "$ups_name" test.battery.start.quick; then
        die 'quick self-test dispatch failed'
    fi

    start_time=$(printf '%(%s)T' -1)
    while :; do
        now=$(printf '%(%s)T' -1)
        elapsed=$((now - start_time))
        physical=$(ups_snapshot "$ups_name") || physical='unavailable'
        virtual=$(ups_snapshot 'cyberpower-virtual') || virtual='unavailable'
        telemetry_after=$(telemetry_size "$telemetry") || telemetry_after='unavailable'
        printf '[%ss] %s: %s | cyberpower-virtual: %s | telemetry: %s\n' \
            "$elapsed" "$ups_name" "$physical" "$virtual" "$telemetry_after"
        if [[ "$telemetry_after" != 'absent' && "$telemetry_after" != 'unavailable' && "$telemetry_after" != "$telemetry_before" ]]; then
            telemetry_grew=1
        fi
        if (( elapsed >= timeout_sec )); then
            break
        fi
        sleep 2
    done

    after=$(ups_snapshot "$ups_name" 2>/dev/null || printf 'unavailable')
    printf 'Result: quick self-test dispatched; final %s: %s\n' "$ups_name" "$after"
    if (( telemetry_grew )); then
        printf 'Telemetry: grew during observation (%s -> %s)\n' "$telemetry_before" "$telemetry_after"
    else
        printf 'Telemetry: no growth observed within %ss\n' "$timeout_sec"
    fi
}

main "$@"
