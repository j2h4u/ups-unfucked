#!/usr/bin/env python3
"""Battery health report — SoH, replacement prediction, R_internal trend, LUT coverage."""

import json
import sys
from pathlib import Path

MODEL_PATH = Path.home() / '.config' / 'ups-battery-monitor' / 'model.json'


def main():
    if not MODEL_PATH.exists():
        print(f"Model not found: {MODEL_PATH}")
        print("Is ups-battery-monitor running?")
        sys.exit(1)

    m = json.loads(MODEL_PATH.read_text())

    # State of Health
    soh = m.get('soh', 1.0)
    status = ' ⚠ below threshold' if soh < m.get('soh_threshold', 0.80) else ''
    print(f'SoH: {soh:.0%}{status}')

    # Capacity
    print(f'Capacity: {m.get("capacity_ah", "?")} Ah')

    # LUT calibration coverage
    lut = m.get('lut', [])
    measured = sum(1 for e in lut if e.get('source') == 'measured')
    print(f'LUT: {len(lut)} points ({measured} measured)')

    # SoH history
    history = m.get('soh_history', [])
    print(f'Discharge events: {len(history)}')
    if history:
        print(f'Latest: {history[-1].get("date", "?")} = {history[-1].get("soh", 0):.0%}')

    # Replacement prediction
    if len(history) >= 3:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from src.replacement_predictor import linear_regression_soh
        result = linear_regression_soh(history, threshold_soh=0.80)
        if result:
            slope, intercept, r2, date = result
            print(f'Predicted replacement: {date} (confidence R²={r2:.2f})')

    # Internal resistance trend
    r_hist = m.get('r_internal_history', [])
    if r_hist:
        latest = r_hist[-1]
        print(f'R_internal: {latest["r_ohm"]*1000:.1f}mΩ ({latest["date"]})')
        if len(r_hist) >= 3:
            delta = (r_hist[-1]['r_ohm'] - r_hist[0]['r_ohm']) * 1000
            print(f'R_internal trend: {delta:+.1f}mΩ since {r_hist[0]["date"]}')


if __name__ == '__main__':
    main()
