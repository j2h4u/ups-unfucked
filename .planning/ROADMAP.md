# Roadmap: UPS Battery Monitor

## Milestones

- ✅ **v1.0 MVP** — Phases 1-6 (shipped 2026-03-14)
- ✅ **v1.1 Expert Panel Review Fixes** — Phases 7-11 (shipped 2026-03-14)
- ✅ **v2.0 Actual Capacity Estimation** — Phases 12-14 (shipped 2026-03-16)
- ✅ **v3.0 Active Battery Care** — Phases 15-17 (shipped 2026-03-20)
- 🔄 **v3.1 Code Quality Hardening** — Phases 18-24 (active)

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

### Phase 15: Foundation

**Goal:** De-risk core technologies — validate NUT upscmd protocol, implement sulfation and ROI pure functions, confirm no daemon regressions. All work is isolated; no changes to main event loop.

**Depends on:** v2.0 (Phase 14 completed)

**Requirements:** SULF-06, SCHED-02

**Success Criteria** (what must be TRUE):
1. User can verify `src/battery_math/sulfation.py` functions compute score [0–1.0] from battery data (unit tests pass, offline harness confirms with synthetic discharge curves)
2. User can verify `src/battery_math/cycle_roi.py` functions estimate desulfation benefit vs wear cost (synthetic test cases show <20% estimation margin)
3. User can verify `nut_client.send_instcmd()` method successfully dispatches `test.battery.start.quick` to real UT850EG (live UPS acknowledges INSTCMD, test.result updates)
4. User can confirm zero daemon regressions — all v2.0 tests pass and main loop exhibits no new errors during import of new math modules

**Plans:** 5 plans (Wave 1: math modules + unit tests, Wave 2: NUT INSTCMD, Wave 3: integration tests, Wave 4: regression check)

- [x] 15-01-PLAN.md — Pure functions: sulfation.py, cycle_roi.py, module exports (Wave 1)
- [x] 15-02-PLAN.md — Unit tests: test_sulfation.py, test_cycle_roi.py, extend test_nut_client.py (Wave 1)
- [x] 15-03-PLAN.md — NUT INSTCMD: send_instcmd() method + live validation script (Wave 2)
- [x] 15-04-PLAN.md — Integration tests: test_sulfation_offline_harness.py with year-simulation (Wave 3)
- [x] 15-05-PLAN.md — Regression tests: full test suite passes, zero regressions (Wave 4)

---

### Phase 16: Persistence & Observability

**Goal:** Extend daemon to observe, measure, and persist sulfation signals (IR trend, recovery delta, physics baseline) without triggering tests. Daemon still read-only. All new observability in place before active control; validates that signals are stable and interpretable in production.

**Depends on:** Phase 15

**Requirements:** SULF-01, SULF-02, SULF-03, SULF-04, SULF-05, ROI-01, ROI-02, RPT-01, RPT-02, RPT-03

**Success Criteria** (what must be TRUE):
1. User can read sulfation_score and recovery_delta for each discharge event from journald (structured events logged with numeric values, timestamp, and confidence)
2. User can view MOTD and see sulfation_score, next_test_eta, blackout_credit_countdown refreshed post-discharge (values display correctly, countdown decrements daily)
3. User can query `GET /health.json` endpoint and confirm sulfation_score, cycle_roi, scheduling_reason, next_test_timestamp exported for Grafana (metrics present, values reasonable given battery state)
4. User can inspect `model.json` and verify sulfation history, ROI history, natural_blackout_events persisted with timestamps (schema extended, all discharge events logged, file remains backward compatible)
5. User can review past blackout (or trigger one) and confirm daemon correctly labels event reason in journald (natural vs test-initiated distinguished in event.reason field)

**Plans:** 6 plans across 5 execution waves

