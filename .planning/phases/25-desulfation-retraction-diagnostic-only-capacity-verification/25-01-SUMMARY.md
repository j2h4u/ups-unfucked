---
phase: 25-desulfation-retraction-diagnostic-only-capacity-verification
plan: "01"
subsystem: scheduler
tags: [scheduler, tdd, diagnostic-cadence, deadlock-fix, two-input-timing-split]
dependency_graph:
  requires: []
  provides: [evaluate_test_scheduling-cadence-engine, get_last_upscmd_status-getter, split-timing-inputs]
  affects: [scheduler_manager, model, wave-2-deletion-unblocked]
tech_stack:
  added: []
  patterns: [two-input-timing-split, rate-limit-vs-cadence-independence, public-getter-symmetry]
key_files:
  created: []
  modified:
    - src/battery_math/scheduler.py
    - src/scheduler_manager.py
    - src/model.py
    - tests/test_scheduler.py
    - tests/test_scheduler_manager.py
decisions:
  - "Two-input timing split: days_since_last_attempt (rate-limit gate) and days_since_last_test_success (cadence gate) are computed and passed independently — collapsed single value cannot satisfy both requirements simultaneously"
  - "get_last_upscmd_status() added as symmetric public getter to BatteryModel — neither calc method reaches into state[] directly for upscmd status"
  - "Engine only proposes test_type=quick per SCH-03; deep is in Literal but not emitted autonomously"
  - "Failed dispatch writes last_upscmd_timestamp but not OK status — so days_since_last_test_success stays inf after a transient error, cadence clock not deferred ~365 days"
metrics:
  duration: "~18 minutes"
  completed: "2026-06-03T20:20:12Z"
  tasks_completed: 2
  tasks_total: 2
  files_changed: 5
  commits: 3
---

# Phase 25 Plan 01: Reframe Scheduler to Diagnostic Time-Cadence Engine Summary

Reframed `evaluate_test_scheduling` from a sulfation/cycle-ROI decision engine into an IEEE-1188 annual capacity/SoH diagnostic scheduler driven by a persistent time cadence, fixing the confirmed bootstrap deadlock (SCH-01/02/03).

## What Was Built

**scheduler.py** — complete signature rewrite:
- New params: `soh_fraction`, `days_since_last_test_success`, `days_since_last_attempt`, `last_blackout_timestamp`, `cycle_budget_remaining`, `grid_stability_cooldown_hours`
- Removed: `sulfation_score`, `cycle_roi`, `active_blackout_credit`, single `days_since_last_test`
- Removed constants: `ROI_THRESHOLD`, `DEEP_SULFATION_THRESHOLD`, `QUICK_SULFATION_THRESHOLD`
- Added: `DIAGNOSTIC_TEST_INTERVAL_DAYS = 365.0` (IEEE-1188)
- Gate order: (1) SoH floor → (2) rate-limit (attempt) → (3) grid stability → (4) cycle budget → (5) cadence (success)
- First test from cold start (inf/inf) → `propose_test/quick/diagnostic_cadence` — deadlock gone

**model.py** — new `get_last_upscmd_status()` public getter, symmetric with existing `get_last_upscmd_timestamp()`

**scheduler_manager.py** — wiring update:
- `_calculate_days_since_last_test` split into two methods:
  - `_calculate_days_since_last_attempt`: status-agnostic, reads only `get_last_upscmd_timestamp()`
  - `_calculate_days_since_last_test_success`: reads timestamp + status via public getters; returns inf when `status != "OK"`
- `_gather_scheduler_inputs`: drops sulfation/cycle_roi/active_credit keys; adds split timing keys
- `evaluate_test_scheduling` call site: passes split kwargs
- Verbose log and `_execute_scheduler_decision` structured log: removed sulfation/roi fields

**tests/test_scheduler.py** — full rewrite (30 tests): cadence engine coverage — first test, annual cadence, restart no-retrigger, rate-limit independence, failed-attempt boundaries, SoH floor, grid cooldown, cycle budget, gate ordering

