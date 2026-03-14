---
created: 2026-03-14T12:17:57.569Z
title: Battery replacement scenario docs and implementation
area: general
files:
  - docs/USER-SCENARIOS.md
  - src/monitor.py
  - src/model.py
---

## Problem

When the battery degrades below SoH threshold (frequent blackouts accelerate this to months, not years), the user needs a clear workflow for physical battery replacement and daemon recalibration. Currently there is no documentation or tooling for this — after swapping the battery, old SoH history, R_internal measurements, and measured LUT points all refer to the dead battery, poisoning predictions for the new one.

## Solution

1. **Documentation** (docs/USER-SCENARIOS.md): Add "Battery Replacement" scenario:
   - When to replace (SoH alert, replacement predictor date, MOTD warning)
   - Physical replacement steps (CyberPower UT850EG specific)
   - Post-replacement: reset model.json, run initial deep test
   - How to verify new battery is working

2. **Implementation**:
   - CLI command or script to reset model.json to defaults (fresh VRLA curve)
   - Archive old model.json (e.g., `model.json.replaced-2026-03-14`) for historical reference
   - Preserve config.toml settings (ups_name, thresholds) — only model data resets
   - Optional: first deep test prompt after reset to kickstart calibration

3. **Model reset scope** — what gets cleared:
   - LUT → back to standard VRLA curve
   - soh_history → empty
   - r_internal_history → empty
   - Peukert exponent → default 1.2
   - battery_install_date → reset to current date (new battery)
   - cycle_count → 0
   - cumulative_on_battery_sec → 0
   - What stays: config.toml, physics defaults in model structure