- [x] 16-01-PLAN.md — Wave 0: Test scaffolds for integration tests (Nyquist Rule gate) — completed 2026-03-17 (29 tests scaffolded, all 4 requirements covered)
- [x] 16-02-PLAN.md — Wave 1: BatteryModel schema extension + sulfation history persistence — completed 2026-03-17
- [x] 16-03-PLAN.md — Wave 2: Discharge handler integration with sulfation/ROI scoring — completed 2026-03-17
- [x] 16-04-PLAN.md — Wave 3: health.json export with Phase 16 observability metrics — completed 2026-03-17 (11 fields, 8 tests, RPT-01/ROI-02/ROI-03 satisfied)
- [x] 16-05-PLAN.md — Wave 4: Journald structured event logging for discharge completion — completed 2026-03-17 (10 event fields, 7 tests, RPT-02 satisfied)
- [x] 16-06-PLAN.md — Wave 5: MOTD module for sulfation status display — completed 2026-03-17 (52-line shell script, 7 tests, RPT-01 satisfied)

---

### Phase 17: Scheduling Intelligence

**Goal:** Implement daemon-controlled scheduling logic — evaluate sulfation score + ROI + safety constraints, make daily decisions about test dispatch, log every decision with reason. All preconditions validated before any upscmd attempt. Daemon-controlled scheduling replaces static systemd timers. Grid stability cooldown configurable (0 = disabled for frequent blackout grids per user feedback). Manual deployment step to mask systemd timers.

**Depends on:** Phase 16

**Requirements:** SCHED-01, SCHED-03, SCHED-04, SCHED-05, SCHED-06, SCHED-07, SCHED-08

**Success Criteria** (what must be TRUE):
1. User can review journald logs and confirm scheduler evaluated test candidates with decision reason codes (daily entry shows: propose_test [reason], defer_test [reason], or block_test [reason])
2. User can verify daemon enforces SoH floor (>=60%) — no test when SoH below floor, reason logged (rejection logged with "SoH below floor (X%)" message, no upscmd attempt)
3. User can verify daemon enforces rate limiting (<=1 test/week, minimum 7-day interval) — skips scheduled test if recent test within grace period, logs reason (deferral logged with "last test 3 days ago" or similar)
4. User can verify daemon credits natural blackouts (>=90% depth, <7 days old) as desulfation equivalent — skips scheduled test when active blackout credit exists, logs credit usage (blackout_credit field shows "active until YYYY-MM-DD")
5. User can verify systemd timers disabled post-deployment — `ups-test-deep.timer` and `ups-test-quick.timer` masked or disabled (systemctl status shows "masked" or "disabled", not "active")
6. User can review precondition checks logged before any upscmd dispatch (SoC >=95%, no power glitches in last 4h, no test already running — each check logged with pass/fail status)

**Plans:** 2 plans across 2 execution waves

- [x] 17-01-PLAN.md — Wave 1: Scheduler decision engine, precondition validator, dispatch integration, blackout credit logic, test suite — completed 2026-03-17
- [x] 17-02-PLAN.md — Wave 2: Configurable grid stability cooldown, configuration validation, deployment checklist — completed 2026-03-17

**Key Implementation Notes (per user feedback):**
- Grid stability cooldown configurable: default 4h, set to 0 to disable gate entirely for frequent blackout grids
- Deep blackouts (>90% DoD) rare (~2 times in 4 years); short blackouts (1.5–3 min) common (several/week)
- **NO systemd timer masking code in Python** — user will manually disable timers during deployment (checklist in Phase 17 Plan 02 DEPLOYMENT.md)
- Conservative deep test bias: natural blackouts provide free desulfation; when ROI marginal, defer test

</details>

### v3.1 Code Quality Hardening (Phases 18-24)

- [x] **Phase 18: Unify Coulomb Counting** — Extract integrate_current(), fix double avg_load computation (completed 2026-03-20)
- [x] **Phase 19: Extract SagTracker** — Extract SagTracker module from MonitorDaemon (completed 2026-03-20)
- [x] **Phase 20: Extract SchedulerManager** — Extract SchedulerManager module from MonitorDaemon (completed 2026-03-20)
- [x] **Phase 21: Extract DischargeCollector** — Extract DischargeCollector + split _score_and_persist_sulfation (completed 2026-03-20)
- [ ] **Phase 22: Naming + Docs Sweep** — Rename BatteryModel.data, category, rls/d; add docstrings
- [ ] **Phase 23: Test Quality Rewrite** — Outcome assertions, dependency injection, integration test accuracy
- [ ] **Phase 24: Temperature + Security Hardening** — NUT sensor check, model.json validation, auth docs

## Phase Details

