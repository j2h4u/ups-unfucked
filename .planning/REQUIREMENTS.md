# Requirements: UPS Battery Monitor v3.1

**Defined:** 2026-03-20
**Core Value:** Сервер выключается чисто и вовремя при блекауте, используя каждую минуту — не полагаясь на прошивку CyberPower.

**Design principle:** When choosing between implementations, pick the one with greater accuracy.

## v3.1 Requirements

Requirements for v3.1 Code Quality Hardening. Structural improvements from 8-agent code quality review.

### Architecture

- [x] **ARCH-01**: Coulomb counting unified into single `integrate_current()` in battery_math with per-step load support
- [x] **ARCH-02**: `_check_alerts` receives avg_load as parameter instead of recomputing
- [x] **ARCH-03**: SagTracker extracted from MonitorDaemon into own module
- [x] **ARCH-04**: SchedulerManager extracted from MonitorDaemon into own module
- [x] **ARCH-05**: DischargeCollector extracted from MonitorDaemon (sample accumulation, calibration writes)
- [x] **ARCH-06**: `_score_and_persist_sulfation` split into compute / persist / log methods

### Naming

- [x] **NAME-01**: `BatteryModel.data` renamed to `state` across entire codebase
- [x] **NAME-02**: `category` renamed to `power_source` in EventClassifier.classify()
- [x] **NAME-03**: `rls` / `d` variables cleaned up in `_sync_physics_from_state`

### Documentation

- [x] **DOC-01**: `_handle_capacity_convergence` write-once behavior documented
- [x] **DOC-02**: `_opt_round` docstring added (monitor_config.py)
- [x] **DOC-03**: Dedup inline comment moved to `_prune_lut` docstring
- [x] **DOC-04**: Buffer start time comment moved to `_classify_discharge_trigger` docstring

### Tests

- [x] **TEST-01**: Mock call sequence replay replaced with outcome assertions (test_monitor.py)
- [x] **TEST-02**: Eager test split into focused single-behavior tests (test_monitor.py)
- [x] **TEST-03**: Fragile Path patching replaced with dependency injection (test_virtual_ups.py)
- [x] **TEST-04**: Private helper assertions replaced with outcome assertions (test_monitor.py)
- [x] **TEST-05**: Integration tests use real collaborators instead of internal mocks (test_monitor_integration.py)
- [x] **TEST-06**: Monte Carlo test marked slow with seed dependency documented
- [x] **TEST-07**: test_motd.py marked as integration test (environment-dependent)
- [x] **TEST-08**: Tautological assertion replaced with content assertion
- [x] **TEST-09**: Assertion roulette fixed with descriptive messages

### Security & Observability

- [x] **SEC-01**: Temperature resolved — check NUT variable, fix logging approach
- [x] **SEC-02**: NUT empty PASSWORD documented as security dependency
- [x] **SEC-03**: model.json scheduling state field-level validation
- [x] **SEC-04**: atomic_write cleanup error logged (failed unlink during exception)

## v3.2+ Requirements

Deferred to future release.

### Advanced Detection

- **ADV-01**: Discharge curve shape analysis (cliff region expansion as sulfation indicator)
- **ADV-02**: Peukert exponent auto-calibration from deep discharge data
- **ADV-03**: Cliff-edge degradation detector (Bayesian SoH inertia)
- **TEMP-02**: Seasonal thermal correction based on ambient temperature variation

## Out of Scope

| Feature | Reason |
|---------|--------|
| MonitorDaemon full rewrite | Incremental extraction, not rewrite — preserve working code |
| Test framework migration | Stay on pytest, improve test quality within current framework |
| New features or behavior changes | Pure structural improvement — no user-visible changes |
| Backward compatibility shims | Single server, single user — not needed |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| ARCH-01 | 18 | Complete |
| ARCH-02 | 18 | Complete |
| ARCH-03 | 19 | Complete |
| ARCH-04 | 20 | Complete |
| ARCH-05 | 21 | Complete |
| ARCH-06 | 21 | Complete |
| NAME-01 | 22 | Complete |
| NAME-02 | 22 | Complete |
| NAME-03 | 22 | Complete |
| DOC-01 | 22 | Complete |
| DOC-02 | 22 | Complete |
| DOC-03 | 22 | Complete |
| DOC-04 | 22 | Complete |
| TEST-01 | 23 | Complete |
| TEST-02 | 23 | Complete |
| TEST-03 | 23 | Complete |
| TEST-04 | 23 | Complete |
| TEST-05 | 23 | Complete |
| TEST-06 | 23 | Complete |
| TEST-07 | 23 | Complete |
| TEST-08 | 23 | Complete |
| TEST-09 | 23 | Complete |
| SEC-01 | 24 | Complete |
| SEC-02 | 24 | Complete |
| SEC-03 | 24 | Complete |
| SEC-04 | 24 | Complete |

**Coverage:**
- v3.1 requirements: 26 total
- Mapped to phases: 26
- Unmapped: 0

**Phase Distribution:**
- Phase 18 (Unify Coulomb Counting): 2 requirements
- Phase 19 (Extract SagTracker): 1 requirement
- Phase 20 (Extract SchedulerManager): 1 requirement
- Phase 21 (Extract DischargeCollector): 2 requirements
- Phase 22 (Naming + Docs Sweep): 7 requirements
- Phase 23 (Test Quality Rewrite): 9 requirements
- Phase 24 (Temperature + Security Hardening): 4 requirements

---

*Requirements defined: 2026-03-20*
*Last updated: 2026-03-20 after initial definition*
