# UPS Battery Monitor

Safe, unattended UPS monitoring for a small host. The daemon reads a physical UPS through NUT,
publishes a conservative virtual UPS, records useful telemetry, and keeps operator-facing history
compact and honest.

## Product contract

- One daemon polls the physical NUT UPS once per second.
- Each successful poll publishes the safety state before optional storage, history, and feedback work. `upsmon`
  remains responsible for host shutdown; this daemon sends no power or shutdown commands and may
  send only the guarded quick self-test command described below.
- A single append-only file, `~/.local/state/ups-battery-monitor/telemetry.jsonl`, records
  raw physical samples for blackouts, tests, and recharge. Samples include up to 120 seconds of
  pre-event and post-event context where available; missing values stay missing.
- `history.jsonl` keeps compact event aggregates, and `scripts/blackout-history.py` prints
  recorded natural blackouts without requiring an operator to scan raw samples. History is derived
  from the raw telemetry and is not the evidence source.
- Runtime JSON contracts are typed and documented beside their codecs in
  `minimal_event_file.py`, `battery_history.py`, and `model_state_schema.py`; decoding validates
  untrusted file contents before the daemon uses them.
- An eligible closed natural blackout may produce one compact, durable load-step IR observation.
  Three consistent observations can produce one bounded automatic IR update; every observation and
  applied change, its size, source event, and reason are appended to `history.jsonl`.
- If no blackout or calibration/self-test has occurred for 14 days and the UPS is at OL100, the
  daemon may run one automatic quick self-test. The result is operational evidence, never a
  capacity or battery-health claim.
- The installer installs the daemon and NUT-facing integration. The optional MOTD script gives a
  short status line at login.

The monitor reports what its sensors observed. It does not infer exact capacity, state of health,
temperature, current, or an empty-battery endpoint when those measurements are unavailable.

The fixed NUT name, five-minute shutdown threshold, and stock 7.2 Ah rating live in the frozen
Python `Config`. This single-host service has no operator-facing configuration file; `model.json` is
managed runtime state.

## Roadmap

Delivered:

- Unattended protection: one-second physical polling, conservative virtual UPS publication, and
  `upsmon`-owned shutdown with fail-closed freshness handling.
- Durable observation: raw blackout, self-test, and recharge telemetry plus derived history and
  compact CLI/MOTD views.
- Safe automatic feedback: eligible natural-blackout IR observations, bounded cohort updates, and
  guarded operational self-tests.

Remaining:

- Make shutdown forecasts visibly model-derived, conservative, and evidence-backed without
  presenting them as exact capacity or state of health.
- Make incomplete-event outcomes, feedback refusals, and comparable battery-health trends clear to
  the operator; report insufficient evidence instead of inventing a health score.

## How it works

```text
physical UPS -> NUT -> monitor.py -> safe virtual UPS -> upsmon / MOTD
                         |
                         `-> telemetry.jsonl + history.jsonl -> blackout-history.py
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

The installer is the single supported installation path. It installs and verifies the systemd and
NUT integration, then enables the monitor. Keep NUT bound to localhost for this single-host setup.

Persistent state lives in `~/.local/state/ups-battery-monitor/`:

- `telemetry.jsonl` is the append-only raw event stream;
- `history.jsonl` contains compact derived episode summaries and feedback receipts;
- `model.json` contains validated runtime model state.

Do not edit, truncate, merge, or reconstruct these files by hand.

### Health checks

```bash
systemctl is-active ups-battery-monitor.service
upsc cyberpower@localhost ups.status
upsc cyberpower-virtual@localhost ups.status
upsc cyberpower-virtual@localhost battery.runtime
scripts/blackout-history.py
bash ~/scripts/motd/51-ups-health.sh
journalctl -u ups-battery-monitor.service --since today --no-pager
```

The physical and virtual UPS should agree during normal online operation. If the service is
inactive, inspect its status and journal, then verify the physical UPS independently with `upsc`.
If physical and virtual status disagree, stop and investigate; do not edit state or start a second
monitor against the same UPS. `upsmon` remains responsible for host shutdown.

## Development

```bash
uv sync --group dev
just check
```

## Requirements and license

Python 3.13+, NUT 2.8+, systemd, and `python3-systemd`.

[MIT](LICENSE)