### Phase 18: Unify Coulomb Counting

**Goal:** All coulomb counting in the codebase uses a single accurate implementation with per-step load support.

**Depends on:** Phase 17 (v3.0 complete)

**Requirements:** ARCH-01, ARCH-02

**Success Criteria** (what must be TRUE):
1. A single `integrate_current()` function exists in `src/battery_math/` and all call sites in the codebase use it — no duplicate coulomb integration logic remains
2. `_check_alerts` receives `avg_load` as a parameter — it does not recompute it internally, and no double-averaging occurs across the call chain
3. `integrate_current()` accepts per-step load values and produces a more accurate result than the previous scalar approximation — confirmed by a unit test comparing both approaches on a variable-load sequence
4. All existing tests pass with no regressions after the consolidation

**Plans:** 1/1 plans complete

Plans:
- [x] 18-01-PLAN.md — Extract integrate_current() to battery_math, fix _check_alerts avg_load propagation, accuracy test

---

### Phase 19: Extract SagTracker

**Goal:** SagTracker logic lives in its own module, fully decoupled from MonitorDaemon internals.

**Depends on:** Phase 18

**Requirements:** ARCH-03

**Success Criteria** (what must be TRUE):
1. A `SagTracker` class exists in its own module under `src/` with a clear public interface — MonitorDaemon instantiates it and delegates sag-related calls to it
2. MonitorDaemon no longer contains sag tracking state or logic inline — the extracted code is the sole owner
3. `SagTracker` has direct unit tests that exercise its behavior without constructing a MonitorDaemon
4. All existing tests pass with no regressions

**Plans:** 1/1 plans complete

Plans:
- [x] 19-01-PLAN.md — Extract SagTracker class, rewire MonitorDaemon delegation, unit tests

---

### Phase 20: Extract SchedulerManager

**Goal:** SchedulerManager logic lives in its own module, MonitorDaemon delegates scheduling decisions to it.

**Depends on:** Phase 18

**Requirements:** ARCH-04

**Success Criteria** (what must be TRUE):
1. A `SchedulerManager` class exists in its own module under `src/` — MonitorDaemon constructs it and calls its methods for all scheduling decisions
2. MonitorDaemon no longer contains scheduler state or decision logic inline — the extracted module is the sole owner
3. `SchedulerManager` has direct unit tests that exercise scheduling decisions (safety gates, rate limiting, blackout credit) without constructing a MonitorDaemon
4. All existing tests pass with no regressions

**Plans:** 1/1 plans complete

Plans:
- [x] 20-01-PLAN.md — Extract SchedulerManager class, rewire MonitorDaemon delegation, update test imports, unit tests

---

### Phase 21: Extract DischargeCollector

**Goal:** DischargeCollector owns sample accumulation and calibration writes; sulfation scoring split into compute, persist, and log methods.

**Depends on:** Phase 18

**Requirements:** ARCH-05, ARCH-06

**Success Criteria** (what must be TRUE):
1. A `DischargeCollector` class exists in its own module — it owns discharge sample accumulation and calibration write logic; MonitorDaemon delegates to it
2. MonitorDaemon no longer contains discharge collection state or calibration write logic inline
3. `_score_and_persist_sulfation` is split into at least three methods: one that computes the score, one that persists it, and one that logs it — each independently testable
4. `DischargeCollector` has direct unit tests covering sample accumulation and calibration write behavior without constructing a MonitorDaemon
5. All existing tests pass with no regressions

**Plans:** 2/2 plans complete

Plans:
- [x] 21-01-PLAN.md — Split _score_and_persist_sulfation into compute/persist/log methods (ARCH-06)
- [x] 21-02-PLAN.md — Extract DischargeCollector class, rewire MonitorDaemon, update tests (ARCH-05)

---

### Phase 22: Naming + Docs Sweep

**Goal:** All renamed symbols and added docstrings are consistent across the entire codebase; no stale references remain.

**Depends on:** Phase 21 (cleaner to rename after decomposition settles the final module structure)

**Requirements:** NAME-01, NAME-02, NAME-03, DOC-01, DOC-02, DOC-03, DOC-04

