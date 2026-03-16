# User Scenarios

The monitor works fully automatically out of the box. These scenarios describe optional actions for users who want more control or accuracy.

## Battery Health Report

The daemon tracks battery health automatically from every discharge event.

```bash
./scripts/battery-health.py
```

Shows: SoH, capacity, LUT calibration coverage, discharge event count, replacement prediction (after 20+ events), and internal resistance trend.

Live metrics via NUT: `upsc cyberpower-virtual@localhost`

**When to worry:** SoH below 80%, replacement prediction within 3 months, or R_internal rising sharply.

---

## Deep Battery Test

Normal short blackouts (1-2 min) calibrate the upper part of the voltage curve. A deep test reaches the "cliff region" (10.5-11.0V) where voltage drops sharply, giving the most accurate low-SoC data.

**When to do this:** Once after initial setup, then every 6-12 months.

### Steps

1. **Lower the shutdown threshold** temporarily:

   ```bash
   nano ~/.config/ups-battery-monitor/config.toml
   # Change: shutdown_minutes = 1
   sudo systemctl restart ups-battery-monitor
   ```

2. **Run the deep test** (30-60 min, server runs on battery):

   ```bash
   sudo ~/scripts/cron/ups-test.sh deep
   ```

3. **Monitor progress** (optional):

   ```bash
   sudo journalctl -u ups-battery-monitor -f --no-pager
   ```

4. **Restore normal threshold:**

   ```bash
   nano ~/.config/ups-battery-monitor/config.toml
   # Change back: shutdown_minutes = 5
   sudo systemctl restart ups-battery-monitor
   ```

**What happens:** The daemon records voltage-SoC points every 10 seconds throughout the discharge. After power restores, cliff region interpolation runs automatically if enough data was captured. The 1-minute threshold ensures the server shuts down before full depletion.

---

## Battery Replacement

When the battery degrades beyond useful life, replace it and tell the daemon to start fresh calibration.

**When to replace:** SoH below 80% (MOTD alert), replacement predictor date approaching, or runtime consistently shorter than expected. The daemon auto-detects new batteries: if measured capacity jumps >10% after convergence, MOTD will show an alert prompting you to confirm.

### Steps

1. **Power off the UPS and replace the physical battery.** For CyberPower UT850EG: slide the front panel down, pull the battery tray out, swap the battery, reconnect terminals (red=positive first), slide tray back.

2. **Tell the daemon about the new battery:**

   ```bash
   sudo systemctl stop ups-battery-monitor
   sudo systemctl start ups-battery-monitor --new-battery
   ```

   Or equivalently, edit the service override to pass the flag once:

   ```bash
   sudo python3 -m src.monitor --new-battery
   # Ctrl+C after it starts, then:
   sudo systemctl start ups-battery-monitor
   ```

3. **Run a deep test** to kickstart calibration (optional but recommended):

   ```bash
   sudo ~/scripts/cron/ups-test.sh deep
   ```

4. **Verify** after the first discharge event:

   ```bash
   ./scripts/battery-health.py
   ```

   SoH should be ~100%, Peukert back to default 1.2, cycle count 0.

### What `--new-battery` resets

| Field | Reset to |
|-------|----------|
| SoH | 1.0 (100%) |
| SoH history | Fresh entry only |
| Peukert exponent | 1.2 (default) |
| RLS estimators (ir_k, Peukert) | P=1.0 (no confidence) |
| Capacity estimates | Cleared |
| Cycle count | 0 |
| Battery install date | Today |

What stays unchanged: LUT (standard VRLA curve entries remain), config.toml settings, R_internal history (cleared separately). Old model.json is preserved in daily borg backup if you need to compare.

---

## Configuration

All settings are in `~/.config/ups-battery-monitor/config.toml`:

```toml
# UPS device name in NUT (as configured in ups.conf)
ups_name = "cyberpower"

# Initiate shutdown when estimated runtime drops below this (minutes)
shutdown_minutes = 5

# Alert when battery health (SoH) drops below this (0.0-1.0)
soh_alert = 0.80
```

After editing: `sudo systemctl restart ups-battery-monitor`
