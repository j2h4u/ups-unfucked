# Research Summary: UPS Battery Monitor v3.0 Active Battery Care

**Project:** UPS Battery Monitor (CyberPower UT850EG)

**Milestone:** v3.0 — Active Battery Care (Sulfation Modeling & Smart Testing)

**Researched:** 2026-03-17

**Mode:** Domain Pitfalls Research

**Overall Confidence:** MEDIUM-HIGH (pitfalls sound, some upscmd details from WebSearch; requires hardware validation in Phase 1)

---

## Executive Summary

Transitioning from v2.0's **passive monitoring** (observe battery health, alert on degradation) to v3.0's **active management** (daemon initiates deep discharge tests, models sulfation recovery) introduces **7 safety-critical pitfalls** absent from v2.0.

### Key Risk Categories

1. **Safety (Blackout collision):** Daemon-scheduled test during grid blackout → runtime halved → unclean shutdown
2. **Reliability (upscmd):** Test commands fail silently; daemon can't verify execution state
3. **Estimation (Temperature):** Sulfation model ±30% error without temperature measurement
4. **Signal/Noise (Recovery):** Single-test recovery indistinguishable from ±1% measurement error
5. **Integration (Timers):** Race condition if systemd timers not disabled during migration
6. **Risk (Degradation):** Deep testing on SoH < 65% accelerates failure instead of helping
7. **Uncertainty (ROI):** Cycle cost-benefit metric confidence interval spans zero

### Bottom Line

**v3.0 is feasible but requires safety gating at multiple decision points.** No architectural blockers. All 11 pitfalls have documented prevention strategies. Field validation window required for empirical calibration (6 months of data collection).

---

## Key Findings

### Stack: No New Dependencies

- v2.0 stack unchanged: Python 3.13, systemd, NUT
- New: `src/sulfation_model.py` (physics-based with empirical calibration)
- New: `src/test_scheduler.py` (heuristic scheduling with safety gates)
- **Optional future:** DS18B20 thermistor (NTC temperature sensor, $2; reduces model error ±30% → ±5%)

### Features: Smart Scheduling Replaces Calendar-Based

**Required (v3.0 table stakes):**
- Daemon-initiated deep discharge via upscmd (replaces systemd timers)
- Sulfation score (days since discharge + SoH trend + IR curve)
- Test scheduling: condition-based (not calendar-based monthly fixed)
- Cycle ROI metric (sulfation benefit vs wear, exported to health.json with confidence)
- Safety gates: SoC floor (>80%), blackout collision detection, cycle wear empiricism

**Differentiators (v3.1):**
- Temperature compensation (if sensor available)
- Predictive scheduling (grid stability pattern)
- IR-based sulfation detection (independent of capacity model)

**Anti-Features:**
- Quick test scheduling (marginal ROI, risk not justified)
- Aggressive reversal at low SoH (battery may not recover)
- Multi-UPS (CyberPower UT850 only)

### Architecture: Daemon Owns Test Lifecycle

Test scheduling migrates from systemd timers to daemon-controlled state machine:

```
Safety gates (checked every poll):
  ✓ SoC > 80%? (avoid collision risk)
  ✓ Blackout > 2h ago? (recent discharge provides natural desulfation)
  ✓ SoH > 65%? (below = active material loss, testing harmful)
  ✓ No test running? (prevent race condition)

Scheduling decision (every 30 min):
  days_since_discharge = now - last_deep_discharge
  sulfation_score = f(days, soh_trend, ir_trend, temperature_assumption)
  if sulfation_score > threshold AND all gates pass:
    → initiate upscmd test.battery.start.deep

Test execution & validation:
  - Poll every 5s: check battery.charge, test.result changed
  - Timeout: abort if > 15 min with no change
  - On OB during test: attempt stop (if supported)
  - Post-test: wait for SoC stabilization
  - Measure ΔSoH, pool with 2+ recent tests before crediting
```

### Pitfalls: 11 Identified, 6 Critical

