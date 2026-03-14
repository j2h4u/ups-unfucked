#!/usr/bin/env python3
"""Battery health report — shows current battery condition and degradation trends."""

import json
import sys
from pathlib import Path

MODEL_PATH = Path.home() / '.config' / 'ups-battery-monitor' / 'model.json'


def main():
    if not MODEL_PATH.exists():
        print(f"No battery model at {MODEL_PATH}")
        print("The daemon creates this file on first start. Try:")
        print("  sudo systemctl restart ups-battery-monitor")
        sys.exit(1)

    m = json.loads(MODEL_PATH.read_text())

    # UPS identity from NUT
    ups_name = m.get('ups_name', 'cyberpower-virtual')
    try:
        import subprocess
        nut_vars = subprocess.run(
            ['upsc', f'{ups_name}@localhost'], capture_output=True, text=True, timeout=2
        ).stdout
        mfr = next((l.split(': ', 1)[1] for l in nut_vars.splitlines() if l.startswith('device.mfr:')), None)
        model = next((l.split(': ', 1)[1] for l in nut_vars.splitlines() if l.startswith('device.model:')), None)
        if mfr and model:
            print(f"  UPS:              {mfr} {model}")
    except Exception:
        pass

    # State of Health — how much usable capacity remains vs new battery
    soh = m.get('soh', 1.0)
    if soh < 0.80:
        print(f"  State of Health:  {soh:.0%}  ⚠ DEGRADED (below 80%, consider replacement)")
    elif soh < 0.90:
        print(f"  State of Health:  {soh:.0%}  (aging, monitor closely)")
    else:
        print(f"  State of Health:  {soh:.0%}  (healthy)")

    # Rated capacity
    capacity = m.get('capacity_ah') or m.get('full_capacity_ah_ref')
    if capacity:
        print(f"  Rated capacity:   {capacity} Ah")

    # Battery age and cycle count
    install_date = m.get('battery_install_date')
    if install_date:
        from datetime import datetime as dt
        age_days = (dt.now() - dt.strptime(install_date, '%Y-%m-%d')).days
        print(f"  Battery age:      {age_days} days (installed {install_date})")
    cycle_count = m.get('cycle_count', 0)
    cumulative_sec = m.get('cumulative_on_battery_sec', 0.0)
    cumulative_min = cumulative_sec / 60
    print(f"  Cycles:           {cycle_count} (total {cumulative_min:.0f} min on battery)")

    # LUT — how well the voltage-SoC curve is calibrated from real data
    lut = m.get('lut', [])
    measured = sum(1 for e in lut if e.get('source') == 'measured')
    if measured == 0:
        print(f"  Calibration:      standard curve (no real discharge data yet)")
    else:
        print(f"  Calibration:      {measured} measured points, {len(lut)} total in LUT")

    # Discharge history — each blackout/test contributes a SoH data point
    history = m.get('soh_history', [])
    print(f"  Discharge events: {len(history)} recorded")
    if history:
        latest = history[-1]
        print(f"  Last discharge:   {latest.get('date', '?')} (SoH was {latest.get('soh', 0):.0%})")

    # Replacement prediction — needs 3+ events for linear regression
    if len(history) >= 3:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from src.replacement_predictor import linear_regression_soh
        result = linear_regression_soh(history, threshold_soh=0.80)
        if result:
            slope, intercept, r2, date = result
            print(f"  Replace battery:  ~{date} (trend confidence R²={r2:.2f})")
        else:
            print(f"  Replace battery:  no degradation trend detected yet")
    elif len(history) > 0:
        print(f"  Replace battery:  need {3 - len(history)} more discharge events for prediction")

    # Internal resistance — rising R means aging (sulfation, grid corrosion)
    r_hist = m.get('r_internal_history', [])
    if r_hist:
        latest = r_hist[-1]
        print(f"  R_internal:       {latest['r_ohm']*1000:.1f} mΩ (measured {latest['date']})")
        if len(r_hist) >= 3:
            delta = (r_hist[-1]['r_ohm'] - r_hist[0]['r_ohm']) * 1000
            direction = "rising ⚠" if delta > 0.5 else "stable" if delta > -0.5 else "improving"
            print(f"  R_internal trend: {delta:+.1f} mΩ since {r_hist[0]['date']} ({direction})")


if __name__ == '__main__':
    main()
