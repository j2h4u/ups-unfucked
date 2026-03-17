# Module 3: Runtime Prediction (`src/runtime_calculator.py`)

**Date:** 2026-03-17
**Panel:** Researcher (electrochemistry/Peukert), System Architect, QA Engineer

| # | Finding | Severity | Action |
|---|---------|----------|--------|
| F13 | `load=0 → runtime=0` — sensor glitch causes false LB flag and unnecessary shutdown | **High** | **Fixed:** return 24h cap instead of 0.0 |
| F14 | I_actual uses nominal voltage (12V) not actual. At low SoC (10.5V), current underestimated 14%. Peukert calibration absorbs at stable loads, breaks at load changes | Medium | Document. Error <3% at stable 14-20% load |
| F15 | Linear SoC scaling below 20% SoC overestimates runtime. Partially compensated by LUT cliff nonlinearity | Medium | Depends on F9 (cliff data gap). Improves with measured data |
| F16 | Peukert at 15.7x C-rate: outside empirical range, but RLS calibrates at actual rate — works as curve-fit | Low | Document |
| F17 | SoH linear scaling: ~5% approximation error (energy-based SoH vs capacity) | Low | Document |
| F18 | Low load (1-2%) → 20h runtime: unrealistic but unreachable in practice | Info | No action |

**Design notes:**
- Peukert formula is correct for the operating point. RLS calibration compensates for systematic biases (nominal voltage, C-rate extrapolation) because calibration and prediction use the same formula
- The formula breaks down if server load changes significantly from calibration load. At stable 14-20%, error is <3%
- Linear SoC × SoH scaling is a good enough approximation because: (a) LUT encodes voltage→SoC nonlinearity already, (b) energy-based SoH tracks capacity within ~5% for VRLA
- Validated: 47 min actual vs 47.0 min predicted (2026-03-12 blackout, 17% load, n=1.15)

## Fix Status

- [x] **F13** (High): Fixed — return 24h cap instead of 0.0 for load=0 sensor glitch
- [x] **F14** (Medium): ✅ Documented in runtime_minutes() docstring — <3% error at stable load
- [x] **F15** (Medium): ✅ Documented in runtime_minutes() docstring — improves with cliff data (F9)
- [x] **F16** (Low): ✅ Documented in runtime_minutes() docstring — RLS calibrates at actual C-rate
- [x] **F17** (Low): ✅ Documented in runtime_minutes() docstring — ~5% VRLA approximation
- [x] **F18** (Info): No action needed — unreachable in practice
