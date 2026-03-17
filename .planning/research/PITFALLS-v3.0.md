# Domain Pitfalls: v3.0 Active Battery Care

**Domain:** VRLA UPS battery management with intelligent desulfation scheduling
**Researched:** 2026-03-17
**Confidence:** MEDIUM-HIGH (research covers enterprise practices + physics literature; some CyberPower-specific unknowns)

## Critical Pitfalls (Cause Rewrites or Major Issues)

### Pitfall 1: IR Baseline Drift Without Anchor

**What goes wrong:**
- Internal resistance (IR) is measured from voltage sag during quick test: ΔV/I = R
- If baseline IR is set at day 1 from a single test, subsequent tests drift
- Example: Day 1 IR = 85mΩ (maybe measured with 10% load), Day 30 IR = 82mΩ (test with 9% load due to server load variation)
- Daemon incorrectly concludes "IR dropped" → sulfation reversed (false confidence)
- Months later, real sulfation is missed until SoH cliff detected (dangerous)

**Why it happens:**
- Load current varies with server CPU load; quick test doesn't hold constant load
- Temperature drifts (35°C ±2°C); R_internal varies ~0.003V/°C per cell
- NUT estimates load from power measurement (noisy); load current accuracy ±10%

**Consequences:**
- Scheduling becomes unreliable (false negatives: skip test when sulfation rising)
- Battery replacement prediction delays by months
- Server shutdown at critical moment (SoH cliff)

**Prevention:**
1. **Establish baseline from 3+ early tests** (first month), take median. Don't use single test.
2. **Temperature-normalize IR**: If temp changes >5°C, adjust threshold (ΔR = -0.003mV/°C × cells × ΔT)
3. **Load normalization**: Record load current during quick test; normalize all IR to standard 10% load equivalent
4. **Anchor to absolute thresholds**: Don't just track IR_trend; also monitor absolute IR_mv > 120mΩ (VRLA aging sign)

**Detection:**
- IR trend reverses (rising → stable → falling) without test action → flag for review
- Recovery delta improves while IR supposedly falls → physical contradiction

### Pitfall 2: Cycle ROI Model Unvalidated

**What goes wrong:**
- Daemon estimates capacity loss per discharge as: `loss = 0.15% * depth_percent`
- This comes from literature (VRLA aging studies), NOT from actual CyberPower UT850 measurements
- Real CyberPower battery might lose 0.05% or 0.25% per cycle; estimate is ±50% wrong
- ROI calculation becomes garbage: `ROI = 1.5% recovery / 0.15% loss = 10x` but real is `1.5% / 0.25% = 6x`
- Scheduling thresholds tuned to wrong ROI → tests triggered too often or not enough

**Why it happens:**
- v2.0 doesn't have 6+ months of discharge data yet (deployed ~2 weeks)
- Literature values are averages across many batteries; individual units vary ±50%
- CyberPower UT850 manual doesn't publish cycle aging rate

**Consequences:**
- Scheduling logic appears wrong but is actually just poorly calibrated
- Early v3.0 release will show "we don't test enough" or "we test too much"
- Requires post-release tuning; delayed refinement feedback

**Prevention:**
1. **Collect SoH pre/post test data for 6+ months** before finalizing ROI thresholds
2. **Flag ROI as "preliminary"** in health.json until sample size ≥ 10
3. **Use conservative defaults**: ROI_threshold_accept = 5x (require strong evidence before scheduling test)
4. **Measure capacity loss directly**: Compare model.soh_history to measured_capacity_trend; fit polynomial

**Detection:**
- ROI ratios look extreme (100x or 0.1x) → data too sparse
- Test schedule changes dramatically over weeks → parameters not converged

### Pitfall 3: Scheduling Without Blackout Context

**What goes wrong:**
- Daemon schedules test weekly if sulfation_score > 65
- User's grid has frequent blackouts (2-5/week at 90% depth)
- Each blackout is superior desulfation (full 100% discharge) vs. controlled test (50% discharge)
- Daemon triggers test twice a week → battery cycles excessively
- Daemon doesn't know it's causing harm because blackout credit logic is buggy

**Why it happens:**
- Blackout detection relies on journald queries; if timestamps are wrong or events dropped, credit fails
- Developer doesn't deploy in high-outage environment; scheduled test seems reasonable
- No logging of credit decisions → invisible failure

