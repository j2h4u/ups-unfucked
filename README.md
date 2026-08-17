# ups-unfucked

**Safe, unattended battery telemetry and honest diagnostics for a small UPS.**

See [`PRODUCT.md`](PRODUCT.md) for the stable product purpose, Jobs to Be Done, promises, limitations and
success measures. This README describes the currently implemented behavior, which may lag that product intent.

This daemon sits between a real UPS and [NUT](https://networkupstools.org/). It keeps the
host-safety path short and explicit: one physical observation is read every second, one frozen
model snapshot is used to calculate the result, and the virtual UPS is published before slower
capture, assessment, and reporting work starts.

The product also learns from ordinary blackouts without an operator. The learning boundary is
deliberately narrow: only an independent, stable load step may conservatively reduce the load-sag
coefficient `ir_k`. A natural event can never make a future shutdown later. Partial, reboot-gapped,
CAL, and test events remain operational or censored evidence; they never update capacity, SoH,
Peukert, or LUT values.

## Current contract

- **Safety first:** the daemon publishes the current virtual UPS state at one-second cadence and
  leaves shutdown decisions to `upsmon`.
- **Unattended processing:** every natural event follows
  `capture -> assess -> compare -> identify -> decide -> report`.
- **Durable evidence:** each blackout whose boundary is accepted by the writer has its own
  append-only JSONL file under `~/.config/ups-battery-monitor/events/`. The event file is
  authoritative scientific evidence; `index.jsonl` and `active.json` are bounded, rebuildable
  projections.
- **Scientific restraint:** natural learning may only reduce `ir_k` after the independent load-step
  and evidence gates. No application path writes capacity, SoH, Peukert, or LUT values from a
  partial, CAL, or test event.
- **Plain-language outcomes:** every terminal event reports what was observed, what was compared,
  and why a model change was accepted or declined. A decline is not presented as a causal battery
  diagnosis.

## How it works

```text
Real UPS (CyberPower UT850EG)
    | USB -> usbhid-ups -> NUT upsd
    v
monitor.py (one-second physical poll)
    |-- safety calculation from one frozen model snapshot
    |      -> virtual UPS publication -> upsmon / Grafana / MOTD
    `-- capture -> assess -> compare -> identify -> decide -> report
             |      -> per-event JSONL evidence
             |      -> bounded index.jsonl and active.json projections
             `-> sole ModelOwner commit lane (ir_k only for qualifying natural evidence)
```

The daemon is a data source, not a shutdown command client. It never sends UPS commands or calls
`systemctl poweroff`; `upsmon` remains responsible for the host shutdown decision. Storage or
reporting degradation is visible in health output and may reduce scientific availability, but it
must not delay or weaken the low-battery safety publication.

## Evidence and learning

Each event file is hash-linked, synchronised, bounded, and closed with one terminal outcome. Gaps,
corruption, and capture damage remain explicit rather than being interpolated. The event file is
the only scientific evidence; `index.jsonl` is a rebuildable summary and `active.json` is a bounded
work registry, not evidence.

Storage degradation has a deliberately finite boundary. Before a writer command is accepted, the
daemon retains at most eight separately identifiable blackout starts in memory; repeated OB polls in
one physical episode coalesce, and OL closes that retained episode. A ninth and later boundary is
not silently dropped: health remains degraded and recovery emits one explicit
`prestart_boundary_overflow` aggregate rejection with its count and first/last boot and monotonic
provenance. Those overflowed boundaries are not represented as individual event files because no
durable writer command was accepted. A hard process or host loss before any command is durable can
still lose the RAM-only boundary.

The current runtime does not open or import the old global
`~/.config/ups-battery-monitor/discharge-events-v1.jsonl` file. That Release A file is preserved as
a read-only archive for forensics. Journald and Grafana are operational/reporting sinks, not inputs
that can reconstruct missing scientific evidence.

A grid-restored blackout is normally partial and censored. It can support a comparison inside the
observed interval, but it cannot identify absolute capacity, SoH, Peukert exponent, or a new
voltage-to-SoC LUT. A qualifying independent load step may reduce `ir_k`; the update is asymmetric
and fail-safe. Vendor self-tests are useful for checking detection, CAL classification, durability,
and reporting, but they never authorize natural learning.

## Quick start

This shortcut is for a fresh install. An existing Release A installation must use the ordered
transform-first cutover in the operations runbook; do not run the installer over live legacy state.

```bash
# Install (requires root for systemd + NUT config)
sudo scripts/install.sh

# Check battery health
scripts/battery-health.py

# View the virtual UPS
upsc cyberpower-virtual@localhost

# Optional: add the MOTD module for an SSH login banner
cp scripts/motd/51-ups-health.sh ~/scripts/motd/
```

The health report shows storage degradation, open or replay-pending events, terminal outcomes,
decline reasons, and the latest plain-language report. A degraded evidence lane must be
investigated, but it never suppresses the NUT low-battery/shutdown path.

## Configuration

`~/.config/ups-battery-monitor/config.toml`:

```toml
ups_name = "cyberpower"     # Your NUT device name
shutdown_minutes = 5         # Minutes of modeled runtime before LB
# capacity_ah = 7.2           # Battery capacity if you replace the cell
```

These are the user-facing settings. Other behavior is fixed by the application and model state;
there is no broad automatic calibration surface. The sole `ModelOwner` writer may persist only the
specific `ir_k` change authorized by the natural-learning policy.

Any hardware capacity test is a written, supervised procedure requiring explicit operator
approval. The daemon does not execute or recommend an automatic hardware deep test; see the
[controlled-capacity protocol](docs/CONTROLLED-CAPACITY-TEST-PROTOCOL.md).

## Requirements

- Python 3.13+
- NUT 2.8+ with the `usbhid-ups` driver
- systemd (`Type=notify`, `WatchdogSec=120`)
- `python3-systemd`

## Development

Development uses [`uv`](https://docs.astral.sh/uv/) and optionally
[`just`](https://github.com/casey/just):

```bash
uv sync --extra dev
just check             # format, lint, typecheck, and tests
just fix               # auto-fix formatting and lint
just --list            # list recipes
```

Without `just`:

```bash
uv run ruff format --check src tests
uv run ruff check src tests
uv run pyright src
uv run pytest
uv run vulture
```

The former active-desulfation/scheduled-discharge premise is historical and retracted. See
[ADR 0001](docs/adr/0001-desulfation-premise-reversal.md) for that archived decision; it is not a
runtime feature or an operator workflow.

## Security

The daemon connects to NUT upsd on localhost using the local single-server setup. Its client is
read-only and implements no authentication or UPS command path. Keep upsd bound to loopback for
this deployment; do not expose this unauthenticated client path on a network interface.

## License

[MIT](LICENSE)
