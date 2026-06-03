#!/usr/bin/env python3
"""Battery health report — shows current battery condition and degradation trends."""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.replacement_predictor import linear_regression_soh

# Production paths — override via env for fixture-based testing (mirrors src/motd_status.py).
# UPS_MODEL_PATH / UPS_HEALTH_PATH allow running against readable copies without sudo.
MODEL_PATH = Path(os.environ.get("UPS_MODEL_PATH", Path.home() / ".config" / "ups-battery-monitor" / "model.json"))
HEALTH_PATH = Path(os.environ.get("UPS_HEALTH_PATH", "/run/ups-battery-monitor/ups-health.json"))


def print_maintenance(model_data: dict, health_data: dict) -> None:
    """Print the Maintenance & schedule section.

    Sources:
      - next_test_timestamp: health.json preferred (per-poll scheduler output),
        falls back to model.json scheduled_test_timestamp.
      - scheduling_reason: health.json.
      - last test: model.json last_upscmd_timestamp / last_upscmd_type / last_upscmd_status.
      - IR trend: health.json ir_trend_rate (Ω/day) or recomputed from model r_internal_history.
      - capacity/SoH: model.json soh, capacity_ah / full_capacity_ah_ref.
    """
    print()
    print("  Maintenance & schedule")
    print("  ─────────────────────")

    # --- Next diagnostic test ---
    next_ts = health_data.get("next_test_timestamp") or model_data.get("scheduled_test_timestamp")
    reason = health_data.get("scheduling_reason", "")
    if next_ts:
        try:
            next_dt = datetime.fromisoformat(next_ts)
            days_away = (next_dt - datetime.now().astimezone()).days
            reason_str = f"  ({reason})" if reason else ""
            print(f"  Next diagnostic test: {next_ts[:10]}"
                  f"  ({days_away:+d} days){reason_str}")
        except (ValueError, TypeError):
            # ValueError: unparseable ISO string. TypeError: a corrupt JSON file
            # stored a non-string (e.g. numeric epoch) — degrade, don't crash (T-25-07).
            print(f"  Next diagnostic test: {next_ts}  ({reason})")
    else:
        print("  Next diagnostic test: not yet scheduled")

    # --- Last test run ---
    last_ts = model_data.get("last_upscmd_timestamp")
    last_type = model_data.get("last_upscmd_type") or ""
    last_status = model_data.get("last_upscmd_status") or ""
    if last_ts:
        try:
            last_dt = datetime.fromisoformat(last_ts)
            days_ago = (datetime.now().astimezone() - last_dt).days
            type_str = f"  type={last_type}" if last_type else ""
            status_str = f"  status={last_status}" if last_status else ""
            print(f"  Last test run:        {last_ts[:10]}"
                  f"  ({days_ago} days ago){type_str}{status_str}")
        except (ValueError, TypeError):
            # See T-25-07 note above: tolerate a corrupt non-string timestamp.
            print(f"  Last test run:        {last_ts}{' ' + last_type if last_type else ''}")
    else:
        print("  Last test run:        never tested")

    # --- IR trend ---
    ir_trend_rate = health_data.get("ir_trend_rate")  # Ω/day from health endpoint
    if ir_trend_rate is not None:
        trend_mohm_day = ir_trend_rate * 1000
        direction = "rising (aging)" if trend_mohm_day > 0.001 else "stable" if trend_mohm_day > -0.001 else "improving"
        print(f"  IR trend rate:        {trend_mohm_day:+.4f} mΩ/day  ({direction})")
    else:
        # Fall back to recomputing from r_internal_history (same logic as main report)
        r_hist = model_data.get("r_internal_history", [])
        if len(r_hist) >= 3:
            delta_mohm = (r_hist[-1]["r_ohm"] - r_hist[0]["r_ohm"]) * 1000
            direction = "rising (aging)" if delta_mohm > 0.5 else "stable" if delta_mohm > -0.5 else "improving"
            span = f"{r_hist[0]['date']} → {r_hist[-1]['date']}"
            print(f"  IR trend:             {delta_mohm:+.1f} mΩ  ({direction}, {span})")
        elif r_hist:
            print(f"  IR trend:             only {len(r_hist)} measurement(s) — need 3+ for trend")
        else:
            print("  IR trend:             no measurements yet")

    # --- Capacity / SoH ---
    soh = model_data.get("soh", 1.0)
    capacity_ah = model_data.get("capacity_ah") or model_data.get("full_capacity_ah_ref")
    measured_cap = model_data.get("measured_capacity_ah")
    soh_str = f"{soh:.0%}"
    if capacity_ah:
        rated_str = f"rated {capacity_ah} Ah"
        if measured_cap:
            print(f"  Capacity / SoH:       SoH={soh_str}  measured={measured_cap:.2f} Ah  {rated_str}")
        else:
            print(f"  Capacity / SoH:       SoH={soh_str}  {rated_str}  (no measured capacity yet)")
    else:
        print(f"  Capacity / SoH:       SoH={soh_str}")


