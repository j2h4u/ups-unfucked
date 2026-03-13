---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
current_plan: Not started
status: completed
last_updated: "2026-03-13T23:18:57.270Z"
progress:
  total_phases: 6
  completed_phases: 6
  total_plans: 21
  completed_plans: 22
---

# Project State — UPS Battery Monitor

**Last Updated:** 2026-03-14 T23:15Z
**Current Focus:** Phase 6 Plan 02 Wave 1 Complete (Calibration Mode Integration & LUT Interpolation)

---

## Project Reference

**Core Value:** Server shuts down cleanly and on time during blackout, using every available minute — without relying on unreliable CyberPower firmware readings.

**Key Constraint:** Use only two reliable physical measurements — battery.voltage and ups.load. Everything else (charge, runtime) is calculated.

**Deliverable:** One systemd service, zero manual intervention after install, production-ready.

---

## Current Position

**Phase:** 06
**Current Plan:** Not started
**Status:** Milestone complete
**Progress:** 2/5 plans completed Phase 6 (22/19 cumulative)

### Phase 6 Completed Plans

- [x] 06-01: Calibration Mode Wave 0 (Flag & Threshold) ✓ COMPLETE - 2026-03-14
  * Implemented --calibration-mode flag parsing (argparse)
  * Override shutdown threshold: 1 min (calibration) vs 5 min (normal)
  * Implemented BatteryModel.calibration_write() with fsync persistence
  * Implemented interpolate_cliff_region() for LUT gap-filling
  * Wired calibration_mode into event handling for discharge buffer writes
  * Created tests/test_monitor.py: 6 comprehensive tests
  * Created tests/test_model.py additions: 5 calibration_write tests
  * Created tests/test_soh_calculator.py additions: 5 interpolation tests
  * All tests passing: 146/146 (Phase 1-6 test coverage)

- [x] 06-02: Calibration Mode Wave 1 (Integration & Interpolation) ✓ COMPLETE - 2026-03-14
  * Integrated interpolate_cliff_region() into monitor.py OB→OL transition handler
  * Implemented BatteryModel.update_lut_from_calibration() for persistent LUT updates
  * Added calibration_mode conditional logic: if BLACKOUT_TEST→ONLINE transition
  * Automatic cliff region interpolation on discharge completion
  * LUT persisted with source field tracking (measured/interpolated/standard/anchor)
  * Completion logging: "Calibration complete; remove --calibration-mode for normal operation"
  * Created test_monitor.py: 4 integration tests + 1 end-to-end test
  * Created test_model.py: 4 update_lut_from_calibration() tests
  * Created test_soh_calculator.py: 5 cliff interpolation tests
  * All tests passing: 160/160 (Phase 1-6 test coverage, +14 new tests)

### Phase 5 Completed Plans

- [x] 05-01: Installation Script (Wave 0) ✓ COMPLETE - 2026-03-13
  * Created scripts/install.sh: Deploy daemon to systemd
  * Installs service unit, sets permissions, enables auto-start
  * Ready for production installation

- [x] 05-02: Systemd Integration Verification (Wave 1) ✓ COMPLETE - 2026-03-14
  * Verified systemd service unit against all OPS requirements
  * Created tests/test_systemd_integration.py: 9 comprehensive tests
  * All tests passing: 130/130 (Phase 1-5 test coverage)
  * Service ready for production deployment

### Phase 4 Completed Plans

- [x] 04-01: Health Monitoring Foundation (Wave 0) ✓ COMPLETE - 2026-03-14
  * Implemented soh_calculator.py: Area-under-curve SoH calculation via trapezoidal rule
  * Implemented replacement_predictor.py: Linear regression degradation trend analysis
  * Implemented alerter.py: Journald structured logging for health threshold alerts
  * 24 new unit tests: 8 per module (test_soh_calculator, test_replacement_predictor, test_alerter)
  * All 115 tests passing (91 prior phases + 24 new, 0 regressions)
  * Ready for integration into monitor.py (Plan 02)

- [x] 04-02: Health Monitoring Integration (Wave 1) ✓ COMPLETE - 2026-03-14
  * Integrated Phase 4 modules into monitor.py polling loop
  * Added discharge_buffer tracking for voltage/time samples during blackout
  * Implemented _update_battery_health() method: SoH calc → prediction → alerts
  * Called on OB→OL transition (discharge event completion)
  * Updates model.json soh_history with new entry (date + SoH)
  * Predicts replacement date via linear regression (3+ points required)
  * Triggers journald alerts if SoH < 80% or runtime@100% < 20 min
  * Created scripts/motd/51-ups-health.sh: Real-time UPS health MOTD display
  * MOTD displays: status, charge%, runtime, load%, SoH%, replacement date
  * Color-coded output: green (healthy), yellow (warning), red (critical)
  * All 115 tests passing, zero regressions
  * Health monitoring pipeline complete and operational

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
| 04-01 | 15 min | 3 | 115 (all phases) | 2026-03-14 |
| 04-02 | ~20 min | 2 | 115 (all phases) | 2026-03-14 |
| 05-01 | ~15 min | 2 | 91 (install.sh + existing tests) | 2026-03-13 |
| 05-02 | ~12 min | 2 | 130 (all phases) | 2026-03-14 |
| 06-01 | ~12 min | 5 | 146 (all phases) | 2026-03-14 |
| 06-02 | ~25 min | 5 | 160 (all phases) | 2026-03-14 |

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

