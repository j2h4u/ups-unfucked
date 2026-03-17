## Module 10: Replacement Prediction (`src/replacement_predictor.py`)

**Date:** 2026-03-17
**Panel:** System Architect, QA Engineer, Kaizen Master

| # | Finding | Severity | Action |
|---|---------|----------|--------|
| F53 | **Replacement prediction depends on broken SoH (F19).** Capacity convergence gate is necessary but not sufficient — wrong SoH entries will poison regression. | **High** | Fix upstream (F19). No change in predictor code. |
| F54 | capacity_ah_ref filter implemented but not wired in caller — dead code, all entries used unfiltered | Medium | Wire in monitor.py (one-line fix) |
| F55 | No outlier rejection — one bad SoH entry poisons regression slope | Medium | Fix after F19 (fix source, not symptom) |
| F56 | R²<0.5 threshold permissive for linear degradation model | Low | Revisit when predictor is active |
| F57 | Multiple entries per day overweight multi-discharge days (~10%) | Low | Negligible for months-ahead extrapolation |
| F58 | **monitor.py 1300+ lines** — monolith accreting complexity. Each feature adds 10-20 lines. | Medium | Decompose into focused modules at next milestone |

**Design notes:**
- Predictor is effectively dormant: gated behind capacity convergence (not yet achieved) AND depends on broken SoH formula (F19)
- Predictor code itself is clean — the fix is upstream (F19 SoH redesign)
- capacity_ah_ref filtering exists but needs wiring in the caller to separate pre/post battery replacement entries
- Once F19 and F24 are fixed, predictor should work with clean data — reassess then
