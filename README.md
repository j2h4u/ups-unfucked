# UPS Battery Monitor

Safe, unattended UPS monitoring for a small host. The daemon reads a physical UPS through NUT,
publishes a conservative virtual UPS, records useful telemetry, and keeps operator-facing history
compact and honest.

## Product contract

- One daemon polls the physical NUT UPS once per second.
- Safety publication is independent of storage and diagnostics. `upsmon` remains responsible for
  host shutdown; this daemon never sends UPS or power commands.
- A single append-only file, `~/.config/ups-battery-monitor/events/telemetry.jsonl`, records
  physical samples for blackouts, tests, and recharge. Samples include up to 120 seconds of
  pre-event and post-event context where available; missing values stay missing.
- `events/history.jsonl` keeps compact event aggregates, and `scripts/blackout-history.py` prints
  recorded natural blackouts without requiring an operator to scan raw samples.
- A closed natural blackout may produce bounded automatic feedback from a raw load-step IR
  observation and a censored-curve, runtime-effective SoH observation. Every applied change, its
  size, source event, and reason are appended to `events/history.jsonl`.
- If no blackout or calibration/self-test has occurred for 14 days and the UPS is at OL100, the
  daemon may run one automatic quick self-test. The result is operational evidence, never a
  capacity or battery-health claim.
- The installer installs the daemon and NUT-facing integration. The optional MOTD script gives a
  short status line at login.

The monitor reports what its sensors observed. It does not infer exact capacity, state of health,
temperature, current, or an empty-battery endpoint when those measurements are unavailable.

## How it works

```text
physical UPS -> NUT -> monitor.py -> safe virtual UPS -> upsmon
                         |
                         `-> events/telemetry.jsonl -> blackout-history.py / MOTD
```

Raw telemetry is append-only and must not be edited by hand. Journald remains useful for service
diagnostics but is not a replacement for the telemetry file.

## Install and operate

```bash
sudo scripts/install.sh
upsc cyberpower-virtual@localhost
scripts/blackout-history.py
bash ~/scripts/motd/51-ups-health.sh
```

The installer is the single supported installation path. Keep NUT bound to localhost for this
single-host setup.

## Development

```bash
uv sync --extra dev
just check
```

See [PRODUCT.md](PRODUCT.md) for the product statement and
[docs/OPERATIONS-RUNBOOK.md](docs/OPERATIONS-RUNBOOK.md) for safe read-only checks.

## Requirements and license

Python 3.13+, NUT 2.8+, systemd, and `python3-systemd`.

[MIT](LICENSE)
