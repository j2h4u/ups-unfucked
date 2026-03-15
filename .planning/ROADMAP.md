# Roadmap: UPS Battery Monitor

## Milestones

- ✅ **v1.0 MVP** — Phases 1-6 (shipped 2026-03-14)
- ✅ **v1.1 Expert Panel Review Fixes** — Phases 7-11 (shipped 2026-03-14)
- 🚀 **v2.0 Actual Capacity Estimation** — Phases 12-14 (in planning)

## Phases

<details>
<summary>✅ v1.0 MVP (Phases 1-6) — SHIPPED 2026-03-14</summary>

- [x] Phase 1: Foundation — NUT Integration & Core Infrastructure (5/5 plans) — completed 2026-03-13
- [x] Phase 2: Battery Model — State Estimation & Event Classification (6/6 plans) — completed 2026-03-14
- [x] Phase 3: Virtual UPS & Safe Shutdown (4/4 plans) — completed 2026-03-14
- [x] Phase 4: Health Monitoring & Battery Degradation (2/2 plans) — completed 2026-03-14
- [x] Phase 5: Operational Setup & Systemd Integration (2/2 plans) — completed 2026-03-14
- [x] Phase 6: Calibration Mode (2/2 plans) — completed 2026-03-14

</details>

<details>
<summary>✅ v1.1 Expert Panel Review Fixes (Phases 7-11) — SHIPPED 2026-03-14</summary>

- [x] Phase 7: Safety-Critical Metrics (2 plans) — completed 2026-03-14
- [x] Phase 8: Architecture Foundation (4 plans) — completed 2026-03-15
- [x] Phase 9: Test Coverage (3 plans) — completed 2026-03-14
- [x] Phase 10: Code Quality & Efficiency (2 plans) — completed 2026-03-14
- [x] Phase 11: Polish & Future Prep (3 plans) — completed 2026-03-14

</details>

<details open>
<summary>🚀 v2.0 Actual Capacity Estimation (Phases 12-14) — IN PLANNING</summary>

- [ ] **Phase 12: Deep Discharge Capacity Estimation** — Coulomb counting + voltage anchor + confidence tracking; implements core measurement algorithm (3/3 plans)
- [ ] **Phase 13: SoH Recalibration & New Battery Detection** — Separates capacity from degradation; enables new battery baseline detection
- [ ] **Phase 14: Capacity Reporting & Metrics** — MOTD display, journald logging, Grafana scraping for capacity metrics

</details>

## Phase Details

### Phase 12: Deep Discharge Capacity Estimation

**Goal:** Measure actual battery capacity (Ah) from deep discharge events, accumulate estimates with statistical confidence, and establish measured baseline to replace rated value.

**Depends on:** v1.1 complete (discharge_buffer, voltage LUT, model.json infrastructure)

**Requirements:** CAP-01, CAP-02, CAP-03, CAP-04, CAP-05, VAL-01, VAL-02

**Success Criteria** (what must be TRUE for users when complete):
1. Daemon measures capacity from discharge events (>50% depth-of-discharge) and logs results to model.json with timestamp and confidence score
2. Multiple discharge measurements accumulate via weighted averaging; confidence increases monotonically with each new valid discharge
3. Discharge quality filters reject micro-discharges (<5 min OR <5% ΔSoC) and shallow discharges (<25% ΔSoC), ensuring only valid samples are used
4. User can reset capacity estimation baseline via `--new-battery` flag; daemon detects when measured value differs >10% from previous estimate and prompts for confirmation
5. MOTD shows "Capacity: X.XAh (measured) vs Y.YAh (rated), Z/3 deep discharges, confidence NNth percentile" when data is available

**Plans:**
- [x] 12-01 — CapacityEstimator core algorithm + unit tests (Wave 1)
- [x] 12-02 — MonitorDaemon integration + model.json persistence (Wave 2)
- [x] 12-03 — Validation gates + MOTD display (Wave 3)

---

### Phase 12.1: Math Kernel Extraction & Formula Stability Tests (INSERTED)

**Goal:** Extract all battery math into a pure-function kernel package (`src/battery_math/`), build a year-long simulation harness over it, and prove the formula system is mathematically stable before Phase 12 adds capacity estimation. Establishes architectural invariant: kernel knows nothing about daemon, daemon knows nothing about math, simulator and daemon are two equal orchestrators of one kernel.

**Depends on:** v1.1 complete (existing formula chain must exist to refactor)

**Requirements:** VAL-01, VAL-02 (stability validation prerequisites)

**Success Criteria** (what must be TRUE for users when complete):

