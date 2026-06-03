# Roadmap: UPS Battery Monitor

## Milestones

- ✅ **v1.0 MVP** — Phases 1-6 (shipped 2026-03-14)
- ✅ **v1.1 Expert Panel Review Fixes** — Phases 7-11 (shipped 2026-03-14)
- ✅ **v2.0 Actual Capacity Estimation** — Phases 12-14 (shipped 2026-03-16)
- ✅ **v3.0 Active Battery Care** — Phases 15-17 (shipped 2026-03-20)
- ✅ **v3.1 Code Quality Hardening** — Phases 18-24 (shipped 2026-03-21)
- 🔵 **v3.2 Honest Monitoring & Diagnostic Verification** — Phases 25-27 (ACTIVE, started 2026-06-03)

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

<details>
<summary>✅ v2.0 Actual Capacity Estimation (Phases 12-14) — SHIPPED 2026-03-16</summary>

- [x] Phase 12: Deep Discharge Capacity Estimation (4/4 plans) — completed 2026-03-16
- [x] Phase 12.1: Math Kernel Extraction & Formula Stability Tests (6/6 plans, INSERTED) — completed 2026-03-16
- [x] Phase 13: SoH Recalibration & New Battery Detection (2/2 plans) — completed 2026-03-16
- [x] Phase 14: Capacity Reporting & Metrics (3/3 plans) — completed 2026-03-16

</details>

<details>
<summary>✅ v3.0 Active Battery Care (Phases 15-17) — SHIPPED 2026-03-20</summary>

- [x] Phase 15: Foundation — Sulfation model, cycle ROI, NUT INSTCMD (5/5 plans) — completed 2026-03-17
- [x] Phase 16: Persistence & Observability — health.json, journald, MOTD (6/6 plans) — completed 2026-03-17
- [x] Phase 17: Scheduling Intelligence — daemon-controlled test scheduling (2/2 plans) — completed 2026-03-17

</details>

<details>
<summary>✅ v3.1 Code Quality Hardening (Phases 18-24) — SHIPPED 2026-03-21</summary>

- [x] Phase 18: Unify Coulomb Counting — single integrate_current() (1/1 plans) — completed 2026-03-20
- [x] Phase 19: Extract SagTracker — own module (1/1 plans) — completed 2026-03-20
- [x] Phase 20: Extract SchedulerManager — own module (1/1 plans) — completed 2026-03-20
- [x] Phase 21: Extract DischargeCollector — own module + sulfation split (2/2 plans) — completed 2026-03-20
- [x] Phase 22: Naming + Docs Sweep — renames + docstrings (2/2 plans) — completed 2026-03-20
- [x] Phase 23: Test Quality Rewrite — outcome assertions + DI (4/4 plans) — completed 2026-03-20
- [x] Phase 24: Temperature + Security Hardening — NUT probe, validation, auth docs (2/2 plans) — completed 2026-03-21

</details>

### 🔵 v3.2 Honest Monitoring & Diagnostic Verification (Phases 25-27) — ACTIVE

**Milestone goal:** Retract the v3.0 "active desulfation via scheduled deep discharges" mechanism (premise disproven — discharge *forms* sulfate, charging reverses it, and the daemon has no charge control on CyberPower), reframe the scheduler to rare diagnostic capacity verification, fix the scheduler bootstrap deadlock, and correct the docs. Single-host, fail-fast, no backward-compat (state regenerates).

- [ ] **Phase 25: Scheduler Reframe + Catch-22 Fix** — diagnostic capacity verification on a persistent time trigger (SCH-01..03)
- [ ] **Phase 26: Desulfation Retraction** — delete sulfation/cycle_roi code, wiring, fields, and their tests (RET-01..04)
- [ ] **Phase 27: Docs, ADR & Operator Report** — correct premise claims, write ADR, add maintenance schedule to battery-health.py (DOC-01..02, RPT-01)

#### Phase 25: Scheduler Reframe + Catch-22 Fix
**Goal**: `evaluate_test_scheduling` proposes a rare diagnostic capacity/SoH verification on a persistent time-based cadence, with safety gates intact and no restart re-trigger — making the daemon's behavior correct before the old desulfation drivers are deleted.
**Depends on**: Nothing (first phase of v3.2; reframe before delete)
**Requirements**: SCH-01, SCH-02, SCH-03
**Success Criteria** (what must be TRUE):
  1. The scheduler proposes tests on a simple ≈365-day IEEE-1188-style time cadence, with zero dependency on a sulfation score or cycle-ROI input.
  2. With only short blackouts and no prior test, the daemon still schedules a first diagnostic test — the bootstrap deadlock is gone.
  3. The trigger reads the persistent `last_upscmd_timestamp` from model.json; a daemon restart does not re-trigger a test.
  4. Safety gates hold (SoH floor ≥60%, grid-stability cooldown, rate-limit) and the default/first test is `quick`.
