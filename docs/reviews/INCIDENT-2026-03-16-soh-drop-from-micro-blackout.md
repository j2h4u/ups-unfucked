# Incident Report: SoH Drop from Micro-Blackout (2026-03-16)

**Date:** 2026-03-16 01:41–01:43 UTC+5
**Impact:** SoH dropped from 99.7% to 88.6%, Peukert exponent jumped from 1.2 to 1.4 (max), replacement prediction went from "healthy" to "replace in 2 days"
**Root cause:** 105-second blackout triggered full SoH recalculation and Peukert auto-calibration — both fundamentally flawed for sub-2-minute discharges
**Severity:** Data corruption (model state), no system impact (daemon continued operating)
**Resolution:** Documents need for Phase 12.1 fixes (30s SoH minimum, Peukert calibration minimum duration)

---

## Timeline

```
01:41:15  OL — V_ema=13.70V, load=15%, charge=100%, runtime=47.3min. All normal.
01:41:25  OL→OB(TEST) — Quick test starts. V_ema drops to 13.17V immediately.
01:41:26  OB(TEST)→OB(REAL) — Input voltage classified as real blackout (not test).
01:41:25–01:41:30  Sag measurement: 13.16V → 13.10V, R_internal=10.6mΩ at 15% load.
01:41:25–01:42:30  Discharge buffer collects 12 points across 65 seconds.
           Voltage: 13.1V (6 points) → 12.5V (4 points). Only 2 distinct ADC levels.
01:42:30–01:43:10  Continued discharge, buffer grows to 15 points.
01:43:10  OB→OL — Power restored. _update_battery_health() fires.
```

## What went wrong

### Problem 1: SoH calculation on 105-second discharge

```
calculate_soh_from_discharge() called with:
  - voltage_series: 13.1, 13.1, 13.1, ... 12.5, 12.5, 12.5 (15 points, 105 seconds)
  - reference_soh: 0.997
  - capacity_ah: 7.2
  - peukert_exponent: 1.2

T_expected = peukert_runtime_hours(15%, 7.2, 1.2) × 3600 ≈ 2820 seconds (47 min)
discharge_weight = min(105 / (0.30 × 2820), 1.0) = min(0.124, 1.0) = 0.124

area_measured: small (105 seconds of 13.1→12.5V curve)
area_reference: large (47 minutes × avg voltage)
degradation_ratio = area_measured / area_reference ≈ 0.037 (battery looks 96% dead)

measured_soh = 0.997 × 0.037 = 0.037 (!)
new_soh = 0.997 × (1 - 0.124) + 0.037 × 0.124 = 0.874 + 0.005 = 0.879

Result: SoH dropped to 87.9%, then a second event pushed it to 88.6%.
```

**The math is correct but the input is garbage.** A 105-second discharge covers <4% of the expected runtime — the area ratio is meaningless. The duration_weight (0.124 = 12.4%) is too high for such a short event.

### Problem 2: Peukert auto-calibration on 1.6-minute discharge

```
_auto_calibrate_peukert() called with:
  - actual_duration_sec: 96 (1.6 minutes)
  - predicted_runtime: 46.1 minutes (at SoH=0.997, load=15%)
  - error: |1.6 - 46.1| / 46.1 = 96.5% — exceeds 10% threshold

New Peukert exponent calculated: 1.4 (maximum allowed)
```

**The calibration assumed the discharge ran to completion.** It compared predicted *full* runtime (46 minutes) with actual *interrupted* runtime (1.6 minutes) and concluded the battery is terrible. In reality, power simply returned after 1.6 minutes — the battery was fine.

### Problem 3: Only 2 distinct voltage levels in 105 seconds

The ADC reports 0.1V resolution. In 105 seconds at 15% load, voltage dropped from 13.1V to 12.5V — exactly 2 discrete levels. The "discharge curve" is a step function with 1 step:

```
        13.2V ─────┐
                    │ (one 0.6V step somewhere around t=35s)
        12.5V      └─────────────
              0    35s          105s
```

