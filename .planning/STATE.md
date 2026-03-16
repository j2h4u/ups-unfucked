---
gsd_state_version: 1.0
milestone: v2.0
milestone_name: Actual Capacity Estimation
current_plan: "12.1-04"
status: in_progress
last_updated: "2026-03-16"
progress:
  total_phases: 3
  completed_phases: 0
  total_plans: 9
  completed_plans: 4
---

# Project State — UPS Battery Monitor

**Last Updated:** 2026-03-16 after Phase 12.1 Plan 04 completion
**Milestone:** v2.0 Actual Capacity Estimation — Wave 4 complete
**Current Position:** Phase 12.1 Wave 4 complete (orchestrator wiring); ready for Wave 5 (adversarial scenarios)

---

## Project Reference

**Core value:** Сервер выключается чисто и вовремя при блекауте, используя каждую минуту — не полагаясь на прошивку CyberPower.

**v2.0 milestone goal:** Measure real battery capacity (Ah) from discharge data — replace rated label value, enable accurate SoH from day one and cross-brand benchmarking.

**Previous milestones shipped:**
- v1.0 MVP (phases 1–6): 6,596 LOC, 205 tests, core daemon with LUT model, calibration mode, safe shutdown
- v1.1 Expert Panel Review Fixes (phases 7–11): Per-poll writes, frozen Config/CurrentMetrics dataclasses, full integration tests, batch writes, MetricEMA extensibility

---

## Current Position

**Phase:** Roadmap complete → Ready for planning
**Milestone progress:** 4/9 plans completed → Next: Phase 12.1 Plan 05 (adversarial scenarios)
**Roadmap status:** ✓ Orchestrator/kernel boundary validated; clean separation of concerns

**Key milestone constraints (locked for v2.0):**
- Peukert exponent stays fixed at 1.2 (circular dependency avoidance)
- Temperature compensation out of scope (indoor ±3°C, ±5% error acceptable)
- Phase ordering: 12 → 13 → 14 (measurement → recalibration → reporting, with hard dependency between 12–13)

---

## Roadmap Summary

### Phase 12: Deep Discharge Capacity Estimation
- **Requirements:** CAP-01, 02, 03, 04, 05, VAL-01, VAL-02 (7 requirements)
- **Goal:** Coulomb counting + voltage anchor + confidence tracking; core measurement algorithm
- **Success criteria:** 5 observable behaviors (measurements logged, confidence increases, filters reject bad discharges, new-battery detection, MOTD reporting)
- **Depends on:** v1.1 discharge_buffer + voltage LUT infrastructure

### Phase 13: SoH Recalibration & New Battery Detection
- **Requirements:** SOH-01, 02, 03 (3 requirements)
- **Goal:** Separate capacity from degradation; enable new battery baseline reset
- **Success criteria:** 5 observable behaviors (SoH normalizes to measured, history tagging, new battery detection on startup, rebaseline events logged, MOTD messaging)
- **Depends on:** Phase 12 (measured capacity must converge)

### Phase 14: Capacity Reporting & Metrics
- **Requirements:** RPT-01, 02, 03 (3 requirements)
- **Goal:** Expose capacity to MOTD, journald, Grafana
- **Success criteria:** 5 observable behaviors (MOTD display, journald events searchable, /health endpoint, Grafana dashboard queries, user can assess convergence)
- **Depends on:** Phases 12–13 (reporting works best with stable data)

---

## Accumulated Context

### Key Decisions (Locked for v2.0)

