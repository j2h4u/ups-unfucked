# Roadmap: UPS Battery Monitor v1.1

## Milestones

- ✅ **v1.0 MVP** — Phases 1-6 (shipped 2026-03-14)
- 🚧 **v1.1 Expert Panel Review Fixes** — Phases 7-11 (Phase 8 complete, Phases 7,9-11 in progress)

## Phases

<details>
<summary>✅ v1.0 MVP (Phases 1-6) — SHIPPED 2026-03-14</summary>

- [x] Phase 1: Foundation — NUT Integration & Core Infrastructure (5/5 plans) — completed 2026-03-13
- [x] Phase 2: Battery Model — State Estimation & Event Classification (6/6 plans) — completed 2026-03-14
- [x] Phase 3: Virtual UPS & Safe Shutdown (4/4 plans) — completed 2026-03-14
- [x] Phase 4: Health Monitoring & Battery Degradation (2/2 plans) — completed 2026-03-14
- [x] Phase 5: Operational Setup & Systemd Integration (2/2 plans) — completed 2026-03-14
- [x] Phase 6: Calibration Mode (2/2 plans) — completed 2026-03-14

</details>

<details open>
<summary>🚧 v1.1 Expert Panel Review Fixes (Phases 7-11) — IN PROGRESS</summary>

- [ ] **Phase 7: Safety-Critical Metrics** - Per-poll virtual UPS writes and LB flag during blackout (1/2 plans complete)
- [x] **Phase 8: Architecture Foundation** - Dataclass refactors and config extraction (4/4 plans complete)
- [ ] **Phase 9: Test Coverage** - Critical path tests (OL→OB→OL, Peukert, signal handler, conftest) (2/3 plans complete)
- [ ] **Phase 10: Code Quality & Efficiency** - Safe save helper, docstrings, batch writes, double logging fix
- [ ] **Phase 11: Polish & Future Prep** - History pruning, fsync optimization, EMA decoupling, logger cleanup, health endpoint

</details>

## Progress

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Foundation | v1.0 | 5/5 | Complete | 2026-03-13 |
| 2. Battery Model | v1.0 | 6/6 | Complete | 2026-03-14 |
| 3. Virtual UPS | v1.0 | 4/4 | Complete | 2026-03-14 |
| 4. Health Monitoring | v1.0 | 2/2 | Complete | 2026-03-14 |
| 5. Operational Setup | v1.0 | 2/2 | Complete | 2026-03-14 |
| 6. Calibration Mode | v1.0 | 2/2 | Complete | 2026-03-14 |
| 7. Safety-Critical Metrics | v1.1 | 1/2 | In progress | — |
| 8. Architecture Foundation | v1.1 | 4/4 | Complete (Wave 0 + Wave 1) | 2026-03-15 |
| 9. Test Coverage | v1.1 | 2/3 | In progress | — |
| 10. Code Quality & Efficiency | v1.1 | 0/5 | Not started | — |
| 11. Polish & Future Prep | v1.1 | 0/5 | Not started | — |

---

## Phase Details

### Phase 7: Safety-Critical Metrics

**Goal**: Eliminate LB flag lag by writing virtual UPS metrics every poll (10s) instead of every 60s during blackout events

**Depends on**: Nothing (Phase 1-6 complete)

**Requirements**: SAFE-01, SAFE-02

**Success Criteria** (what must be TRUE):
1. Virtual UPS metrics file (dummy-ups state) updated every 10s while OB state active (verified via file mtime during test blackout)
2. LB flag decision in `_handle_event_transition()` executes on every poll while OB state active (verified via debug log timestamps)
3. upsmon receives LB signal within 10s of actual out-of-battery condition (verified via systemd journal and upsmon log timing)
4. No metric writes occur during OL state (normal operations) — writes only during OB state transition (verified via file audit log)

**Plans**: 1/2 complete (Phase 7 Plan 01 done, Plan 02 in progress)

---

### Phase 8: Architecture Foundation

**Goal**: Eliminate untyped dicts and module-level globals by refactoring to frozen dataclasses for config and metrics

**Depends on**: Phase 7 (minor: safety logic in place before refactor)

**Requirements**: ARCH-01, ARCH-02, ARCH-03

**Success Criteria** (what must be TRUE):
1. `current_metrics` dict replaced with `CurrentMetrics` dataclass with 9 typed fields (soc, battery_charge, time_rem_minutes, event_type, transition_occurred, shutdown_imminent, ups_status_override, previous_event_type, timestamp) — verified by type checking and test instantiation
2. `_cfg`, `UPS_NAME`, `MODEL_DIR`, `SHUTDOWN_THRESHOLD_MINUTES`, `SOH_THRESHOLD` module-level globals extracted into `Config` frozen dataclass passed to MonitorDaemon.__init__ (verified by constructor signature and no module-level state pollution)
3. All imports (`from enum import Enum`, `from src.soh_calculator import interpolate_cliff_region`) moved to module top, no late imports in method bodies (verified via code inspection and linting)
4. Existing tests pass without modification — dataclass refactor is internal only (verified by running test suite)

