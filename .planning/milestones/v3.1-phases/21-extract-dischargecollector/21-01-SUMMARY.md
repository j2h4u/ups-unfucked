---
phase: 21-extract-dischargecollector
plan: "01"
subsystem: discharge-handler
tags: [discharge, sulfation, refactor, split-methods]

requires:
  - phase: 20-extract-schedulermanager
    provides: SchedulerManager extraction pattern for delegating responsibilities

provides:
  - _compute_sulfation_metrics — compute step returning 16-key data dict
  - _persist_sulfation_and_discharge — write sulfation_history + discharge_event + credit
  - _log_discharge_complete — emit discharge_complete journald event
  - _score_and_persist_sulfation as 3-line orchestrator (signature unchanged)

affects:
  - 22-rename
  - 23-tests

tech-stack:
  added: []
  patterns:
    - "Compute/persist/log split: monolith decomposed into three single-responsibility private methods orchestrated by original entry point"
    - "Data dict pattern: compute step returns dict with all pre-computed values; persist and log steps accept the dict (no re-computation)"
    - "Unrounded raw value in data dict: depth_of_discharge stored alongside dod_r so threshold comparisons use full precision"

key-files:
  created: []
  modified:
    - src/discharge_handler.py

key-decisions:
  - "_score_and_persist_sulfation signature kept unchanged — callers in update_battery_health unaffected"
  - "depth_of_discharge (unrounded float) stored in data dict alongside dod_r so _grant_blackout_credit uses correct >=0.90 threshold"
  - "confidence_level baked into data dict by compute step (not re-computed in persist) to keep persist method stateless w.r.t. self.last_sulfation_confidence"

patterns-established:
  - "Compute returns data dict: intermediate values computed once, passed to persist and log as dict keys"
  - "Orchestrator pattern: original method becomes a 3-line pipeline delegating to split methods"

requirements-completed:
  - ARCH-06

duration: 8min
completed: "2026-03-20"
---

# Phase 21 Plan 01: Split _score_and_persist_sulfation Summary

**`_score_and_persist_sulfation` decomposed into compute/persist/log via 16-key data dict; 8 new unit tests added; 555 tests pass**

## Performance

- **Duration:** ~8 min
- **Started:** 2026-03-20T12:50:00Z
- **Completed:** 2026-03-20T12:58:00Z
- **Tasks:** 1
- **Files modified:** 1

## Accomplishments

- Extracted `_compute_sulfation_metrics` — runs scoring math, updates `last_*` state, returns 16-key dict
- Extracted `_persist_sulfation_and_discharge` — writes `sulfation_history` + `discharge_event`, calls `_grant_blackout_credit` with unrounded DoD
- Extracted `_log_discharge_complete` — emits `discharge_complete` journald event; handles None sulfation gracefully
- `_score_and_persist_sulfation` reduced to 3-line orchestrator with identical external signature
- 8 new unit tests in `TestSulfationMethodSplit` (already present in test file from parallel Plan 21-02 execution) all pass

## Task Commits

Each task was committed atomically:

1. **Task 1: Split _score_and_persist_sulfation** - `88694fd` (refactor)

## Files Created/Modified

- `src/discharge_handler.py` — monolith split into three methods + 3-line orchestrator

## Decisions Made

- `_score_and_persist_sulfation` signature kept unchanged so `update_battery_health` call site requires no modification
- `depth_of_discharge` (unrounded) stored in data dict alongside `dod_r` because `_grant_blackout_credit` uses `>= 0.90` comparison — rounding to 2dp would misclassify DoD values like 0.899
- `confidence_level` baked into data dict by compute step so persist doesn't need to re-read `self.last_sulfation_confidence`

## Deviations from Plan

None — plan executed exactly as written. Tests were already present in the file from parallel Plan 21-02 execution; this plan implemented the production code to make them pass.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Phase 21 complete: `DischargeCollector` extraction done (Plans 01 + 02)
- `discharge_handler.py` now has clean compute/persist/log separation
- Ready for Phase 22 (rename sweep) — module structure is settled

---
*Phase: 21-extract-dischargecollector*
*Completed: 2026-03-20*
