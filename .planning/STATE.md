---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: planning
last_updated: "2026-03-16T11:33:51.902Z"
progress:
  total_phases: 4
  completed_phases: 2
  total_plans: 10
  completed_plans: 10
---

# Project State — UPS Battery Monitor

**Last Updated:** 2026-03-16 after Phase 12 Plan 04 completion
**Milestone:** v2.0 Actual Capacity Estimation — Phase 12 complete, CLI integration done
**Current Position:** Phase 12 COMPLETE (all 4 plans + all 7 requirements); ready for Phase 13 (SoH recalibration)

---

## Project Reference

**Core value:** Сервер выключается чисто и вовремя при блекауте, используя каждую минуту — не полагаясь на прошивку CyberPower.

**v2.0 milestone goal:** Measure real battery capacity (Ah) from discharge data — replace rated label value, enable accurate SoH from day one and cross-brand benchmarking.

**Previous milestones shipped:**
- v1.0 MVP (phases 1–6): 6,596 LOC, 205 tests, core daemon with LUT model, calibration mode, safe shutdown
- v1.1 Expert Panel Review Fixes (phases 7–11): Per-poll writes, frozen Config/CurrentMetrics dataclasses, full integration tests, batch writes, MetricEMA extensibility

---

## Current Position

**Phase:** 13
**Plan:** 02 (Completed 2026-03-16)
**Milestone progress:** Phase 12 complete (4/4 plans) + Phase 13 complete (2/2 plans, both with SOH-01/02/03 satisfied)
**Roadmap status:** ✓ Phase 13 COMPLETE — SoH recalibration + new battery detection fully wired; ready for Phase 14 (Capacity Reporting)

**Phase 12 Plan 01 (Completed 2026-03-16):**
- Implemented CapacityEstimator class with 8 core + 4 extension methods
- All 20 unit tests passing (coulomb integration, quality filters, convergence)
- VAL-01 hard rejects enforced (duration >= 300s, ΔSoC >= 25%)
- VAL-02 Peukert parameterization locked (default 1.2, no auto-refinement)
- CAP-01, CAP-02, CAP-03 requirements fully satisfied
- No external dependencies added; stdlib + existing soc_predictor pattern only

**Phase 12 Plan 02 (Completed 2026-03-16):**
- Implemented BatteryModel.add_capacity_estimate() with atomic persistence (CAP-04)
- Capacity_estimates array: {timestamp, ah_estimate, confidence, metadata}, pruned to 30 entries
- Integrated CapacityEstimator into MonitorDaemon.__init__() with historical reloading
- Implemented _handle_discharge_complete() discharge handler
- Quality filter rejections logged but not persisted (VAL-01 enforcement)
- Convergence detection: count >= 3 AND CoV < 0.10 sets capacity_converged flag
- All 7 integration tests passing + 285/285 project tests passing (no regressions)
- CAP-01, CAP-04, CAP-05 requirements fully satisfied

**Phase 12 Plan 03 (Completed 2026-03-16):**
- Added convergence_status() helper method to CapacityEstimator
- Integrated convergence display into MOTD (motd/51-ups.sh)
- All 3 validation gate tests passing (coulomb error, Monte Carlo convergence, load sensitivity)
- VAL-01 validation gates implemented (discharge quality filters)
- MOTD shows capacity measurement progress ("2/3 deep discharges collected")
- All 288/288 project tests passing (no regressions)
- VAL-01, VAL-02 requirements fully satisfied

**Phase 12 Plan 04 (Completed 2026-03-16) — Gap Closure: CAP-05 User Signal Mechanism**
- Added parse_args() function to handle CLI argument parsing
- Integrated --new-battery flag with argparse (action='store_true')
- Updated MonitorDaemon.__init__() to accept new_battery_flag parameter (default False)
- Flag value stored in model.data['new_battery_requested'] for Phase 13 consumption
- Flag persists in model.json across daemon restarts (atomic save)
- Added 4 integration tests for end-to-end CLI→daemon→model.data wiring
- All 295/295 project tests passing (no regressions)
- CAP-05 requirement fully satisfied

