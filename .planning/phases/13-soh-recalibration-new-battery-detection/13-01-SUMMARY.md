---
phase: 13-soh-recalibration-new-battery-detection
plan: 01
subsystem: battery_health
tags: [soh-normalization, capacity-baseline-tagging, regression-filtering]
dependency_graph:
  requires: [Phase-12-capacity-estimation]
  provides: [SOH-01-capacity-normalization, SOH-02-history-versioning, SOH-03-regression-filtering]
  affects: [Phase-13-02-new-battery-detection, Phase-14-capacity-reporting]
tech_stack:
  added: []
  patterns: [orchestrator-pattern, tuple-return-values]
  modified: [model.py, replacement_predictor.py, monitor.py, soh_calculator.py-new]
key_files:
  created:
    - src/soh_calculator.py (orchestrator layer for capacity selection)
    - tests/test_soh_calculator.py (SOH-01 unit tests)
  modified:
    - src/model.py (add_soh_history_entry extended)
    - src/replacement_predictor.py (linear_regression_soh extended)
    - src/monitor.py (_update_battery_health integration)
    - tests/test_model.py (SOH-02 tests added)
    - tests/test_replacement_predictor.py (SOH-03 tests added)
    - tests/test_monitor.py (fixture updates)
decisions:
  - Created separate soh_calculator.py orchestrator module (not integrated into monitor.py directly)
  - Orchestrator reads convergence status and selects measured vs. rated capacity at call time
  - SoH history tagged with capacity_ah_ref when entry created; backward compatible (None = no field)
  - Regression model filters by capacity_ah_ref using .get() with 7.2Ah default for old entries
  - Monitor.py integrated to pass battery_model to orchestrator and tag history with returned capacity_ah
execution_time: "12 minutes"
metrics:
  completed_date: "2026-03-16"
  duration_seconds: 720
  tasks_completed: 7
  tests_added: 8
  tests_passing: 10_integration_plus_278_full_suite
  files_modified: 6
  files_created: 2
  commits: 12
---

# Phase 13 Plan 01: SoH Capacity Normalization & History Versioning

**One-liner:** Implemented orchestrator layer for capacity-aware SoH calculation with history baseline tagging and regression filtering, enabling separation of aging from capacity loss.

## Summary

Successfully implemented all three core requirements for Phase 13 Plan 01:

### Requirement SOH-01: Capacity Normalization
- **Created:** `src/soh_calculator.py` orchestrator function
- **Logic:** Reads `battery_model.get_convergence_status()` to determine if Phase 12 capacity estimation has converged
- **Behavior:**
  - If `converged=True`: Uses measured capacity (`latest_ah`) from convergence data
  - If `converged=False`: Falls back to rated capacity (`get_capacity_ah()` = 7.2Ah)
- **Returns:** Tuple `(soh_new, capacity_ah_used)` for caller to tag history entry
- **Integration:** Monitor._update_battery_health() wired to pass battery_model and handle tuple return

### Requirement SOH-02: SoH History Versioning
- **Modified:** `src/model.py:add_soh_history_entry()` method signature
- **Signature:** `add_soh_history_entry(date, soh, capacity_ah_ref=None)`
- **Behavior:**
  - When `capacity_ah_ref` provided: Stored in entry dict as rounded float (2 decimals)
  - When `None`: Entry omitted field (backward compatible with old entries)
- **Result:** Single `soh_history` array with mixed entries (some tagged, some not)
- **Backward compat:** Old entries without field default to 7.2Ah in filtering logic

### Requirement SOH-03: Regression Filtering by Baseline
- **Modified:** `src/replacement_predictor.py:linear_regression_soh()` function
- **Signature:** Added optional `capacity_ah_ref` parameter (default=None)
- **Behavior:**
  - When `capacity_ah_ref` provided: Filters history to only entries matching baseline
  - Uses `.get('capacity_ah_ref', 7.2)` to default old entries to 7.2Ah
  - Returns `None` if fewer than 3 entries match baseline (minimum guard per-baseline)
  - Backward compatible: When `capacity_ah_ref=None`, uses all entries as before
- **Separation:** Battery replacement (new capacity baseline) automatically excludes old entries from regression

## Test Coverage

All 8 unit tests passing (SOH-01, SOH-02, SOH-03):

### SOH-01 Capacity Selection (2 tests)
- `test_soh_with_measured_capacity`: Verifies measured capacity (6.8Ah) used when converged=True
- `test_soh_with_rated_capacity_fallback`: Verifies rated capacity (7.2Ah) used when converged=False

### SOH-02 History Versioning (3 tests)
- `test_soh_history_entry_with_baseline`: Verifies capacity_ah_ref stored when provided
- `test_soh_history_entry_backward_compat`: Verifies backward compatibility (no field when None)
- `test_mixed_baseline_entries`: Verifies old/new entries coexist in single array

### SOH-03 Regression Filtering (3 tests)
- `test_regression_filters_by_baseline`: Verifies different-baseline entries excluded
- `test_regression_backward_compat`: Verifies missing field defaults to 7.2Ah
- `test_regression_min_entries_per_baseline`: Verifies 3+ entries required per baseline

### Integration Tests (2 tests in test_monitor.py)
- `test_discharge_buffer_cleared_after_health_update`: Verifies orchestrator executed in health update flow
- `test_ol_ob_ol_discharge_lifecycle_complete`: Verifies full OL→OB→OL cycle with soh_calculator integration

