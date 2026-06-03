# ups-unfucked

**Datacenter-grade battery telemetry and honest diagnostics for your $30 UPS.**

---

I bought a CyberPower UT850EG. Plugged it in. The firmware said 22 minutes of runtime. During a real blackout, it ran for **47 minutes**. The charge indicator hit 0% with **12 minutes of actual runtime left**. The numbers were fiction.

Turns out, building an accurate electrochemical battery model used to require a battery chemistry background, six months buried in textbooks, or expensive expert consultations. Now it's a weekend. This daemon was written in **one day** — an LLM-assisted sprint from "this is bullshit" to a physics-based monitoring system with 568 tests and three rounds of expert review.

It sits between your UPS and [NUT](https://networkupstools.org/), replacing firmware guesswork with a real electrochemical model — Peukert's law, IR compensation, voltage-SoC lookup tables, adaptive EMA filtering, trapezoidal integration for SoH, Bayesian prior-posterior blending for degradation tracking, linear regression for replacement prediction. Since v2.0, it also measures actual battery capacity from deep discharge events using coulomb counting with voltage anchoring, surfacing a measured-capacity estimate (with CoV-based convergence and new-battery detection) alongside the rated label. SoH is computed as each discharge's measured capacity against the rated value. The model isn't static: every discharge over 5 minutes recalibrates the electrochemical model; shorter events still contribute cycle count and on-battery time tracking. It auto-calibrates continuously, getting more accurate the longer it runs. After a few weeks of real-world events, the generic VRLA curve is replaced entirely by *your* battery's actual discharge characteristics.

Since v3.2 the daemon also runs a rare safety-gated diagnostic capacity test on an IEEE-1188-style annual cadence — to *measure* SoH and verify remaining capacity, not to treat the battery. (v3.0 shipped "active desulfation via scheduled discharges"; that premise was later disproven — discharges form sulfate, charging reverses it, and the daemon has no charge-side control on CyberPower hardware. See [ADR 0001](docs/adr/0001-desulfation-premise-reversal.md) for the full evidence and reversal.)

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
| **State of Health** | Capacity-based: measured_Ah / rated_Ah | APC `upsAdvBatteryHealthStatus` |
| **Replacement date** | Linear regression on SoH history | APC `upsAdvBatteryReplaceIndicator` |
| **Cycle count** | OL→OB transition counter | Eaton cumulative transfer count |
| **Internal resistance** | Voltage sag measurement (dV/dI) | APC impedance test |
| **Cumulative on-battery time** | Sum of discharge durations | Eaton on-battery timer |
| **Battery age** | Install date tracking | APC `battery.date` |
| **Low battery flag** | Physics-based, configurable threshold | Firmware fixed threshold |
| **Measured capacity** | Coulomb counting + voltage anchor from deep discharges | APC `upsAdvBatteryCapacity` |
| **Capacity confidence** | CoV-based convergence (3+ samples, CoV<10%) | *(not available)* |
| **New battery detection** | >10% capacity jump post-discharge | APC `upsAdvBatteryReplaceIndicator` |
| **Diagnostic capacity test** | Annual safety-gated capacity/SoH verification (IEEE-1188 cadence, `quick` default) | APC self-test scheduling |

All metrics self-calibrate. Discharges over 5 minutes recalibrate the electrochemical model; shorter events track cycle count and cumulative on-battery time.

## How it works

The daemon polls NUT every 10 seconds. Raw voltage and load pass through:

1. **Adaptive EMA** — dynamic smoothing that reacts instantly to power events but filters sensor noise
2. **IR compensation** — removes voltage sag caused by load, revealing true open-circuit voltage
3. **Voltage→SoC lookup** — maps compensated voltage to state of charge via a self-updating LUT
4. **Peukert runtime** — physics-based runtime prediction accounting for non-linear discharge at higher currents
5. **SoH tracking** — compares measured capacity (coulomb counting) against rated capacity to track degradation
6. **Capacity estimation** — coulomb counting from deep discharges, voltage-anchored, with CoV-based convergence
7. **Diagnostic scheduler** — proposes a rare capacity/SoH verification test on an IEEE-1188-style annual cadence with safety gates (SoH floor, grid-stability cooldown, rate limit, cycle budget)

Results are published through a virtual NUT device. Your existing tools (upsmon, Grafana, MOTD scripts) see the virtual UPS — no downstream changes needed.

## Architecture

```
Real UPS (CyberPower UT850EG)
    │ USB → usbhid-ups driver
    ▼
NUT upsd (:3493)
    │ TCP (LIST VAR, single connection)
    ▼
ups-unfucked daemon (10s poll)
    │ EMA → IR compensation → SoC (LUT) → Runtime (Peukert)
    │ Event classifier → SoH tracking → Replacement prediction
    │ Diagnostic scheduler → capacity/SoH verification test (annual cadence)
    ▼
/run/ups-battery-monitor/ups-virtual.dev (atomic tmpfs write)
    │
    ▼
NUT dummy-ups → upsd → upsmon (shutdown decisions)
                      → Grafana (dashboards)
                      → MOTD (login banner)
```

The daemon is a **data source**, not a decision maker. Shutdown logic stays with upsmon where it belongs.

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

- [x] **v1.0 — Physics model & safe shutdown.** The daemon replaces firmware guesswork with real electrochemistry: voltage-to-SoC lookup tables, Peukert's law for runtime prediction, IR compensation for load-independent readings, State of Health tracking via discharge curve analysis, and automatic model calibration from every power event. Every blackout makes the model smarter. 212 tests, zero external dependencies beyond stdlib.

- [x] **v1.1 — Expert panel hardening.** Three rounds of expert review (electrochemist, statistician, embedded systems engineer) identified edge cases in short-discharge bias, mutable state risks, and SSD write amplification. Fixes: frozen dataclasses, batched calibration writes (60x fewer disk ops), full integration test suite, extensible EMA filter architecture. The math didn't change — the engineering around it got serious.

- [x] **v2.0 — Measured capacity.** The label on your battery says 7.2Ah. Is that true? After a year of float charging at 35°C, probably not. This milestone measures actual capacity from real discharge events using coulomb counting (current × time integration), cross-validated against the voltage curve. Three deep discharges are enough to converge. Measured capacity is surfaced with CoV-based convergence and new-battery detection (baseline versioning so old and new battery data never mix); SoH is computed against rated capacity. All battery math extracted into a pure-function kernel (`src/battery_math/`) with a year-long simulation harness that proves the formula system doesn't diverge — because when five interdependent equations feed each other's outputs across months of operation, you want mathematical proof, not hope.

- [x] **v3.0 — Daemon-controlled test scheduling.** The daemon dispatches NUT battery tests directly via upscmd, replacing static systemd timers. Introduced a test scheduler with safety gates (SoH floor, rate limiting, grid stability, cycle budget). 453 tests. *(Note: v3.0 also shipped a "sulfation model + cycle ROI" premise that was retracted in v3.2 — discharge forms sulfate, charging reverses it, and the daemon has no charge-side control. See [ADR 0001](docs/adr/0001-desulfation-premise-reversal.md).)*

## Security

**NUT authentication:** The daemon connects to NUT upsd on localhost using empty-password authentication (`USERNAME upsmon` / `PASSWORD` with no value). This is the standard NUT setup for single-server deployments where upsd listens on loopback only (`LISTEN 127.0.0.1` in `/etc/nut/upsd.conf`).

Security implications:
- Any local process can query UPS variables (read-only, no auth required)
- Any local process that knows the upsmon username can send INSTCMD commands (battery tests, beeper control)
- This is acceptable when NUT is not exposed on the network

If you expose NUT on a network interface, configure a password in `/etc/nut/upsd.users` and set the `PASSWORD` value in `src/nut_client.py` accordingly.

## License

[MIT](LICENSE)
