---
gsd_state_version: 1.0
milestone: v3.0
milestone_name: Active Battery Care
status: in_progress
last_updated: "2026-03-17T00:00:00.000Z"
progress:
  total_phases: 0
  completed_phases: 0
  total_plans: 0
  completed_plans: 0
---

# Project State — UPS Battery Monitor

**Last Updated:** 2026-03-17 after v3.0 milestone start
**Milestone:** v3.0 Active Battery Care — Defining requirements
**Current Position:** Phase: Not started (defining requirements)

---

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-17)

**Core value:** Сервер выключается чисто и вовремя при блекауте, используя каждую минуту — не полагаясь на прошивку CyberPower.
**Current focus:** v3.0 — transform daemon from passive observer to active battery manager

**Milestones shipped:**
- v1.0 MVP (phases 1–6): 5,003 LOC, 160 tests, core daemon with LUT model, calibration mode, safe shutdown
- v1.1 Expert Panel Review Fixes (phases 7–11): Per-poll writes, frozen Config/CurrentMetrics, full integration tests, batch writes, MetricEMA
- v2.0 Actual Capacity Estimation (phases 12–14): 11,602 LOC, 291 tests → 337 after audit fixes. Capacity estimation, SoH recalibration, math kernel, reporting pipeline

---

## Accumulated Context

### Open Blockers

None.

### Design Decisions (v3.0)

- Daemon calls upscmd directly for deep tests (replaces static systemd timers)
- Temperature: research NUT HID first, fallback to configurable constant (~35°C)
- Cycle ROI: health.json only (Grafana), not MOTD
- Sulfation model: hybrid (Shepherd/Bode physics baseline + IR/curve/recovery data-driven detection)
- Natural blackout credit: skip scheduled tests when recent blackouts cover desulfation

### Known v2.1+ Candidates (deferred)

- Peukert exponent auto-calibration (CAL2-02) — requires stable measured capacity as reference
- Cliff-edge degradation detector — Bayesian SoH inertia at rapid degradation
- Seasonal thermal correction — if field data shows >±3% summer/winter discrepancy

---

*State created: 2026-03-15*
*Last updated: 2026-03-17 after v3.0 milestone start*