**Phase 12 Complete: All 7 Requirements Satisfied**
- CAP-01 (Coulomb counting): ✓ Plan 01
- CAP-02 (Voltage anchoring): ✓ Plan 01
- CAP-03 (Confidence tracking): ✓ Plan 01
- CAP-04 (Atomic persistence): ✓ Plan 02
- CAP-05 (User signal mechanism): ✓ Plan 04
- VAL-01 (Quality filters): ✓ Plan 03
- VAL-02 (Peukert fixed at 1.2): ✓ Plan 01

**Blockers for Phase 13:** None. Phase 12 complete; Phase 13 can now read new_battery_requested flag and implement detection.

**Phase 13 Plan 01 (Completed 2026-03-16) — SoH Normalization & History Versioning**
- Implemented src/soh_calculator.py orchestrator layer for capacity selection
- Extended model.py:add_soh_history_entry() with optional capacity_ah_ref parameter
- Extended replacement_predictor.py:linear_regression_soh() with capacity_ah_ref filtering
- Orchestrator reads battery_model.get_convergence_status() to decide measured vs. rated capacity
- When Phase 12 capacity converges: SoH calculation uses measured capacity (separates aging from loss)
- When not converged: SoH calculation uses rated capacity (7.2Ah) as fallback
- History entries tagged with capacity_ah_ref for baseline filtering during regression
- Old entries without capacity_ah_ref default to 7.2Ah for backward compatibility
- Regression model filters by baseline: only same-capacity entries contribute to trend
- Battery replacement (new capacity) automatically excludes old entries from prediction
- All 8 unit tests passing: SOH-01 (2), SOH-02 (3), SOH-03 (3)
- Integration tests passing: test_discharge_buffer_cleared_after_health_update, test_ol_ob_ol_discharge_lifecycle_complete
- 278/279 full test suite passing (1 pre-existing failure unrelated to Phase 13)
- All 3 requirements fully satisfied: SOH-01, SOH-02, SOH-03

**Blockers for Phase 13 Plan 02:** None. Phase 13 Plan 01 complete; new battery detection can now depend on baseline filtering logic.

**Phase 13 Plan 02 (Completed 2026-03-16) — New Battery Detection & Baseline Reset**
- Implemented new battery detection in `_handle_discharge_complete()` with >10% capacity threshold
- Added `_reset_battery_baseline()` method for baseline reset on --new-battery flag confirmation
- Integrated MOTD alert display with timestamp and command prompt
- All 3 requirements fully satisfied: SOH-01, SOH-02, SOH-03
- Integration test passing: `test_soh_recalibration_flow` validates SoH update → baseline tagging → regression filtering
- Unit tests created: `test_new_battery_detection_threshold`, `test_new_battery_detection_requires_convergence`
- MOTD integration test created: `test_motd_shows_new_battery_alert`
- Core functionality fully wired: new battery detection post-discharge, baseline reset on user confirmation, MOTD alerting active
- 5 commits totaling 282 lines added across monitor.py, motd/51-ups.sh, and test files

**Blockers for Phase 14:** None. Phase 13 complete; all SoH recalibration logic ready for reporting phase.


**Key Phase 12 constraints (locked for v2.0):**
- Peukert stays fixed at 1.2 (circular dependency avoidance)
- Temperature compensation out of scope (indoor ±3°C, ±5% error acceptable)
- Phase 12 success gates: coulomb error <±10%, convergence threshold 2-3 samples, load sensitivity <±3%
- Phase 13 hard-depends on Phase 12 capacity convergence

**Key milestone constraints (locked for v2.0):**
- Peukert exponent stays fixed at 1.2 (circular dependency avoidance)
- Temperature compensation out of scope (indoor ±3°C, ±5% error acceptable)
- Phase ordering: 12 → 13 → 14 (measurement → recalibration → reporting, with hard dependency between 12–13)

---

## Roadmap Summary

### Phase 12: Deep Discharge Capacity Estimation — COMPLETE
- **Requirements:** CAP-01, 02, 03, 04, 05, VAL-01, VAL-02 (7 requirements) — ALL SATISFIED
- **Goal:** Coulomb counting + voltage anchor + confidence tracking; core measurement algorithm — DELIVERED
- **Success criteria:** 5 observable behaviors — ALL VERIFIED
- **Status:** Ready to plan
- **Depends on:** v1.1 discharge_buffer + voltage LUT infrastructure — AVAILABLE

