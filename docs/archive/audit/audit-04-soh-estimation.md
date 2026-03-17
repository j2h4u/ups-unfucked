# Module 4: SoH Estimation (`src/soh_calculator.py` + `src/battery_math/soh.py`)

**Date:** 2026-03-17
**Panel:** Researcher (electrochemistry), System Architect, QA Engineer

| # | Finding | Severity | Action |
|---|---------|----------|--------|
| F19 | **Formula bug: degradation_ratio measures discharge fraction, not health.** `area_measured / area_reference` compares partial discharge to full Peukert runtime. Ratio is always << 1.0 for partial discharges regardless of battery health. One 7-min blackout: SoH 100%→55%. Root cause of 2026-03-16 incident (100%→80.6%). | **Critical** | Redesign formula: normalize by ΔSoC range or switch to capacity-based SoH |
| F20 | `discharge_weight = duration / (0.30 * T_expected)` — the 0.30 constant has no derivation. Gives weight=0.76 for a 10-min discharge, amplifying F19. | **High** | Part of formula redesign |
| F21 | "Bayesian blend" is not Bayesian — `measured_soh = reference × ratio` treats partial discharge fraction as total health estimate. Real Bayesian would compare same SoC range. | **High** | Part of formula redesign |
| F22 | min_duration_sec=30s (kernel) vs 300s (monitor) — kernel allows shorter for simulation, monitor has operational guard | Low | Document only |
| F23 | Anchor trimming correct for partial discharges (no readings below 10.5V = untrimmed, which is fine) | OK | No fix needed |

**Root cause analysis:**
The formula compares area of a partial discharge (e.g., 600s) to the area of a full discharge (e.g., 2632s). The ratio is inherently ~duration/T_expected, not a health metric. A brand new battery produces the same ratio as a degraded one for the same partial discharge — the formula cannot distinguish between "short blackout" and "degraded battery."

**Impact:** SoH feeds into `runtime_minutes(... * soh)`. Wrong SoH → wrong runtime → wrong shutdown timing. Currently mitigated by: (a) 300s guard rejects micro-discharges, (b) replacement_due gated by capacity convergence. But a 5+ minute blackout will still produce wrong SoH.

**Redesign options (for future milestone):**
1. **ΔSoC normalization:** Compare energy rate (V·s per second = avg voltage) for the observed SoC range vs expected avg voltage for that range from LUT. Ratio ≈ 1.0 for healthy battery regardless of duration.
2. **Capacity-based SoH:** `SoH = measured_capacity / rated_capacity`. CapacityEstimator (Module 5) already computes this. Deprecate voltage-area formula entirely.
3. **Hybrid:** Use voltage-area for quick health checks (short discharges), capacity-based for authoritative SoH (deep discharges).

## Fix Status

- [x] **F19** (Critical): Fixed — capacity-based SoH replaces area-under-curve (deprecated voltage-area formula)
- [x] **F20** (High): Fixed — new SoH algorithm doesn't use arbitrary 0.30 weight
- [x] **F21** (High): Fixed — real Bayesian blend weighted by ΔSoC depth
- [x] **F22** (Low): ✅ Documented in both soh.py (kernel param) and soh_calculator.py (operational guard)
- [x] **F23** (OK): No action needed — anchor trimming correct