| Decision | Rationale | Implementation |
|----------|-----------|-----------------|
| **Peukert stays fixed at 1.2** | Avoid circular dependency (capacity ↔ exponent). v2.0 owns capacity measurement; v2.1+ owns Peukert refinement (CAL2-02). | VAL-02 constraint: CapacityEstimator uses fixed 1.2. ±3% error acceptable for v2.0. |
| **Deep discharges first** | MVP focuses on >50% ΔSoC events (most reliable, IEEE-450 backed). Partial discharge accumulation (Phase 3, v2.1+) only if field data justifies. | Phase 12: requires ΔSoC > 50%, duration > 300s, voltage-anchored. Rejects ΔSoC < 25%. |
| **Confidence threshold ≈ 2–3 deep discharges** | IEEE-450 + field experience: 2–3 samples → ±5% accuracy, 95% confidence. Coefficient of variation < 10% as lockpoint. | Phase 12: convergence when count ≥ 3 AND CoV < 10%. MOTD shows progress ("2/3 deep discharges"). |
| **Temperature out of scope** | Indoor ±3°C year-round, ±5% seasonal variation acceptable. No hardware sensor; adds change. | Phase 12: store discharge metadata (V, I, t) for post-hoc analysis if sensor added (v3+). |
| **New battery detection on startup** | User may forget battery swap. Daemon auto-detects via >10% capacity jump, prompts for confirmation. | Phase 13: on startup, compare stored estimate vs. current discharge runtime. Prompt: "New battery installed? [y/n]" if >10% diff. |
| **Math kernel as package** | Formulas have different change frequencies (Peukert stable 100 years, SoH blending revised weekly). Mixing in one file means every tweak touches foundational physics. | Phase 12.1: `src/battery_math/` package with focused modules: `peukert.py`, `soh.py`, `soc.py`, `calibration.py`, `capacity.py`, `types.py`. Re-exports via `__init__.py`. |
| **Frozen BatteryState dataclass** | Circular dependencies become structurally visible at type level when kernel functions take/return immutable state. Mutable state = hidden side channels. | Phase 12.1: `BatteryState(frozen=True)` — kernel functions take and return frozen state. Orchestrator converts to/from BatteryModel. |
| **Inject current_time, don't mock** | `_weighted_average_by_voltage` uses `time.time()` internally, breaking purity and making simulation require mocking. Parameter injection is cleaner. | Phase 12.1: add `current_time: float = None` parameter (defaults to `time.time()`). Zero behavior change for daemon, full control for simulator. |
| **Per-iteration Lyapunov, not aggregate** | Aggregate "±3% after 10 iterations" passes a system with 10% per-iteration amplification (1.1^100 = 13,780x blowup). Per-iteration ratio < 1.0 catches instability early. | Phase 12.1: primary gate is `divergence[i]/divergence[i-1] < 1.0` per step. Aggregate ±3% is secondary smoke test only. |
| **Phase 12 capacity_estimates isolated** | Measured capacity must NOT flow back into `full_capacity_ah_ref` — SoH calculator uses rated capacity. Replacing rated→measured is Phase 13 scope, not Phase 12. | Phase 12: `capacity_estimates[]` is a separate array. `full_capacity_ah_ref` unchanged. Expert panel: "Phase 12 only measures and stores, does not replace." |
| **Convergence score, not confidence** | `1 - CoV` is not a statistical confidence interval. Calling it "confidence" misleads. For n<3, CoV is meaningless. | Phase 12: field named `convergence_score`. Returns 0.0 for n<3. Uses population std (÷n, not ÷n-1). May fluctuate — NOT monotonic. |
| **Raw load in discharge buffer** | EMA-filtered load lags real load changes during discharge. Coulomb counting accuracy requires raw data. EMA is fine for real-time display. | Phase 12.1: monitor.py line 676 changed from `self.ema_buffer.load` to raw `ups_data['ups.load']`. Discharge buffer voltages already raw (line 673). |
| **30s minimum for SoH update** | At 3s discharge, Bayesian weight = 0.0035 — negligible signal, but pollutes SoH history with noise entries. 20 flicker-storm events/hour × 52 weeks = 1000+ junk entries degrading replacement predictor regression. | Phase 12.1: kernel `calculate_soh` returns None for < 30s. Orchestrator skips SoH but still increments cycle_count and cumulative_on_battery_sec. |
| **Discharge cooldown 60s** | Power flicker (OB→OL→OB within seconds) is physically one discharge — battery doesn't recover in 5s. Processing as two separate events wastes signal and produces two half-accuracy estimates. | Phase 12.1: if OL duration < 60s, append to existing discharge buffer. Orchestrator change only. |
| **Discharge-mode voltage floor 10.0V** | Global bounds check (8.0-15.0V) too loose during OB — voltage 8.5V is physically impossible (UPS cuts off at 10.5V) but passes bounds. Would corrupt LUT with garbage calibration point. | Phase 12.1: during OB state, skip samples with voltage < 10.0V. Tighter than global 8.0V floor. |

### Known Limitations (Discovered by Expert Panels, Accepted for v2.0)

| Limitation | Severity | Detail | Mitigation |
|------------|----------|--------|------------|
| **EMA systematic bias on SoH** | Low | Adaptive EMA lags voltage by 40-60s during slow discharge. Area-under-curve systematically underestimated by 2-5%. | Discharge buffer uses raw voltage (not EMA), so SoH calculation is unaffected. Bias exists for EMA-derived real-time SoC display only. Documented, no fix needed. |
| **Bayesian SoH inertia at cliff edge** | Medium | VRLA batteries degrade sigmoidally — "fall off cliff" after 2-3 years. Short discharges (failing battery) get low Bayesian weight, so SoH model lags true state. At cliff: true SoH=0.20, model SoH=0.56 after short discharge. | Phase 12.1 year simulation will discover and document severity. Fix candidates: adjust 0.30 weight constant, add rapid-change detector. Deferred to v2.1+. |
| **Thermal bias during high load** | Low | Server exhaust heats UPS by 5-8°C above ambient. VRLA capacity coefficient ≈ -0.5%/°C. At 33°C, capacity is ~4% lower than at 25°C. All high-load discharge measurements systematically underestimate capacity. | Within ±10% accuracy target. Seasonal variation (±3%) within CoV < 10% for convergence_score. No hardware sensor planned for v2.0. |
| **ADC quantization** | Negligible | CyberPower UT850EG: 0.1V voltage resolution, 1% load resolution. Quantization error for coulomb counting: ±0.02% (√N random walk). | Non-issue for capacity estimation. Documented for completeness. |
| **Sulfation recovery** | Info | VRLA can genuinely recover 2-5% capacity after deep discharge (sulfation reversal). SoH may increase. | Model handles naturally — Bayesian update is symmetric, SoH capped at 1.0. No code change needed. Simulation scenario added to verify. |

