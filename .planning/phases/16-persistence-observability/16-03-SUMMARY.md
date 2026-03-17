---
phase: 16-persistence-observability
plan: 03
subsystem: battery-model
tags: [sulfation-scoring, cycle-roi, discharge-handler, model-persistence]

# Dependency graph
requires:
  - phase: 16-02
    provides: "Model.json schema with append_sulfation_history() and append_discharge_event() methods"
  - phase: 15
    provides: "Pure functions compute_sulfation_score() and compute_cycle_roi() in battery_math/"
provides:
  - "DischargeHandler integrates sulfation/ROI scoring into discharge completion flow"
  - "Monitor.py passes sulfation state to health.json export"
  - "In-memory sulfation state maintained across polls for observability"
  - "Model.json persists sulfation_history and discharge_events on every discharge"
affects:
  - "Phase 16 Wave 3 (health.json export)"
  - "Phase 17 (scheduler decision logic)"

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Discharge handler integration: helper methods for historical lookups + scoring pipeline"
    - "In-memory state variables for cross-poll observability (last_sulfation_score, last_cycle_roi, etc.)"
    - "Graceful error handling for scoring failures (log warning, continue with None)"

key-files:
  created: []
  modified:
    - src/discharge_handler.py
    - src/monitor.py

key-decisions:
  - "Phase 16 hardcodes event_reason to 'natural' (Phase 17 will add test detection)"
  - "Temperature fixed at 35°C per v3.0 scope (Phase 3.1 will add sensor integration)"
  - "In-memory state initialized to None/0.0 for graceful startup handling"
  - "Sulfation scoring errors are non-fatal: log warning, continue daemon"

requirements-completed:
  - SULF-01
  - SULF-02
  - SULF-03
  - SULF-04
  - ROI-01
  - ROI-02

# Metrics
duration: 23min
completed: 2026-03-17
---

# Phase 16 Plan 03: Discharge Handler Integration Summary

**DischargeHandler integrates sulfation and ROI scoring into discharge completion flow, persisting results to model.json and maintaining in-memory state for health.json export.**

## Performance

- **Duration:** 23 min
- **Started:** 2026-03-17T13:10:42Z
- **Completed:** 2026-03-17T13:33:42Z
- **Tasks:** 4
- **Files modified:** 2

## Accomplishments

- **Helper methods added:** 5 new DischargeHandler methods for historical lookups (_calculate_days_since_deep, _estimate_ir_trend, _classify_event_reason, _estimate_dod_from_buffer, _estimate_cycle_budget)
- **Sulfation/ROI integration:** update_battery_health() now calls compute_sulfation_score() and compute_cycle_roi() after SoH calculation
- **Model persistence:** Sulfation history and discharge events appended atomically to model.json on every discharge completion
- **In-memory state:** 8 state variables (last_sulfation_score, last_cycle_roi, etc.) maintained for health.json export in Wave 3
- **Monitor integration:** write_health_endpoint() call updated to pass 11 Phase 16 parameters from discharge_handler state
- **Test coverage:** All 14 integration tests (sulfation + discharge events) pass; 389 total tests pass with no regressions

## Task Commits

Each task was committed atomically:

1. **Task 1: Add helper methods to DischargeHandler** - `5c0a3b2` (feat)
   - _calculate_days_since_deep() queries discharge_events, returns days since last >70% DoD
   - _estimate_ir_trend() calculates IR drift (dR/dt) over last 30 days via linear regression
   - _classify_event_reason() hardcoded to 'natural' for Phase 16
   - _estimate_dod_from_buffer() estimates depth of discharge from voltage samples
   - _estimate_cycle_budget() estimates remaining cycles from SoH

2. **Task 2: Integrate sulfation/ROI into update_battery_health()** - `a7f4c9e` (feat)
   - Call helper methods to gather sulfation signals
   - compute_sulfation_score() with days_since_deep, ir_trend_rate, recovery_delta, temperature
   - compute_cycle_roi() with dod, cycle_budget, sulfation_score
   - Store in-memory state (8 variables) for wave 3 health.json export
   - append_sulfation_history() with 8-field entry
   - append_discharge_event() with 6-field entry
   - safe_save() atomic write at end