**Plans**: TBD

#### Phase 26: Desulfation Retraction
**Goal**: All disproven-premise desulfation machinery — `sulfation.py`, `cycle_roi.py`, their production wiring, the `sulfation_score`/`cycle_roi` fields, and the tests that exercise them — is removed, leaving the codebase vulture- and grep-clean.
**Depends on**: Phase 25 (scheduler no longer references sulfation/ROI before deletion)
**Requirements**: RET-01, RET-02, RET-03, RET-04
**Success Criteria** (what must be TRUE):
  1. `src/battery_math/sulfation.py` and `src/battery_math/cycle_roi.py` (and all callers) are gone; grep for `sulfation`/`cycle_roi` is clean across src/tests/scripts.
  2. Blackout-credit-as-desulfation and recovery_delta-as-desulfation-evidence logic is removed from the discharge path and scheduler.
  3. `sulfation_score`/`cycle_roi` (and sulfation-confidence/recovery) fields are absent from the health endpoint, model.json schema/validation, MOTD, and journald discharge events.
  4. The tests covering the deleted code are removed; full pytest is green and vulture/ruff/pyright are clean.
**Plans**: TBD

#### Phase 27: Docs, ADR & Operator Report
**Goal**: The project documentation reflects honest monitoring (no active-desulfation claims), an ADR records the premise reversal and the no-charge-control fact, and operators can read the maintenance schedule from `battery-health.py`.
**Depends on**: Phase 26 (docs describe the shipped, retracted state)
**Requirements**: DOC-01, DOC-02, RPT-01
**Success Criteria** (what must be TRUE):
  1. README.md and ROADMAP/MILESTONES contain no "actively fights sulfation / extends life via desulfation" claims; replaced with "honest monitoring + periodic diagnostic capacity verification."
  2. An ADR exists recording the premise reversal, the evidence (BU-804b, Vertiv BattCon, IEEE-1188, etc.), and the no-charge-control fact.
  3. `scripts/battery-health.py` shows a "Maintenance & schedule" section (next diagnostic test, last test run, IR trend, capacity/SoH), reading model.json + the health endpoint without JSON hand-parsing.
**Plans**: TBD


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
| 12. Deep Discharge Capacity Estimation | v2.0 | 4/4 | Complete | 2026-03-16 |
| 12.1 Math Kernel & Stability Tests | v2.0 | 6/6 | Complete | 2026-03-16 |
| 13. SoH Recalibration & New Battery | v2.0 | 2/2 | Complete | 2026-03-16 |
| 14. Capacity Reporting & Metrics | v2.0 | 3/3 | Complete | 2026-03-16 |
| 15. Foundation | v3.0 | 5/5 | Complete | 2026-03-17 |
| 16. Persistence & Observability | v3.0 | 6/6 | Complete | 2026-03-17 |
| 17. Scheduling Intelligence | v3.0 | 2/2 | Complete | 2026-03-17 |
| 18. Unify Coulomb Counting | v3.1 | 1/1 | Complete | 2026-03-20 |
| 19. Extract SagTracker | v3.1 | 1/1 | Complete | 2026-03-20 |
| 20. Extract SchedulerManager | v3.1 | 1/1 | Complete | 2026-03-20 |
| 21. Extract DischargeCollector | v3.1 | 2/2 | Complete | 2026-03-20 |
| 22. Naming + Docs Sweep | v3.1 | 2/2 | Complete | 2026-03-20 |
| 23. Test Quality Rewrite | v3.1 | 4/4 | Complete | 2026-03-20 |
| 24. Temperature + Security Hardening | v3.1 | 2/2 | Complete | 2026-03-21 |
| 25. Scheduler Reframe + Catch-22 Fix | v3.2 | 0/? | Not started | - |
| 26. Desulfation Retraction | v3.2 | 0/? | Not started | - |
| 27. Docs, ADR & Operator Report | v3.2 | 0/? | Not started | - |

---

*Roadmap updated: 2026-06-03 — v3.2 Honest Monitoring & Diagnostic Verification milestone started (Phases 25-27, active)*
