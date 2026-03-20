---
gsd_state_version: 1.0
milestone: v3.1
milestone_name: Code Quality Hardening
status: unknown
last_updated: "2026-03-20T13:20:10.316Z"
progress:
  total_phases: 7
  completed_phases: 4
  total_plans: 7
  completed_plans: 5
---

# Project State — UPS Battery Monitor

**Last Updated:** 2026-03-20 after Phase 21 Plan 02 completion
**Milestone:** v3.1 Code Quality Hardening
**Current Position:** Phase 21 — Extract DischargeCollector (COMPLETE)

---

## Current Position

Phase: 22 (naming-docs-sweep) — EXECUTING
Plan: 1 of 2

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-20)

**Core value:** Сервер выключается чисто и вовремя при блекауте, используя каждую минуту — не полагаясь на прошивку CyberPower.
**Current focus:** Phase 22 — naming-docs-sweep

**Milestones shipped:**

- v1.0 MVP (phases 1–6): 5,003 LOC, 160 tests
- v1.1 Expert Panel Review Fixes (phases 7–11): Per-poll writes, frozen Config/CurrentMetrics
- v2.0 Actual Capacity Estimation (phases 12–14): 11,602 LOC, 291 tests
- v3.0 Active Battery Care (phases 15–17): 5,239 LOC, 476 tests, sulfation model + smart scheduling + kaizen

---

## Performance Metrics

| Metric | v3.0 | v3.1 Target |
|--------|------|-------------|
| Tests passing | 476 | 476+ |
| LOC Python | 5,239 | ~same (structural only) |
| MonitorDaemon responsibilities | many | reduced by 3 extracted modules |
| Duplicate coulomb counting implementations | 2 | 1 |
| Phase 21-extract-dischargecollector P02 | 15 | 2 tasks | 5 files |
| Phase 21-extract-dischargecollector P01 | 8 | 1 tasks | 1 files |

### Execution History

| Phase | Duration | Tasks | Files |
|-------|----------|-------|-------|
| 18-unify-coulomb-counting P01 | 3 min | 3 | 5 |

---

## Accumulated Context

### Key Decisions (v3.1)

1. Phase ordering: 18 (unify math) → 19/20/21 (extract modules) → 22 (rename) → 23 (tests) → 24 (security/temp)
2. Phase 18 first: integrate_current() used by all extracted modules — unify before extraction avoids doing it twice
3. Phases 19/20/21 depend on 18, but are independent of each other — can plan in parallel if desired
4. Phase 22 after 21: renaming after decomposition settles final module structure, avoids churn
5. Phase 23 after 21: extracted modules change which internals exist; rewriting tests before extraction would require rewriting twice
6. Phase 24 independent: temperature + security changes touch no MonitorDaemon structure
7. Design principle: when choosing between implementations, pick the one with greater accuracy
8. No backward compatibility obligation: single server, single user — no compat shims
9. (Phase 18) integrate_current() is a pure function in battery_math — no class state, importable by phases 19-21 without carrying CapacityEstimator as a dependency
10. (Phase 18) avg_load computed once in update_battery_health() and passed to _check_alerts() — narrowest change that avoids _compute_soh return signature modification
11. (Phase 18) _log_discharge_prediction() fallback changed from 0.0 to reference_load_percent via self._avg_load() for consistency with all other avg_load call sites
12. (Phase 21) DischargeCollector.track() receives current_metrics and reads previous_event_type from it — cleaner than tracking previous event internally
13. (Phase 21) MonitorDaemon LOC reduction: 5 methods + 4 state fields removed; monitor.py shrunk by ~120 LOC; 547 tests pass post-extraction

### Key Decisions (v3.0, carried forward)

1. No systemd timer masking in code — manual deployment step
2. Grid stability gate configurable (grid_stability_cooldown_hours=0 disables)
3. Conservative deep test bias: natural blackouts provide free desulfation

### Open Questions (v3.1)

1. Temperature sensor: does NUT expose a variable for UT850EG temperature? (resolved in Phase 24)
2. After DischargeCollector extraction: how much does MonitorDaemon shrink? Track LOC post-21.

### Todos

- [x] After Phase 21: MonitorDaemon shrunk ~120 LOC; 5 methods + 4 state fields moved to DischargeCollector; 547 tests pass
- [ ] Phase 24: check `upsc cyberpower` output for any temperature variable before implementing SEC-01

---

*State updated: 2026-03-20 after v3.1 roadmap creation*