| # | Pitfall | Category | Prevention |
|---|---------|----------|-----------|
| 1 | Blackout collision (daemon test + real outage) | Safety | SoC >80%, blackout >2h, abort on OB |
| 2 | upscmd silent failures (test doesn't run) | Reliability | State machine, polling, timeout, verification |
| 3 | Temperature model ±30% error | Estimation | Configure constant, document, use conservative defaults |
| 4 | Recovery noise (can't detect +0.3% signal) | Signal/Noise | 3-test pooling, noise floor gating |
| 5 | Timer race condition (double-testing) | Integration | Disable systemd, daemon-only scheduling |
| 6 | Deep test on SoH <65% accelerates failure | Risk | SoH floor, IR trend check, empirical validation |
| 7 | ROI metric instability (confidence spans zero) | Uncertainty | Report confidence intervals, require lower_CI > 0% |
| 8 | Cycle wear over-estimate (literature ±5–50x) | Estimation | Measure empirically after 6 months |
| 9 | Temperature estimation cascades (heuristic T) | Estimation | User config override, conservative schedule |
| 10 | Test abort incomplete (firmware doesn't stop) | Reliability | Check support, log, monitor SoC during test |
| 11 | Capacity not converged at startup | Timing | Gate scheduling on convergence flag |

**CRITICAL (must prevent):** 1, 2, 5, 6

**MODERATE (mitigate):** 3, 7, 8, 9

**MINOR (observe):** 10, 11

---

## Implications for Roadmap

### Suggested Phase Structure

**Phase 1: Safety Foundation (Weeks 1–2)**
- Daemon-only test scheduling (disable systemd timers)
- Pre-test safety gates: SoC > 80%, blackout > 2h, SoH > 65%
- upscmd state machine with polling & timeout
- Test collision detection (OB during test)
- **Outcome:** Can schedule/execute one deep test safely
- **Avoids:** Pitfalls 1, 2, 5, 6
- **Validation:** Simulate blackout during test; verify abort

**Phase 2: Estimation & Empiricism (Weeks 3–4)**
- Sulfation score (physics + temperature uncertainty docs)
- Recovery measurement (3-test pooling, noise floor)
- Placeholder cycle wear (conservative 0.3% until 6-month empirical data)
- Empirical calibration (runs monthly, updates model.json)
- **Outcome:** 10 tests complete; recovery signal emerging
- **Avoids:** Pitfalls 3, 4, 7, 8
- **Validation:** Synthetic discharge data with noise; verify recovery extraction

**Phase 3: Scheduling Intelligence (Weeks 5–6)**
- Heuristic scheduling (days_since_discharge + sulfation_score + SoH)
- IR trend monitoring (distinguish sulfation from material loss)
- Conservative defaults (test every 4 weeks until 6-month empirical data)
- ROI export (health.json with confidence intervals)
- **Outcome:** Scheduling adapts to battery condition
- **Avoids:** Pitfalls 6, 7, 9
- **Validation:** Year-long simulation with varying SoH

**Phase 4: Empirical Calibration (Weeks 7–8)**
- Cycle wear from SoH regression (measured, not assumed)
- Feedback loop: if tests correlate with faster degradation → disable
- All assumptions transparent in health.json
- **Outcome:** System self-corrects; user confidence via transparency
- **Testing:** Real field data validation

### Phase Ordering Rationale

1. **Safety first:** Basic test execution must be safe before introducing decisions
2. **Measurement foundation:** Establish recovery signal-to-noise before scheduling
3. **Intelligence:** Teach daemon when to test based on solid measurement
4. **Refinement:** Empirical data refutes/confirms theory; self-correct over 6 months

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| **Blackout collision risk** | HIGH | Operating context (several/week blackouts) + physics validates |
| **upscmd reliability** | MEDIUM | NUT GitHub confirms issues; requires UT850 hardware validation Phase 1 |
| **Sulfation modeling** | MEDIUM | Physics sound (Shepherd/Bode); parameters vary 5–10x by manufacturer |
| **Temperature effects** | HIGH | Battery chemistry (exponential dependence); well-established |
| **Empirical cycle wear** | MEDIUM | 6-month validation window required; literature unreliable |
| **Recovery signal/noise** | HIGH | Statistical theory sound; noise floor ±1% well-founded |
| **ROI uncertainty** | HIGH | Uncertainty propagation (small difference of large uncertainties) inevitable |
| **Timer race condition** | HIGH | Standard Unix pattern; prevention straightforward |

---

## Research Flags for Implementation

| Phase | Topic | Flag Type | Reason |
|-------|-------|-----------|--------|
| Phase 1 | upscmd support on UT850 | **VALIDATE** | Test `test.battery.start.deep`, `.stop`, state transitions on real hardware |
| Phase 1 | onlinedischarge_calibration behavior | **VALIDATE** | Verify NUT driver correctly interprets test vs natural discharge |
| Phase 2 | Discharge repeatability | **MEASURE** | Collect 10–15 discharges; measure ±% SoH coefficient of variation |
| Phase 2 | Temperature baseline | **COLLECT** | User measures UPS case temp at setup; configure as constant |
| Phase 3 | Sulfation model tuning | **RESEARCH** | If empirical data diverges from theory, source manufacturer Shepherd/Bode parameters |
| Phase 4 | Cycle wear empiricism | **VALIDATE** | After 6 months: does SoH degradation match model? By how much? |
| Phase 4 | Recovery trend significance | **VALIDATE** | Do recovery signals correlate with SoH improvement? Immediate or lagged? |

---

## Standards & Best Practices

- **VRLA Safety:** Don't test below SoH 65% (IEEE-450-2010; active material loss dominates)
- **Statistical Rigor:** Pool 3+ samples before crediting small signals (IEEE testing standards)
- **Temperature Awareness:** Accept ±30% model error without sensor; document explicitly
- **Conservative Defaults:** When uncertain, don't test; wait for more data or lower risk
- **Empirical Over Theory:** Measure cycle wear from real data, not literature; recalibrate at 6 months
- **Confidence Reporting:** Always report ranges not points; require lower CI > 0% before acting

---

## Next Actions

### For Roadmap Phase

- [ ] Confirm phase ordering with stakeholders
- [ ] Identify Phase 1 upscmd validation requirements
- [ ] Estimate effort per phase (current: 2 weeks each = 8 weeks total)
- [ ] Plan field validation window (data collection in parallel with dev)

### For Phase 1 Design

- [ ] upscmd state machine specification
- [ ] Safety gate thresholds (SoC >80%, blackout >120 min, SoH >65%)
- [ ] Test scheduling JSON schema
- [ ] Test execution error handling (timeout, collision, failure modes)

### For Testing

- [ ] Synthetic scenarios (collision, timeout, abort, silent failure)
- [ ] Replay 2026-03-12 blackout through scheduler
- [ ] Validation matrix: each pitfall → test case + detection signal

---

**Status:** Ready for roadmap. No architectural blockers. All pitfalls have documented prevention strategies.

**Detailed Pitfalls Analysis:** See `PITFALLS-V3.md` (11 pitfalls, 6 critical, full prevention strategies)

**Last Updated:** 2026-03-17