**Plans**:
- [x] 08-00-PLAN.md — Wave 0: Test fixtures and stubs (current_metrics_fixture, config_fixture, test stubs) — completed 2026-03-15
- [x] 08-01-PLAN.md — Wave 1: CurrentMetrics dataclass refactor (ARCH-01) — completed 2026-03-15
- [x] 08-02-PLAN.md — Wave 1: Config dataclass extraction (ARCH-02) — completed 2026-03-15
- [x] 08-03-PLAN.md — Wave 1: Import consolidation (ARCH-03) — completed 2026-03-15

---

### Phase 9: Test Coverage

**Goal**: Test critical paths (OL→OB→OL lifecycle, Peukert calibration, signal handler) and fix test infrastructure issues

**Depends on**: Phase 8 (dataclass refactors make mocking easier)

**Requirements**: TEST-01, TEST-02, TEST-03, TEST-04, TEST-05

**Success Criteria** (what must be TRUE):
1. Integration test covers full OL→OB→OL discharge cycle: `_handle_event_transition()` → `_update_battery_health()` → `_track_discharge()` as connected flow (verified by test execution and coverage report)
2. Peukert auto-calibration method `_auto_calibrate_peukert()` has unit tests for math and edge cases (divide by zero, empty history, etc.) (verified by test execution)
3. Signal handler `_signal_handler()` tested: SIGTERM triggers model save (verified by mock SIGTERM injection in test)
4. `conftest.py` `mock_socket_ok` returns proper multi-line LIST VAR response format for `get_ups_vars()` testing (verified by test execution and no "unparseable response" errors)
5. Floating-point comparison in `soc_from_voltage()` replaced with tolerance-based check or documented as safe (verified by code inspection and test with similar voltage values)

**Plans**:
- [x] 09-01-PLAN.md — Wave 1: Test infrastructure fix (TEST-04, TEST-05) — completed 2026-03-14
- [x] 09-02-PLAN.md — Wave 1: Peukert + signal handler tests (TEST-02, TEST-03) — completed 2026-03-14
- [ ] 09-03-PLAN.md — Wave 2: OL→OB→OL integration test (TEST-01) — pending

---


### Phase 10: Code Quality & Efficiency

**Goal**: Reduce code duplication, fix docstrings, optimize writes, and eliminate double-logging

**Depends on**: Phase 9 (tests verify correctness after refactors)

**Requirements**: QUAL-01, QUAL-02, QUAL-03, QUAL-04, QUAL-05

**Success Criteria** (what must be TRUE):
1. Repeated `try/except OSError` for `model.save()` extracted to `_safe_save()` helper function used in 4 places (verified by grep for `try:.*save()` showing 0 occurrences after refactor)
2. Hardcoded date `'2026-03-13'` in `_default_vrla_lut()` replaced with `datetime.now().strftime('%Y-%m-%d')` (verified by code inspection and test with mocked datetime)
3. `soc_from_voltage()` docstring corrected: either says "linear scan" or updated implementation uses binary search (verified by docstring and code inspection match)
4. `calibration_write()` batches points in memory, single `model.save()` per REPORTING_INTERVAL instead of per-point atomic writes (verified by test showing N points → 1 save, and model.json mtime audit log)
5. Double error log in `virtual_ups.py` (lines 90 and 93) eliminated — single handler logs the failure once (verified by code inspection and test with induced error)

**Plans**: TBD

---

### Phase 11: Polish & Future Prep

**Goal**: Clean up future technical debt, optimize system calls, and add observability hooks

**Depends on**: Phase 10 (all core fixes done, now polish)

**Requirements**: LOW-01, LOW-02, LOW-03, LOW-04, LOW-05

**Success Criteria** (what must be TRUE):
1. `soh_history` and `r_internal_history` lists in model.json have pruning logic — old entries removed when list > N items (verified by test creating synthetic large history and verifying pruning)
2. `atomic_write_json()` uses `os.fdatasync()` instead of `os.fsync()` (metadata sync skipped) (verified by code inspection — function signature and strace log showing fdatasync call)
3. EMAFilter voltage/load tracking decoupled into generic per-metric base class (prepare for temperature sensor addition) (verified by code inspection showing EMA reusable for new metrics)
4. `setup_ups_logger()` wrapper in alerter.py removed — direct `logging.getLogger()` calls used (verified by code inspection showing no setup_ups_logger calls)
5. Daemon health endpoint added — last poll time and current SoC exposed via file for external monitoring tools (verified by file exists, contains valid JSON, updates on poll)

**Plans**: TBD

---

*Roadmap updated: 2026-03-14 after Phase 9 Plan 02 completion (TEST-02, TEST-03)*
