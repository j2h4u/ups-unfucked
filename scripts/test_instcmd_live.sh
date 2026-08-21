#!/usr/bin/env bash
set -euo pipefail

read -r _ _ _ nut_user nut_password _ < <(awk '$1 == "MONITOR" { print; exit }' /etc/nut/upsmon.conf)
upscmd -u "$nut_user" -p "$nut_password" cyberpower@localhost test.battery.start.quick