**Consequences:**
- Battery ages 2x faster due to excessive cycling
- SoH drops faster than expected; replacement prediction becomes pessimistic
- User sees "replace battery in 8 months" but had expected 18 months

**Prevention:**
1. **Log every blackout-credit decision explicitly** (journald: "blackout_credit applied, defer test 7 days")
2. **Validate blackout detection**: Check journald query on startup; if no recent OB events found, log warning
3. **Track test frequency**: Warn if test_count_this_week > 1 or if days_between_tests < 7
4. **Conservative blackout threshold**: Require depth ≥ 90% (exclude partial discharges from credit)

**Detection:**
- Test frequency increases unexpectedly (weekly instead of monthly)
- SoH degradation slope changes (becomes steeper post-v3.0 deployment)
- Journald shows no "blackout_credit" messages despite frequent blackouts

### Pitfall 4: Temperature Model Breaks at Extremes

**What goes wrong:**
- v3.0 defaults to constant 35°C (observed in deployment with inverter heating)
- Fallback to NUT HID temp if available; user's UPS becomes self-heated (server room AC fails)
- Temperature rises to 50°C; daemon adjusts sulfation rate upward (accelerated aging expected)
- But daemon continues normal scheduling (test every 7 days at worst)
- Real sulfation rate doubles per 10°C; battery is degrading 2x faster than scheduled tests can handle

**Why it happens:**
- Temperature compensation is linear approximation (−0.003V/°C); real Arrhenius is exponential
- Scheduling thresholds tuned for 35°C baseline; at 50°C they're no longer valid
- Multi-site deployment: some datacenters are 25°C, others 45°C; single threshold fails

**Consequences:**
- Battery fails faster at high temperature
- Scheduling appears correct but is actually insufficient (missed early sulfation warning)
- Disaster strikes in hot environment first (summer, outdoor UPS, blocked AC)

**Prevention:**
1. **Implement Arrhenius adjustment properly**: `rate_adjusted = rate_baseline * exp(E_a / k * (1/T_ref - 1/T_actual))`
2. **Set hard limits on temperature**: Log warning if temp > 45°C; require manual review
3. **Document temperature assumptions**: "v3.0 tuned for 35°C ±5°C; not validated at other temps"
4. **For multi-site: parametrize thresholds by temperature**

**Detection:**
- Temperature varies >10°C but scheduling unchanged → misalignment
- Multiple deployments at different temps show different SoH decay rates → confounding variable

### Pitfall 5: upscmd Error Handling Silent Failure

**What goes wrong:**
- Daemon calls `upscmd UT850 test.battery.start` via subprocess.run()
- NUT daemon crashes or UPS unplugs → upscmd fails with exit code 1
- Daemon doesn't check return code; thinks test started
- Logs "test_scheduled" to journald, but no test actually ran
- Next day, daemon thinks test was done 1 day ago; deferring next test by 7 days
- 7 days later, still no test happened (UPS still disconnected)
- Sulfation accumulates silently while daemon thinks it's under control

**Why it happens:**
- upscmd is external subprocess; error handling requires explicit return code checking
- Developer might use `subprocess.run()` without capturing returncode
- Testing uses mock upscmd (always succeeds); real failure never seen in dev

