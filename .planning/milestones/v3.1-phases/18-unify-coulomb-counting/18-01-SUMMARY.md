---
phase: 18-unify-coulomb-counting
plan: 01
subsystem: battery_math
tags: [coulomb-counting, trapezoidal-integration, battery-math, capacity-estimator, discharge-handler]

# Dependency graph
requires: []
provides:
  - "Standalone integrate_current() in src/battery_math/integration.py (IEEE-1106 trapezoidal)"
  - "integrate_current exported from src/battery_math/__init__"
  - "CapacityEstimator uses integrate_current() via import (no private method)"
  - "_check_alerts() receives avg_load as parameter (no internal recomputation)"
  - "_log_discharge_prediction() uses self._avg_load() (consistent fallback)"
  - "Accuracy comparison test: trapezoidal exactly matches analytical, scalar does not"
affects: [19-extract-sag-tracker, 20-extract-scheduler, 21-extract-discharge-collector, 23-test-quality]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Pure-function extraction: physics math moved to battery_math package, not on class instances"
    - "Parameter threading: computed values passed through the call chain rather than recomputed"

key-files:
  created:
    - src/battery_math/integration.py
    - tests/test_integration_math.py
  modified:
    - src/battery_math/__init__.py
    - src/capacity_estimator.py
    - src/discharge_handler.py

key-decisions:
  - "integrate_current() is a pure function in battery_math — no class state, easily testable and reusable by extracted modules in phases 19-21"
  - "avg_load computed once in update_battery_health() and threaded to _check_alerts() — avoids double _avg_load() call without changing _compute_soh return signature"
  - "_log_discharge_prediction() fallback changed from 0.0 to reference_load_percent (via self._avg_load()) for consistency with all other call sites"

patterns-established:
  - "Pure function in battery_math: coulomb counting logic lives in battery_math.integration, not on CapacityEstimator"
  - "Parameter threading: compute once at the top of a pipeline, pass down — don't recompute in callees"

requirements-completed: [ARCH-01, ARCH-02]

# Metrics
duration: 3min
completed: 2026-03-20
---

# Phase 18 Plan 01: Unify Coulomb Counting Summary

**Standalone integrate_current() extracted to battery_math.integration with IEEE-1106 trapezoidal rule, CapacityEstimator._integrate_current() deleted, avg_load double-computation eliminated from _check_alerts(), 480 tests pass**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-20T10:35:29Z
- **Completed:** 2026-03-20T10:38:xx Z
- **Tasks:** 3
- **Files modified:** 5

## Accomplishments
- Created `src/battery_math/integration.py` with standalone `integrate_current()` — pure function, no class state, ready for reuse by phases 19-21 extracted modules
- Deleted `CapacityEstimator._integrate_current()` private method; call site updated to `integrate_current()` via import
- Fixed avg_load double-computation: `update_battery_health()` now computes once and passes to `_check_alerts()` as parameter
- Fixed inline avg_load inconsistency in `_log_discharge_prediction()`: replaced `sum/len or 0.0` with `self._avg_load()` for consistent `reference_load_percent` fallback
- Accuracy test proves trapezoidal integration exactly matches analytical result for piecewise-linear current, while scalar-average approach does not

## Task Commits

Each task was committed atomically:

1. **Task 1: Extract integrate_current() to battery_math and update call sites** - `062244e` (feat)
2. **Task 2: Fix avg_load double computation in _check_alerts and inline inconsistency** - `b937df1` (fix)
3. **Task 3: Full regression test suite** - `85ca172` (test)

## Files Created/Modified
- `src/battery_math/integration.py` - New: standalone integrate_current() with full docstring and F27 bias note
- `src/battery_math/__init__.py` - Added integrate_current import and __all__ export
- `src/capacity_estimator.py` - Replaced self._integrate_current() call with integrate_current(); deleted private method; added import
- `src/discharge_handler.py` - avg_load computed in update_battery_health(), passed to _check_alerts(); _log_discharge_prediction() uses self._avg_load()
- `tests/test_integration_math.py` - New: 4 tests covering constant load, empty input, single point, and trapezoidal accuracy comparison

## Decisions Made
- Pure-function extraction pattern: integrate_current() is a module-level function in battery_math, not a static method. This matches the existing battery_math style (peukert_runtime_hours, calibrate_peukert, etc.) and makes it immediately importable by phases 19-21 without carrying CapacityEstimator as a dependency.
- Did not change `_compute_soh()` return signature to avoid wider refactor — avg_load is computed again in `update_battery_health()` (one extra call per discharge event, not a hot path). This is the narrowest change that eliminates the duplicate computation in `_check_alerts`.

## Deviations from Plan

None — plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required
None — no external service configuration required.

## Next Phase Readiness
- `integrate_current` is now available from `src.battery_math` — phases 19/20/21 can import it directly when extracting SagTracker, SchedulerManager, DischargeCollector
- All 480 tests pass; no regressions

## Self-Check: PASSED

All created files exist. All task commits verified in git log.

---
*Phase: 18-unify-coulomb-counting*
*Completed: 2026-03-20*
