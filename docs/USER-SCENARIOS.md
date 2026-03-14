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

> **TODO:** This scenario requires implementation — see `.planning/todos/pending/2026-03-14-battery-replacement-scenario-docs-and-implementation.md`

When the battery degrades beyond useful life (SoH below threshold, replacement predictor alerts), replace it and reset the daemon's model to start fresh calibration on the new battery.

Steps (planned):
1. Replace the physical battery in the UPS
2. Reset model.json (archive old data, restore standard VRLA curve)
3. Run a deep battery test to kickstart calibration
4. Verify the new battery works (check health report after first discharge)

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
