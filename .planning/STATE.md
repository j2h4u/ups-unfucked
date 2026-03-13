---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
current_plan: 03-04
status: completed
last_updated: "2026-03-14T00:30:00.000Z"
progress:
  total_phases: 6
  completed_phases: 2
  total_plans: 11
  completed_plans: 16
  percent: 100
---

# Project State — UPS Battery Monitor

**Last Updated:** 2026-03-14
**Current Focus:** Phase 3 in Progress (Plans 01-04 Complete, Wave 3 Systemd & NUT Configuration Finished)

---

## Project Reference

**Core Value:** Server shuts down cleanly and on time during blackout, using every available minute — without relying on unreliable CyberPower firmware readings.

**Key Constraint:** Use only two reliable physical measurements — battery.voltage and ups.load. Everything else (charge, runtime) is calculated.

**Deliverable:** One systemd service, zero manual intervention after install, production-ready.

---

## Current Position

**Phase:** 3
**Current Plan:** 03-05 (next)
**Status:** 03-04 COMPLETE
**Progress:** 4/4 plans completed Phase 3 (100%)

### Phase 3 Completed Plans

- [x] 03-01: Virtual UPS infrastructure (Wave 0) ✓ COMPLETE - 2026-03-14
  * Atomic tmpfs write function (write_virtual_ups_dev)
  * 9 test stubs for all Phase 3 requirements
  * 2 concrete tests (test_write_to_tmpfs, test_nut_format_compliance)
  * 88/88 tests passing (0 regressions)

- [x] 03-02: Virtual UPS status override & shutdown thresholds (Wave 1) ✓ COMPLETE - 2026-03-14
  * compute_ups_status_override() fully implemented with all EventType cases
  * 5 new tests: test_field_overrides, test_passthrough_fields, test_lb_flag_threshold, test_configurable_threshold, test_calibration_mode_threshold
  * LB flag boundary behavior verified (< not <=)
  * Configurable shutdown threshold validated across [1, 3, 5, 10] thresholds
  * 88/88 tests passing (0 regressions)

- [x] 03-03: Monitor virtual UPS integration (Wave 2) ✓ COMPLETE - 2026-03-13
  * write_virtual_ups_dev() integrated into monitor.py polling loop
  * Virtual metrics dict construction with 3 overrides + passthrough fields
  * compute_ups_status_override() called for every poll cycle
  * 3 new integration tests: test_monitor_virtual_ups_integration, test_monitor_virtual_ups_below_threshold, test_monitor_virtual_ups_error_handling
  * Error handling: tmpfs write failures logged but daemon continues
  * 91/91 tests passing (all phases, 0 regressions)

- [x] 03-04: Systemd configuration & NUT integration (Wave 3) ✓ COMPLETE - 2026-03-14
  * systemd service updated with After=sysinit.target dependency
  * Ensures /dev/shm tmpfs available before daemon starts
  * config/dummy-ups.conf created with NUT dummy-ups device configuration
  * Device parameters: driver=dummy-ups, port=/dev/shm/ups-virtual.dev, mode=dummy-once
  * CONTEXT.md extended with 5-step shutdown coordination flow documentation
  * Event classification (ONLINE, BLACKOUT_REAL, BLACKOUT_TEST) documented
  * Ready for Phase 5 installation and live NUT integration testing

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
| 03-03 | ~2 min | 3 | 91 (all phases) | 2026-03-13 |
| 03-04 | ~4 min | 3 | 91 (all phases) | 2026-03-14 |

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

**Last session:** Completed plan 03-03 (monitor virtual UPS integration Wave 2). Integrated write_virtual_ups_dev() call into monitor.py polling loop to construct and write virtual_metrics dict every poll cycle. Created 3 integration tests covering main flow, threshold variations, and error handling. All 91 tests passing (0 regressions).

**Next phase:** Plan 03-05 (Wave 4): Alert thresholds and model lifecycle (Phase 4).

**Lessons Learned (from 03-01, 03-02, 03-03, 03-04):**

- Tmpfs atomic writes (tempfile + fsync + rename) are essential for crash safety without SSD wear
- Event type enum provides clean pattern matching for conditional logic (ONLINE/BLACKOUT_REAL/BLACKOUT_TEST)
- Threshold-based decisions (< vs <=) require careful boundary testing to ensure correct behavior
- Integration tests validate end-to-end flows better than unit tests alone
- Error handling in polling loops should catch exceptions, log, and continue (non-fatal failures)
- Virtual UPS pattern: 3 computed overrides + passthrough fields preserves transparency
- Configurable thresholds via function parameters enable testing without environment variables
- Systemd dependency ordering critical: sysinit.target must precede network-online.target for tmpfs availability
- NUT dummy-ups mode=dummy-once provides atomic reads without CPU polling overhead
- Configuration provisioning artifacts (ready-for-install files) bridge development and operations

---

*State updated: 2026-03-14 after plan 03-04 completion — Phase 3 Wave 3 Systemd & NUT Configuration COMPLETE*
