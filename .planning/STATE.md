---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
current_plan: 02-06
status: in_progress
last_updated: "2026-03-14T00:15:00Z"
progress:
  total_phases: 6
  completed_phases: 1
  total_plans: 7
  completed_plans: 10
  percent: 143
---

# Project State — UPS Battery Monitor

**Last Updated:** 2026-03-14
**Current Focus:** Phase 2 in Progress (Plans 01-06 Complete)

---

## Project Reference

**Core Value:** Server shuts down cleanly and on time during blackout, using every available minute — without relying on unreliable CyberPower firmware readings.

**Key Constraint:** Use only two reliable physical measurements — battery.voltage and ups.load. Everything else (charge, runtime) is calculated.

**Deliverable:** One systemd service, zero manual intervention after install, production-ready.

---

## Current Position

**Phase:** 2
**Current Plan:** 02-06 (completed: Event classification integration)
**Status:** In Progress
**Progress:** 6/6 plans completed Phase 2 (100%)

### Phase 2 Completed Plans

- [x] 02-01: Test infrastructure and Wave 0 implementations ✓ COMPLETE
- [x] 02-02: SoC predictor (voltage-based LUT lookup) ✓ COMPLETE
- [x] 02-03: Runtime calculator (Peukert's Law) ✓ COMPLETE
- [x] 02-04: Event classifier (state machine for blackout vs test detection) ✓ COMPLETE
- [x] 02-05: Monitor loop integration (daemon integration) ✓ COMPLETE
- [x] 02-06: Event-driven shutdown and model update logic ✓ COMPLETE

### Phase 1 Status (COMPLETE)

- [x] 01-01: Test infrastructure
- [x] 01-02: NUT socket client (COMPLETE)
- [x] 01-03: EMA smoothing and IR compensation (COMPLETE)
- [x] 01-04: Battery model persistence (COMPLETE)
- [x] 01-05: Daemon integration and systemd service (COMPLETE)

---

## Performance Metrics

| Plan | Duration | Tasks | Tests | Completion Date |
|------|----------|-------|-------|-----------------|
| 01-01 | 8 min | 5 | 38 | 2026-03-13 |
| 01-02 | 45 min | 2 | 4 | 2026-03-13 |
| 01-03 | — | — | 14 | 2026-03-13 |
| 01-04 | 98 sec | 1 | 20 | 2026-03-13 |
| 01-05 | 18 min | 3 | 38 (all phases) | 2026-03-13 |
| 02-01 | 30 min | 4 | 40 | 2026-03-14 |
| 02-02 | — | — | — | 2026-03-14 |
| 02-03 | — | — | — | 2026-03-14 |
| 02-04 | 8 min | 1 | 13 | 2026-03-14 |
| 02-05 | 15 min | 2 | 78 (all phases) | 2026-03-13 |
| 02-06 | 12 min | 2 | 78 (all phases) | 2026-03-14 |

---

## Accumulated Context

### Key Decisions Baked Into Roadmap

| Decision | Phase | Why |
|----------|-------|-----|
| dummy-ups as transparent proxy | Phase 3 | Grafana and upsmon don't change; data source just switches to virtual |
| Distinguish blackout/test via input.voltage | Phase 2 | Physical invariant, independent of firmware interpretation |
| We control ups.status and LB flag | Phase 3 | Bypasses `onlinedischarge_calibration` bug without touching NUT config |
| LUT + IR + Peukert, not formulas | Phase 2 | VRLA curve is individual per battery; only LUT + measured points work |
| model.json as persistent storage | Phase 1 | Discharge events are rare (monthly); SSD not worn by constant writes |
| SoH via area-under-curve (V×time) | Phase 4 | Only way to measure degradation without access to `calibrate.start` |

### Open Questions (Deferred to Planning)

- **Polling interval:** 5 sec vs 10 sec? (Phase 1 plan will confirm)
- **Shutdown threshold:** How many minutes before end is "safe" to trigger LB? (Phase 3 plan)
- **SoH alert threshold:** SoH < 80%? (Phase 4 plan)
- **Runtime alert threshold:** Time_rem@100% < X minutes? (Phase 4 plan, X value TBD)
- **Installation method:** Script, Makefile, or package? (Phase 5 plan)

---

## Session Continuity

**When you resume:** Check `Current Position` above. If phase is "Not started", start with `/gsd:plan-phase N` where N is the phase number.

**Blockers:** None currently.

**Last session:** Completed plan 02-06 (event classifier integration and event-driven shutdown logic). Phase 2 is now complete (6/6 plans).

**Next phase:** Phase 3 planning will integrate virtual UPS dummy-ups proxy, implement shutdown coordination, and provide transparent data source switching without changing Grafana dashboards.

**Lessons Learned:**

- Stateless socket polling (connect/send/recv/close per poll) enables automatic NUT restart recovery without daemon changes
- Socket timeout (2.0 sec) prevents daemon hangs; exceptions re-raised for daemon-level retry logic
- Mock socket parameter in __init__ provides clean testing without patching built-in socket module
- Python stdlib socket is sufficient; PyNUT library not needed for this use case
- Test fixtures (conftest.py) with Mock sockets provide comprehensive coverage for socket edge cases
- Inline configuration (environment variables) simplifies daemon deployment and testing vs separate config file
- JournalHandler with stderr fallback makes journald optional (graceful degradation if systemd unavailable)
- Type=simple systemd service with foreground daemon simpler than Type=forking with PID management
- Physical sensor invariant (input.voltage) beats firmware flag interpretation for reliable event classification (eliminates onlinedischarge_calibration bug)
- Voltage thresholds with hysteresis (>100V vs <50V) prevent oscillation in undefined range and provide clear decision boundaries

---

*State updated: 2026-03-14 after plan 02-01 completion — Phase 2 Wave 0 Test Infrastructure and Implementations COMPLETE*
