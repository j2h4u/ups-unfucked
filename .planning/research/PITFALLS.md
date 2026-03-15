# Domain Pitfalls: Battery Capacity Estimation

**Domain:** UPS/Lead-Acid Battery Capacity Measurement

**Researched:** 2026-03-15

---

## Critical Pitfalls

Mistakes that cause rewrites, silent failures, or wildly inaccurate predictions.

### Pitfall 1: Coulomb-Only Counting Without Voltage Anchor

**What goes wrong:**
- Measure current × time over discharge: `Q = ∫ I(t) dt`
- On first discharge: Q ≈ 6Ah (reasonable)
- On 5th discharge: Q ≈ 7.2Ah (drifted up 20%)
- On 20th discharge: estimates oscillate wildly ±30%

**Why it happens:**
- ADC noise (±0.1A on 10A load) integrates to ±600mAh error per hour
- Sensor bias (systematic offset) accumulates
- Load estimation (V/R) has 5-10% error
- No recalibration → error grows unbounded

**Consequences:**
- User loses confidence in capacity estimates
- Replacement predictor becomes useless
- System appears broken ("why did capacity jump 20% overnight?")

**Prevention:**
- Combine coulomb counting with voltage LUT as anchor
- Use 10.5V (VRLA cutoff) as zero-error reference point
- Periodically reset cumulative error when voltage reaches known state
- Validate every 5-10 discharges: compare coulomb estimate to voltage-based estimate

**Detection:**
- Monitor coefficient of variation: if >15% across last 3 estimates → log WARNING
- Set alert threshold: if estimated_capacity > 1.2× or < 0.8× moving average → investigate

---

### Pitfall 2: Circular Dependency: Capacity ↔ Peukert Exponent

**What goes wrong:**
- Capacity formula includes Peukert correction: `Q = ∫ I dt × f(exponent)`
- Peukert exponent also depends on discharge profile (which changes with capacity)
- Attempt to auto-calibrate both simultaneously → divergent iteration → no convergence

**Example:**
```
Iteration 1: Assume exp=1.2, measure capacity=5.8Ah
Iteration 2: Reverse-engineer exp=1.15, recalculate capacity=6.1Ah
Iteration 3: Measure exp=1.22, recalculate capacity=5.5Ah
...oscillates forever
```

**Why it happens:**
- Desire to "get everything right at once"
- No clear ordering: which to calibrate first?

**Consequences:**
- Capacity estimates flail ±10-15% instead of converging
- User sees "preliminary" forever
- Replacement predictor unreliable

**Prevention:**
- **v2 decision:** Fix Peukert at 1.2 (typical for VRLA). Measure capacity only.
- **v3+ decision:** Once capacity stable (3+ deep discharges), refine Peukert in separate milestone (CAL2-02)
- Document explicitly: "Peukert = 1.2 (fixed), Capacity = measured (from v2.0 onwards)"

**Detection:**
- If capacity_confidence oscillates instead of monotonically increasing → likely Peukert problem
- Flag in logs: "Capacity oscillating; check if Peukert is being auto-updated"

---

### Pitfall 3: SoH Recalibration Without User Awareness

**What goes wrong:**
- System measures capacity = 5.8Ah on day 1
- Historical SoH was calculated as: 5.2Ah / 7.2Ah = 72%
- After convergence, recompute: 5.2Ah / 5.8Ah = 90%
- User sees MOTD jump from "SoH 72% — replace soon" to "SoH 90% — good health"
- User assumes bug or misunderstands what happened

**Why it happens:**
- Initial capacity was a config guess (7.2Ah rated)
- Real battery is smaller (5.8Ah)
- SoH measured discharge curve area vs reference
- Reference was normalized wrong

**Consequences:**
- Confusion + distrust of the system
- User manually corrects config (defeats measurement)
- Alerts become noisy (false positives → false negatives)

**Prevention:**
- **Before rebaseline:** Log and alert clearly
- Message: "Battery capacity converged to 5.8Ah (was 7.2Ah assumed). Recalculating SoH..."
- Include both old and new SoH in MOTD for one week
- Document: "SoH interpretation changed in v2.0; see CHANGELOG"
- Add metadata to model.json: `{"soh_recalibration_date": "2026-03-14", "reason": "capacity_convergence"}`

**Detection:**
- Monitor for SoH jumps >10% between days (except on battery swap)
- Flag in logs when rebaseline happens