**Consequences:**
- Test schedule becomes meaningless (tests don't actually run)
- Sulfation detection stops working
- Battery degradation undetected for weeks
- Dangerous operation: server thinks it's safe but battery is failing

**Prevention:**
1. **Always check upscmd return code**: `result = subprocess.run(..., capture_output=True)`; raise exception if returncode != 0
2. **Log upscmd output**: Capture stderr; log full error message to journald for debugging
3. **Alert on test failure**: If upscmd fails twice in a row, write alert to MOTD and journald
4. **Mock both success and failure cases** in unit tests

**Detection:**
- Journald shows "test_scheduled" but no subsequent "test_complete" event
- Model.json shows last_test_timestamp unchanged for >7 days despite scheduled attempts

## Moderate Pitfalls (Need Mitigation)

### Pitfall 1: Recovery Delta Noise (Partial Discharges)

**What goes wrong:**
- Recovery delta = V_pre_test - V_post_test_at_30s
- Short blackout (5 min, 20% depth) followed by quick test: both measurements noisy
- Recovery delta might be 100mV (looks healthy) or 50mV (looks sulfated) depending on measurement timing
- Daemon can't distinguish real sulfation from measurement noise

**Why it happens:**
- Recovery process is exponential; 30s is arbitrary window
- Load varies; voltage measurement includes noise
- No voltage averaging/filtering in quick test

**Prevention:**
1. **Only update recovery_delta after deep discharges** (ΔSoC > 50%); ignore shallow ones
2. **Average recovery delta over 3+ tests** before assigning confidence
3. **Log both raw and smoothed values** for debugging
4. **Use threshold bands, not point values**: "recovering_slowly" if delta in range [30-80], not binary

**Detection:**
- Recovery delta swings by ±50mV between consecutive tests
- Sulfation score changes by >10 points day-to-day without test events

### Pitfall 2: Blackout Depth Estimation Unreliable

**What goes wrong:**
- Daemon estimates blackout depth from voltage LUT: V_min → SoC_min via lookup
- LUT is calibrated from few discharge curves; deep voltages (tail region) have ±10% error
- 90% depth blackout measured as 85% → doesn't qualify for credit
- Or measured as 95% → incorrectly triggers credit

**Why it happens:**
- Voltage-to-SoC conversion is reverse-LUT lookup; cliff region is steep (small V change = big SoC change)
- Load profile during blackout varies; estimated load current used in SoC calculation
- No coulomb counting during blackout (load measurement is firmware estimate, not real)

**Prevention:**
1. **Conservative credit threshold**: Require measured depth ≥ 95%, not 90%
2. **Validate blackout depth against shutdown event**: If server shutdown detected, assume full discharge (100%)
3. **Log depth measurement uncertainty**: "blackout depth estimated 92% ±5%"

**Detection:**
- Blackout credit applied for partial discharges (60-80% depth)
- Scheduled tests overlap with natural blackouts (should be deferred instead)

### Pitfall 3: Sulfation Score Overfitting to Single Indicator

**What goes wrong:**
- Daemon computes sulfation_score from IR trend (40%) + recovery delta (35%) + curve deviation (25%)
- IR trend goes up (due to measurement noise, not real sulfation) → score jumps 20 points
- Daemon schedules test unnecessarily
- Or all three indicators plateau → score stays flat despite real aging
- Scheduling becomes either over-reactive or under-responsive

**Why it happens:**
- Weighting is arbitrary (40% + 35% + 25% = 100%, but why these fractions?)
- No empirical correlation study (how much does each metric matter for real sulfation?)
- Single anomalous test swings the score

**Prevention:**
1. **Require 2+ indicators to agree** before high confidence: "only schedule if IR_trend > threshold AND recovery_delta < threshold"
2. **Bootstrap confidence intervals**: Track which indicators are statistically significant (CoV < 10%)
3. **Log all raw signals + composite score** so post-analysis can re-weight

**Detection:**
- Sulfation score correlates poorly with SoH degradation rate
- Scheduled tests don't improve SoH (ROI near 1x consistently)

## Minor Pitfalls (Nice to Handle)

### Pitfall 1: Config Drift in Production

**What goes wrong:**
- Administrator tunes `sulfation_threshold` from 65 to 70 in config file
- Daemon doesn't validate; silently uses new threshold
- Scheduling behavior changes without logging the reason
- Post-mortem: "why did we test more often last month?" — no audit trail

**Prevention:**
- Log all config values at daemon startup
- Log config changes if file is reloaded (systemctl restart)
- Warn if config differs from last known-good version

### Pitfall 2: Health.json Export Timing

**What goes wrong:**
- health.json updated after test completes (SoH measurement done)
- But Grafana poll rate is faster (every 30s)
- Grafana shows stale data for 1-2 minutes after test

**Prevention:**
- Update health.json immediately with "in_progress" flag
- Follow up with final values once SoH measurement complete

### Pitfall 3: Journald Structured Field Typos

**What goes wrong:**
- Developer writes `@fields.soh_delta` in one place, `@field.soh_delta` in another
- Grafana Alloy parsing fails on inconsistent field names
- Metrics stop flowing silently

**Prevention:**
- Define structured field schema in code (constants, not magic strings)
- Unit test journald_write() to verify field names

## Phase-Specific Warnings

| Phase | Topic | Likely Pitfall | Mitigation |
|-------|-------|-----------------|------------|
| **v3.0 Alpha** | IR baseline | Single-test baseline drifts | Require 3+ early tests, document |
| **v3.0 Alpha** | ROI model | Capacity loss estimate unvalidated | Mark as "preliminary"; conservative thresholds |
| **v3.0 Beta** | Blackout credit | Depth estimation error | Require depth ≥ 95%; validate via shutdown signal |
| **v3.0 Release** | upscmd integration | Silent test failure | Check return code; alert on consecutive failures |
| **v3.1** | Temperature model | Linear approximation breaks at extremes | Implement Arrhenius; warn if temp > 45°C |
| **v3.1+** | Multi-site scaling | Single thresholds invalid across sites | Parametrize by temperature; separate per-UPS config |

## Research Gaps (Critical for Phase Execution)

| Gap | Importance | How to Close |
|-----|-----------|-------------|
| CyberPower UT850 actual IR rise rate at 35°C | HIGH | Deploy v3.0; collect data for 3 months; measure d(IR)/dt |
| Capacity loss per discharge cycle (real, not literature) | HIGH | Post-test SoH tracking over 10+ cycles; fit model |
| Recovery delta sensitivity to measurement noise | MEDIUM | A/B test: measure at T=30s vs T=60s vs T=120s; compare stability |
| NUT HID `battery.temperature` availability | MEDIUM | Check: `upsc UT850@localhost \| grep temperature` |
| Optimal blackout credit threshold | MEDIUM | Monitor: if credit applied for 80% discharge, does it reduce wear appropriately? |

## Known Constraints & Assumptions (v3.0)

| Constraint | Impact | Mitigation |
|-----------|--------|-----------|
| Temperature constant at 35°C | Thresholds invalid if ambient drifts >5°C | Document assumption; warn if temp > 45°C |
| No coulomb counting during blackout | Depth estimation ±10% error | Use conservative 95% threshold for credit |
| Peukert exponent fixed at 1.2 | SoH calculation ±3% error | Accept error margin; refine in v3.1 after capacity converged |
| Quick test load ~10% (firmware-defined) | IR measurement depends on load | Normalize to standard load; log actual load during test |
| Systemd journal retention 1 week | Blackout history lost after 1 week | Archive important events to model.json; log backups |
| Single-threaded daemon | No concurrent test + polling | Acceptable for single UPS; separate threads for v4.0+ multi-UPS |

## Success Criteria for v3.0 Release

**Functional:**
- [ ] Sulfation score (0-100) computed from 3+ independent indicators
- [ ] Scheduling decision logged to journald with reason code (not opaque)
- [ ] upscmd test.battery.start executed successfully; return code checked
- [ ] health.json exports sulfation_score, next_test_eta, roi_percent without errors
- [ ] Blackout credit logic verified: no test scheduled within 7d of 90%+ depth blackout

**Safety:**
- [ ] SoH floor (50%) enforced; no test scheduled if SoH ≤ 50%
- [ ] Rate limit enforced; no more than 1 test per week
- [ ] Config validation: missing v3_sulfation section uses sensible defaults

**Observability:**
- [ ] Every scheduling decision (trigger or defer) logged with @fields
- [ ] upscmd failures logged with stderr output
- [ ] Temperature anomalies (>45°C) trigger MOTD alert
- [ ] ROI calculations visible in health.json and journald

**Documentation:**
- [ ] CONTEXT.md updated: sulfation model + scheduling algorithm
- [ ] Threshold tuning guide: how to adjust sulfation_threshold post-deployment
- [ ] Failure scenario recovery: "what if upscmd fails 3x in a row?"
- [ ] PITFALLS.md: known limitations (temperature, ROI unvalidated, depth estimation error)

## Sources

**Enterprise Pitfall Case Studies:**
- Eaton ABM field experience: scheduling algorithm tuning feedback from datacenters
- Schneider UPS reliability reports: internal resistance baseline drift in production
- Vertiv battery degradation analysis: cycle ROI miscalibration risks

**VRLA Physics Edge Cases:**
- Battery University: temperature effects on discharge curve shape and recovery
- NREL VRLA aging study: capacity loss per cycle variation across manufacturers
- Sandia SAND2004-3149: sulfation detection false positives in noisy environments

**Software Engineering:**
- Distributed systems pitfalls (CACM, 2023): timing assumptions, silent failures
- Systemd best practices: journald structured logging, reliable service communication

---

*Pitfall analysis for: UPS Battery Monitor v3.0 (Active Battery Care)*
*Researched: 2026-03-17*
*Status: Ready for detailed design + mitigation planning*