3. **Task 3: Update monitor.py to pass sulfation state** - `d8e2f1b` (feat)
   - write_health_endpoint() call extended with 11 Phase 16 parameters
   - sulfation_score, sulfation_confidence, days_since_deep, ir_trend_rate, recovery_delta
   - cycle_roi, cycle_budget_remaining, last_discharge_timestamp
   - Phase 17 placeholders: scheduling_reason, next_test_timestamp, natural_blackout_credit

4. **Task 4: Run integration tests** - (no separate commit; tests from Plan 02 verified)
   - test_sulfation_persistence.py: 7 PASS (append_sulfation_history, persistence, pruning, schema)
   - test_discharge_event_logging.py: 7 PASS (append_discharge_event, persistence, pruning, reason values)
   - Full regression suite: 389 PASS, 1 xfailed
   - No regressions from v2.0

## Files Created/Modified

- `src/discharge_handler.py` - Added imports, in-memory state initialization, 5 helper methods, sulfation/ROI integration in update_battery_health()
- `src/monitor.py` - Extended write_health_endpoint() call with 11 Phase 16 parameters

## Decisions Made

- **Hardcoded 'natural' event reason:** Phase 16 cannot distinguish test-initiated from natural discharges yet (no upscmd support). Phase 17 will compare discharge start time to last upscmd timestamp for classification.
- **Fixed 35°C temperature:** Per v3.0 scope, temperature is constant. Future Phase 3.1 will integrate NUT HID battery temperature sensor if available.
- **Graceful error handling:** Sulfation scoring failures are non-fatal; log warning, continue with None values to prevent daemon crash. Robustness over accuracy during Phase 16 observational period.
- **In-memory state vs model.json:** Both maintained simultaneously for different purposes: in-memory for polling latency (health.json export every poll), model.json for historical records (append on discharge completion, pruned to 30 entries).

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None - all acceptance criteria met, tests passing, no blockers.

## Test Coverage

**Phase 16 Wave 2 verification:**

```
Sulfation Persistence Tests (7):
  ✓ test_append_sulfation_history_single_entry
  ✓ test_append_sulfation_history_multiple_entries
  ✓ test_sulfation_history_saved_to_model_json
  ✓ test_prune_sulfation_history_keeps_last_30
  ✓ test_append_discharge_event
  ✓ test_discharge_event_schema_correctness
  ✓ test_backward_compatibility_missing_keys

Discharge Event Logging Tests (7):
  ✓ test_append_discharge_event_to_model
  ✓ test_discharge_event_schema_required_fields
  ✓ test_discharge_event_reason_values
  ✓ test_discharge_event_persisted_in_model_json
  ✓ test_discharge_event_timestamp_format
  ✓ test_prune_discharge_events_keeps_last_30
  ✓ test_discharge_events_queryable_by_reason

Full Regression Suite:
  ✓ 389 passed, 1 xfailed

Requirements Coverage:
  ✓ SULF-01: Sulfation score computation ✓
  ✓ SULF-02: Historical data lookups ✓
  ✓ SULF-03: Discharge event tracking ✓
  ✓ SULF-04: Model.json persistence ✓
  ✓ ROI-01: Cycle ROI calculation ✓
  ✓ ROI-02: ROI passed to health export ✓
```

## Next Phase Readiness

**Wave 2 complete.** Phase 16 Plan 04 (health.json export) can now:
- Read in-memory sulfation state from discharge_handler
- Export health.json with all sulfation metrics populated
- Maintain observability data for Grafana/MOTD display

**No blockers.** All infrastructure ready for health endpoint work.

---

*Phase: 16-persistence-observability*
*Plan: 03-discharge-handler-integration*
*Completed: 2026-03-17T13:33:42Z*
