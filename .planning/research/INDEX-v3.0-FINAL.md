# v3.0 Active Battery Care — Research Complete

**Phase 6 Research Milestone**
**Researched:** 2026-03-17
**Status:** ✓ Complete — Ready for Phase 7 (Detailed Design)

## Research Output Index

All files created in `.planning/research/`:

### Core Research Documents (Just Created — Phase 6)

| File | Purpose | Focus | Status |
|------|---------|-------|--------|
| **FEATURES-v3.0.md** | Feature landscape for v3.0 milestone | Table stakes, differentiators, anti-features, MVP definition, complexity | ✓ Complete |
| **STACK-v3.0-RESEARCH.md** | Technology stack analysis | No new deps (apscheduler only), config schema, installation | ✓ Complete |
| **ARCHITECTURE-v3.0.md** | System design patterns | Async daemon, sulfation model, scheduling algorithm, data flow | ✓ Complete |
| **PITFALLS-v3.0.md** | Risk analysis & mitigation | 5 critical pitfalls, 3 moderate, 3 minor; detection & prevention | ✓ Complete |
| **SUMMARY-v3.0.md** | Executive summary | Roadmap implications, phase structure, risk assessment, deliverables | ✓ Complete |

### Previous Versions (v2.0 Research — Reference)

| File | Purpose | Notes |
|------|---------|-------|
| SUMMARY.md | v2.0 research summary | Capacity estimation focus |
| STACK.md | v2.0 technology stack | Python 3.13, NUT, systemd baseline |
| FEATURES.md | v2.0 feature landscape | Coulomb counting, SoH recalibration |
| ARCHITECTURE.md | v2.0 architecture | Polling + discharge detection |
| PITFALLS.md | v2.0 pitfalls | Capacity model risks, LUT accuracy |

## Key Research Findings

### Domain Ecosystem (Sulfation Management)

**Enterprise Standard (Eaton ABM, Schneider, Vertiv):**
- Internal resistance (IR) is primary sulfation indicator
- IR trending (20-30% rise = action needed) + recovery delta (voltage recovery post-discharge) + curve shape analysis provide confidence
- Temperature compensation critical (Arrhenius: rate doubles per 10°C)
- Scheduling: IF sulfation_score > threshold AND soh > floor AND days_since_test > min_interval THEN test()
- Blackout credit: full discharge (100% depth) counts as maintenance; defer scheduled test for N days

**This Deployment:**
- CyberPower UT850EG, USB via NUT, 2-5 blackouts/week, battery ~35°C
- No SNMP API (unlike APC/Eaton enterprise); only upscmd available for test triggering
- v2.0 already collects discharge data (discharge_buffer); v3.0 adds scheduling logic

### Technology Stack (No Breaking Changes)

**v3.0 Additions:**
- `apscheduler` (lightweight job scheduler, 1 external dependency)
- Extended config schema (v3_sulfation, v3_roi sections)
- Extended health.json (sulfation_score, next_test_eta, cycle_roi)
- Structured journald events (@fields.reason, @fields.soh_delta)

**Backward Compatible:**
- model.json schema extended with defaults; v2.0 instances auto-migrate
- Old systemd timers (ups-test-deep.timer) disabled but not removed
- MOTD format unchanged; new fields optional

### Features (MVP = 7, v3.1 = 5, Deferred = 3)

**v3.0 Launch (Required):**
1. Sulfation detection (IR trend + recovery delta)
2. Safe discharge constraints (SoH>50%, ≤1 test/week)
3. Natural blackout credit (defer after 90%+ depth blackout)
4. daemon upscmd integration (direct NUT test triggering)
5. Cycle ROI metric (benefit/cost analysis)
6. Journald structured events (observability)
7. Temperature fallback constant (35°C for this deployment)

**v3.1 Enhancements (After 6-month data):**
- Temperature compensation from NUT HID (if available)
- Peukert exponent calibration (once capacity converged)
- Shallow test as leading indicator (forecast desulfation need)
- Cliff-edge degradation detector (SoH sudden drop alerts)
- Multi-battery capacity normalization (post-replacement baseline)

### Architecture Patterns

**Hybrid Sulfation Model (Physics + Data-Driven):**
- Shepherd discharge curve (theoretical) vs. actual measurement (observed)
- IR trend (rising = sulfation) + recovery delta (slow recovery = sulfation)
- Composite score (0-100) with confidence tracking

**Intelligent Scheduling Decision Tree:**
```
IF sulfation_score ≥ 65
  AND soh ≥ 50%
  AND days_since_test ≥ 7
  AND test_count_this_week < 1
  AND NOT has_blackout_credit()
THEN schedule_test()
ELSE defer
```

**Cycle ROI = Benefit / Cost:**
- Benefit: SoH improvement post-test (%)
- Cost: estimated capacity loss from discharge (%)
- ROI > 5x = strong case for test
- ROI 1-5x = marginal (score tie-breaker)
- ROI < 1x = skip (wear exceeds benefit)

