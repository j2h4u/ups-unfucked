# ups-unfucked

**Datacenter-grade battery telemetry and honest diagnostics for your $30 UPS.**

---

I bought a CyberPower UT850EG. Plugged it in. The firmware said 22 minutes of runtime. During a real blackout, it ran for **47 minutes**. The charge indicator hit 0% with **12 minutes of actual runtime left**. The numbers were fiction.

Turns out, building an accurate electrochemical battery model used to require a battery chemistry background, six months buried in textbooks, or expensive expert consultations. Now it's a weekend. This daemon was written in **one day** — an LLM-assisted sprint from "this is bullshit" to a physics-based monitoring system with 568 tests and three rounds of expert review.

It sits between your UPS and [NUT](https://networkupstools.org/), replacing firmware guesswork with a real electrochemical model — Peukert's law, IR compensation, voltage-SoC lookup tables, adaptive EMA filtering, current/time integration, and linear regression for replacement prediction. Eligible controlled-capacity evidence can produce an authoritative measured-capacity/SoH estimate. Ordinary partial or reboot-gapped blackouts are retained as operational evidence and are never presented as absolute capacity, SoH, or Peukert measurements. The model learns conservatively from accepted observations, while the raw on-battery stream is preserved in a durable local journal.

Any hardware capacity test is a written, supervised procedure requiring explicit operator
approval; this project does not execute or recommend an automatic hardware deep test. (v3.0
shipped "active desulfation via scheduled discharges"; that premise was later disproven —
discharges form sulfate, charging reverses it, and the daemon has no charge-side control on
CyberPower hardware. See [ADR 0001](docs/adr/0001-desulfation-premise-reversal.md) for the full
evidence and reversal.)

This gives you the telemetry and honest diagnostics that only $2,000+ rack-mount units (APC Smart-UPS, Eaton 9PX) provide — from hardware that costs less than a pizza.

## Before / After

Real data from a blackout on 2026-03-12 (CyberPower UT850EG, 15% load):

| Metric | Firmware said | Reality | ups-unfucked |
|--------|--------------|---------|--------------|
| Runtime at full charge | 22 min | 47 min | 45 min (±10%) |
| Charge at shutdown | 0% | ~25% SoC remaining | 26% |
| Runtime at "0%" | 0 min | 12 min left | 11.4 min |
| State of Health | *(not available)* | — | 94% |
| Replacement prediction | *(not available)* | — | 2027-01-15 |
| Internal resistance | *(not available)* | — | 38 mΩ |

## What you get

Enterprise-equivalent metrics, computed from physics — no special hardware required:

| Metric | How | Enterprise equivalent |
|--------|-----|---------------------|
| **State of Charge** | Voltage LUT + IR compensation | APC coulomb counter |
| **Runtime prediction** | Peukert's law, load-adjusted, SoH-aware | Eaton runtime estimate |
| **State of Health** | Authoritative capacity-based result only after the evidence gate | APC `upsAdvBatteryHealthStatus` |
| **Replacement date** | Linear regression on SoH history | APC `upsAdvBatteryReplaceIndicator` |
| **Cycle count** | OL→OB transition counter | Eaton cumulative transfer count |
| **Internal resistance** | Voltage sag measurement (dV/dI) | APC impedance test |
| **Cumulative on-battery time** | Sum of discharge durations | Eaton on-battery timer |
| **Battery age** | Install date tracking | APC `battery.date` |
| **Low battery flag** | Physics-based, configurable threshold | Firmware fixed threshold |
| **Measured capacity** | Controlled load/current + voltage evidence; ordinary partial events are operational only | APC `upsAdvBatteryCapacity` |
| **Capacity confidence** | CoV-based convergence (3+ samples, CoV<10%) | *(not available)* |
| **New battery detection** | >10% capacity jump post-discharge | APC `upsAdvBatteryReplaceIndicator` |
| **Diagnostic capacity test** | Written/supervised protocol with explicit approval; never automatic hardware deep test | APC self-test scheduling |

Operational telemetry is continuously observed, but authoritative capacity/SoH/Peukert changes
require their evidence gates. Short or interrupted events still contribute only the operational
evidence and counters permitted by their lifecycle.

## How it works

The daemon polls NUT every 1 second. Durable journal samples are recorded every 10 seconds,
and the human-readable report is emitted every 60 seconds. Raw voltage and load pass through:

1. **Adaptive EMA** — dynamic smoothing that reacts instantly to power events but filters sensor noise
2. **IR compensation** — removes voltage sag caused by load, revealing true open-circuit voltage
3. **Voltage→SoC lookup** — maps compensated voltage to state of charge via a self-updating LUT
4. **Peukert runtime** — physics-based runtime prediction accounting for non-linear discharge at higher currents
5. **SoH tracking** — applies measured capacity against rated capacity only after the evidence gate
6. **Capacity estimation** — controlled load/current and voltage evidence with CoV-based convergence; partial events remain operational
7. **Diagnostic context** — exposes safety and scheduling context; any hardware test follows the written supervised protocol and explicit approval gate

Results are published through a virtual NUT device. Your existing tools (upsmon, Grafana, MOTD scripts) see the virtual UPS — no downstream changes needed.

## Architecture

```
Real UPS (CyberPower UT850EG)
    │ USB → usbhid-ups driver
    ▼
NUT upsd (:3493)
    │ TCP (LIST VAR, single connection)
    ▼
ups-unfucked daemon (1s physical poll; 10s durable samples; 60s human report)
    │ EMA → IR compensation → SoC (LUT) → Runtime (Peukert)
    │ Event classifier → SoH tracking → Replacement prediction
    │ Journal → replay/recovery → operational evidence (authoritative model updates are gated)
    ▼
/run/ups-battery-monitor/ups-virtual.dev (atomic tmpfs write)
    │
    ▼
NUT dummy-ups → upsd → upsmon (shutdown decisions)
                      → Grafana (dashboards)
                      → MOTD (login banner)
```

The daemon is a **data source**, not a decision maker. Shutdown logic stays with upsmon where it belongs.

Discharge observations are written to the local append-only journal at
`~/.config/ups-battery-monitor/discharge-events-v1.jsonl`. Each accepted on-battery sample is
synced before the daemon reports it as durable. The journal is the local operational source of
truth across orderly shutdown, crash, and reboot; Grafana is a secondary forensic copy. A
partial or reboot-gapped event can improve runtime-to-safety-threshold trends, but it is not an
absolute capacity, SoH, or Peukert measurement.

The journal adds no NUT privilege, command, listener, or external runtime dependency.

## Quick start

```bash
# Install (requires root for systemd + NUT config)
sudo scripts/install.sh

# Check battery health
scripts/battery-health.py

# View computed metrics
upsc cyberpower-virtual@localhost

# Optional: add MOTD module for SSH login banner
cp scripts/motd/51-ups-health.sh ~/scripts/motd/
```

The health report shows journal degradation, open/replay-pending events, and recovered
operational events separately from authoritative capacity/SoH. A degraded journal must be
investigated, but journal persistence never suppresses the NUT low-battery/shutdown path.

## Configuration

`~/.config/ups-battery-monitor/config.toml`:

```toml
ups_name = "cyberpower"     # Your NUT device name
shutdown_minutes = 5         # Minutes of runtime before LB flag
soh_alert = 0.80             # Alert when SoH drops below this
# capacity_ah = 7.2         # Battery capacity (change if you swap in a bigger cell)
```

Everything else is either hardcoded or stored in `model.json` and auto-calibrated from real discharge data.

## Requirements

- Python 3.13+
- NUT 2.8+ with `usbhid-ups` driver
- systemd (Type=notify, WatchdogSec=120)
- `python3-systemd` package

## Development

Dev tooling uses [`uv`](https://docs.astral.sh/uv/) (environment + dependencies) and,
optionally, [`just`](https://github.com/casey/just) (task runner).

```bash
uv sync --extra dev    # create venv + install dev deps from uv.lock
just check             # format-check + lint + typecheck + tests
just fix               # auto-fix formatting and lint
just --list            # list all recipes
```

Without `just`, run the underlying commands directly:

```bash
uv run ruff format --check src tests   # format check
uv run ruff check src tests            # lint
uv run pyright src                     # type check
uv run pytest                          # tests
uv run vulture                         # dead-code sieve (advisory)
```

## Roadmap

- [x] **v1.0 — Physics model & safe shutdown.** The daemon replaces firmware guesswork with real electrochemistry: voltage-to-SoC lookup tables, Peukert's law for runtime prediction, IR compensation for load-independent readings, State of Health tracking via discharge evidence, and conservative calibration from accepted observations. 212 tests, zero external dependencies beyond stdlib.

- [x] **v1.1 — Expert panel hardening.** Three rounds of expert review (electrochemist, statistician, embedded systems engineer) identified edge cases in short-discharge bias, mutable state risks, and SSD write amplification. Fixes: frozen dataclasses, batched calibration writes (60x fewer disk ops), full integration test suite, extensible EMA filter architecture. The math didn't change — the engineering around it got serious.

- [x] **v2.0 — Measured capacity.** Eligible controlled-capacity evidence can estimate actual capacity from current × time integration, cross-validated against the voltage curve. Ordinary partial or reboot-gapped blackouts remain operational evidence and do not update authoritative capacity, SoH, or Peukert state. Measured capacity is surfaced with CoV-based convergence and new-battery detection; all battery math lives in a pure-function kernel (`src/battery_math/`).

- [x] **v3.0 — Diagnostic scheduling.** The daemon can expose a safety-gated diagnostic proposal and health context. Any hardware capacity test remains a written, supervised procedure requiring explicit operator approval; no automatic hardware deep test is executed or recommended. *(Note: v3.0 also shipped a "sulfation model + cycle ROI" premise that was retracted in v3.2 — discharge forms sulfate, charging reverses it, and the daemon has no charge-side control. See [ADR 0001](docs/adr/0001-desulfation-premise-reversal.md).)*

For the supervised procedure and evidence gate, see
[CONTROLLED-CAPACITY-TEST-PROTOCOL.md](docs/CONTROLLED-CAPACITY-TEST-PROTOCOL.md). It is a
runbook, not an automation interface.

## Security

**NUT authentication:** The daemon connects to NUT upsd on localhost using empty-password authentication (`USERNAME upsmon` / `PASSWORD` with no value). This is the standard NUT setup for single-server deployments where upsd listens on loopback only (`LISTEN 127.0.0.1` in `/etc/nut/upsd.conf`).

Security implications:
- Any local process can query UPS variables (read-only, no auth required)
- Any local process that knows the upsmon username can send INSTCMD commands (battery tests, beeper control)
- This is acceptable when NUT is not exposed on the network

If you expose NUT on a network interface, configure a password in `/etc/nut/upsd.users` and set the `PASSWORD` value in `src/nut_client.py` accordingly.

## License

[MIT](LICENSE)
