# Module 5: Capacity Estimation (`src/capacity_estimator.py`)

**Date:** 2026-03-17
**Panel:** Researcher (coulomb counting), System Architect, QA Engineer

| # | Finding | Severity | Action |
|---|---------|----------|--------|
| F24 | **ΔSoC ≥ 25% gate too strict** — typical 1-2 min blackouts produce ΔSoC ~7-15%, always rejected. Only monthly deep tests pass. Convergence (≥3) takes ≥3 months. | **High** | Lower to 15%. CoV<10% handles noise. |
| F25 | `_compute_ir()` computes discharge-slope (ΔV_total/I_avg = 352mΩ), not internal resistance (~20mΩ). 17x wrong. Metadata only, not used in calculations. | Medium | Rename or remove — misleading metadata |
| F26 | Hardcoded `nominal_ah=7.2` and `voltage_drop=3.5` in cross-check — should come from model | Low | Use model params. Cross-check is loose enough to work anyway. |
| F27 | Nominal voltage in coulomb counting: Ah overestimated ~4%. Systematic bias, doesn't affect convergence. | Low | Same as F14. Converged capacity 4% optimistic — acceptable. |
| F28 | `capacity_estimates` is empty — zero measurements passed quality filter in 4 days | Info | Confirms F24. Monthly deep test is only current path to convergence. |
| F29 | Convergence CoV<10% with population std — sound | OK | No fix needed |

**Design notes:**
- Coulomb counting core is correct (IEEE-1106 trapezoidal integration)
- 4% nominal voltage bias is consistent (same direction as F14) and absorbed by convergence
- `_compute_ir()` result is purely informational metadata — real IR comes from `_record_voltage_sag()` in monitor.py
- 5-min blackout at 16% load: 13.5→12.5V = ΔSoC 31% (passes 25% gate but barely). 3-min blackout: ΔSoC ~15% (fails). Lowering to 15% doubles the number of contributing events.