### Roadmap Evolution

- Phase 12.1 inserted after Phase 12: Math Kernel Extraction & Formula Stability Tests (URGENT). Three expert panels (2026-03-15) shaped scope:
  - **Panel 1** (Electrochemist, Statistician, Architect): circular dependency risk, convergence_score rename, capacity isolation
  - **Panel 2** (Numerical Methods, Functional Architect, Daemon Expert): kernel package, frozen BatteryState, Lyapunov per-iteration, permutation tests, orchestrator wiring
  - **Panel 3** (Metrologist, Adversarial QA, VRLA Lifecycle): raw load in buffer, 30s SoH minimum, discharge cooldown, 5 adversarial scenarios, cliff-edge degradation, instrument characterization
  - Six waves: (1) math kernel package, (2) simulation harness, (3) stability tests, (4) orchestrator wiring, (5) adversarial scenarios, (6) lifecycle & instrument scenarios
  - Full panel transcripts: `docs/reviews/EXPERT-PANEL-2026-03-15-phase12-*.md` (3 files)

### Research Findings (From SUMMARY.md 2026-03-15)

**Critical pitfalls (v2.0 prevents these):**
1. **Coulomb-only without voltage anchor** → ±30% error. Mitigation: Phase 12 uses voltage LUT (10.5V cutoff as zero-error anchor)
2. **Circular dependency (capacity ↔ Peukert)** → oscillation. Mitigation: Peukert fixed at 1.2
3. **Silent SoH rebaseline** → user confusion. Mitigation: Phase 13 logs before/after, shows both for 1 week, clear MOTD messaging
4. **Temperature sensitivity** → winter/summer estimates differ. Mitigation: Accept ±5%, document clearly, flag large jumps
5. **Shallow discharge masquerading as deep** → false confidence. Mitigation: VAL-01 rejects ΔSoC < 25% OR duration < 300s

**Validation gaps (address during Phase 12 planning):**
- **Coulomb error accumulation:** Replay real 2026-03-12 discharge_buffer through estimator → expect <±10% with voltage anchor (vs >±20% without)
- **Convergence threshold:** Monte Carlo on synthetic Gaussian noise (±5% load, ±0.1V voltage) → confirm 2–3 samples → <10% coefficient of variation
- **Load profile sensitivity:** Test Phase 12 implementation across 10–30% load scenarios → validate Peukert ±3% prediction holds

### Phase Derivation Rationale

**Why 3 phases instead of fewer?**
- Phase 12 is independent: works immediately with existing discharge_buffer infrastructure, no blocking dependencies
- Phase 13 **hard-depends** on Phase 12: SoH rebaseline only meaningful after capacity converges (circular otherwise)
- Phase 14 **soft-depends** on Phases 12–13: reporting works best with stable data, but could be deferred if needed

**Why Phase 12 covers validation (VAL-01, VAL-02)?**
- VAL-01 (discharge quality filters) is part of the core algorithm, not optional polish
- VAL-02 (Peukert fixed at 1.2) is an algorithm constraint, implemented in CapacityEstimator

**Why not combine Phase 13 with Phase 12?**
- SoH recalibration logic is orthogonal to capacity estimation
- Premature recalibration (< 3 samples) would corrupt SoH history
- Can be deferred to minor release if needed

**Why Phase 14 separate?**
- Reporting doesn't block core functionality (Phase 12–13)
- Multiple reporting channels (MOTD, journald, Grafana) can be staggered across sub-phases during planning

---

## Planning Guidance

### Expert Panel Involvement

**Phase 12 — CRITICAL expert review needed:**
- Validation gap #1 (coulomb error replay): Domain expert reviews discharge_buffer analysis methodology
- Validation gap #2 (convergence threshold): Statistician reviews Monte Carlo setup, coefficient_of_variation threshold, sample size assumptions
- Validation gap #3 (load sensitivity): Battery engineer assesses Peukert ±3% assumption across 10–30% load profiles
- Algorithm design: CapacityEstimator signature, confidence formula derivation, storage format in model.json