---

### Pitfall 4: Temperature Sensitivity Without Sensor

**What goes wrong:**
- Battery capacity changes ±5% with temperature (±10°C)
- Summer: capacity higher (5.8Ah at 25°C)
- Winter: capacity lower (5.5Ah at 10°C)
- System estimates capacity in winter, then winter ≠ summer → looks like degradation

**Example:**
```
Jan (10°C): Estimate 5.5Ah → "converged"
Jun (25°C): New discharge, estimate 5.8Ah → "capacity increased 5% overnight?"
```

**Why it happens:**
- No temperature sensor on UPS
- Peukert exponent itself is temperature-dependent
- Coulomb counting affected by electrolyte resistance (temperature-dependent)

**Consequences:**
- Capacity estimates noisy across seasons
- Degradation tracking (SoH trend) contaminated by temperature
- Replacement predictor inaccurate

**Prevention:**
- **v2 decision:** Accept ±5% temperature effect as within margin of error
- Document: "Capacity ±5% accuracy; seasonal variation expected"
- In model.json, store discharge metadata: `{"temperature_estimated_from_discharge_curve": 22}` (if time-of-day pattern detectable)
- Flag in logs: "Large capacity jump detected; consider temperature difference"
- **v3+ decision:** If accuracy critical, add optional temperature sensor (DS18B20 ~$2)

**Detection:**
- If estimate change > 5% but voltage LUT shift < 3% → likely temperature
- Compare estimates from same time-of-year → should be stable

---

### Pitfall 5: Shallow Discharge Masquerading as Deep

**What goes wrong:**
- UPS goes offline, system estimates ΔSoC = 30%, estimates capacity = 6.8Ah
- Marked as "valid estimate," converges after 2 of these
- But true capacity is 5.8Ah (real deep discharge would show ΔSoC = 60%+)
- Replacement predictor underestimates battery age

**Why it happens:**
- Voltage LUT lookup for small ΔV has high uncertainty
- Example: V = 12.9V → SoC = 72% (from LUT), ΔV = 0.4V → ΔSoC = 8% (error ±3%)
- Cascade error → capacity estimate off by 30%

**Consequences:**
- Converged estimate is wrong by 15-20%
- Replacement predictor off by months
- Won't discover true capacity until forced deep discharge

**Prevention:**
- Reject estimates where ΔSoC < 25% or ΔV < 1.0V
- Reject estimates where discharge_duration < 300 sec (5 min) unless load was high
- Require at least 1 "deep" (ΔSoC > 50%) before marking converged
- Log rejected estimates: "Discharge too shallow (ΔSoC=12%, need >25%)"

**Detection:**
- Compare final SoH (from measurement) against rated capacity
- If SoH < 80% without visible degradation trend → suspect bad baseline capacity
- Request manual deep discharge test if confidence remains low after 4 weeks

---

## Moderate Pitfalls

### Pitfall 6: Peukert Exponent Overfitting

**What goes wrong:**
- Discharge curve for UT850 at 15% load: n_peukert ≈ 1.15 (typical)
- Discharge curve for same battery at 25% load: n_peukert ≈ 1.25 (higher current = different curve shape)
- If you fit exponent per discharge, it oscillates 1.15–1.25
- Capacity estimates also oscillate

**Why it happens:**
- Peukert exponent is not universal constant; varies with discharge rate
- Attempting fine-grained auto-calibration magnifies this variation

**Prevention:**
- Use fixed Peukert (1.2 is safe default for typical VRLA)
- Accept ±3% error from exponent variance → within measurement error anyway
- If discharge profile unusual (rare, very high load), log and ignore that estimate

**Detection:**
- If exponent estimate oscillates >10% across discharges → log WARNING

---

### Pitfall 7: New Battery Detection False Positive

**What goes wrong:**
- User claims to replace battery, but actually didn't (forgot, or swapped same battery back)
- System detects ΔCapacity > 10%, prompts "Is this new? [y/n]"
- User says "yes"
- System resets SoH baseline to 100%
- Over next 3 months, SoH drops to 85%
- User thinks battery degraded quickly; actually battery was already 85% from before swap

**Why it happens:**
- No way to verify battery serial number or visual inspection
- User can lie or be mistaken

**Consequences:**
- Replacement timeline off by months
- User distrust

