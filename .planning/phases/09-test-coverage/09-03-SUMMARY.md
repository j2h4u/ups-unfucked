---
phase: 09-test-coverage
plan: 03
subsystem: testing
tags: [integration test, discharge lifecycle, state machine, battery health]

requires:
  - phase: 09-01
    provides: mock_socket_list_var fixture and floating-point tolerance fixes for soc_from_voltage
  - phase: 08-architecture
    provides: CurrentMetrics dataclass, Config dataclass, monitor.py refactored structure

provides:
  - TEST-01 comprehensive integration test covering full OL→OB→OL discharge lifecycle
  - Verification of _track_discharge(), _handle_event_transition(), _update_battery_health() working as connected flow
  - Demonstration that discharge buffer state machine works correctly over multiple cycles

affects: [Phase 10 (QUAL-01..05), Phase 11 (LOW-01..05)]

tech-stack:
  added: []
  patterns:
    - Integration test pattern using real method execution with mocked external dependencies
    - State machine verification approach: set up state, call transition, verify state changes

key-files:
  created: []
  modified:
    - tests/test_monitor.py (added test_ol_ob_ol_discharge_lifecycle_complete, 208 lines)

key-decisions:
  - "Integration test uses real _track_discharge() and _handle_event_transition() (not mocked) to test actual state machine logic"
  - "External dependencies mocked: soh_calculator, replacement_predictor, runtime_minutes, interpolate_cliff_region"
  - "Battery model methods mocked to avoid complex physics calculations while testing daemon workflow"
  - "Test verifies buffer state at critical points: before and after OB→OL transitions, with buffer clearing in between"
  - "Two complete OL→OB→OL cycles verify state doesn't carry over between discharge events"

requirements-completed: [TEST-01]

metrics:
  duration: 12min
  completed: 2026-03-14
---

# Phase 09 Plan 03: Integration Test for OL→OB→OL Discharge Lifecycle Summary

**Comprehensive integration test for full blackout discharge cycle: OL→OB (buffer accumulation)→OL (health update + buffer clear), with multiple cycles verifying no state carryover**

## Performance

- **Duration:** 12 min
- **Started:** 2026-03-14T15:16:32Z
- **Completed:** 2026-03-14T15:28:32Z
- **Tasks:** 1 (TDD: RED→GREEN)
- **Files modified:** 1

## Accomplishments

- Integration test `test_ol_ob_ol_discharge_lifecycle_complete` covers complete daemon discharge workflow
- Verification that _track_discharge() accumulates voltage samples only during OB state
- Confirmation that _handle_event_transition() detects OB→OL and triggers _update_battery_health()
- Validation that discharge buffer is properly cleared after health update, enabling second cycle
- Proof that multiple OL→OB→OL cycles work without state carryover between events
- TEST-01 requirement fully satisfied

## Task Commits

1. **Task 1: Integration test for OL→OB→OL discharge lifecycle (TEST-01)** - `469184a` (test)

**Plan metadata:** Summary created alongside task commit (integrated in execution)

## Files Created/Modified

- `tests/test_monitor.py` - Added integration test function (208 lines)
  - Cycle 1: OL→OL→OB (3 voltage samples: 12.0, 11.5, 11.0)→OL (health update called, buffer cleared)
  - Cycle 2: OL→OB (2 voltage samples: 12.5, 11.2)→OL (second health update called, buffer cleared)
  - Verifies discharge buffer 'collecting' flag toggles correctly on transitions
  - Verifies model.add_soh_history_entry() called exactly twice (once per OB→OL transition)

## Decisions Made

- **State machine verification approach:** Tested real state machine logic by calling actual methods (_track_discharge, _handle_event_transition) in sequence while mocking only external dependencies (NUT client, battery model, physics calculators)
- **Buffer state verification timing:** Checked discharge buffer contents BEFORE calling _handle_event_transition() on OB→OL (before buffer clearing), then verified buffer cleared AFTER transition
- **Multiple cycle verification:** Included second complete OL→OB→OL cycle to ensure discharge_buffer state doesn't carry over from first cycle (buffer properly reset)
- **Mock strategy:** Mocked soh_calculator.calculate_soh_from_discharge, replacement_predictor.linear_regression_soh, runtime_minutes, interpolate_cliff_region to avoid dependency on complex physics; mocked battery_model methods but left discharge_buffer real (to test actual state management)

## Deviations from Plan

None - plan executed exactly as written. The integration test was designed with clear behavior specification and all test assertions matched the specified requirements.

## Test Results

All 17 tests in test_monitor.py pass:
- test_ol_ob_ol_discharge_lifecycle_complete: **PASSED**
- All 16 pre-existing tests: **PASSED** (no regressions)

## Issues Encountered

None - test passed on first execution after initial setup. The daemon state machine worked as designed.

## Test Coverage

- **Discharge buffer accumulation:** Verified that voltages append only during OB state (not OL)
- **State machine transitions:** Verified OB→OL transition triggers _update_battery_health() via mocked add_soh_history_entry()
- **Buffer clearing:** Verified discharge_buffer['voltages'] and ['times'] reset to [] after transition
- **Collecting flag:** Verified 'collecting' flag set to True on OL→OB, False on OB→OL
- **Cycle count:** Verified add_soh_history_entry called exactly 2 times (once per cycle)
- **No state carryover:** Second cycle buffer contains only 2 samples (not 3+2=5), proving first cycle buffer was cleared

## Next Phase Readiness

Phase 09 complete:
- TEST-01: Integration test ✓ (09-03)
- TEST-02: Peukert auto-calibration ✓ (09-02)
- TEST-03: Signal handler ✓ (09-02)
- TEST-04: NUT mock socket ✓ (09-01)
- TEST-05: Floating-point tolerance ✓ (09-01)

All 5 test coverage requirements (TEST-01 through TEST-05) from phase 09 are complete.

Ready to proceed to Phase 10 (Code Quality: QUAL-01 through QUAL-05).

---
*Phase: 09-test-coverage*
*Plan: 03*
*Completed: 2026-03-14*