**Success Criteria** (what must be TRUE):
1. `BatteryModel.data` is renamed to `state` everywhere — no remaining references to `.data` on BatteryModel instances in source or tests
2. `category` is renamed to `power_source` in `EventClassifier.classify()` and all call sites updated
3. `rls` and `d` variable names in `_sync_physics_from_data` are replaced with descriptive names
4. `_handle_capacity_convergence`, `_opt_round`, `_prune_lut`, and `_classify_discharge_trigger` each have docstrings that capture their non-obvious behaviors (write-once guard, rounding intent, dedup logic, buffer start semantics)
5. All tests pass after the rename sweep — no broken attribute references

**Plans:** TBD

---

### Phase 23: Test Quality Rewrite

**Goal:** Test suite asserts observable outcomes and uses dependency injection; no mock call sequence replay, no private method assertions, no tautological checks.

**Depends on:** Phase 21 (decomposition changes which internals exist; rewriting tests before extraction would require rewriting twice)

**Requirements:** TEST-01, TEST-02, TEST-03, TEST-04, TEST-05, TEST-06, TEST-07, TEST-08, TEST-09

**Success Criteria** (what must be TRUE):
1. No test in `test_monitor.py` asserts on mock call sequences or internal method call order — assertions target observable state or return values
2. Each test function in the affected files exercises a single behavior — no test that previously verified multiple unrelated assertions remains as-is
3. `test_virtual_ups.py` uses dependency injection for path configuration rather than `Path` patching
4. `test_monitor_integration.py` integration tests use real collaborators (real `SagTracker`, `SchedulerManager`, `DischargeCollector`) instead of internal mocks
5. The Monte Carlo test is marked `@pytest.mark.slow` with a comment documenting its seed and expected runtime
6. `test_motd.py` is marked as an integration test with a comment explaining its environment dependency
7. All 476+ tests pass after the rewrite

**Plans:** TBD

---

### Phase 24: Temperature + Security Hardening

**Goal:** Temperature sensor is resolved (real check or documented absence), model.json has field-level validation, security gaps are documented.

**Depends on:** Nothing (independent — can run at any time after Phase 17)

**Requirements:** SEC-01, SEC-02, SEC-03, SEC-04

**Success Criteria** (what must be TRUE):
1. Daemon checks for a NUT temperature variable at startup and logs a structured message — either "temperature sensor found: X°C" or "temperature sensor unavailable, skipping thermal compensation" — no silent placeholder behavior remains
2. `model.json` loading validates required fields at field level and raises a descriptive error on schema violation — an invalid or missing field produces a clear log message, not a raw KeyError or AttributeError
3. NUT empty PASSWORD security implication is documented in a code comment at the connection site and in the relevant config or README section
4. `atomic_write` logs a warning when the temp file cleanup fails during exception handling — the failed unlink is not silently swallowed

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
| 12. Deep Discharge Capacity Estimation | v2.0 | 4/4 | Complete | 2026-03-16 |
| 12.1 Math Kernel & Stability Tests | v2.0 | 6/6 | Complete | 2026-03-16 |
| 13. SoH Recalibration & New Battery | v2.0 | 2/2 | Complete | 2026-03-16 |
| 14. Capacity Reporting & Metrics | v2.0 | 3/3 | Complete | 2026-03-16 |
| 15. Foundation | v3.0 | 5/5 | Complete | 2026-03-17 |
| 16. Persistence & Observability | v3.0 | 6/6 | Complete | 2026-03-17 |
| 17. Scheduling Intelligence | v3.0 | 2/2 | Complete | 2026-03-17 |
| 18. Unify Coulomb Counting | v3.1 | 1/1 | Complete    | 2026-03-20 |
| 19. Extract SagTracker | v3.1 | 1/1 | Complete    | 2026-03-20 |
| 20. Extract SchedulerManager | v3.1 | 1/1 | Complete    | 2026-03-20 |
| 21. Extract DischargeCollector | v3.1 | 2/2 | Complete    | 2026-03-20 |
| 22. Naming + Docs Sweep | v3.1 | 0/? | Not started | - |
| 23. Test Quality Rewrite | v3.1 | 0/? | Not started | - |
| 24. Temperature + Security Hardening | v3.1 | 0/? | Not started | - |

---

*Roadmap updated: 2026-03-20 after Phase 21 planning*
