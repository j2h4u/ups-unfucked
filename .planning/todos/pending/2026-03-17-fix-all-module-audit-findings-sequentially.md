---
created: "2026-03-17T00:00:00.000Z"
title: Fix all module audit findings sequentially
area: general
files:
  - docs/internal/MODULE-AUDIT.md
  - src/event_classifier.py
  - src/virtual_ups.py
  - src/battery_math/soh.py
  - src/soh_calculator.py
  - src/monitor.py
  - src/battery_math/calibration.py
  - src/capacity_estimator.py
  - src/replacement_predictor.py
---

## Problem

Module audit (2026-03-17) found 58 findings across 10 modules. 2 critical, 7 high, 14 medium. Several findings block each other (F19 blocks F53 and F30). Need sequential execution in dependency order.

## Solution

Execute in priority order from MODULE-AUDIT.md. Each step is a commit.

### Step 1: F36 + F36a — Event classifier (Critical+High)
- Change `_STATUS_CATEGORY` from exact match to flag-based: `"OB" in status → battery`
- Fallback: when classifier can't parse, pass original `ups.status` to virtual UPS instead of computed override
- Files: `src/event_classifier.py`, `src/monitor.py` (status override fallback)

### Step 2: F41 — BLACKOUT_TEST LB safety net (High)
- `compute_ups_status_override`: add hard floor — if `time_rem < 2 min`, return LB regardless of event type
- File: `src/virtual_ups.py`

### Step 3: F19 + F20 + F21 — SoH formula redesign (Critical+High)
- **Biggest change.** Current formula compares partial discharge area to full discharge area → always wrong.
- Option A: ΔSoC normalization (compare energy rate for observed SoC range)
- Option B: Capacity-based SoH = `measured_capacity / rated_capacity` (uses CapacityEstimator)
- Option C: Hybrid
- Needs design decision before implementation. Expert panel recommended option B as simplest.
- Files: `src/battery_math/soh.py`, `src/soh_calculator.py`, `src/monitor.py`

### Step 4: F30 — Peukert RLS skip clamped values (High)
- In `_auto_calibrate_peukert`: if `calibrate_peukert()` returns a value == 1.0 or 1.4 (clamp bounds), skip RLS update
- File: `src/monitor.py`

### Step 5: F24 — Capacity ΔSoC gate 25%→15% (High)
- `_passes_quality_filter`: change `delta_soc < 0.25` to `delta_soc < 0.15`
- File: `src/capacity_estimator.py`

### Step 6: F42 — Virtual UPS log level INFO→DEBUG (Medium)
- `write_virtual_ups_dev`: change `logger.info` to `logger.debug` for the "metrics written" line
- File: `src/virtual_ups.py`

### Step 7: F54 — Wire capacity_ah_ref in replacement prediction caller (Medium)
- Pass `capacity_ah_ref` to `linear_regression_soh()` from `_update_battery_health`
- File: `src/monitor.py`

### Step 8: F58 — Decompose monitor.py (Medium)
- Extract logical groups into focused modules. Candidates:
  - Discharge tracking → `src/discharge_tracker.py`
  - Sag measurement → `src/sag_tracker.py`
  - Virtual UPS writing → already in `src/virtual_ups.py` (just the metric assembly in monitor.py)
  - Health endpoint → `src/health_endpoint.py`
- Largest refactoring step. Do last.