## Deviations from Plan

### Auto-fixed Issues (Rule 2: Missing Critical Functionality)

**1. [Rule 2 - Missing Integration] Orchestrator not wired into monitor.py**
- **Found during:** Task 3 (created soh_calculator.py) → Task 7 (test failures)
- **Issue:** soh_calculator.py existed but monitor.py still called old interface with `capacity_ah` parameter directly
- **Fix:** Updated monitor._update_battery_health() to:
  - Pass `battery_model` to orchestrator (not `capacity_ah`)
  - Handle tuple return `(soh_new, capacity_ah_used)`
  - Tag history entry with returned capacity_ah via new `capacity_ah_ref` parameter
- **Commits:** 79236fe (integration), 41cde55 (test mock update)

**2. [Rule 3 - Blocking Import Error] interpolate_cliff_region imported from wrong module**
- **Found during:** Task 7 (test suite run)
- **Issue:** monitor.py and test_monitor.py imported `interpolate_cliff_region` from soh_calculator, but function is in battery_math.soh
- **Fix:** Corrected imports in both files to `from src.battery_math.soh import interpolate_cliff_region`
- **Commits:** c992bdb (monitor.py), 7f2700a (test_monitor.py)

**3. [Rule 1 - Bug] anchor_voltage parameter passed to kernel function that doesn't accept it**
- **Found during:** Task 7 (test execution)
- **Issue:** soh_calculator.calculate_soh_from_discharge() had `anchor_voltage` parameter, but battery_math_soh kernel function only accepts `min_duration_sec`
- **Fix:** Removed `anchor_voltage` from soh_calculator signature and kernel call
- **Commits:** 68690f0 (code + tests)

**4. [Rule 2 - Missing Mock] battery_model.get_convergence_status() not mocked in tests**
- **Found during:** Task 7 (test mock failures)
- **Issue:** test_discharge_buffer_cleared_after_health_update didn't mock get_convergence_status(), causing MagicMock formatting errors
- **Fix:** Added mock return value: `{'converged': False, 'sample_count': 1}`
- **Commit:** 72fd0e3

**5. [Rule 2 - Missing Mock] soh_calculator mock returns float instead of tuple**
- **Found during:** Task 7 (unpacking error)
- **Issue:** test_ol_ob_ol_discharge_lifecycle_complete mocked soh_calculator.calculate_soh_from_discharge() to return float (0.95), but new orchestrator returns tuple
- **Fix:** Updated mock to return `(0.95, 7.2)` tuple
- **Commit:** 41cde55

## Test Results

### Phase 13 Specific Tests (10/10 PASSING)
- SOH-01 tests: 2/2 passing
- SOH-02 tests: 3/3 passing
- SOH-03 tests: 3/3 passing
- Integration tests: 2/2 passing

### Full Test Suite
- **Total tests run:** 279
- **Passed:** 278 (99.6%)
- **Failed:** 1 (pre-existing issue in test_auto_calibration_end_to_end, unrelated to Phase 13)
  - Error: Logging handler level mismatch during MonitorDaemon initialization
  - Not caused by Phase 13 changes; test doesn't use soh_calculator
  - Documented in deferred items

### No Regressions
All existing tests continue to pass except for the one pre-existing failure.

## Verification Checklist

- [x] model.py add_soh_history_entry() accepts optional capacity_ah_ref parameter
- [x] replacement_predictor.py linear_regression_soh() filters by capacity_ah_ref when provided
- [x] soh_calculator.py created with orchestrator logic selecting measured/rated capacity
- [x] All 8 unit tests passing (SOH-01, SOH-02, SOH-03)
- [x] No regressions in existing test suite (278/279 passing)
- [x] Code ready for Plan 02 (new battery detection can now depend on this baseline filtering)
- [x] History entries tagged with capacity baseline for accurate degradation tracking
- [x] Orchestrator layer properly integrated into monitor._update_battery_health()

## Key Implementation Details

### Orchestrator Pattern
`src/soh_calculator.py` implements orchestrator pattern:
- Pure Python, no I/O (except logging)
- Takes battery_model as parameter, reads convergence status
- Selects capacity reference and calls kernel
- Returns selected capacity along with result for caller to tag history

### Backward Compatibility
- Old SoH history entries without `capacity_ah_ref` field continue to work
- Regression filtering defaults missing field to 7.2Ah (original rated)
- No schema changes required; uses optional dict field

### Separation of Concerns
- **Kernel (battery_math_soh):** Pure physics, accepts capacity_ah parameter
- **Orchestrator (soh_calculator):** Decision logic for capacity selection
- **Model (monitor + model.py):** Persistence and tagging with baseline
- **Predictor (replacement_predictor):** Filtering by baseline for trend analysis

## Ready for Phase 13 Plan 02

This implementation provides the foundation for Plan 02 (new battery detection):
- SoH calculations now tagged with capacity baseline
- Regression model can isolate entries by baseline
- Battery replacement triggers baseline change, resetting aging clock
- Plan 02 can detect >10% capacity jump and prompt user for confirmation

---

**Execution completed:** 2026-03-16
**Plan duration:** 12 minutes
**Commits:** 12 (3 feature, 5 test, 4 fix)
