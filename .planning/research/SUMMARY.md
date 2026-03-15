# Project Research Summary

**Project:** UPS Battery Monitor v2.0 — Actual Capacity Estimation
**Domain:** Battery State-of-Health (SoH) Monitoring with Capacity Measurement
**Researched:** 2026-03-15
**Confidence:** MEDIUM-HIGH

## Executive Summary

Battery capacity estimation for lead-acid UPS batteries is a well-established problem with strong theoretical foundations (coulomb counting, voltage-anchoring, Peukert's law) but underspecified in practice. Real batteries commonly ship underrated — labeled 7.2Ah but measuring 5.8Ah actual — which causes SoH calculations to be artificially pessimistic from day one. This project solves that by measuring real capacity from actual discharge events (blackouts) using mathematical integration rather than relying on manufacturer specs.

The recommended approach combines three proven techniques: (1) coulomb counting (integrating current × time over discharge duration), (2) voltage-based state-of-charge anchoring to prevent cumulative error drift, and (3) statistical confidence tracking to show users when estimates are preliminary vs. reliable. Implementation requires only Python stdlib (no new dependencies), making the solution lightweight and maintainable. 2-3 deep discharges (>50% depth-of-discharge) produce estimates accurate to ±5-10%, matching manufacturer tolerance.

Key risks center on circular dependencies (capacity ↔ Peukert exponent), temperature effects without a sensor, and shallow discharges being mistaken for deep ones. Mitigation is straightforward: fix Peukert at 1.2 for v2 (defer refinement to v3), accept ±5% seasonal variation as acceptable error margin, and enforce minimum depth/duration thresholds before accepting estimates. The biggest pitfall to avoid is coulomb-only counting without voltage anchoring, which causes estimates to drift ±30% over many discharges due to ADC noise and sensor bias accumulation.

## Key Findings

### Recommended Stack

The technology stack is deliberately minimal — **no new external dependencies**. All capacity estimation is pure Python using only the stdlib (math, statistics, json, datetime). This is possible because the algorithm involves basic mathematical operations (trapezoidal integration, mean/variance calculations, LUT lookups) that don't require numerical libraries like numpy or scipy. The stack reuses existing infrastructure (discharge_buffer collection in monitor.py, voltage LUT from v1.1, atomic JSON write patterns in model.py).

**Core technologies:**
- **Python 3.13** — capacity estimator algorithm; already in use, stable, minimal dependencies
- **Standard library `math` + `statistics`** — trapezoidal integration, variance/confidence calculations; no scipy/numpy needed for small sample sizes
- **Standard library `json` + `datetime`** — persisting capacity estimates with atomic writes and timestamps
- **Existing discharge buffer** — voltage/time/load samples already collected during blackouts; no new hardware

### Expected Features

**Table stakes (must have):**
- **Capacity measurement from discharge** — core v2 requirement; without this feature, capacity remains a config guess
- **Continuous refinement** — as more discharges occur, estimates converge via weighted averaging of recent samples
- **New battery baseline detection** — on battery swap, system must distinguish "new battery" from "degraded battery" via user y/n prompt (>10% capacity change triggers)
- **Statistical confidence tracking** — users want to know if estimate is solid (3 deep discharges) or preliminary (1 short blackout)
- **Separation of capacity from degradation** — store both `rated_capacity` (reference) and `measured_capacity` (current); recalculate SoH baseline when measurement converges

**Differentiators (should have, v2.1+):**
- **Partial discharge accumulation** — estimate from many short blackouts (2-5 min each) using Bayesian weighting; enables faster measurement on stable grids
- **Discharge quality metadata** — track depth (ΔSoC %), duration, load profile, temperature; serve to user for analysis

**Defer to v3+:**
- **Peukert exponent auto-calibration** — circular dependency with capacity; fixed at 1.2 for v2
- **Temperature-corrected estimates** — no sensor available; adds ±5% baseline uncertainty
- **Cross-brand benchmarking** — requires multi-UPS data; out of scope for v2

### Architecture Approach

The architecture is event-driven and follows existing patterns in v1.1: on power restoration (OB→OL transition), capacity estimator processes the discharge buffer and stores result in model.json. BatteryModel (persistent data layer) is extended with methods for adding/averaging capacity estimates and detecting when convergence threshold is crossed. SoH calculator formula is updated to normalize against measured capacity instead of rated capacity when measurement becomes available. This design minimizes coupling, keeps responsibility boundaries clear, and reuses proven atomic write patterns (no data loss on crash).

**Major components:**
1. **DischargeBuffer** (existing) — collects voltage/time/load samples during OB state
2. **CapacityEstimator** (new) — pure function: (V_series, t_series, I_series, LUT) → (estimated_ah, confidence, metadata)
3. **BatteryModel** (extended) — persists capacity_estimates array, tracks convergence, detects new battery
4. **SoH Calculator** (formula updated) — normalizes to measured_capacity when available, recalculates SoH history
5. **Monitor** (orchestration updated) — calls estimator on OB→OL, stores result via battery_model.add_capacity_estimate()

### Critical Pitfalls

1. **Coulomb-only counting without voltage anchor** — Current × time alone accumulates ADC noise (±0.1A × 3600 sec ≈ ±600mAh/hr) unbounded. Estimates drift ±30% over time. Prevent: anchor to voltage LUT using 10.5V (VRLA cutoff) as zero-error reference point. Validate every 5-10 discharges.

2. **Circular dependency: capacity ↔ Peukert exponent** — Capacity formula depends on Peukert (Q = ∫I×t × f(exponent)), but exponent depends on discharge profile (which changes with capacity). Simultaneous auto-calibration = oscillation (no convergence). Decision: **fix Peukert at 1.2 for v2** (±3% error acceptable), defer refinement to v3.

3. **SoH recalibration without user awareness** — When capacity converges, SoH baseline changes (e.g., 72% → 90%). User sees MOTD jump and loses confidence. Prevent: log warning before rebaseline, show both old/new SoH for one week, add metadata to model.json with rebaseline reason.

4. **Temperature sensitivity without sensor** — Capacity varies ±5% over ±10°C range. Winter vs. summer estimates look like degradation. Accept as within acceptable margin (document clearly), store discharge metadata for future if sensor added, flag large jumps in logs.

5. **Shallow discharge masquerading as deep** — Voltage LUT lookup for small ΔV has ±3% uncertainty; ΔV=0.4V → ΔSoC=8% ± 3%. Prevent: reject if ΔSoC < 25% or duration < 300 sec, require at least 1 "deep" (>50%) before marking converged.

## Implications for Roadmap

Based on research findings, the implementation should follow this phase structure to avoid pitfalls and deliver value incrementally.

### Phase 1: Deep Discharge Capacity Estimation

**Rationale:** Simplest path to MVP; no circular dependencies; eliminates guesswork for 90% of cases. Existing discharge_buffer already collects needed data. Two deep discharges ≈ 95% confidence; allows most users to converge in 1-4 weeks.

**Delivers:**
- `capacity_estimator.py` — coulomb counting + voltage anchor + Peukert correction (pure function)
- `BatteryModel.add_capacity_estimate()` — persist estimates in capacity_estimates array
- Weighted capacity averaging — recency + confidence-based weights
- MOTD enhancement: "Capacity: 5.8Ah (measured) vs 7.2Ah (rated), 2/3 deep discharges"
- `battery-health.py` fields: `capacity_ah_measured`, `capacity_confidence`

**Addresses (table stakes):**
- Capacity measurement from discharge
- Continuous refinement via weighted averaging
- Statistical confidence tracking (depth, duration, stability scores)
- New battery detection (>10% difference triggers user prompt)

**Avoids:**
- Pitfall 1: Voltage LUT anchoring built into estimator design
- Pitfall 2: Peukert fixed at 1.2; no auto-calibration
- Pitfall 5: Minimum depth/duration filters (ΔSoC > 25%, duration > 300 sec)

**Research flags:**
- Coulomb error accumulation validation (replay real 2026-03-12 discharge_buffer)
- Coefficient of variation convergence (2-3 samples sufficient? Test with synthetic Gaussian noise)
- Load profile sensitivity (Peukert factor adequate across 10-30% loads?)

### Phase 2: SoH Recalibration & New Battery Logic

**Rationale:** Phase 1 establishes measured capacity baseline. Phase 2 feeds that back into SoH calculation (separates capacity from degradation). New battery detection prompts user, resets baseline, recalculates historical SoH. Must come after Phase 1 convergence or logic is meaningless.

**Delivers:**
- Updated SoH formula: `SoH = area_measured / (area_reference × measured/rated)`
- Automatic SoH history recomputation when capacity converges
- Startup new battery detection: compare stored vs. runtime estimate, prompt if >10% diff
- Logging/alerting for SoH rebaseline event with clear messaging

**Addresses:**
- Separation of capacity from degradation (foundation for replacement predictor accuracy)

**Avoids:**
- Pitfall 3: Clear logging before/after rebaseline, MOTD messaging, changelog note

**Effort:** Low (1 day; mostly formula change + state management)

### Phase 3: Partial Discharge Accumulation (Conditional)

**Rationale:** Only if field data shows users on stable grids waiting months without deep discharges. Phase 1 handles most cases; Phase 3 is fallback for edge case. Requires Bayesian variance weighting; medium complexity.

**Delivers:**
- Accumulation logic for ΔSoC > 20% discharges
- Variance-weighted averaging (deeper = lower variance = higher weight)
- Progress tracking: "2/3 deep discharges needed, or 3/10 partial discharges"

**Condition:** Build only if initial estimate confidence remains <0.8 after 4 weeks on deployed systems.

**Defer to:** v2.1 (post-MVP validation)

### Phase Ordering Rationale

1. **Phase 1 → Phase 2:** Capacity must converge before SoH recalibration makes sense. Phase 2 depends on stable Phase 1 baseline.
2. **Phase 1 first, not Phase 2:** User immediately sees honest capacity + confidence from day 1. Even without SoH rebaseline, they know battery is 5.8Ah (not 7.2Ah), which fixes replacement predictor accuracy.
3. **Phase 3 optional:** Only triggers if field data shows need. Adds complexity; Phase 1 alone covers typical scenarios.
4. **Deferred: Peukert refinement (v3):** Breaks circular dependency by deferring. v2 owns capacity measurement; v3 owns Peukert refinement (milestone CAL2-02).

This ordering avoids Pitfall 2 (circular dependency) and Pitfall 3 (silent rebaseline) by making dependencies explicit and user-facing.

### Research Flags

Phases needing deeper research during planning:
- **Phase 1 — Critical validation:** How much does ΔSoC error compound over full discharge? Run replay test with real 2026-03-12 discharge_buffer; expect <±10% total error with voltage anchor (vs >±20% without).
- **Phase 1 — Convergence threshold:** Does coefficient of variation reach <10% by sample 3? Test with synthetic Gaussian noise (±5% load, ±0.1V voltage).
- **Phase 1 — Load sensitivity:** How much does capacity estimate change across 10-30% loads? Peukert predicts ±3%; validate empirically.

Phases with standard patterns (skip research-phase):
- **Phase 2:** SoH formula straightforward math; existing atomic write patterns proven in v1.1.
- **Phase 3 (if triggered):** Variance weighting well-documented; defer research until field data justifies it.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| **Stack** | HIGH | Python stdlib sufficient; verified existing discharge_buffer + LUT patterns cover all I/O needs. No external libs required. |
| **Features** | MEDIUM-HIGH | Table stakes clear (coulomb counting + confidence tracking = standard practice). Statistical thresholds (2-3 discharges for convergence) backed by IEEE-450 and field experience, but not validated on CyberPower UT850 specifically. |
| **Architecture** | MEDIUM-HIGH | Component design clean (estimator as pure function, BatteryModel extended sensibly). Patterns (voltage anchor, weighted averaging, multi-factor confidence) proven in literature + existing codebase. One gap: optimal confidence formula parameters inferred from domain knowledge, not empirically validated. |
| **Pitfalls** | HIGH | Critical pitfalls (coulomb-only, circular dependency, SoH rebaseline) well-understood; prevention strategies concrete + testable. Temperature and load estimation effects grounded in battery physics. Shallow-discharge filtering straightforward. |

**Overall confidence:** MEDIUM-HIGH

### Gaps to Address

1. **Coulomb counting error accumulation (CRITICAL):** How much does LUT ΔSoC lookup error compound over 30+ minute discharge? Replay real 2026-03-12 discharge_buffer through estimator to validate error < ±10%. Mitigation: validation test during Phase 1 requirements.

2. **Convergence threshold empirics (IMPORTANT):** Assumption that 2-3 deep discharges → 95% confidence needs field validation. Run Monte Carlo on synthetic discharge profiles with Gaussian noise. Mitigation: add to Phase 1 testing strategy.

3. **Load profile sensitivity (IMPORTANT):** Discharge_buffer load varies (10-20% typical). How sensitive is capacity estimate to this variance? Mitigation: test Phase 1 implementation with discharge_buffer from different load scenarios.

4. **Temperature effect quantification (NICE-TO-HAVE):** Can discharge curve shape be used to back-calculate temperature? Helps prioritize hardware sensor for v3. Mitigation: store discharge metadata; analyze post-hoc if data accumulates.

5. **New battery false positive rate (MODERATE):** Pitfall 7 (user forgets to swap, claims new battery). No way to verify. Mitigation: log user response + timestamp, add config override for manual correction.

## Sources

### Primary (HIGH confidence)

- **STACK.md:** Python 3.13 stdlib survey (math, statistics, json, datetime) — verified all required operations available without external deps
- **FEATURES.md:** IEEE-1188 + IEEE-450-2010 (VRLA capacity testing standards); Battery University BU-904 (coulomb counting methodology)
- **ARCHITECTURE.md:** Existing v1.1 codebase patterns; MDPI Energies Vol. 15 No. 21 (voltage-anchored coulomb counting)
- **PITFALLS.md:** Real 2026-03-12 blackout data analysis; Peukert's Law literature; thermal physics (±5% temperature coefficient)

### Secondary (MEDIUM confidence)

- ScienceDirect SOH Estimation from Multiple Features (variance weighting methods)
- Analog Devices SOC/SOH Estimation Techniques (confidence scoring heuristics)
- Vertiv UPS Battery Acceptance/Capacity Test Procedure

### Tertiary (validation needed)

- Confidence threshold (2-3 deep discharges for convergence) — empirical validation needed
- ΔSoC LUT lookup error propagation — needs replay testing with real discharge_buffer
- Load profile sensitivity — needs comparison across discharge scenarios

---

**Research completed:** 2026-03-15
**Ready for roadmap:** Yes — sufficient clarity for Phase 1-2 detailed design. Phase 3 roadmap gate pending field validation.
