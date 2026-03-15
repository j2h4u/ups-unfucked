# ups-unfucked

**Datacenter-grade battery telemetry for your $30 UPS.**

---

I bought a CyberPower UT850EG. Plugged it in. The firmware said 22 minutes of runtime. During a real blackout, it ran for **47 minutes**. The charge indicator hit 0% with **12 minutes of actual runtime left**. The numbers were fiction.

Turns out, building an accurate electrochemical battery model used to require a battery chemistry background, six months buried in textbooks, or expensive expert consultations. Now it's a weekend. This daemon was written in **one day** — an LLM-assisted sprint from "this is bullshit" to a physics-based monitoring system with 212 tests and three rounds of expert review.

It sits between your UPS and [NUT](https://networkupstools.org/), replacing firmware guesswork with a real electrochemical model — Peukert's law, IR compensation, voltage-SoC lookup tables, adaptive EMA filtering, trapezoidal integration for SoH, Bayesian prior-posterior blending for degradation tracking, linear regression for replacement prediction. The model isn't static: every blackout, every test, every 60-second power flicker feeds measured data back into the model. It auto-calibrates continuously, getting more accurate the longer it runs. After a few weeks of real-world events, the generic VRLA curve is replaced entirely by *your* battery's actual discharge characteristics.

This gives you the telemetry that only $2,000+ rack-mount units (APC Smart-UPS, Eaton 9PX) provide — from hardware that costs less than a pizza.

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
| **State of Health** | Discharge curve area analysis | APC `upsAdvBatteryHealthStatus` |
| **Replacement date** | Linear regression on SoH history | APC `upsAdvBatteryReplaceIndicator` |
| **Cycle count** | OL→OB transition counter | Eaton cumulative transfer count |
| **Internal resistance** | Voltage sag measurement (dV/dI) | APC impedance test |
| **Cumulative on-battery time** | Sum of discharge durations | Eaton on-battery timer |
| **Battery age** | Install date tracking | APC `battery.date` |
| **Low battery flag** | Physics-based, configurable threshold | Firmware fixed threshold |

All metrics self-calibrate. Every blackout — even a 60-second flicker — teaches the model your battery's real characteristics.

## How it works

The daemon polls NUT every 10 seconds. Raw voltage and load pass through:

1. **Adaptive EMA** — dynamic smoothing that reacts instantly to power events but filters sensor noise
2. **IR compensation** — removes voltage sag caused by load, revealing true open-circuit voltage
3. **Voltage→SoC lookup** — maps compensated voltage to state of charge via a self-updating LUT
4. **Peukert runtime** — physics-based runtime prediction accounting for non-linear discharge at higher currents
5. **SoH tracking** — compares each discharge curve area against the reference to track degradation

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
    ▼
/dev/shm/ups-virtual.dev (atomic tmpfs write)
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

`~/.config/ups-battery-monitor/config.toml` — only 3 settings:

```toml
ups_name = "cyberpower"     # Your NUT device name
shutdown_minutes = 5         # Minutes of runtime before LB flag
soh_alert = 0.80             # Alert when SoH drops below this
```

Everything else is either hardcoded or stored in `model.json` and auto-calibrated from real discharge data.

## Requirements

- Python 3.11+
- NUT 2.8+ with `usbhid-ups` driver
- systemd (Type=notify, WatchdogSec=120)
- `python3-systemd` package

## License

[MIT](LICENSE)
