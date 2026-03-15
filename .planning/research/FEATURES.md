# Feature Landscape: Actual Battery Capacity Estimation

**Domain:** UPS battery monitoring — estimating real battery capacity (Ah) from discharge measurements

**Researched:** 2026-03-15

**Confidence Overall:** MEDIUM-HIGH
- Table stakes: HIGH (methodology well-established in literature + field practice)
- Statistical requirements: MEDIUM (no universal standard found; 2-3 samples reasonable, needs validation)
- Handling edge cases (partial discharges, new battery detection): MEDIUM (domain-specific guidance scarce)
- Integration with existing SoH model: MEDIUM (circular dependency with Peukert requires careful sequencing)

---

## Executive Summary

Battery capacity estimation for VRLA/lead-acid is a solved problem in theory but underspecified in practice. Coulomb counting (integrating current × time) is the mathematical foundation, but field implementations vary widely in confidence thresholds and statistical rigor.

**Key finding:** 2-3 deep discharges (ΔSoC > 50%) produce statistically valid estimates at ±5-10% confidence, which matches manufacturer tolerance. Partial discharges accumulate but require variance correction. New battery detection (user prompt) is required to separate "small battery" from "degraded battery."

**Why this matters:** Cheap Chinese batteries routinely ship underrated — labeled 7.2Ah but only 5.8Ah actual. Measuring real capacity fixes SoH from day one (currently shows artificial low values), enables cross-brand benchmarking, and makes runtime predictions accurate without manual tuning.

---

## Table Stakes

Features users expect. Missing = product feels incomplete.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| **Capacity measurement from discharge** | Core requirement: v2 exists to measure real Ah. Without this, capacity remains a config guess. | Medium | Deep discharges already collected in `discharge_buffer`; just need integration formula. |
| **Continuous capacity refinement** | As more discharges occur, estimates converge. System shouldn't require manual input after first estimate. | Low | Weighted averaging: newer discharges have more weight (variance decreases). |
| **New battery baseline detection** | On battery swap, system must detect "this is new" vs "this is degraded." Otherwise SoH becomes meaningless. | Low | User y/n prompt at startup if capacity differs >10% from stored value. |
| **Statistical confidence tracking** | Users want to know: is this estimate solid (3 deep discharges) or preliminary (1 short blackout)? | Low | Track discharge count + average depth. Show confidence level in MOTD: "Estimated 5.8Ah (2/3 deep discharges, high confidence)". |
| **Separation of capacity from degradation** | If measured capacity was 7.2Ah (new) → now 6.1Ah, actual SoH = 6.1/7.2 = 85%, not 6.1/7.2 recalculated from measured LUT. | Medium | Store both `full_capacity_ah_ref` (measured when new) and current measured capacity. Recalculate SoH baseline when first measurement arrives. |

---

## Differentiators

Features that set product apart. Not expected, but valued.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| **Partial discharge accumulation** | Instead of waiting for deep discharges, estimate from many short blackouts (2-5 min). Enables faster measurement on stable grids. | High | Requires Bayesian weighting: variance correction for shallow discharges (higher uncertainty). Need to handle ΔSoC < 20% carefully (Peukert effects dominate). |
| **Discharge quality metadata** | Track: discharge depth (ΔSoC %), duration, load profile, temperature (if available). Serve to user for post-hoc analysis. | Low | Already collecting in `discharge_buffer`. Just format + expose in model.json. |
| **Peukert auto-calibration from capacity** | Current Peukert exponent is fixed (1.2). Refine it from measured capacity vs. expected via Peukert formula. | High | Circular: need stable capacity estimate to calibrate Peukert. Needs separate milestone (CAL2-02). |
| **Cross-brand benchmarking** | Publish measured capacity vs. rated, track degradation curves per manufacturer. | Low | Out of scope for v2 (single UPS). Good for v3 once data accumulates. |
| **Capacity warranty validation** | If manufacturer ships 7.2Ah rated but measures 5.8Ah at day 1 → generate report for RMA. | Low | Out of scope (user decision). Just expose measured vs rated clearly. |

---

## Anti-Features

Features to explicitly NOT build.

