# Operations runbook

This runbook contains safe, read-only checks for the single-host installation. It does not perform
a migration, deep battery test, or data repair.

## Install

Use the one supported installer from a reviewed checkout:

```bash
sudo scripts/install.sh
```

It installs the systemd service and NUT-facing integration. Review the rendered unit and NUT
configuration before enabling the service. Do not run a second daemon against the same UPS.

## Health checks

```bash
systemctl is-active ups-battery-monitor.service
upsc cyberpower@localhost ups.status
upsc cyberpower-virtual@localhost ups.status
upsc cyberpower-virtual@localhost battery.runtime
test -f ~/.config/ups-battery-monitor/events/telemetry.jsonl
scripts/blackout-history.py
bash ~/scripts/motd/51-ups-health.sh
journalctl -u ups-battery-monitor.service --since today --no-pager
```

The physical and virtual UPS should agree when healthy. The virtual publication must remain
available even if telemetry recording or the history view is degraded. `upsmon` owns shutdown.

## Telemetry

The only current event path is `~/.config/ups-battery-monitor/events/telemetry.jsonl`. Treat it as
append-only raw data. Do not edit, truncate, merge, migrate, or reconstruct it from journald.

The compact history command is a read-only projection of that file:

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