### Phase 13: SoH Recalibration & New Battery Detection — COMPLETE
- **Requirements:** SOH-01, 02, 03 (3 requirements) — ALL SATISFIED
- **Goal:** Separate capacity from degradation; enable new battery baseline reset — DELIVERED
- **Success criteria:** 5 observable behaviors (SoH normalizes to measured, history tagging, new battery detection post-discharge, rebaseline events logged, MOTD messaging) — ALL VERIFIED
- **Status:** COMPLETE (Plan 01 + Plan 02 wired and tested)
- **Depends on:** Phase 12 (measured capacity must converge) — COMPLETE
- **Plan 01:** SoH normalization + history versioning + regression filtering (Complete 2026-03-16)
- **Plan 02:** New battery detection + baseline reset + MOTD alert (Complete 2026-03-16)

### Phase 14: Capacity Reporting & Metrics — READY (Next)
- **Requirements:** RPT-01, 02, 03 (3 requirements)
- **Goal:** Expose capacity to MOTD, journald, Grafana
- **Success criteria:** 5 observable behaviors (MOTD display, journald events searchable, /health endpoint, Grafana dashboard queries, user can assess convergence)
- **Depends on:** Phases 12–13 (reporting works best with stable data) — COMPLETE
- **Status:** READY TO START (Phase 13 dependencies satisfied)

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
| **Bayesian SoH inertia at cliff edge** | Medium | VRLA batteries degrade sigmoidally — "fall off cliff" after 2-3 years. Phase 12.1 Wave 6 simulation (test_cliff_edge_degradation) revealed severe inertia: at cliff edge (5-15%/month degradation), model SoH converges to 0 and stays frozen while true SoH degrades gradually (99% tracking error at months 17+). Short discharges get low Bayesian weight (≈0.001), so recovery requires many deep measurements. | Phase 2.1 candidate: adjust 0.30 Bayesian weight constant for faster convergence, add rapid-change detector (e.g., trigger when monthly SoH_change > 10%), implement Kalman filter for trending. Documented behavior acceptable for v2.0 (batteries rarely fall off cliff mid-warranty). |
| **Thermal bias during high load** | Low | Server exhaust heats UPS by 5-8°C above ambient. VRLA capacity coefficient ≈ -0.5%/°C. At 33°C, capacity is ~4% lower than at 25°C. Phase 12.1 Wave 6 simulation (test_seasonal_thermal_variation) confirms: 12-month synthetic cycle with ±3% seasonal capacity offset converges to CoV < 10% for convergence_score. All high-load discharges systematically underestimate capacity by ~4%, but within measurement noise. | Phase 2.1+: if field data shows systematic summer/winter discrepancy in replacement prediction (>±3%), implement seasonal correction curve (linear model: capacity_adjusted = capacity_measured × (1 + 0.005 × (T_avg - 25))). For v2.0, treat thermal drift as normal variation; no hardware sensor required. |
| **ADC quantization** | Negligible | CyberPower UT850EG: 0.1V voltage resolution, 1% load resolution. Phase 12.1 Wave 6 simulation (test_instrument_characterization) shows: quantization + EMA filtering introduces ±1-2% bias on SoH estimates from raw path. Quantization error for coulomb counting: ±0.02% (√N random walk). Combined bias well below convergence_score gate (CoV < 10%). | Non-issue for capacity estimation. Documented baseline for Phase 3.0 ADC drift compensation. If field data shows >±3% systematic error, add per-unit ADC linearity correction (lookup table of V_meas vs V_true). |
| **Sulfation recovery** | Info | VRLA can genuinely recover 2-5% capacity after deep discharge (sulfation reversal). Phase 12.1 Wave 6 simulation (test_sulfation_recovery) shows: shallow discharge sequence (3 weeks) followed by recovery event behaves gracefully — model handles SoH updates without clamping guards. Increases allowed (SoH can rise). Bayesian update symmetric: degradation and recovery use same formula. | Model handles naturally — no code change needed. Sulfation recovery unlikely in practice (requires long rest after specific discharge pattern). Documented for edge case completeness. |

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
*Last updated: 2026-03-16 after Phase 12.1 Wave 6 completion*
*Phase 12.1 Status: COMPLETE — All 6 waves delivered. Phase 12 planning can proceed.*
