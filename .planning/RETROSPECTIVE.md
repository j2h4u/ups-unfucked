# Project Retrospective

*A living document updated after each milestone. Lessons feed forward into future planning.*

## Milestone: v1.0 — MVP

**Shipped:** 2026-03-14
**Phases:** 6 | **Plans:** 21 | **Tests:** 160

### What Was Built
- Physics-based battery monitoring daemon replacing unreliable CyberPower firmware
- Virtual UPS proxy (dummy-ups) transparent to all consumers (upsmon, Grafana)
- Battery health tracking with SoH degradation and replacement date prediction
- Calibration mode for cliff region data acquisition
- Production-ready systemd service with install script

### What Worked
- Test-driven development: every plan started with Wave 0 tests, caught regressions early
- Real blackout data (2026-03-12) as ground truth for model validation — no guessing
- Atomic write + fsync pattern reused across phases (model.json, calibration, virtual UPS)
- Wave-based parallelization kept plans small and focused (2-6 plans per phase)
- 2-day timeline for 6 phases — aggressive but achievable with clear requirements

### What Was Inefficient
- SUMMARY.md one-liner extraction was inconsistent (some summaries lacked structured frontmatter)
- Phase 3 plan 03-04 was documentation-only (systemd config + CONTEXT.md) — could have been folded into Phase 5
- Some lessons learned duplicated across STATE.md sections (06-01 appeared twice)

### Patterns Established
- EMA → IR compensation → LUT → Peukert as standard battery estimation pipeline
- model.json as single source of truth (LUT + SoH history + metadata)
- tmpfs (/dev/shm) for runtime metrics, disk only for persistent model
- Source field tracking (standard/measured/interpolated/anchor) for LUT provenance
- Discharge buffer pattern for event-driven sample collection

### Key Lessons
1. Physical invariants (input.voltage for blackout detection) are more reliable than firmware state machines
2. Trapezoidal integration with non-uniform time intervals is essential for real-world discharge profiles
3. Linear regression needs ≥3 points and R² validation — graceful degradation when data is sparse
4. Calibration mode as one-time setup avoids ongoing disk wear while capturing cliff region data

### Cost Observations
- Model mix: ~80% sonnet (execution), ~15% opus (planning/audit), ~5% haiku (exploration)
- Sessions: ~6-8 sessions across 2 days
- Notable: Wave-based execution with parallel plan creation kept context windows manageable

---

## Milestone: v1.1 — Expert Panel Review Fixes

**Shipped:** 2026-03-14
**Phases:** 5 | **Plans:** 14 | **Tests:** 205

### What Was Built
- Per-poll virtual UPS writes during blackout (eliminates 60s LB flag lag)
- Frozen Config + CurrentMetrics dataclasses replacing untyped dicts and module globals
- Full OL→OB→OL integration test, Peukert auto-calibration tests, signal handler tests
- Batch calibration writes (60x SSD wear reduction), _safe_save helper
- History pruning (30 entries max), fdatasync optimization, health.json endpoint
- MetricEMA generic class for extensible per-metric EMA tracking

### What Worked
- Expert panel review as requirements source — clear priorities (P0-P3), concrete findings, no ambiguity
- Audit-before-complete caught stale checkboxes (LOW-03/LOW-04 marked pending but actually done)
- Wave-based execution with parallel plans kept velocity high (5 phases in ~1 day)
- Dataclass refactors (Phase 8) made subsequent phases easier to test and reason about

### What Was Inefficient
- SUMMARY.md one-liner extraction still inconsistent — some summaries had "Objective:" or "Problem:" instead of actual one-liner
- Phase 7 plan 02 never explicitly completed in roadmap (showed 1/2) despite SAFE-01/02 being verified satisfied
- STATE.md accumulated stale context from earlier phases (copy-paste from prior sessions)

### Patterns Established
- Frozen dataclass for config — passed to __init__, no module globals
- MetricEMA as generic per-metric EMA (extensible without code changes)
- health.json as liveness file for external monitoring (Grafana, check_mk)
- _safe_save() helper for all model persistence calls

### Key Lessons
1. Expert panel review → requirements → phases is an efficient pipeline for improvement milestones
2. Dataclass refactors pay for themselves immediately in test writability
3. History pruning should be default from day 1 — unbounded lists are a ticking bomb
4. fdatasync vs fsync is a free ~50% I/O win for JSON files where metadata doesn't matter

### Cost Observations
- Model mix: ~70% sonnet (execution), ~25% opus (planning/audit/milestone), ~5% haiku
- Sessions: ~4-5 sessions across 1 day
- Notable: Parallel phase execution (8+9 in same wave) kept total time short

---

## Cross-Milestone Trends

### Process Evolution

| Milestone | Phases | Plans | Key Change |
|-----------|--------|-------|------------|
| v1.0 | 6 | 21 | First milestone — established test-first wave pattern |
| v1.1 | 5 | 14 | Expert panel review as requirements source; dataclass-first refactoring |

### Cumulative Quality

| Milestone | Tests | LOC | Zero-Dep Additions |
|-----------|-------|-----|-------------------|
| v1.0 | 160 | 5,003 | All (stdlib only + NUT) |
| v1.1 | 205 | 6,596 | All (stdlib only + NUT) |

### Top Lessons (Verified Across Milestones)

1. Physics-based estimation beats firmware readings for VRLA batteries
2. Test-first with real data (actual blackout measurements) produces reliable models
3. Expert panel review → concrete requirements is an efficient improvement pipeline
4. Dataclass refactors pay for themselves immediately in testability