def main() -> None:
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
    # Validate before passing to subprocess — reject leading hyphens to prevent argument injection
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$', ups_name):
        print(f"  WARNING: Invalid ups_name in model.json: {ups_name!r}")
        ups_name = 'cyberpower-virtual'
    try:
        upsc_proc = subprocess.run(
            ['upsc', f'{ups_name}@localhost'], capture_output=True, text=True, timeout=2
        )
        if upsc_proc.returncode != 0:
            stderr_msg = (upsc_proc.stderr.strip() or f'exit {upsc_proc.returncode}')[:200]
            print(f"  UPS: (upsc failed: {stderr_msg})")
        else:
            nut_vars = {k: v for k, v in (line.split(': ', 1) for line in upsc_proc.stdout.splitlines() if ': ' in line)}
            mfr = nut_vars.get('device.mfr')
            device_model = nut_vars.get('device.model')
            if mfr and device_model:
                print(f"  UPS:              {mfr} {device_model}")
    except FileNotFoundError:
        print("  UPS: (upsc not installed)")
    except subprocess.TimeoutExpired:
        print("  UPS: (upsc timed out)")
    except OSError:
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
        replacement_prediction = linear_regression_soh(soh_history, threshold_soh=0.80)
        if replacement_prediction:
            slope, intercept, r2, date = replacement_prediction
            print(f"  Replace battery:  ~{date} (trend confidence R²={r2:.2f})")
        else:
            print(f"  Replace battery:  no degradation trend detected yet")
    elif len(soh_history) > 0:
        print(f"  Replace battery:  need {3 - len(soh_history)} more discharge events for prediction")

    # Internal resistance — rising R means cell aging (plate corrosion / electrolyte loss)
    r_hist = model_data.get('r_internal_history', [])
    if r_hist:
        latest = r_hist[-1]
        print(f"  R_internal:       {latest['r_ohm']*1000:.1f} mΩ (measured {latest['date']})")
        if len(r_hist) >= 3:
            delta_mohm = (r_hist[-1]['r_ohm'] - r_hist[0]['r_ohm']) * 1000
            direction = "rising ⚠" if delta_mohm > 0.5 else "stable" if delta_mohm > -0.5 else "improving"
            print(f"  R_internal trend: {delta_mohm:+.1f} mΩ since {r_hist[0]['date']} ({direction})")

    # Maintenance & schedule — load health endpoint, degrade gracefully if unavailable
    # /run/ups-battery-monitor/ups-health.json is typically root-owned; non-root runs hit
    # PermissionError. Set UPS_HEALTH_PATH to a readable copy/fixture to avoid sudo.
    try:
        health_data = json.loads(HEALTH_PATH.read_text())
    except PermissionError:
        print()
        print(f"  Maintenance & schedule: health endpoint unavailable (daemon not running, or run with sudo)")
        print(f"  Tip: set UPS_HEALTH_PATH=/path/to/readable/health.json to use a fixture without sudo")
        health_data = {}
    except (OSError, ValueError):
        health_data = {}

    print_maintenance(model_data, health_data)


if __name__ == '__main__':
    main()