*Wave 1 — Math Kernel Package:*
1. `src/battery_math/` package exists with focused modules: `peukert.py` (runtime formulas, stable physics), `soh.py` (SoH calculation, evolving statistics), `soc.py` (voltage→SoC interpolation), `calibration.py` (Peukert calibration, extracted from monitor.py), `capacity.py` (placeholder for Phase 12), `types.py` (frozen BatteryState dataclass). Re-exports via `__init__.py` — callers use single import path.
2. `types.py` defines `BatteryState(frozen=True)` dataclass: `soh`, `peukert_exponent`, `capacity_ah_rated`, `capacity_ah_measured`, `lut` (as immutable tuple), `cycle_count`, `cumulative_on_battery_sec`. Every kernel function takes/returns frozen BatteryState — circular dependencies become structurally visible at type level.
3. Each function is pure: all inputs as arguments, returns result, no access to model/self/globals, no I/O, no logging — only `math`/`statistics` stdlib. `_weighted_average_by_voltage` gets `current_time: float = None` parameter (defaults to `time.time()`) instead of calling `time.time()` internally — enables simulation without mocking.
4. `_auto_calibrate_peukert()` in monitor.py refactored: math extracted to `battery_math.calibrate_peukert(actual_duration_sec, avg_load_percent, current_soh, capacity_ah, current_exponent, ...) → float | None`. Guard clauses (< 2 samples, < 60s, invalid load) stay in monitor.py — they're about data availability, not math. Monitor.py applies result and handles logging/persistence (orchestrator pattern).
5. `interpolate_cliff_region` becomes pure `lut → lut` transform in kernel. Orchestrator calls it and writes result to model.
6. All existing tests pass after extraction (zero behavior change, pure refactor). Call ordering within `_update_battery_health` preserved exactly (SoH → Peukert, not reversed).

*Wave 2 — Simulation Harness:*
7. `tests/test_year_simulation.py` exists: configurable year simulation (blackout frequency 1-5/week, depth distribution uniform/bimodal, load profile 10-40%, battery degradation rate 1-3%/month) runs full formula chain through 365 days of synthetic events in < 30 seconds.
8. Simulation uses `battery_math` functions directly — no MonitorDaemon, no model.json, no I/O. State is a `BatteryState` frozen dataclass passed through pure functions, new state returned after each discharge event.
9. Simulation runs with 5 different random seeds; results at same iteration count agree within 1%. If not — noise model dominates signal and must be tuned before stability tests are meaningful.

*Wave 3 — Stability Tests:*
10. Fixed-point convergence: 20 identical discharges → SoH, Peukert, capacity all converge (range over last 5 < 1% of mean).
11. Per-iteration Lyapunov stability: ±1% capacity_ah perturbation → compute `divergence[i] / divergence[i-1]` for each step → ratio < 1.0 for all iterations after warmup period (first 3). This is the primary stability gate. Secondary smoke test: aggregate divergence after 10 iterations < ±3%.
12. Permutation test: 10 discharge events of varying depth (20%, 40%, 60%, 80%) run in 5 random orderings → final state (SoH, Peukert) agrees within ±2% regardless of ordering. Catches path-dependent bias in Bayesian SoH blending.
13. Year simulation invariants: SoH ∈ [0.60, 1.0], Peukert ∈ [1.0, 1.4], no parameter oscillation (max-min over last 10 iterations < 5% of mean).
14. Degradation scenario: battery degrades 2%/month for 12 months → SoH reaches ~0.76, Peukert drifts < 0.05, no divergence.

*Wave 4 — Orchestrator Wiring Tests:*
15. 3+ orchestrator-level tests that verify monitor.py passes correct arguments to kernel functions: (a) `capacity_ah` passed to SoH calculator is `full_capacity_ah_ref` (rated), never measured; (b) `avg_load` passed to Peukert calibration is discharge-average, not current poll value; (c) `calibrate_peukert` result applied to model only when non-None.
16. Call-order test: assert SoH updates *before* Peukert calibration within `_update_battery_health` (the new SoH feeds into `current_soh` parameter of calibration — reversing order changes math).
17. Systemd watchdog survival: daemon runs 10 poll cycles under systemd after refactoring without watchdog kill.

*Wave 5 — Adversarial Simulation Scenarios (from Panel 3: QA):*
18. Flicker storm: 20 × 3-second OB/OL transitions in 1 hour → assert SoH unchanged (weight too low to matter), cycle_count increments correctly, SoH history not polluted with noise entries.
19. Interrupted discharge: 300s OB → 5s OL → 295s OB → assert combined capacity estimate within ±5% of single 600s discharge. Tests discharge cooldown logic (60s OL threshold before clearing buffer).
20. Voltage spike: one poll at 8.5V during normal OB discharge → LUT not corrupted, sample skipped. Tests tightened discharge-mode voltage floor (10.0V during OB).
21. Stale ADC: 50 seconds of identical 12.4V readings during OB → SoH not inflated, discharge flagged as suspect.
22. NTP timestamp jump: timestamp decreases by 2 seconds mid-discharge → integration handles gracefully (skip negative dt intervals).

