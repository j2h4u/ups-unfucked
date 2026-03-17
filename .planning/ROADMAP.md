# Roadmap: UPS Battery Monitor

## Milestones

- ✅ **v1.0 MVP** — Phases 1-6 (shipped 2026-03-14)
- ✅ **v1.1 Expert Panel Review Fixes** — Phases 7-11 (shipped 2026-03-14)
- ✅ **v2.0 Actual Capacity Estimation** — Phases 12-14 (shipped 2026-03-16)
- ⏳ **v3.0 Active Battery Care** — Phases 15-17 (planning in progress)

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
<summary>⏳ v3.0 Active Battery Care (Phases 15-17) — PLANNING</summary>

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
- [ ] 15-05-PLAN.md — Regression tests: full test suite passes, zero regressions (Wave 4)

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

**Plans:** TBD

---

### Phase 17: Scheduling Intelligence

**Goal:** Implement daemon-controlled scheduling logic — evaluate sulfation score + ROI + safety constraints, make daily decisions about test dispatch, log every decision with reason. All preconditions validated before any upscmd attempt. Disable static systemd timers and rely entirely on daemon scheduling.

**Depends on:** Phase 16

**Requirements:** SCHED-01, SCHED-03, SCHED-04, SCHED-05, SCHED-06, SCHED-07, SCHED-08

**Success Criteria** (what must be TRUE):
1. User can review journald logs and confirm scheduler evaluated test candidates with decision reason codes (daily entry shows: propose_test [reason], defer_test [reason], or block_test [reason])
2. User can verify daemon enforces SoH floor (≥60%) — no test when SoH below floor, reason logged (rejection logged with "SoH below floor (X%)" message, no upscmd attempt)
3. User can verify daemon enforces rate limiting (≤1 test/week, minimum 7-day interval) — skips scheduled test if recent test within grace period, logs reason (deferral logged with "last test 3 days ago" or similar)
4. User can verify daemon credits natural blackouts (≥90% depth, <7 days old) as desulfation equivalent — skips scheduled test when active blackout credit exists, logs credit usage (blackout_credit field shows "active until YYYY-MM-DD")
5. User can verify systemd timers disabled on daemon startup — `ups-test-deep.timer` and `ups-test-quick.timer` masked or disabled (systemctl status shows "masked" or "disabled", not "active")
6. User can review precondition checks logged before any upscmd dispatch (SoC ≥95%, no power glitches in last 4h, no test already running — each check logged with pass/fail status)

**Plans:** TBD

</details>

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
| 15. Foundation | v3.0 | 4/5 | In Progress|  |
| 16. Persistence & Observability | v3.0 | 0/3 | Not started | — |
| 17. Scheduling Intelligence | v3.0 | 0/3 | Not started | — |

---

*Roadmap updated: 2026-03-17 after Phase 15 planning*
