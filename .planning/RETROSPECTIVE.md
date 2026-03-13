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

## Cross-Milestone Trends

### Process Evolution

| Milestone | Phases | Plans | Key Change |
|-----------|--------|-------|------------|
| v1.0 | 6 | 21 | First milestone — established test-first wave pattern |

### Cumulative Quality

| Milestone | Tests | LOC | Zero-Dep Additions |
|-----------|-------|-----|-------------------|
| v1.0 | 160 | 5,003 | All (stdlib only + NUT) |

### Top Lessons (Verified Across Milestones)

1. Physics-based estimation beats firmware readings for VRLA batteries
2. Test-first with real data (actual blackout measurements) produces reliable models
