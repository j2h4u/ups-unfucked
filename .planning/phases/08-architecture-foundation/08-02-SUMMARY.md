---
phase: 08-architecture-foundation
plan: 02
subsystem: architecture
tags: [dataclass, config, testability, frozen, immutability, refactoring]

# Dependency graph
requires:
  - phase: 08-00
    provides: "Planning and requirements for phase 8"
  - phase: 08-01
    provides: "CurrentMetrics dataclass, forming basis for Config pattern"
provides:
  - "Config frozen dataclass with 13 fields replacing module-level globals"
  - "MonitorDaemon.__init__(config: Config) parameter injection enabling testability"
  - "All global references (polling_interval, nut_host, shutdown_minutes, etc.) now config attributes"
  - "Immutable configuration preventing runtime mutation"
  - "config_fixture pytest fixture returning Config instance"
  - "test_config_dataclass and test_config_immutability tests validating frozen semantics"
affects:
  - "08-03 (imports cleanup depends on config refactor complete)"
  - "09 (test coverage can now mock configs easily)"
  - "All future daemon extensions (multi-UPS, reconfiguration) enabled by config injection"

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Frozen dataclass for immutable configuration objects"
    - "Dependency injection of config parameter to daemon constructor"
    - "Module-level backward compatibility exports (UPS_NAME, MODEL_DIR) from _default_config"
    - "Config instance created at startup in main() and passed to daemon"

key-files:
  created: []
  modified:
    - "src/monitor.py - Added Config dataclass, refactored _load_config(), updated MonitorDaemon.__init__, migrated all global references to config attributes"
    - "tests/conftest.py - Updated config_fixture to return Config instance instead of dict"
    - "tests/test_monitor.py - Implemented test_config_dataclass and test_config_immutability, updated make_daemon and test_auto_calibration fixtures"

key-decisions:
  - "Config frozen=True prevents accidental mutation; enables contracts on passed configuration"
  - "Kept module-level backward-compat exports (UPS_NAME, MODEL_DIR, MODEL_PATH) for scripts importing from monitor"
  - "_default_config created at module load for non-daemon code; daemon always receives injected config"
  - "reporting_interval_polls computed dynamically from config.reporting_interval / config.polling_interval to avoid hardcoded assumptions"

requirements-completed: [ARCH-02]

# Metrics
duration: "18min"
completed: "2026-03-14"
---

# Phase 8 Plan 2: Config Extraction Summary

**Immutable Config frozen dataclass with 13 fields replaces module-level globals; MonitorDaemon accepts config parameter enabling testability and future multi-UPS support**

## Performance

- **Duration:** 18 min
- **Started:** 2026-03-14T14:44:59Z
- **Completed:** 2026-03-14T15:02:00Z (estimated)
- **Tasks:** 6 completed (all auto)
- **Files modified:** 3

## Accomplishments

- Config frozen dataclass defined with 13 fields (ups_name, polling_interval, reporting_interval, nut_host, nut_port, nut_timeout, shutdown_minutes, soh_alert_threshold, model_dir, config_dir, runtime_threshold_minutes, reference_load_percent, ema_window_sec)
- _load_config() refactored to return Config instance instead of dict; moved Config definition before _load_config to enable forward reference
- MonitorDaemon.__init__(config: Config) signature updated; self.config stored for use throughout instance methods
- All global constant references migrated to config attributes:
  - SHUTDOWN_THRESHOLD_MINUTES → config.shutdown_minutes
  - SOH_THRESHOLD → config.soh_alert_threshold
  - UPS_NAME → config.ups_name
  - NUT_HOST/PORT/TIMEOUT → config.nut_*
  - POLL_INTERVAL → config.polling_interval
  - EMA_WINDOW → config.ema_window_sec
  - REPORTING_INTERVAL_POLLS computed on-the-fly from config.reporting_interval / config.polling_interval
  - RUNTIME_THRESHOLD_MINUTES → config.runtime_threshold_minutes
  - REFERENCE_LOAD_PERCENT → config.reference_load_percent
- main() and run() updated to create Config instance and pass to MonitorDaemon
- config_fixture in conftest.py updated to return Config dataclass with test values (tmp_path integration for model_dir/config_dir)
- test_config_dataclass() verifies all 13 fields with fixture values and custom instantiation
- test_config_immutability() validates FrozenInstanceError raised on attempted field mutation
- make_daemon() fixture updated to accept config_fixture parameter and pass to MonitorDaemon
- test_auto_calibration_end_to_end() updated to accept and use config_fixture
- All 14 tests passing (no regressions); both new config tests pass

## Task Commits

Single atomic commit for entire plan:

1. **ARCH-02 Complete** - `e6139f2` (feat)

## Files Created/Modified

- `src/monitor.py` - Config dataclass added (lines ~44-68), _load_config() updated to return Config, MonitorDaemon.__init__(config: Config) refactored, all method body references updated to self.config.*, module-level backward-compat exports (UPS_NAME, SHUTDOWN_THRESHOLD_MINUTES, SOH_THRESHOLD, MODEL_DIR, MODEL_PATH) use _default_config
- `tests/conftest.py` - config_fixture updated to import Config and return instance instead of dict (lines ~179-207)
- `tests/test_monitor.py` - make_daemon updated to accept config_fixture (line 15), test_auto_calibration_end_to_end accepts config_fixture (line 359), test_config_dataclass implemented with 13 field assertions and custom instantiation (lines 445-481), test_config_immutability implemented with FrozenInstanceError validation (lines 484-495)

## Decisions Made

- **Immutability contract:** Config frozen=True selected over regular dataclass to prevent accidental runtime mutation and communicate immutability intent clearly
- **Backward compatibility:** Module-level exports (UPS_NAME, MODEL_DIR, MODEL_PATH) retained for scripts that import from monitor directly; they reference _default_config
- **Config creation timing:** Config created once at module load (_default_config) for module-level references, and again in main() for daemon injection; daemon always receives fresh/injected config enabling test isolation
- **Dynamic reporting_interval_polls:** Instead of hardcoding 6 polls, compute from config.reporting_interval // config.polling_interval in run() and error handling, enabling flexible configuration
- **All global refs in Config:** Consolidated all daemon-relevant parameters (NUT settings, thresholds, intervals, paths) into Config; no loose globals remain in MonitorDaemon methods

## Deviations from Plan

None - plan executed exactly as written. Config dataclass with all 13 fields created, _load_config refactored, MonitorDaemon.__init__ updated, all global references migrated, fixtures updated, tests passing.

## Issues Encountered

None - implementation straightforward. Forward reference issue (Config used in _load_config before definition) resolved by moving Config definition before _load_config.

## Next Phase Readiness

- Config injection pattern fully working, testable with different configurations
- Basis laid for ARCH-03 (imports cleanup)
- All 14 tests passing provides confidence for next phases
- Phase 8 Plan 3 (ARCH-03) can proceed immediately: only 2 import fixes remaining

---

*Phase: 08-architecture-foundation*
*Plan: 02*
*Completed: 2026-03-14*