*Wave 6 — Lifecycle & Instrument Scenarios (from Panel 3: Battery Expert + Metrologist):*
23. Cliff-edge degradation: months 1-24 at 1.5%/month, months 25-30 at 5%/month, months 31-33 at 15%/month → assert model SoH tracks true SoH within ±15% at all times. **Expected: may fail with current Bayesian blending params** — that's a documented finding, not a bug to fix in this phase. Log result to STATE.md.
24. Sulfation recovery: after 4 weeks without discharge, battery recovers 3% capacity → SoH increases smoothly from 0.85 to ~0.88, no guards triggered, no clamping artifacts.
25. Seasonal thermal variation: "summer months" have 3% lower true capacity than "winter months" → convergence_score still works (seasonal variation within CoV < 10%), capacity estimates reflect seasonal pattern without divergence.
26. Instrument characterization: same synthetic discharge fed through two paths — (a) raw values, (b) values quantized to 0.1V/1% load then EMA-filtered → compare SoH and capacity deltas. Documents systematic instrument bias. If SoH delta > 3%, add note to STATE.md.

*Wave 5-6 Orchestrator Fixes (zero behavior change for existing tests):*
27. Discharge buffer stores raw `ups.load` instead of EMA-filtered load (monitor.py line 676). One-line change — improves coulomb counting accuracy for variable-load discharges.
28. Minimum 30s discharge for SoH update: kernel `calculate_soh` returns None for duration < 30s. Orchestrator skips SoH update but still increments cycle_count and cumulative_on_battery_sec. Prevents flicker-storm pollution of SoH history.
29. Discharge cooldown: 60s OL before clearing discharge buffer. If OB→OL→OB within 60s, treat as continuation of same discharge event. Orchestrator-level change only.

**Plans:** TBD

---

### Phase 13: SoH Recalibration & New Battery Detection

**Goal:** Separate capacity degradation from battery aging; recalibrate SoH formula and history when measured capacity converges; detect new batteries installed by user.

**Depends on:** Phase 12 (measured capacity must converge before SoH rebaseline is meaningful)

**Requirements:** SOH-01, SOH-02, SOH-03

**Success Criteria** (what must be TRUE for users when complete):
1. SoH formula normalizes against measured capacity instead of rated when available, separating aging from capacity loss
2. SoH history entries tag their capacity_ah_ref baseline; regression model for replacement date ignores entries from different baselines (no mixing old vs. new battery data)
3. On startup, daemon detects new battery by comparing stored capacity estimate to current discharge runtime estimate; if difference >10%, prompts user with "New battery installed? [y/n]"
4. When user confirms new battery, baseline resets; daemon logs "New battery event: capacity_ref reset from X.XAh to current discharge measurement" with timestamp
5. MOTD clearly shows SoH recalibration event with before/after values and explanation that aging clock has been reset

**Plans:** TBD

---

### Phase 14: Capacity Reporting & Metrics

**Goal:** Expose capacity estimation to user and monitoring systems via MOTD, journald, and Grafana metrics.

**Depends on:** Phase 12 (capacity measurement) and Phase 13 (SoH recalibration) complete; reporting depends on stable data

**Requirements:** RPT-01, RPT-02, RPT-03

**Success Criteria** (what must be TRUE for users when complete):
1. MOTD module displays "Capacity: X.XAh (measured) vs Y.YAh (rated), confidence NN%, 2/3 deep discharges" on every login
2. Journald logs are searchable (`journalctl -t ups-battery-monitor | grep capacity`) with events: "capacity_measurement", "confidence_update", "baseline_lock", "baseline_reset"
3. `/health` endpoint exposes capacity_ah_measured, capacity_ah_rated, capacity_confidence, capacity_samples_count fields in JSON for Grafana scraping
4. Grafana dashboard (or pre-built query) shows capacity convergence over time (scatter: discharge_date vs estimated_Ah, confidence band) and SoH trend (line: date vs SoH%)
5. User can identify when capacity is still uncertain (2 samples, 60% confidence) vs. locked (3+ deep discharges, 95%+ confidence) through MOTD/Grafana combination

**Plans:** TBD

---

## Progress

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Foundation | v1.0 | 5/5 | Complete | 2026-03-13 |
| 2. Battery Model | v1.0 | 6/6 | Complete | 2026-03-14 |
| 3. Virtual UPS | v1.0 | 4/4 | Complete | 2026-03-14 |
| 4. Health Monitoring | v1.0 | 2/2 | Complete | 2026-03-14 |
| 5. Operational Setup | v1.0 | 2/2 | Complete | 2026-03-14 |
| 6. Calibration Mode | v1.0 | 2/2 | Complete | 2026-03-14 |
| 7. Safety-Critical Metrics | v1.1 | 2/2 | Complete | 2026-03-14 |
| 8. Architecture Foundation | v1.1 | 4/4 | Complete | 2026-03-15 |
| 9. Test Coverage | v1.1 | 3/3 | Complete | 2026-03-14 |
| 10. Code Quality & Efficiency | v1.1 | 2/2 | Complete | 2026-03-14 |
| 11. Polish & Future Prep | v1.1 | 3/3 | Complete | 2026-03-14 |
| 12. Deep Discharge Capacity Estimation | v2.0 | 3/3 | Planned | — |
| 13. SoH Recalibration & New Battery | v2.0 | 0/TBD | Not started | — |
| 14. Capacity Reporting & Metrics | v2.0 | 0/TBD | Not started | — |

---

*Roadmap updated: 2026-03-15 after Phase 12 planning complete*
