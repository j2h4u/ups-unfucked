---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
current_plan: 03-03
status: executing
last_updated: "2026-03-14T00:53:00.000Z"
progress:
  total_phases: 6
  completed_phases: 2
  total_plans: 11
  completed_plans: 14
  percent: 100
---

# Project State — UPS Battery Monitor

**Last Updated:** 2026-03-14
**Current Focus:** Phase 3 in Progress (Plans 01-02 Complete)

---

## Project Reference

**Core Value:** Server shuts down cleanly and on time during blackout, using every available minute — without relying on unreliable CyberPower firmware readings.

**Key Constraint:** Use only two reliable physical measurements — battery.voltage and ups.load. Everything else (charge, runtime) is calculated.

**Deliverable:** One systemd service, zero manual intervention after install, production-ready.

---

## Current Position

**Phase:** 3
**Current Plan:** 03-03 (next)
**Status:** 03-02 COMPLETE
**Progress:** 2/4 plans completed Phase 3 (50%)

### Phase 3 Completed Plans

- [x] 03-01: Virtual UPS infrastructure (Wave 0) ✓ COMPLETE - 2026-03-14
  * Atomic tmpfs write function (write_virtual_ups_dev)
  * 9 test stubs for all Phase 3 requirements
  * 2 concrete tests (test_write_to_tmpfs, test_nut_format_compliance)
  * 87/87 tests passing (0 regressions)

- [x] 03-02: Virtual UPS status override & shutdown thresholds (Wave 1) ✓ COMPLETE - 2026-03-14
  * compute_ups_status_override() fully implemented with all EventType cases
  * 5 new tests: test_field_overrides, test_passthrough_fields, test_lb_flag_threshold, test_configurable_threshold, test_calibration_mode_threshold
  * LB flag boundary behavior verified (< not <=)
  * Configurable shutdown threshold validated across [1, 3, 5, 10] thresholds
  * 88/88 tests passing (0 regressions)

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
| 03-01 | 10 min | 3 | 87 (all phases) | 2026-03-14 |
| 03-02 | ~8 min | 4 | 88 (all phases) | 2026-03-14 |

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

**Last session:** Completed plan 03-02 (virtual UPS status override & shutdown thresholds Wave 1). Implemented compute_ups_status_override() with all EventType cases. Created 5 new tests covering field overrides, passthrough fields, LB flag threshold logic, configurable thresholds, and calibration mode thresholds. All 88 tests passing (0 regressions).

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

*State updated: 2026-03-14 after plan 03-02 completion — Phase 3 Wave 1 Status Override & Shutdown Thresholds COMPLETE*