**Last session:** Completed plan 06-01 Wave 0 (Calibration Mode Flag & Threshold Override). Implemented --calibration-mode flag with argparse, reduced shutdown threshold to 1 min in calibration mode (vs 5 min normal). Created BatteryModel.calibration_write() for atomic fsync writes during discharge testing. Implemented interpolate_cliff_region() for linear interpolation of cliff region (11.0V–10.5V) with source field tracking ('measured', 'interpolated'). Wired calibration_mode into event handling to collect discharge buffer points and write via calibration_write() every ~60 seconds. Test suite: 146/146 passing (16 new tests for Phase 6 + 130 Phase 1-5 tests, 0 regressions).

**Next phase:** Plan 06-02 (Wave 1): Integrate interpolate_cliff_region() into discharge completion flow; test full calibration cycle (collect → write → interpolate → persist); verify LUT quality improvements with measured/interpolated entries.

**Lessons Learned (from 06-01):**

- Calibration mode threshold (1 min) allows battery to discharge closer to cutoff (10.5V) for better measured data coverage in low-voltage cliff region
- Instance variables (self.calibration_mode) vs sys.argv polling ensure flag is immutable after startup (no runtime polling)
- Discharge buffer batching (6 polls ≈ 60 seconds) minimizes fsync overhead during calibration without blocking polling loop
- Source field tracking ('measured', 'interpolated') prevents auto-interpolation from overwriting manual calibration data; enables rollback if needed
- Linear interpolation fills gaps at 0.1V resolution; adequate for VRLA discharge curve smoothing without overfitting
- Duplicate prevention (±0.01V tolerance) avoids redundant LUT entries from noisy voltage readings
- Atomic write + fsync pattern reused from Phase 1 ensures calibration data survives power loss during write


**Lessons Learned (from 05-01 and 05-02):**

- Systemd .service files are INI-style (ConfigParser-compatible); direct parsing without systemctl enables CI/unit-test friendly validation
- Service file verification requires 0 root privileges; tests validate [Unit], [Service], [Install] sections independently
- Restart throttling (StartLimitBurst=3, StartLimitIntervalSec=60) prevents infinite crash loops; on-failure respects clean exits
- User=unprivileged + StandardOutput=journal ensures security + observability without privilege escalation
- SyslogIdentifier enables precise journalctl filtering; downstream log analysis benefits from tagged output
- Soft dependencies (ConditionPathExists) prevent hard failures when optional resources (NUT socket) unavailable
- WorkingDirectory + PYTHONPATH enables Python module discovery without relying on shell environment

**Lessons Learned (from 04-01 and 04-02):**

- Area-under-curve via trapezoidal rule captures non-linear VRLA discharge shapes (exponential tail)
- Least-squares regression without scipy avoids dependencies; math O(n) with simple stability checks
- R² > 0.5 validation rejects noisy trends; conservative but appropriate for small datasets (3–20 points)
- Fire-and-forget journald alerts (no deduplication) simplify daemon logic; journald handles filtering
- Structured extra fields (BATTERY_SOH, THRESHOLD, DAYS_TO_REPLACEMENT) enable journalctl queries
- SysLogHandler fallback to stderr keeps code testable (no /dev/log dependency in tests)
- Non-uniform time intervals Δt in discharge profiles require weighted trapezoidal rule, not simple averaging
- Anchor voltage trimming (10.5V) at physical limit prevents false calibration from incomplete discharge
- Reference area (12V × 2820s) empirically derived from 2026-03-12 blackout; baseline not constant
- Proportional degradation model: new_soh = reference_soh × (measured_area / reference_area) preserves monotonicity
- Discharge buffer pattern allows event-driven sample collection during states (BLACKOUT_REAL) before consuming on transition (OB→OL)
- MOTD script integration requires robust error handling (upsc may not be available, model.json may not exist, jq may fail)
- Configuration via environment variables (SOH_THRESHOLD, RUNTIME_THRESHOLD_MINUTES) enables testing without code changes
- SoH history requires 3+ points for meaningful regression; prediction gracefully degrades if insufficient data

**Lessons Learned (from 06-01 and 06-02):**

- Calibration mode threshold (1 min) allows battery to discharge closer to cutoff (10.5V) for better measured data coverage in low-voltage cliff region
- Instance variables (self.calibration_mode) vs sys.argv polling ensure flag is immutable after startup (no runtime polling)
- Discharge buffer batching (6 polls ≈ 60 seconds) minimizes fsync overhead during calibration without blocking polling loop
- Source field tracking ('measured', 'interpolated') prevents auto-interpolation from overwriting manual calibration data; enables rollback if needed
- Linear interpolation fills gaps at 0.1V resolution; adequate for VRLA discharge curve smoothing without overfitting
- Duplicate prevention (±0.01V tolerance) avoids redundant LUT entries from noisy voltage readings
- Atomic write + fsync pattern reused from Phase 1 ensures calibration data survives power loss during write
- Cliff region = 11.0V to 10.5V; interpolation separates cliff measured points from non-cliff (all entries below 10.5V or above 11.0V preserved)
- Integration into existing _handle_event_transition() flow minimizes code duplication and leverages existing discharge buffer clearing
- End-to-end testing without real hardware validates full calibration workflow before operator manual testing on real UPS


*State updated: 2026-03-14 T23:15Z after plan 06-02 completion — Phase 6 Wave 1 Calibration Mode Integration COMPLETE*
