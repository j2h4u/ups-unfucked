---
gsd_state_version: 1.0
milestone: v3.1
milestone_name: Code Quality Hardening
status: active
last_updated: "2026-03-20"
progress:
  total_phases: 7
  completed_phases: 0
  total_plans: 0
  completed_plans: 0
---

# Project State — UPS Battery Monitor

**Last Updated:** 2026-03-20 after v3.1 roadmap creation
**Milestone:** v3.1 Code Quality Hardening
**Current Position:** Phase 18 — Unify Coulomb Counting (not started)

---

## Current Position

```
Phase 18 [          ] Not started
Phase 19 [          ] Waiting on 18
Phase 20 [          ] Waiting on 18
Phase 21 [          ] Waiting on 18
Phase 22 [          ] Waiting on 21
Phase 23 [          ] Waiting on 21
Phase 24 [          ] Independent — can run anytime
```

**Next action:** `/gsd:plan-phase 18`

---

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-20)

**Core value:** Сервер выключается чисто и вовремя при блекауте, используя каждую минуту — не полагаясь на прошивку CyberPower.
**Current focus:** v3.1 Code Quality Hardening — decompose MonitorDaemon god class, unify coulomb counting, rewrite implementation-coupled tests, resolve temperature placeholder and security gaps.

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

### Key Decisions (v3.0, carried forward)

1. No systemd timer masking in code — manual deployment step
2. Grid stability gate configurable (grid_stability_cooldown_hours=0 disables)
3. Conservative deep test bias: natural blackouts provide free desulfation

### Open Questions (v3.1)

1. Temperature sensor: does NUT expose a variable for UT850EG temperature? (resolved in Phase 24)
2. After DischargeCollector extraction: how much does MonitorDaemon shrink? Track LOC post-21.

### Todos

- [ ] After Phase 21: measure MonitorDaemon LOC reduction, note in STATE.md
- [ ] Phase 24: check `upsc cyberpower` output for any temperature variable before implementing SEC-01

---

*State updated: 2026-03-20 after v3.1 roadmap creation*
