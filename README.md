# UPS Battery Monitor

**Turn a $30 budget UPS into an enterprise-grade battery monitoring system.**

Budget UPS (CyberPower, basic APC) ship with firmware that reports wildly inaccurate battery metrics — runtime predictions off by 100%+, charge percentages that hit 0% with 12 minutes of real runtime left. This daemon sits between your UPS and NUT, replacing firmware guesswork with physics-based calculations.

## What it does

The daemon reads raw voltage and load from your UPS, applies real battery physics (Peukert's law, IR compensation, adaptive EMA filtering), and publishes corrected metrics through a virtual NUT device. Your existing tools (upsmon, Grafana, MOTD) see the virtual UPS and get accurate data — no configuration changes needed downstream.

### Enterprise-equivalent metrics from a budget UPS

| Metric | Enterprise UPS (APC/Eaton) | This daemon | Source |
|--------|---------------------------|-------------|--------|
| State of Charge | Drifting coulomb counter | Voltage LUT + Peukert model | Physics |
| Remaining runtime | Firmware estimate (±50%) | Peukert prediction (±10%) | Physics |
| State of Health | SNMP OID (some models) | Discharge curve analysis | Measured |
| Replacement prediction | Manual check | Linear regression on SoH trend | Computed |
| Battery install date | Manual entry via SNMP | Automatic, persisted | Tracked |
| Cycle count | Eaton SNMP | OL→OB transition counter | Counted |
| Cumulative on-battery time | Eaton SNMP | Sum of discharge durations | Accumulated |
| Internal resistance | APC impedance test | Voltage sag measurement (dV/dI) | Measured |
| Shutdown signal (LB flag) | Firmware threshold | Physics-based, configurable | Computed |

### Self-calibrating

The daemon learns your battery over time:
- Every blackout adds measured voltage→SoC points to the lookup table, replacing the standard VRLA curve with real data
- Time-weighted averaging ensures recent measurements dominate as the battery ages (half-life: 90 days)
- Peukert exponent auto-calibrates from actual vs predicted runtime
- Internal resistance tracked from voltage sag on every power transition

## Architecture

```
Real UPS (CyberPower UT850EG)
    │ USB → usbhid-ups driver
    ▼
NUT upsd (:3493)
    │ TCP (LIST VAR, single connection)
    ▼
ups-battery-monitor daemon
    │ Adaptive EMA → IR compensation → SoC (LUT) → Runtime (Peukert)
    │ Event classifier → SoH tracking → Replacement prediction
    ▼
/dev/shm/ups-virtual.dev (atomic tmpfs write)
    │
    ▼
NUT dummy-ups driver → upsd → upsmon (shutdown decisions)
                             → Grafana/Alloy (dashboards)
                             → MOTD (server login status)
```

The daemon is a **data source**, not a decision maker. Shutdown logic stays with upsmon where it belongs.

## Quick start

```bash
# Install (requires root for systemd + NUT config)
sudo scripts/install.sh

# Check battery health
scripts/battery-health.py

# View live metrics
upsc cyberpower-virtual@localhost
```

## Configuration

`~/.config/ups-battery-monitor/config.toml` — only 3 settings:

```toml
ups_name = "cyberpower"     # NUT device name
shutdown_minutes = 5         # Safety margin before forced shutdown
soh_alert = 0.80             # Alert when battery health drops below this
```

Everything else (poll interval, EMA window, NUT connection, physics params) is either hardcoded or stored in `model.json` and auto-calibrated.

## User scenarios

See [docs/USER-SCENARIOS.md](docs/USER-SCENARIOS.md) for:
- Battery health report
- Deep battery test (cliff region calibration)
- Battery replacement workflow
- Configuration reference

## Requirements

- Python 3.11+ (stdlib `tomllib`)
- NUT 2.8+ with `usbhid-ups` driver
- systemd (service + watchdog)
- `python3-systemd` (JournalHandler + sd_notify)
