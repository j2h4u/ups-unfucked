# Module 6: RLS Calibration (`src/battery_math/rls.py` + `calibration.py` + wiring)

**Date:** 2026-03-17
**Panel:** Researcher (control theory/RLS), System Architect, QA Engineer

| # | Finding | Severity | Action |
|---|---------|----------|--------|
| F30 | **Peukert RLS fed clamped values from short discharges.** Any discharge <~30 min at 16% load solves n>1.4, clamps to 1.4, feeds to RLS. Converges theta to 1.4 instead of correct ~1.15. Root cause of Peukert 1.2→1.4 incident. | **High** | Skip RLS update when `calibrate_peukert` returns a value hitting clamp bounds |
| F31 | Peukert calibration depends on SoH (`T_effective = T_full * soh`). SoH is broken (F19). At current C-rate, result clamps regardless of SoH — dependency masked. Will matter when F30+F19 both fixed. | Medium | Cross-dependency — fix after F19 redesign |
| F32 | ir_k formula derivation correct: `k = R × P / (V × 100)` from `V_norm = V_ema + k*(L - L_base)` | OK | Verified |
| F33 | Clamping after RLS: minor P/theta inconsistency. P tracks unclamped trajectory. Bounded by narrow range. | Low | Document only |
| F34 | λ=0.97 effective memory: ~30 events to halve, ~10-15 weeks. Appropriate for battery aging. | OK | No change |
| F35 | P floor not needed: forgetting factor bounds P_steady ≈ 0.031, confidence caps ~97% | OK | No change |

**Design notes:**
- RLS kernel math is textbook correct (Haykin Adaptive Filter Theory, scalar φ=1 case)
- Peukert calibration works correctly for long discharges: 2026-03-12 blackout (47 min) → n=1.15 (validated)
- Short discharges (<30 min at 16% load) always produce n>1.4 (clamped) — the formula can't distinguish "short blackout" from "high Peukert" at 15.7x C-rate
- ir_k calibration from sag measurements is sound: R_internal → ir_k conversion has correct units and physics

## Fix Status

- [x] **F30** (High): Fixed — skip RLS update when calibrate_peukert returns a value hitting clamp bounds
- [ ] **F31** (Medium): Open — Peukert depends on SoH; monitor after F19+F30 fix
- [x] **F32** (OK): No action needed — ir_k formula derivation correct
- [ ] **F33** (Low): Open — clamping after RLS, minor P/theta inconsistency
- [x] **F34** (OK): No action needed — λ=0.97 effective memory is appropriate
- [x] **F35** (OK): No action needed — P floor not needed
