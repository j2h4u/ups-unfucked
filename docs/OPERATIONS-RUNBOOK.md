# Operations runbook

This runbook contains safe, read-only checks for the single-host installation and documents the
supported installer. It does not perform a migration, deep battery test, or data repair.

## Install

Use the one supported installer from a reviewed checkout:

```bash
sudo scripts/install.sh
```

The installer stages and validates the rendered systemd units, applies the unit and NUT changes
transactionally, restarts the required services, verifies the virtual UPS, and enables the monitor
last. Use `--dry-run` or a reviewed checkout when you need to inspect the planned changes. Do not run
a second daemon against the same UPS.

## Health checks

```bash
systemctl is-active ups-battery-monitor.service
upsc cyberpower@localhost ups.status
upsc cyberpower-virtual@localhost ups.status
upsc cyberpower-virtual@localhost battery.runtime
test -f ~/.local/state/ups-battery-monitor/telemetry.jsonl
scripts/blackout-history.py
bash ~/scripts/motd/51-ups-health.sh
journalctl -u ups-battery-monitor.service --since today --no-pager
```

The physical and virtual UPS should agree when healthy. Each successful poll publishes the safety
projection before telemetry and history work. An ordinary telemetry I/O error is degraded
operation, not a reason to edit raw data; other poll failures can stop the daemon. A publication
failure invalidates the old output. `upsmon` owns host shutdown.

## Telemetry

The raw event stream is `~/.local/state/ups-battery-monitor/telemetry.jsonl`. Treat it as
append-only raw data. Do not edit, truncate, merge, migrate, or reconstruct it from journald.
`history.jsonl` is a compact derived aggregate containing episode summaries and eligible
learning receipts; it is not a replacement for the raw stream.

The compact history command reads the raw stream and the derived history file to exclude attributed
self-tests:

```bash
scripts/blackout-history.py --help
scripts/blackout-history.py
```

## Automatic quick self-test

The daemon may run a quick self-test only when the physical UPS is OL100 and no blackout or
calibration/self-test has occurred for 14 days. It records the result as operational telemetry.
This is not a capacity test, a state-of-health measurement, or permission to run a deep discharge.

## Troubleshooting

If the service is inactive, inspect `systemctl status` and the journal, then verify NUT independently
with `upsc`. Preserve the telemetry file and logs before changing configuration. If physical and
virtual status disagree, treat the virtual state as safety-critical and stop for investigation; do
not hand-edit state or start another monitor.
