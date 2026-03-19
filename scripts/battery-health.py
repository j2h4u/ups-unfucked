#!/usr/bin/env python3
"""Battery health report — shows current battery condition and degradation trends."""

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

MODEL_PATH = Path.home() / '.config' / 'ups-battery-monitor' / 'model.json'


def main():
    if not MODEL_PATH.exists():
        print(f"No battery model at {MODEL_PATH}")
        print("The daemon creates this file on first start. Try:")
        print("  sudo systemctl restart ups-battery-monitor")
        sys.exit(1)

    try:
        model_data = json.loads(MODEL_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error reading {MODEL_PATH}: {e}")
        sys.exit(1)

    # UPS identity from NUT
    ups_name = model_data.get('ups_name', 'cyberpower-virtual')
    # Validate ups_name before passing to subprocess (same regex as nut_client._validate_nut_identifier)
    if not re.match(r'^[a-zA-Z0-9._-]+$', ups_name):
        print(f"  WARNING: Invalid ups_name in model.json: {ups_name!r}")
        ups_name = 'cyberpower-virtual'
    try:
        result = subprocess.run(
            ['upsc', f'{ups_name}@localhost'], capture_output=True, text=True, timeout=2
        )
        if result.returncode != 0:
            print(f"  UPS: (upsc failed: {result.stderr.strip() or 'exit ' + str(result.returncode)})")
        else:
            nut_vars = result.stdout
            mfr = next((line.split(': ', 1)[1] for line in nut_vars.splitlines() if line.startswith('device.mfr:')), None)
            ups_device_model = next((line.split(': ', 1)[1] for line in nut_vars.splitlines() if line.startswith('device.model:')), None)
            if mfr and ups_device_model:
                print(f"  UPS:              {mfr} {ups_device_model}")
    except (OSError, subprocess.TimeoutExpired):
        print("  UPS: (NUT unavailable)")

    # State of Health — how much usable capacity remains vs new battery
    soh = model_data.get('soh', 1.0)
    if soh < 0.80:
        print(f"  State of Health:  {soh:.0%}  ⚠ DEGRADED (below 80%, consider replacement)")
    elif soh < 0.90:
        print(f"  State of Health:  {soh:.0%}  (aging, monitor closely)")
    else:
        print(f"  State of Health:  {soh:.0%}  (healthy)")

    # Rated capacity
    capacity_ah = model_data.get('capacity_ah') or model_data.get('full_capacity_ah_ref')
    if capacity_ah:
        print(f"  Rated capacity:   {capacity_ah} Ah")

    # Battery age and cycle count
    install_date = model_data.get('battery_install_date')
    if install_date:
        try:
            age_days = (datetime.now() - datetime.strptime(install_date, '%Y-%m-%d')).days
            print(f"  Battery age:      {age_days} days (installed {install_date})")
        except ValueError:
            print(f"  Battery age:      unknown (bad install_date: {install_date!r})")
    cycle_count = model_data.get('cycle_count', 0)
    cumulative_sec = model_data.get('cumulative_on_battery_sec', 0.0)
    cumulative_min = cumulative_sec / 60
    print(f"  Cycles:           {cycle_count} (total {cumulative_min:.0f} min on battery)")

    # LUT — how well the voltage-SoC curve is calibrated from real data
    lut = model_data.get('lut', [])
    measured = sum(1 for e in lut if e.get('source') == 'measured')
    if measured == 0:
        print(f"  Calibration:      standard curve (no real discharge data yet)")
    else:
        print(f"  Calibration:      {measured} measured points, {len(lut)} total in LUT")

    # Discharge history — each blackout/test contributes a SoH data point
    soh_history = model_data.get('soh_history', [])
    print(f"  Discharge events: {len(soh_history)} recorded")
    if soh_history:
        latest = soh_history[-1]
        print(f"  Last discharge:   {latest.get('date', '?')} (SoH was {latest.get('soh', 0):.0%})")

    # Replacement prediction — needs 3+ events for linear regression
    if len(soh_history) >= 3:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from src.replacement_predictor import linear_regression_soh
        soh_trend_result = linear_regression_soh(soh_history, threshold_soh=0.80)
        if soh_trend_result:
            slope, intercept, r2, date = soh_trend_result
            print(f"  Replace battery:  ~{date} (trend confidence R²={r2:.2f})")
        else:
            print(f"  Replace battery:  no degradation trend detected yet")
    elif len(soh_history) > 0:
        print(f"  Replace battery:  need {3 - len(soh_history)} more discharge events for prediction")

    # Internal resistance — rising R means aging (sulfation, grid corrosion)
    r_hist = model_data.get('r_internal_history', [])
    if r_hist:
        latest = r_hist[-1]
        print(f"  R_internal:       {latest['r_ohm']*1000:.1f} mΩ (measured {latest['date']})")
        if len(r_hist) >= 3:
            delta = (r_hist[-1]['r_ohm'] - r_hist[0]['r_ohm']) * 1000
            direction = "rising ⚠" if delta > 0.5 else "stable" if delta > -0.5 else "improving"
            print(f"  R_internal trend: {delta:+.1f} mΩ since {r_hist[0]['date']} ({direction})")


if __name__ == '__main__':
    main()