SoH via area-under-curve on this is dominated by where exactly the step happens. Move the step 10 seconds earlier = completely different area ratio. **The measurement has no statistical significance.**

### Problem 4: Replacement prediction from 4 points

```
SoH history: [100%, 99.7%, 89.4%, 88.6%]
Linear regression: R²=0.68, prediction = "replace by 2026-03-18"
```

A 2-day-old battery predicted to die in 2 days. The regression is fitting a cliff that doesn't exist — it's fitting garbage data from micro-blackouts.

---

## What the expert panels predicted (2026-03-15, one day before this incident)

| Expert | Prediction | What actually happened |
|--------|-----------|----------------------|
| **QA (Panel 3):** "Add minimum 30s discharge for SoH update" | SoH should NOT have been updated for a 105-second discharge | SoH updated, dropped 11% |
| **QA (Panel 3):** "Peukert calibration needs minimum duration" | Peukert should NOT have been recalibrated | Peukert jumped to 1.4 (max) |
| **QA (Panel 3):** "Flicker storm: cycle_count inflates" | Cycle count is now 3 for 112 total seconds | Battery looks "3 cycles used" |
| **Statistician (Panel 1):** "CoV with 2 samples is meaningless" | Only 2 distinct voltage levels in the discharge | Area-under-curve is noise |
| **Battery Expert (Panel 3):** "Bayesian SoH has inertia at cliff" | Weight 0.124 for 105s is too high — should be ~0 | SoH blending formula is too aggressive for short discharges |
| **Metrologist (Panel 3):** "ADC resolution 0.1V limits measurement" | 2 voltage levels in 105s | Discharge "curve" is a single step function |

---

## Impact on model.json (current corrupt state)

| Field | Before (2026-03-15) | After (2026-03-16) | Ground truth |
|-------|---------------------|---------------------|-------------|
| `soh` | 0.997 (99.7%) | 0.886 (88.6%) | ~100% (brand new battery, installed 2026-03-14) |
| `peukert_exponent` | 1.2 | 1.4 (max) | ~1.15 (from 2026-03-12 real blackout calibration) |
| `cycle_count` | 1 | 3 | 3 (correct, but misleading for 112s total) |
| replacement_due | null | 2026-03-18 | null (battery is 2 days old) |
| runtime prediction | ~47 min at 15% load | ~22 min at 15% load | ~47 min (proven 2026-03-12) |

---

## Required fixes (all already in Phase 12.1 roadmap)

1. **30s minimum for SoH update** — kernel returns None for < 30s. Would have prevented the SoH drop entirely.
2. **Minimum discharge duration for Peukert calibration** — currently 60s in code, but should be much higher (300s? match VAL-01?). 105s is above 60s threshold but still garbage for calibration.
3. **Peukert must compare against *completed* discharges only** — an interrupted discharge (power returned) is NOT a capacity measurement. The calibration must know the discharge was interrupted.
4. **Discharge cooldown** — the BLACKOUT_TEST → BLACKOUT_REAL transition in 1 second suggests a classification issue. Timer-triggered quick test coincided with real power interruption?
5. **Math kernel extraction** — all these fixes belong in pure functions, testable in simulation before production.

## Replay test case for Phase 12.1 simulation

```python
def test_replay_2026_03_16_micro_blackout():
    """
    Real incident: 105-second blackout should NOT corrupt model state.

    Input: discharge_buffer from 2026-03-16 01:41:25–01:43:10
    V = [13.1]*8 + [12.5]*7  (15 points, 105 seconds, 15% load)

    Assert:
    - SoH change < 1% (should be ~0% for 105s discharge on new battery)
    - Peukert exponent unchanged (discharge too short for calibration)
    - Replacement prediction: null (insufficient data)
    """
```

---

**Filed:** 2026-03-16
**Status:** Open — fixes planned in Phase 12.1 Wave 5 (adversarial scenarios) and Wave 1 (kernel extraction)
**Cross-reference:** Expert panels: `docs/reviews/EXPERT-PANEL-2026-03-15-phase12.1-metrology-adversarial-lifecycle.md`