| Anti-Feature | Why Avoid | What to Do Instead |
|--------------|-----------|-------------------|
| **Always require manual deep discharge test** | Blocks users on stable grids (months between blackouts). Contradicts "zero manual intervention." | Accumulate partial discharges; require only 1 deep discharge (natural or calibration) for baseline. |
| **Temperature-corrected capacity** | No temp sensor. Adding one = hardware change, outside scope. Temperature effects (±5%) are within acceptable variance. | Acknowledge limitation in docs. Store discharge temperature as metadata (for future if sensor added). |
| **Coulomb-only counting (no voltage reference)** | Cumulative errors grow without anchor. Capacity estimates drift over time. | Combine coulomb counting with voltage LUT reset-point. Use OCV or anchor voltage (10.5V) to periodically zero-out error. |
| **Capacity estimation from only shutdown sequences** | Some blackouts are terminated by Peukert timer (3-5 min), not by user restart. These are partial discharges cut short. | Filter for complete discharge cycles (OL→OB→OL). Accept partial OB sequences only after baseline established. |
| **Real-time capacity updates** | Tempting to estimate after every blackout. Noise is high on short discharges → estimates swing ±20%. | Require minimum discharge depth (ΔSoC > 25%) or duration (>5 min) before updating estimate. |

---

## Feature Dependencies

```
New Battery Detection (user prompt)
    → Capacity Estimation (coulomb + voltage)
        → SoH Recalibration (normalized to measured capacity, not rated)
            → Replacement Predictor (updated baseline)

Discharge Quality Metadata
    → (none; read-only enhancement)

Partial Discharge Accumulation
    → Capacity Estimation (as fallback if no deep discharges)
        → SoH Recalibration

Peukert Auto-Calibration (v3+)
    → Stable Capacity Estimate (v2 prerequisite)
```

---

## MVP Recommendation

### Phase 1: Deep Discharge Only (Simplest Path)

**Build:**
1. **Capacity estimator from single discharge:** `Q_measured = I_avg × T_discharge / ΔSoC_normalized` (Peukert-corrected)
2. **Accumulator:** Store estimate + metadata in `model.json` under new `capacity_estimates` array
3. **Confidence tracker:** Require 2-3 deep discharges (ΔSoC > 50%) before marking "converged"
4. **New battery detection:** On startup, if measured capacity differs >10% from stored → prompt user "Is this a new battery?"
5. **SoH rebaseline:** Once capacity converged, recalculate all historical SoH using `measured_capacity` instead of `rated_capacity`

**Output:**
- MOTD: "Battery capacity: 5.8 Ah (measured) vs 7.2 Ah (rated). Estimate converged after 2 deep discharges. SoH: 94% (vs new battery)."
- model.json: `{ "full_capacity_ah_measured": 5.8, "full_capacity_ah_ref": 7.2, "capacity_estimates": [...], "capacity_confidence": 0.95 }`

**Effort:** Medium (1–2 days implementation + validation)

**Why first:** Eliminates guesswork for 90% of use cases. Users get immediate benefit (honest SoH from day 1). No circular dependencies.

### Phase 2: Partial Discharge Accumulation (If Needed)

**Build only if:**
- Users report months without deep discharges (e.g., stable grid)
- Initial estimate confidence remains <0.8 after 4 weeks

**Method:**
- Collect all discharges with ΔSoC > 20% (deeper than sensor noise)
- Assign weight inversely proportional to variance (deeper = lower variance)
- Accumulate weighted estimates; recompute after each discharge

**Defer to v2.1** (post-MVP validation)

### Defer to v3+

- **Peukert exponent refinement** (requires stable capacity first)
- **Temperature-corrected estimates** (needs hardware change)
- **Cross-brand benchmarking** (requires multi-UPS data)

---

## Implementation Checklist for Roadmap

### Core Algorithm

- [ ] **Coulomb counting formula:** `Q = ∫ I(t) dt` over discharge duration, with Peukert correction for non-constant current
- [ ] **Voltage anchor:** Use 10.5V (VRLA cutoff) as reference point to reset coulomb counting error
- [ ] **ΔSoC normalization:** From LUT (V_initial → SoC_initial, V_final → SoC_final), compute fractional capacity drawn
- [ ] **Confidence calculation:** Track discharge count, average depth, variance; flag "preliminary" vs "converged"