### Risk Assessment

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|-----------|
| IR baseline drift (measurement noise) | MEDIUM | HIGH (schedule unreliability) | Use 3+ test median; normalize for temp/load |
| Capacity loss model unvalidated | HIGH | MEDIUM (ROI miscalibrated) | Mark preliminary; collect 6+ months data |
| Blackout depth estimation error | MEDIUM | MEDIUM (credit misapplied) | Conservative 95% threshold; validate vs shutdown |
| upscmd silent failure | MEDIUM | HIGH (dangerous; tests don't run) | Check return code; alert on consecutive failures |
| Temperature breaks at extremes | LOW (35°C constant) | MEDIUM (multi-site only) | Document assumption; warn if > 45°C |

**Overall: MEDIUM risk, mitigated by conservative thresholds + extensive logging**

## Roadmap Implications

### Phase Sequencing

```
v3.0 (3 weeks, immediate)
├─ Week 1: Sulfation model + safe constraints + blackout credit
├─ Week 2: upscmd integration + journald events + health.json
├─ Week 3: Testing + documentation
└─ Release: v3.0.0-alpha (2026-04-07)

Parallel Rollout (2 weeks, validation)
├─ Both v2.0 timers + v3.0 daemon active
├─ Monitor: test frequency, SoH trends, journald events
└─ Decision: full migration (2026-04-21)

v3.1 (6 weeks post-release, tuning)
├─ Data analysis: actual IR rise rate, capacity loss per cycle
├─ Temperature compensation (NUT HID if available)
├─ Threshold refinement based on real measurements
└─ Release: v3.1.0 (2026-10-17)

v4.0+ (future, scaling)
├─ Multi-UPS support
├─ Seasonal patterns (12+ months data)
├─ Grid stability integration
└─ Advanced wear optimization
```

### Why This Order

1. **Sulfation model first** — foundation for all decisions
2. **Safe constraints early** — prevent daemon from harming battery while learning
3. **Blackout credit high-priority** — domain specific (2-5 blackouts/week)
4. **upscmd replaces timers** — only after scheduling logic proven
5. **Observability throughout** — every decision must be logged
6. **ROI metric for v3.1** — enables data-driven tuning after 6-month collection

## Research Validation Checklist

**Before v3.0 Alpha:**
- [ ] Verify `upscmd UT850 test.battery.start` works on senbonzakura
- [ ] Check NUT 2.8.1+ supports upscmd test command
- [ ] Test journald query for recent OB→OL events (blackout detection)
- [ ] Validate subprocess error handling (mock failures)

**During v3.0 Parallel Rollout:**
- [ ] Compare test frequency (v2.0 timers vs v3.0 daemon)
- [ ] Monitor SoH trends (should be similar)
- [ ] Verify journald events are structured correctly
- [ ] Check health.json schema (Grafana parse OK)

**Before v3.1 (6-month data needed):**
- [ ] Measure actual IR rise rate (d(IR)/dt) for CyberPower UT850
- [ ] Compute capacity loss per cycle (SoH pre/post test)
- [ ] Correlate recovery delta with IR trend (statistical significance)
- [ ] Determine optimal sulfation threshold (tune via post-hoc analysis)
- [ ] Check NUT HID for `battery.temperature` availability

## Known Unknowns (Research Gaps)

| Question | Importance | Resolution |
|----------|-----------|-----------|
| What is CyberPower UT850 IR rise rate at 35°C? | HIGH | Collect quick-test data for 3 months; compute d(IR)/dt |
| How much capacity loss per discharge cycle (real)? | HIGH | Pre/post SoH over 10+ cycles; fit model |
| Does recovery delta correlate with sulfation score? | MEDIUM | Statistical analysis of recovery vs IR trend |
| What is safe SoH floor (currently 50% assumption)? | MEDIUM | Analyze discharge curves at SoH=50% vs 40% |
| Can NUT HID provide battery.temperature? | MEDIUM | Query: `upsc UT850@localhost \| grep temperature` |

## Handoff to Phase 7 (Detailed Design)

**What's Ready:**
- ✓ Feature set defined + prioritized (MVP, v3.1+, deferred)
- ✓ Architecture sketched (async daemon, scheduling tree, data flow)
- ✓ Risks catalogued with prevention strategies
- ✓ Roadmap structured (v3.0 core → v3.1 tuning → v4.0 scaling)
- ✓ Testing approach outlined (unit, integration, E2E simulation)

**What Needs Phase 7:**
- Function signatures + method contracts
- Detailed test plan + test cases
- Config schema (final + migration)
- Grafana dashboard extensions
- CLI/MOTD updates
- Systemd unit changes (v3.0 vs v2.0)
- Upgrade documentation (v2.0 → v3.0 migration path)

**Critical Path for v3.0 (3 weeks):**
1. Day 1-3: Core sulfation model + unit tests
2. Day 4-6: Safe constraints + blackout credit
3. Day 7-9: upscmd integration + error handling
4. Day 10-12: Journald structured events + ROI export
5. Day 13-15: E2E simulation (replay v2.0 data)
6. Day 16-18: Testing + bug fixes
7. Day 19-21: Documentation + release prep

---

**Research Complete — Ready to Schedule Phase 7 Kickoff**

*All analysis files committed to `.planning/research/` with cross-references*
*Risk level: MEDIUM (physics sound; CyberPower unknowns mitigated by conservative thresholds)*
*Confidence: MEDIUM (enterprise patterns validated; deployment-specific validation needed post-release)*