**Phase 13 — moderate expert review:**
- SoH formula review: ensure mathematical separation of capacity from degradation is correct
- New battery detection: is >10% threshold reasonable? How to avoid false positives from measurement noise?
- Rebaseline flow: when does it trigger? On startup (automatic) or only on user confirmation?

**Phase 14 — no expert review required:**
- MOTD + journald are straightforward reporting
- Grafana integration proven in v1.1 codebase

### Key Design Points to Lock During Planning

**Phase 12:**
1. `CapacityEstimator.estimate(V_series, t_series, I_series, LUT) → (ah, confidence, metadata)` signature and algorithm
2. Confidence formula: how to combine depth, count, variance into 0–100% metric
3. Storage format: model.json capacity_estimates array schema (timestamp, Ah, confidence, ΔSoC%, duration, load_avg)
4. Convergence definition: coefficient_of_variation < 10% AND count ≥ 3? Alternative thresholds?
5. Minimum depth/duration: VAL-01 says >25% ΔSoC AND >300s — hard rejects or warnings?

**Phase 13:**
1. New SoH formula: how does `area_measured / (area_reference × measured/rated)` math work?
2. History tagging: which SoH fields include `capacity_ah_ref`? How does regression model filter?
3. Rebaseline trigger: startup (automatic) or confirmation-only?
4. User prompts: exact wording for "New battery installed? [y/n]" flow

**Phase 14:**
1. MOTD format: single line or multi-line? Insert location in motd/51-ups.sh?
2. journald event naming: standardize (capacity_measurement, confidence_update, baseline_lock, baseline_reset)
3. /health endpoint schema: add fields (capacity_ah_measured, capacity_ah_rated, capacity_confidence, capacity_samples_count)
4. Grafana pre-built queries or just expose fields?

---

## Performance Targets

**Baseline (v1.1):**
- 6,596 LOC, 205 tests, 2 days wall-clock
- Zero downtime during operation (discharge_buffer unchanged, model.json already atomic)
- Memory: <50MB daemon

**v2.0 targets:**
- Phase 12: +800–1000 LOC (CapacityEstimator + model.json extension)
- Phase 13: +200–300 LOC (SoH formula + new battery detection)
- Phase 14: +100–150 LOC (MOTD module + /health endpoint)
- Total: ~1200 LOC addition (18–22% growth, manageable)
- Test coverage: +40–60 tests (capacity unit tests, integration, confidence thresholds)
- SSD writes: **unchanged** (one write per discharge event, same as v1.1)
- Memory: +10–20MB (capacity_estimates array is small)
- No new external dependencies (pure Python stdlib: math, statistics, json, datetime)

---

## Expert Review Results (2026-03-15)

**Reviewers:** Dr. Elena Voronova (battery electrochemist), Mikhail Petrov (embedded systems architect)
**Verdict:** APPROVE WITH CHANGES (both)

### Mandatory for Phase 12 planning:
1. **Validation gates in success criteria**: coulomb error <±10% (replay 2026-03-12), Monte Carlo CoV<10% by sample 3 (95% trials), load sensitivity <±3% across 10-30%
2. **IR metadata logging**: compute discharge IR = (V_start - V_end) / I_avg, store alongside Ah estimate (foundation for v3.0)
3. **Peukert as parameter, not hardcode**: CapacityEstimator accepts peukert_exponent, defaults to 1.2
4. **Confidence formula**: lock during design review, not implementation. Define CoV = std/mean, convergence = count≥3 AND CoV<0.10

### Mandatory for Phase 13 planning:
5. **New battery detection is POST-DISCHARGE, not on daemon startup**: compare fresh measurement to stored estimate
6. **model.json backward compat**: decide mutate soh_history entries vs parallel soh_history_v2 array
7. **SoH formula review via expert panel**: confirm mathematical separation of capacity from degradation

### Deferred to v3.0:
- Cell failure detection (voltage curve shape deviation >20%)
- Voltage sensor drift compensation
- Internal resistance trend as leading failure indicator

---

## Known Unknowns & Blockers

**None currently.** Research + expert review complete; sufficient clarity for Phase 12 detailed design.

---

## Session Continuity

When returning to this milestone:
1. Check ROADMAP.md "Phase Details" for Phase 12/13/14 scope
2. Read REQUIREMENTS.md traceability table (which phase owns which requirement)
3. Review "Planning Guidance" section above for expert panel recommendations
4. Check git for recent commits: `git log --oneline v1.1..HEAD`
5. If planning Phase 13, ensure Phase 12 design is finalized first (hard dependency)
6. If stuck on validation gaps, refer to RESEARCH/SUMMARY.md validation gaps section

---

*State created: 2026-03-15*
*Last updated: 2026-03-15 after roadmap creation*
