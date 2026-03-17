---
gsd_state_version: 1.0
milestone: v3.0
milestone_name: Active Battery Care — Phase 17 Scheduling Intelligence
status: in_progress
last_updated: "2026-03-17T21:15:00Z"
progress:
  total_phases: 3
  completed_phases: 2
  total_plans: 13
  completed_plans: 12
---

# Project State — UPS Battery Monitor

**Last Updated:** 2026-03-17 after Phase 17 Plan 01 completion
**Milestone:** v3.0 Active Battery Care — Phase 15 complete, Phase 16 COMPLETE (6/6), Phase 17 Plan 01 COMPLETE (1/3)
**Current Position:** Phase 17 Plan 01 (Scheduling Intelligence - Foundation) COMPLETE → Ready for Plan 02 (Timer Migration)

---

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-17)

**Core value:** Сервер выключается чисто и вовремя при блекауте, используя каждую минуту — не полагаясь на прошивку CyberPower.
**Current focus:** Phase 17 — scheduling-intelligence (Wave 1 complete)

**Milestones shipped:**

- v1.0 MVP (phases 1–6): 5,003 LOC, 160 tests, core daemon with LUT model, calibration mode, safe shutdown
- v1.1 Expert Panel Review Fixes (phases 7–11): Per-poll writes, frozen Config/CurrentMetrics, full integration tests, batch writes, MetricEMA
- v2.0 Actual Capacity Estimation (phases 12–14): 11,602 LOC, 291 tests → 337 after audit fixes. Capacity estimation, SoH recalibration, math kernel, reporting pipeline
- v3.0 (in progress): 20 requirements across 3 phases

---

## Accumulated Context

### Key Decisions (v3.0)

1. **Three-phase structure** derived from research recommendations: Foundation (de-risk) → Persistence (observe) → Scheduling (decide).

2. **Phase 15 isolation:** Math models (sulfation, ROI) as pure functions, fully testable offline.

3. **Phase 16 read-only:** Observability integrated, daemon behavior unchanged from user perspective.

4. **Phase 17 preconditions:** Every decision logged. Safety gates (SoH floor, rate limiting, blackout credit, grid stability) enforced before upscmd.

5. **No staging needed:** Single server, single UPS — daemon acts directly when approved.

6. **Conservative deep test bias:** Natural blackouts provide free desulfation; daemon tests are last resort. Better to under-test than over-test.

7. **Grid stability gate configurable (Phase 17):** grid_stability_cooldown_hours=0 fully disables. Deployment flexibility.

8. **No systemd timer masking in code:** Manual deployment step, not daemon concern.

### Requirement Coverage

**Phase 17 Plan 01 COMPLETE:** SCHED-01, SCHED-03, SCHED-04, SCHED-05, SCHED-06, SCHED-08 (6/8)

**Coverage:** 20/20 ✓

### Phase 17 Plan 01 Completion Report

**Executed:** 2026-03-17, 4 tasks, 66 tests, ~1700 LOC

| Task | Deliverable | Status | Tests |
|------|-------------|--------|-------|
| 1 | Scheduler decision engine | ✅ COMPLETE | 33 |
| 2 | Model schema extension | ✅ COMPLETE | 10 |
| 3 | Precondition & dispatch | ✅ COMPLETE | 13 |
| 4 | Blackout credit & classification | ✅ COMPLETE | 10 |

**Commits:**
- f2a0cc5: feat(17-01): scheduler decision engine
- 2363224: feat(17-02): model schema extension
- c22ad51: feat(17-03): precondition validator and dispatch
- e278a67: feat(17-04): blackout credit logic
- 929e83f: docs(17-01): complete summary

### Open Blockers

None. Phase 17 Plan 01 complete.

### Known v3.1+ Candidates (deferred)

- Temperature sensor integration
- Peukert exponent auto-calibration
- Cliff-edge degradation detector
- Discharge curve shape analysis

### Open Questions

1. NUT upscmd behavior on UT850EG (Phase 15 validation)
2. Sulfation score stability threshold (<5 points/day variance)
3. Blackout credit classification accuracy (60s window heuristic)
4. Temperature sensor availability (v3.1 ready)

---

## Session Continuity

**Last session:** 2026-03-17 (Phase 17 Plan 01 execution)

**Artifacts created:**

- `.planning/phases/17-scheduling-intelligence/17-01-SUMMARY.md`
- `src/battery_math/scheduler.py` (267 LOC)
- `src/model.py` extension (82 LOC)
- `src/monitor.py` extension (176 LOC)
- `src/discharge_handler.py` extension (56 LOC)
- `tests/test_scheduler.py` (528 LOC)
- `tests/test_dispatch.py` (230 LOC)
- `tests/test_discharge_handler.py` (268 LOC)
- `tests/test_model.py` extension (158 LOC)

**Total:** 5 commits, 66 tests passing

**Next phase:** Phase 17 Plan 02 (Timer Migration)

---

*State updated: 2026-03-17 after Phase 17 Plan 01 completion*
