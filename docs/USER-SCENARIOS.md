# User Scenarios

The monitor works fully automatically out of the box. These scenarios describe optional actions for users who want more control or accuracy.

## Deep Battery Test (Cliff Region Calibration)

The standard VRLA voltage curve is accurate above 11V but approximate in the "cliff region" (11.0V-10.5V) where voltage drops sharply. A deep battery test measures this region with real data, improving SoC accuracy at low charge.

**When to do this:** Once after initial setup, and optionally every 6-12 months to track battery aging in the cliff region.

**What it does:** The UPS discharges the battery fully under controlled conditions. The daemon records voltage-SoC points throughout the discharge, including the cliff region that normal short blackouts don't reach.

### Steps

1. **Lower the shutdown threshold** so the battery discharges deeper than normal:

   ```bash
   # Edit config
   nano ~/.config/ups-battery-monitor/config.toml
   # Change: shutdown_minutes = 1
   ```

2. **Restart the daemon** to pick up the new threshold:

   ```bash
   sudo systemctl restart ups-battery-monitor
   ```

3. **Start the deep test:**

   ```bash
   sudo ~/scripts/cron/ups-test.sh deep
   ```

   This runs for 30-60 minutes depending on battery capacity and load. The UPS will switch to battery and discharge until low battery. The daemon collects voltage-SoC data points every 10 seconds.

4. **Monitor progress** (optional):

   ```bash
   sudo journalctl -u ups-battery-monitor -f --no-pager
   ```

   You'll see periodic poll lines with decreasing charge% and time_rem. When the test completes, you'll see "LUT cliff region updated from measured discharge data" if cliff points were captured.

5. **Restore the normal shutdown threshold:**

   ```bash
   nano ~/.config/ups-battery-monitor/config.toml
   # Change back: shutdown_minutes = 5
   sudo systemctl restart ups-battery-monitor
   ```

**What changes in model.json:** New LUT entries with `"source": "measured"` and `"source": "interpolated"` appear, replacing `"standard"` entries in the cliff region.

**Risk:** The server will run on battery for the entire test duration. If the test is interrupted (e.g., real blackout during test), the daemon still collects whatever data it can. The 1-minute shutdown threshold ensures the server shuts down before the battery is fully depleted.

---

## Check Battery Health

The daemon tracks battery State of Health (SoH) automatically from every discharge event. To check current status:

```bash
# Quick status from MOTD
cat ~/.config/ups-battery-monitor/model.json | python3 -c "
import json, sys
m = json.load(sys.stdin)
print(f'SoH: {m.get(\"soh\", 1.0):.0%}')
print(f'Capacity: {m.get(\"capacity_ah\", \"?\")} Ah')
print(f'LUT points: {len(m.get(\"lut\", []))} ({sum(1 for e in m.get(\"lut\", []) if e.get(\"source\")==\"measured\")} measured)')
history = m.get('soh_history', [])
if history:
    print(f'SoH history: {len(history)} entries, latest {history[-1].get(\"date\", \"?\")} = {history[-1].get(\"soh\", \"?\"):.0%}')
"
```

Or check the virtual UPS via NUT:

```bash
upsc cyberpower-virtual@localhost
```

**When to worry:** SoH below 80% means the battery delivers significantly less runtime than rated. The daemon logs a warning to journald and the MOTD health script shows an alert.

---

## Check Replacement Prediction

After 20+ discharge events (~2-3 months of operation), the daemon can predict when battery replacement will be needed:

```bash
cat ~/.config/ups-battery-monitor/model.json | python3 -c "
import json, sys
m = json.load(sys.stdin)
history = m.get('soh_history', [])
print(f'Discharge events tracked: {len(history)}')
if len(history) >= 3:
    from src.replacement_predictor import linear_regression_soh
    result = linear_regression_soh(history, threshold_soh=0.80)
    if result:
        slope, intercept, r2, date = result
        print(f'Predicted replacement: {date} (R²={r2:.2f})')
    else:
        print('Not enough data or no degradation trend yet')
else:
    print('Need at least 3 discharge events for prediction')
"
```

---

## View LUT Sources

See which voltage-SoC points are from the standard curve vs measured data:

```bash
cat ~/.config/ups-battery-monitor/model.json | python3 -c "
import json, sys
lut = json.load(sys.stdin).get('lut', [])
for e in sorted(lut, key=lambda x: -x['v']):
    print(f'{e[\"v\"]:5.2f}V → SoC {e[\"soc\"]:5.1%}  [{e.get(\"source\", \"?\")}]')
"
```

Over time, `standard` entries are supplemented (and in the cliff region replaced) by `measured` and `interpolated` entries from real discharge data.

---

## Change UPS Device Name

If your UPS is configured with a different name in NUT (`/etc/nut/ups.conf`):

```bash
nano ~/.config/ups-battery-monitor/config.toml
# Change: ups_name = "your-ups-name"
sudo systemctl restart ups-battery-monitor
```

---

## Adjust Shutdown Safety Margin

The default 5-minute threshold provides comfortable margin for clean shutdown. Adjust if needed:

```bash
nano ~/.config/ups-battery-monitor/config.toml
# Change: shutdown_minutes = 3   (tighter, more runtime before shutdown)
# Or:     shutdown_minutes = 10  (conservative, earlier shutdown)
sudo systemctl restart ups-battery-monitor
```

---

## View Internal Resistance History

The daemon measures battery internal resistance on every OL→OB transition (voltage sag method). Rising R_internal over months indicates degradation:

```bash
cat ~/.config/ups-battery-monitor/model.json | python3 -c "
import json, sys
m = json.load(sys.stdin)
for e in m.get('r_internal_history', []):
    print(f'{e[\"date\"]}: R={e[\"r_ohm\"]*1000:.1f}mΩ  (V: {e[\"v_before\"]:.2f}→{e[\"v_after\"]:.2f}V at {e[\"load_pct\"]:.0f}% load)')
"
```
