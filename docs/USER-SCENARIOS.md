# User Scenarios

The monitor works fully automatically out of the box. These scenarios describe optional actions for users who want more control or accuracy.

## Battery Health Report

The daemon tracks battery health automatically from every discharge event. All data lives in `~/.config/ups-battery-monitor/model.json`.

```bash
cat ~/.config/ups-battery-monitor/model.json | python3 -c "
import json, sys
m = json.load(sys.stdin)

# State of Health
soh = m.get('soh', 1.0)
print(f'SoH: {soh:.0%}' + (' ⚠ below 80%' if soh < 0.80 else ''))

# Capacity
print(f'Capacity: {m.get(\"capacity_ah\", \"?\")} Ah')

# LUT calibration coverage
lut = m.get('lut', [])
measured = sum(1 for e in lut if e.get('source') == 'measured')
print(f'LUT: {len(lut)} points ({measured} measured, {len(lut)-measured} standard/interpolated)')

# SoH history and replacement prediction
history = m.get('soh_history', [])
print(f'Discharge events: {len(history)}')
if history:
    print(f'Latest: {history[-1].get(\"date\", \"?\")} = {history[-1].get(\"soh\", 0):.0%}')
if len(history) >= 3:
    sys.path.insert(0, '.')
    from src.replacement_predictor import linear_regression_soh
    result = linear_regression_soh(history, threshold_soh=0.80)
    if result:
        slope, intercept, r2, date = result
        print(f'Predicted replacement: {date} (confidence R²={r2:.2f})')

# Internal resistance trend
r_hist = m.get('r_internal_history', [])
if r_hist:
    latest = r_hist[-1]
    print(f'R_internal: {latest[\"r_ohm\"]*1000:.1f}mΩ ({latest[\"date\"]})')
    if len(r_hist) >= 3:
        first, last = r_hist[0], r_hist[-1]
        delta = (last['r_ohm'] - first['r_ohm']) * 1000
        print(f'R_internal trend: {delta:+.1f}mΩ since {first[\"date\"]}')
"
```

You can also check live metrics via NUT: `upsc cyberpower-virtual@localhost`

**When to worry:** SoH below 80%, replacement prediction within 3 months, or R_internal rising sharply — all indicate the battery should be replaced soon.

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
