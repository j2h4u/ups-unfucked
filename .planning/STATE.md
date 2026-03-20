---
gsd_state_version: 1.0
milestone: v3.0
milestone_name: Active Battery Care
status: completed
last_updated: "2026-03-20"
progress:
  total_phases: 3
  completed_phases: 3
  total_plans: 13
  completed_plans: 13
---

# Project State — UPS Battery Monitor

**Last Updated:** 2026-03-20 after v3.0 milestone completion
**Milestone:** v3.0 Active Battery Care — SHIPPED
**Current Position:** Milestone complete, planning v3.1

---

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-20)

**Core value:** Сервер выключается чисто и вовремя при блекауте, используя каждую минуту — не полагаясь на прошивку CyberPower.
**Current focus:** Planning v3.1 Code Quality Hardening

**Milestones shipped:**

- v1.0 MVP (phases 1–6): 5,003 LOC, 160 tests
- v1.1 Expert Panel Review Fixes (phases 7–11): Per-poll writes, frozen Config/CurrentMetrics
- v2.0 Actual Capacity Estimation (phases 12–14): 11,602 LOC, 291 tests → 337 after audit
- v3.0 Active Battery Care (phases 15–17): 5,239 LOC, 476 tests, sulfation model + smart scheduling + kaizen

---

## Accumulated Context

### Key Decisions (v3.0)

1. Three-phase structure: Foundation (de-risk) → Persistence (observe) → Scheduling (decide)
2. Phase 15 isolation: Math models as pure functions, fully testable offline
3. Phase 16 read-only: Observability integrated, daemon behavior unchanged
4. Phase 17 preconditions: Every decision logged. Safety gates enforced before upscmd
5. Conservative deep test bias: Natural blackouts provide free desulfation; daemon tests are last resort
6. Grid stability gate configurable: grid_stability_cooldown_hours=0 disables
7. No systemd timer masking in code: Manual deployment step
8. No backward compatibility obligation: No compat shims, no staged rollout
9. Timer Migration (Plan 03) dropped: Over-engineering for a migration that wasn't needed

### Open Questions

1. Temperature sensor availability via NUT HID (v3.1 candidate)
2. Peukert exponent auto-calibration approach (v3.1+ candidate)

---

*State updated: 2026-03-20 after v3.0 milestone completion*
