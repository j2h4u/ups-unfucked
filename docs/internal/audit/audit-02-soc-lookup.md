# Module 2: SoC Lookup (`src/soc_predictor.py` + IR compensation)

**Date:** 2026-03-17
**Panel:** Researcher (electrochemistry), System Architect, QA Engineer
**Resolves from Module 1:** F3 (IR compensation during OB)

| # | Finding | Severity | Action |
|---|---------|----------|--------|
| F7 | LUT 80% duplicates (24/31 entries) — wastes 200-entry prune budget | Low | Dedup in `calibration_write` or prune logic |
| F8 | IR compensation during OB: LUT stores raw voltage, lookup uses IR-compensated — inconsistent reference frame, ≤0.06V / ≤5% SoC error at typical loads | Medium | Document as known limitation. Error is `k*(L_actual - L_calibration)` — negligible when load ≈ L_base=20% |
| F9 | Cliff region (10.5-11.0V) has no measured data — 0.5V/6% SoC, 0.1V ADC → 1.2% SoC per step. Most safety-critical, least accurate | Medium | Resolves after first deep discharge. Data gap, not code bug |
| F10 | ±0.01V tolerance: latent bug if same voltage has different SoC values in LUT | Low | Prevented by dedup (F7). No current risk |
| F11 | IR compensation has zero effect in OL at 14-20% load (flat SoC=1.0 region) | Info | By design — becomes relevant during discharge |
| F12 | bisect with duplicate voltages: correct — brackets span distinct voltage levels | OK | No fix needed |

**Design notes:**
- LUT is built from raw (uncompensated) voltage during discharge calibration
- IR compensation normalizes to L_base=20% reference load — correct for OL, approximate for OB
- The formula direction is correct: load < L_base → V_norm < V_ema (less IR drop at lower load means observed voltage is inflated relative to LUT reference)
- Cliff region accuracy will improve organically with each deep discharge — no code change needed
- F3 from Module 1 resolved: IR comp during OB is approximate but error bounded to ≤0.06V at typical loads

## Fix Status

- [ ] **F7** (Low): Open — LUT dedup
- [ ] **F8** (Medium): Open — known limitation, documented
- [ ] **F9** (Medium): Open — resolves after first deep discharge
- [ ] **F10** (Low): Open — prevented by dedup (F7)
- [x] **F11** (Info): No action needed — by design
- [x] **F12** (OK): No action needed