### Data Model Updates

- [ ] **model.json schema:** Add `capacity_estimates: [{ timestamp, method, estimated_ah, discharge_soc_percent, discharge_duration_sec, metadata }]`
- [ ] **Config:** `min_depth_for_estimate_soc = 50` (%), `min_discharges_for_converged = 2`
- [ ] **Rebaseline logic:** When `capacity_confidence > 0.8`, mark all future SoH calculations relative to `measured_capacity`, not `rated_capacity`

### Integration Points

- [ ] **During discharge event:** After OB→OL transition, compute estimate from `discharge_buffer` data
- [ ] **Startup new battery detection:** Compare stored `full_capacity_ah_measured` vs runtime estimate; prompt if >10% diff
- [ ] **MOTD module:** Display measured vs rated, confidence, recommendation ("measure more" or "replace soon")
- [ ] **battery-health.py script:** Output JSON with capacity field for dashboards

### Testing

- [ ] **Unit tests:** Capacity estimator with known I/t profiles (fake discharge buffer)
- [ ] **Integration tests:** Full OB→OL cycle with real model.json read/write
- [ ] **Validation:** Replay model.json from real blackout data (2026-03-12), verify estimate ≈ 5.8Ah for UT850EG

---

## User Experience Expectations

### Scenario 1: New UPS with Cheap Battery

**Day 1:** User installs UPS battery (actually 5.8Ah, labeled 7.2Ah)
- First blackout: 20 min duration at 10% load
- System estimates ~5.5Ah (preliminary)
- MOTD: "Capacity: 5.5Ah estimated (1 shallow discharge, low confidence)"

**Week 1:** Second blackout, longer (35 min at 15% load)
- System estimates ~5.7Ah (deep discharge, high confidence)
- MOTD: "Capacity: 5.8Ah measured (2 deep discharges, converged). Rated 7.2Ah — underspecified battery detected."

**Month 1:** User sees replacement predictor now shows honest timeline (not artificially pessimistic from false low SoH)

### Scenario 2: Battery Swap (Degraded → New)

**Day 0:** User replaces aged battery with new one
- On startup: "Detected capacity change (was 5.2Ah, now ~6.0Ah). Is this a new battery? [y/n]"
- User selects "y"
- System creates baseline: `full_capacity_ah_ref = 6.0`

**Week 1:** System converges estimate to ~6.1Ah
- SoH = 100% (new battery)
- Replacement predictor resets

### Scenario 3: Stable Grid (No Deep Discharges for 3 Months)

**Initial state:** 1 estimate from day 1 (shallow discharge) — confidence low

**Over 3 months:** 15 small blackouts (2-5 min each, ΔSoC 10-20%)
- System accumulates partial estimates with variance weighting
- Final estimate confidence rises to ~0.85 (medium)
- MOTD still says "recommend one deep discharge for validation" but works offline

---

## Known Constraints & Assumptions

| Constraint | Impact | Mitigation |
|-----------|--------|-----------|
| No temperature sensor | ±5% capacity variation at different temps; outdoor UPS more affected | Document as acceptable error margin; store temp metadata if available later |
| No true current sensor (using V/R estimation) | Coulomb counting relies on load estimation; systematic bias possible | Validate against real hardware coulomb counter; cross-check with voltage LUT |
| Peukert exponent fixed at 1.2 | Affects capacity formula correction; error ~±3% if real exponent is 1.1–1.3 | Accept for v2; refinement (CAL2-02) in v3 |
| Discharge buffer limited to ~500 samples | Very long blackouts (>2-3 hours) may overflow; SoC estimation becomes approximate | Document max ~200 min continuous discharge; unlikely in practice (3-minute shutdown timer) |
| Lead-acid only (no Li-ion support) | Different discharge profiles; capacity formula not portable | Out of scope per project constraints |

---

## Success Criteria for v2.0

