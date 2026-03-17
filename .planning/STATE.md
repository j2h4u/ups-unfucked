---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: unknown
last_updated: "2026-03-17T13:31:38.165Z"
progress:
  total_phases: 2
  completed_phases: 2
  total_plans: 11
  completed_plans: 11
---

# Project State — UPS Battery Monitor

**Last Updated:** 2026-03-17 after Phase 16 Plan 06 completion
**Milestone:** v3.0 Active Battery Care — Phase 15 complete, Phase 16 ALL PLANS COMPLETE (6/6)
**Current Position:** Phase 16 (Persistence-Observability) COMPLETE — all 10 requirements (SULF-01 through RPT-03) satisfied. Ready for Phase 17 (Scheduling Intelligence)

---

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-17)

**Core value:** Сервер выключается чисто и вовремя при блекауте, используя каждую минуту — не полагаясь на прошивку CyberPower.
**Current focus:** Phase 16 — persistence-observability

**Milestones shipped:**

- v1.0 MVP (phases 1–6): 5,003 LOC, 160 tests, core daemon with LUT model, calibration mode, safe shutdown
- v1.1 Expert Panel Review Fixes (phases 7–11): Per-poll writes, frozen Config/CurrentMetrics, full integration tests, batch writes, MetricEMA
- v2.0 Actual Capacity Estimation (phases 12–14): 11,602 LOC, 291 tests → 337 after audit fixes. Capacity estimation, SoH recalibration, math kernel, reporting pipeline

**Next milestone (v3.0):** 20 requirements across 3 phases — sulfation model, smart scheduling, cycle ROI metric

---

## Accumulated Context

### Key Decisions (v3.0)

1. **Three-phase structure** derived from research recommendations: Foundation (de-risk) → Persistence (observe) → Scheduling (decide). No Phase 18 (upscmd dispatch) in v3.0 roadmap; deferred per research validation gates requiring 30+ days field monitoring.

2. **Phase 15 isolation:** Math models (sulfation, ROI) implemented as pure functions in `src/battery_math/` — fully testable offline, zero daemon coupling risk.

3. **Phase 16 read-only:** All observability (logging, persistence, reporting) integrated before any test dispatch logic. Daemon behavior unchanged from user perspective.

4. **Phase 17 preconditions:** Every scheduling decision logged with reason code. Safety gates (SoH floor 60%, rate limiting 1/week, blackout credit, grid stability) enforced before any upscmd consideration.

5. **No staging needed:** Single server, single UPS, single user — daemon acts directly when decision made. No dry-run mode, advisory mode, or staged rollout infrastructure.

6. **Conservative deep test bias:** Deep test leaves battery partially discharged — if real blackout coincides, less runtime available. Given frequent (but short) blackouts, scheduler must have explicit bias toward fewer deep tests. When ROI is marginal → don't test. Natural blackouts provide free desulfation; daemon-initiated deep tests are last resort, not routine. Better to under-test than over-test.

### Requirement Coverage

All 20 v3.0 requirements mapped to exactly one phase:

**Phase 15:** SULF-06, SCHED-02 (2 requirements)
**Phase 16:** SULF-01, SULF-02, SULF-03, SULF-04, SULF-05, ROI-01, ROI-02, RPT-01, RPT-02, RPT-03 (10 requirements)
**Phase 17:** SCHED-01, SCHED-03, SCHED-04, SCHED-05, SCHED-06, SCHED-07, SCHED-08 (8 requirements)

No orphans. No duplicates. Coverage: 20/20 ✓

### Open Blockers

None. Roadmap creation complete.

### Known v3.1+ Candidates (deferred)

- Temperature sensor integration (TEMP-01, TEMP-02) — requires NUT HID discovery and USB probe architecture
- Peukert exponent auto-calibration (CAL2-02) — requires stable measured capacity baseline (deferred from v2.0)
- Cliff-edge degradation detector (ADV-03) — Bayesian SoH inertia at rapid degradation
- Discharge curve shape analysis (ADV-01) — cliff region expansion as sulfation indicator

### Open Questions

1. **NUT upscmd behavior on UT850EG:** Phase 15 validates protocol works on target hardware. Manual test required; script provided.
2. **Sulfation score stability:** Phase 16 observes for stability (variance <5 points/day). If noise >5 points/day, Phase 17 thresholds may need tuning.
3. **Blackout credit classification:** Phase 16 labels events as natural vs test-initiated. Accuracy depends on discharge curve shape matching heuristics.
4. **Temperature sensor availability:** Phase 16 uses constant 35°C. If NUT HID exposes `battery.temperature` in future, architecture ready for replacement; deferred to v3.1.

### Todos

- [ ] Approve roadmap (3 phases, 20 requirements, 15 success criteria)
- [ ] Create Phase 15 plan (math module tests, upscmd validation, daemon import test)
- [ ] Create Phase 16 plan (model.json schema, discharge handler integration, health.json export, MOTD module)
- [ ] Create Phase 17 plan (scheduler decision tree, safety gates, systemd timer migration, journald structured events)

---

## Session Continuity

**Last session:** 2026-03-17 (Phase 16 Plan 06 — MOTD sulfation module)

**Artifacts created this session:**

- `.planning/phases/16-persistence-observability/16-06-SUMMARY.md` (Wave 5 completion report)
- Created `scripts/motd/55-sulfation.sh`: MOTD module for sulfation status display
- Updated `scripts/install.sh`: Added sulfation module to MOTD installation section

**MOTD Module Summary:**

- File: `scripts/motd/55-sulfation.sh` (52 lines, 1.7 KB)
- Functionality: Reads health.json, displays "Battery health: Sulfation XX% · Next test in Xd · Blackout credit YY%"
- JSON parsing: Safe jq extraction with error suppression
- Edge cases: Handles missing files, null values, invalid JSON, past/future/no-date test timestamps
- Integration: Module 55 runs after 51-ups.sh (alphabetical order in 50-59 status range)
- Testing: 7 test cases all PASS, dynamic update verified
- Status: PASS — Wave 5 complete, RPT-01 (sulfation score display) requirement satisfied

**Phase 16 Completion Summary:**

- Plan 01 (Model persistence): ✅ model.json schema + append/prune methods
- Plan 02 (Health export): ✅ health.json schema extension with Phase 16 fields
- Plan 03 (Discharge integration): ✅ discharge_handler imports sulfation + cycle_roi modules
- Plan 04 (Event structure): ✅ journald event schema with reason field
- Plan 05 (Event logging): ✅ logger.info() call in discharge_handler + 7 integration tests
- Plan 06 (MOTD display): ✅ 55-sulfation.sh module + 7 functional tests

**Requirements satisfied:** 10/10

- SULF-01 (sulfation compute): Phase 15
- SULF-02 (physics baseline): Phase 15
- SULF-03 (IR trend): Phase 15
- SULF-04 (recovery delta): Phase 15
- SULF-05 (sulfation history persistence): Phase 16 Plan 01 ✅
- ROI-01 (cycle ROI compute): Phase 15
- ROI-02 (ROI factors): Phase 15
- RPT-01 (sulfation export to display): Phase 16 Plan 06 ✅
- RPT-02 (discharge events to journald): Phase 16 Plan 05 ✅
- RPT-03 (health.json schema): Phase 16 Plan 02 ✅

**Next session:** Phase 17 Plan 01 (Scheduling Intelligence) — implement discharge handler integration and scheduling decision tree

---

*State updated: 2026-03-17 after Phase 16 Plan 06 completion*