**tests/test_scheduler_manager.py** — rewrite (39 tests): two-input split methods, three required boundary cases (recent-failed→rate_limit, old-failed→diagnostic_cadence, success-30d→within_cadence), fresh-model deadlock proof, no sulfation/cycle_roi refs

## Commits

| Hash | Type | Description |
|------|------|-------------|
| 57a5a56 | test | RED: failing tests for diagnostic cadence engine |
| 8601681 | feat | GREEN: reframe evaluate_test_scheduling to cadence engine |
| 694a502 | feat | Task 2: SchedulerManager wiring + model getter + test rewrites |

## Verification Results

All plan verification checks pass:
- `grep -rn "sulfation\|cycle_roi" src/battery_math/scheduler.py src/scheduler_manager.py` → empty
- `grep -n "days_since_last_test\b" src/battery_math/scheduler.py src/scheduler_manager.py` → empty (collapsed key gone)
- `src/model.py` defines `get_last_upscmd_status()` at line 889
- No direct `state["last_upscmd_status"]` or `state.get("last_upscmd_status")` in scheduler_manager.py
- `uv run pytest tests/test_scheduler.py tests/test_scheduler_manager.py` → **69/69 passed**
- Deadlock proof: fresh model (no upscmd history, soh=0.9) → `propose_test/quick/diagnostic_cadence`
- Restart-safety proof: `days_since_last_test_success=30` (<365) → `defer_test/within_cadence`
- Two-input split boundary cases (all three pass simultaneously):
  - (a) recent failed (attempt=1d, success=inf) → `defer_test/rate_limit`
  - (b) old failed (attempt=8d, success=inf) → `propose_test/diagnostic_cadence`
  - (c) success (attempt=30d, success=30d) → `defer_test/within_cadence`

## Decisions Made

1. **Two-input split design**: A single `days_since_last_test` cannot satisfy both "failed attempt is rate-limited" and "failed attempt does not defer cadence" — the cycle-2 HIGH that drove the split. Rate-limit gate sees the last attempt (any status); cadence gate sees only the last success.

2. **Public getter symmetry**: `get_last_upscmd_status()` added alongside existing `get_last_upscmd_timestamp()`. Neither calc method reaches into `battery_model.state[...]` directly for upscmd status — consistent encapsulation boundary.

3. **quick-only autonomous proposal**: Per SCH-03, the engine only proposes `test_type="quick"`. The `deep` literal is kept in the type for dataclass completeness but the engine never emits it autonomously.

4. **last_upscmd_status as success proxy**: Using `last_upscmd_status == "OK"` as the cadence proxy rather than a dedicated timestamp is a deliberate design choice (documented in threat model as accepted LOW risk). A new persisted `last_successful_diagnostic_timestamp` field is explicitly out of scope.

## Wave 2 Unblocked

`src/battery_math/scheduler.py` and `src/scheduler_manager.py` no longer reference `sulfation_score`, `cycle_roi`, or any module from `src/battery_math/sulfation.py` / `src/battery_math/cycle_roi.py`. Wave 2 can delete those modules with no remaining live callers in the scheduler subsystem.

## Deviations from Plan

None — plan executed exactly as written. The `files_modified` frontmatter already included `src/model.py` (the plan specified adding the getter there), so no deviation tracking needed.

## Known Stubs

None.

## Threat Flags

None — no new network endpoints, auth paths, or schema changes introduced. The `get_last_upscmd_status()` getter is a read-only accessor of existing persisted state.

## Self-Check

Files exist:
- src/battery_math/scheduler.py: FOUND
- src/scheduler_manager.py: FOUND
- src/model.py: FOUND
- tests/test_scheduler.py: FOUND
- tests/test_scheduler_manager.py: FOUND

Commits exist:
- 57a5a56: FOUND
- 8601681: FOUND
- 694a502: FOUND

## Self-Check: PASSED
