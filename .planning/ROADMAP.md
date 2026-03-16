# Roadmap: UPS Battery Monitor

## Milestones

- ✅ **v1.0 MVP** — Phases 1-6 (shipped 2026-03-14)
- ✅ **v1.1 Expert Panel Review Fixes** — Phases 7-11 (shipped 2026-03-14)
- ✅ **v2.0 Actual Capacity Estimation (Phase 12)** — Phases 12-14 (Phase 12 shipped 2026-03-16)

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
<summary>🚀 v2.0 Actual Capacity Estimation (Phases 12-14)</summary>

- [x] **Phase 12: Deep Discharge Capacity Estimation** — Coulomb counting + voltage anchor + confidence tracking (4/4 plans + 1 inserted) — completed 2026-03-16
- [x] **Phase 13: SoH Recalibration & New Battery Detection** — Separates capacity from degradation; enables new battery baseline detection (2/2 plans) — completed 2026-03-16
- [ ] **Phase 14: Capacity Reporting & Metrics** — MOTD display, journald logging, Grafana scraping for capacity metrics (3 plans)

</details>

## Phase Details

### Phase 12: Deep Discharge Capacity Estimation

**Goal:** Measure actual battery capacity (Ah) from deep discharge events, accumulate estimates with statistical confidence, and establish measured baseline to replace rated value.

**Depends on:** v1.1 complete (discharge_buffer, voltage LUT, model.json infrastructure)

**Requirements:** CAP-01, CAP-02, CAP-03, CAP-04, CAP-05, VAL-01, VAL-02

**Success Criteria** (what must be TRUE for users when complete):
1. Daemon measures capacity from discharge events (>50% depth-of-discharge) and logs results to model.json with timestamp and confidence score ✓
2. Multiple discharge measurements accumulate via weighted averaging; confidence increases monotonically with each new valid discharge ✓
3. Discharge quality filters reject micro-discharges (<5 min OR <5% ΔSoC) and shallow discharges (<25% ΔSoC), ensuring only valid samples are used ✓
4. User can reset capacity estimation baseline via `--new-battery` flag; daemon detects when measured value differs >10% from previous estimate and prompts for confirmation ✓
5. MOTD shows "Capacity: X.XAh (measured) vs Y.YAh (rated), Z/3 deep discharges, confidence NNth percentile" when data is available ✓

**Plans:**
4/4 plans complete
- [x] 12-01 — Core algorithm: CapacityEstimator class, quality filters, convergence detection
- [x] 12-02 — MonitorDaemon integration + model.json persistence
- [x] 12-03 — Validation gates + MOTD display
- [x] 12-04 — CLI flag integration (gap closure: CAP-05 user signal mechanism)

**Verification:** All 7 requirements satisfied, 295 tests passing (100%), 3 validation gates closed.

---

### Phase 12.1: Math Kernel Extraction & Formula Stability Tests (INSERTED)

**Goal:** Extract all battery math into a pure-function kernel package (`src/battery_math/`), build a year-long simulation harness over it, and prove the formula system is mathematically stable before Phase 12 adds capacity estimation. Establishes architectural invariant: kernel knows nothing about daemon, daemon knows nothing about math, simulator and daemon are two equal orchestrators of one kernel.

**Depends on:** v1.1 complete (existing formula chain must exist to refactor)

**Requirements:** VAL-01, VAL-02 (stability validation prerequisites)

**Plans:** 6/6 plans complete

---

### Phase 13: SoH Recalibration & New Battery Detection

**Goal:** Separate capacity degradation from battery aging; recalibrate SoH formula and history when measured capacity converges; detect new batteries installed by user.

**Depends on:** Phase 12 (measured capacity must converge before SoH rebaseline is meaningful)

**Requirements:** SOH-01, SOH-02, SOH-03

**Success Criteria** (what must be TRUE for users when complete):
1. SoH formula normalizes against measured capacity instead of rated when available, separating aging from capacity loss ✓
2. SoH history entries tag their capacity_ah_ref baseline; regression model for replacement date ignores entries from different baselines (no mixing old vs. new battery data) ✓
3. Post-discharge, daemon detects new battery by comparing measured capacity to stored baseline; if difference >10%, sets new_battery_detected flag ✓
4. When user confirms new battery via `--new-battery` flag, baseline resets; daemon logs "New battery event: capacity_ref reset from X.XAh to Y.YAh" with timestamp ✓
5. MOTD clearly shows new battery detection alert and SoH recalibration event ✓

**Plans:**
2/2 plans complete
- [x] 13-01 — SoH formula normalization (SOH-01), history versioning (SOH-02), regression filtering (SOH-03)
- [x] 13-02 — New battery detection post-discharge (>10% threshold), baseline reset on CLI flag, MOTD alert

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

**Plans:**
3 plans
- [ ] 14-01 — MOTD capacity display with convergence status badge and confidence % (RPT-01)
- [ ] 14-02 — Structured journald logging for capacity events with EVENT_TYPE tagging (RPT-02)
- [ ] 14-03 — Health endpoint extension with capacity metrics for Grafana scraping (RPT-03)

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
| 12.1 Math Kernel & Stability Tests | v2.0 | 6/6 | Complete    | 2026-03-16 |
| 12. Deep Discharge Capacity Estimation | v2.0 | 4/4 | Complete    | 2026-03-16 |
| 13. SoH Recalibration & New Battery | v2.0 | 2/2 | Complete    | 2026-03-16 |
| 14. Capacity Reporting & Metrics | v2.0 | 0/3 | Planning    | — |

---

*Roadmap updated: 2026-03-16 after Phase 14 planning*