**Prevention:**
- Document: "New battery detection is user-initiated; be honest"
- Add optional manual override: config file `new_battery_baseline_soh = 1.0` or `0.95`
- Log user response + timestamp: "User confirmed new battery at 2026-03-15"
- If SoH drops >3% in first week after "new" → log warning

**Detection:**
- Monitor for new battery claims followed by rapid SoH drop
- If SoH decline rate unusual → suggest user verify actual battery age

---

### Pitfall 8: Load Estimation Error Cascades

**What goes wrong:**
- Load estimated as 15% (from ups.load), actual is 12%
- Coulomb calculation: `Q = I_avg × t` uses 15% → estimates capacity high
- Next discharge: actual load 18%, estimates low
- Capacity bounces ±8%

**Why it happens:**
- `ups.load` from firmware is not ground truth
- Server-side process load varies (disk I/O spikes, CPU bursts)
- UPS firmware estimates load from voltage droop (not reliable)

**Consequences:**
- Capacity estimates noisy; confidence takes longer to converge

**Prevention:**
- Average load over entire discharge, don't use single sample
- Filter out spike samples (if load jumps >50% in one interval, smooth it)
- Use median of load_percent instead of mean (robust to outliers)
- Document in MOTD: "Capacity estimates based on firmware load ±5% uncertainty"

**Detection:**
- If load_std_dev > 50% of load_mean → log warning "Unusual load profile"

---

## Minor Pitfalls

### Pitfall 9: Discharge Buffer Overflow During Long Blackout

**What goes wrong:**
- Very long blackout (>3 hours): discharge_buffer overflows at 500 samples
- Data truncated; ΔSoC calculation based on partial discharge
- Capacity estimate wrong

**Why it happens:**
- Buffer fixed at 500 samples (~300 sec per sample for 30+ min discharge)
- Sampling every 5 sec = 360 samples in 30 min, OK
- But 3+ hour blackout = 2160 samples, overflow

**Consequences:**
- Lost data; inaccurate capacity (rare problem, unlikely in practice due to 3-min shutdown timer)

**Prevention:**
- Document discharge buffer limit in config comments
- Set alert: if buffer near full, log WARNING "Discharge buffer filling up"
- Unlikely to trigger in practice (shutdown timer fires at 3-5 min runtime)

**Detection:**
- Check buffer usage in logs

---

### Pitfall 10: Confidence Threshold Too High/Too Low

**What goes wrong:**
- Too high (conf > 0.95): convergence never reached; users wait indefinitely
- Too low (conf > 0.50): early convergence with wrong estimate; replacement timeline off

**Why it happens:**
- No standard guidance; chose threshold empirically without validation

**Consequences:**
- Either user frustration (waiting), or system unreliability (wrong estimate)

**Prevention:**
- Document choice: "2-3 deep discharges ≈ 95% confidence for ±5% accuracy"
- Make threshold configurable: `min_confidence_for_convergence` in config.toml
- Log convergence event with confidence breakdown

**Detection:**
- Monitor convergence time; should be 1-4 weeks on typical grid
- If still <0.8 after 8 discharges → capacity_estimator might have bugs

---

## Phase-Specific Warnings

| Phase Topic | Likely Pitfall | Mitigation |
|-------------|---------------|------------|
| **Design** | Over-complicating Peukert + capacity coupling | Decision: Fix Peukert, measure capacity alone. Defer refinement to v3. |
| **Implementation** | Accumulating coulomb error without voltage anchor | Code review: Verify LUT reset-point logic. Test with synthetic discharge data. |
| **Validation** | Testing only on stable grid (no blackouts for weeks) | Use replay: Feed real discharge_buffer from historical blackouts to estimator. |
| **Release** | Silent SoH recalibration without user alert | Add MOTD message + changelog note explaining new SoH baseline. |
| **Field** | Temperature variation mistaken for degradation | Document ±5% seasonal variation in user guide. Add temp metadata to estimates. |

---

## Sources

- Real blackout data (2026-03-12): Revealed coulomb-only estimates drift over time
- Battery University: Peukert law limitations, temperature effects
- IEEE-450-2010: Standard discharge testing procedures (reveals shallow/deep distinction)
- Field experience: UPS battery testing guides mention load estimation uncertainty

---

**Next Steps:** During implementation (Phase 1), add test cases for each pitfall. Document detection logic in code comments.