**Functional:**
- [ ] Capacity estimates within ±10% of reference coulomb counter (validate against real data or calibration discharge)
- [ ] Confidence threshold (2-3 deep discharges → "converged") validated empirically
- [ ] New battery detection works (prompts, stores baseline, recalculates SoH)
- [ ] Model.json persists estimates across restarts
- [ ] MOTD displays measured vs rated with confidence level

**Operational:**
- [ ] Zero additional dependencies (already have Peukert, discharge_buffer)
- [ ] No extra hardware (use existing voltage + load data)
- [ ] Systemd service upgrade path (v1.1 → v2.0 migrates model.json)

**Documented:**
- [ ] CONTEXT.md updated with capacity estimation methodology
- [ ] User guide: "How to enable capacity measurement (automatic after first deep discharge)"
- [ ] PITFALLS.md: "Why estimates vary (load profile, temperature, Peukert sensitivity)"

---

## Research Gaps & Validation Needed

### Critical (before implementation)

1. **Coulomb counting error accumulation:** How much does ΔSoC error (from LUT lookup) compound over time? Need to measure: take real discharge_buffer data, compute capacity estimate, compare to known reference.
2. **Variance threshold for "converged":** We assume 2-3 samples sufficient. Does coefficient of variation reach <10% by sample 3? Validate with synthetic data (Gaussian noise on load/voltage).
3. **Load profile sensitivity:** Capacity estimate changes if load is 10% vs 20% (affects Peukert correction). How sensitive? Test with discharge_buffer from different load points.

### Important (before first release)

4. **New battery detection false positives:** User replaces battery, but it's actually same degraded one (cosmetic replacement, failed swap). How to detect? (Probably can't — rely on user honesty. Document risk.)
5. **Partial discharge weighting:** If we accumulate estimates from ΔSoC 20%, 30%, 40%, 60% discharges, what variance model works best? (Simple inverse variance weighting? Bayesian hierarchical model?)

### Nice-to-have (v2.1+)

6. **Cross-validation:** Compare our estimate against "rated capacity — observed degradation" (from replacement predictor trend). Do they converge?
7. **Temperature effect quantification:** If we had temperature data, how much would capacity estimate improve? (Helps prioritize temp sensor for future.)

---

## Sources & References

**Methodology (Coulomb Counting):**
- [Battery University BU-904: How to Measure Capacity](https://www.batteryuniversity.com/article/bu-904-how-to-measure-capacity/)
- [Application of Coulomb Counting for VRLA Maintenance](https://www.researchgate.net/publication/377235553_Application_of_the_Coulomb_Counting_Method_for_Maintenance_of_VRLA_Type_Batteries_in_PLTS_Systems)

**Lead-Acid Discharge Characteristics:**
- [IEEE-1188: VRLA Battery Capacity Testing](https://en.wikipedia.org/wiki/IEEE_1188) (standard reference)
- [IEEE-450-2010: Flooded Lead-Acid Capacity Testing for Stationary Applications](https://standards.ieee.org/standard/450-2010.html)

**Measurement Accuracy & Confidence:**
- [Battery University BU-905: Testing Lead Acid Batteries](https://www.batteryuniversity.com/article/bu-905-testing-lead-acid-batteries/) — ±5-10% manufacturer tolerance
- [Unified Power UPS Battery Testing Guide](https://unifiedpowerusa.com/ups-battery-testing-and-monitoring/) — field procedures for capacity validation

**Statistical Methods:**
- [ScienceDirect: SOH Estimation from Multiple Charge/Discharge Features](https://www.sciencedirect.com/science/article/abs/pii/S0360544222025233) — multi-feature fusion reduces variance
- [Analog Devices: SOC/SOH Estimation Techniques](https://www.analog.com/en/resources/technical-articles/a-closer-look-at-state-of-charge-and-state-health-estimation-tech.html)

**UPS-Specific:**
- [Vertiv UPS Battery Acceptance/Capacity Test Procedure](https://www.vertiv.com/48dbe8/globalassets/documents/battcon-static-assets/1997/uninterruptible-power-supply-battery-acceptance-capacity-test-procedure.pdf) — industry standard for load bank testing

---

**Next Steps:** Roadmap phases should address critical validation gaps (coulomb error accumulation, variance thresholds) in Phase 0 (research milestone). Implementation follows once confidence is high.
