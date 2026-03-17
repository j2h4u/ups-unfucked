---
phase: 17-scheduling-intelligence
plan: 01
subsystem: Scheduling
tags: [scheduler, safety-gates, blackout-credit, test-dispatch]
dependency_graph:
  requires: [SULF-06, ROI-01, ROI-02, RPT-02]
  provides: [SCHED-01, SCHED-03, SCHED-04, SCHED-05, SCHED-06, SCHED-08]
  affects: [discharge_handler, monitor.py, model.json schema]
tech_stack:
  added: []
  patterns: [pure_function, stateless_decision_engine, audit_trail_logging]
key_files:
  created:
    - src/battery_math/scheduler.py (267 LOC)
    - tests/test_scheduler.py (528 LOC)
    - tests/test_dispatch.py (230 LOC)
    - tests/test_discharge_handler.py (268 LOC)
  modified:
    - src/model.py (added Phase 17 schema, 82 LOC)
    - src/monitor.py (integrated scheduler evaluation, 176 LOC)
    - src/discharge_handler.py (blackout credit + classification, 56 LOC)
    - tests/test_model.py (added 10 Phase 17 backward-compat tests, 158 LOC)
decisions:
  - Grid stability gate configurable: grid_stability_cooldown_hours=0 fully disables (user feedback)
  - Blackout credit only for natural ≥90% DoD discharges (not test-initiated, avoids reinforcing cycle)
  - Daily scheduler evaluation at 08:00 UTC (configurable via Config object)
  - No systemd timer masking in code (manual deployment step)
  - Precondition validation before upscmd dispatch (SoC ≥95%, no grid glitches, no test running)
metrics:
  duration: ~2.5 hours (4 tasks executed serially)
  completed_date: 2026-03-17
  test_count: 66 (33 scheduler + 13 dispatch + 10 model + 10 discharge_handler)
  lines_added: ~1700 (code + tests)
---

# Phase 17 Plan 01: Scheduling Intelligence Summary

**One-liner:** Daemon-controlled test scheduling with safety gates, precondition validation, and blackout credit for natural deep discharges.

## Completion Status

**All 4 tasks complete, all 66 tests passing.**

| Task | Name | Status | Files | Tests |
|------|------|--------|-------|-------|
| 1 | Scheduler Decision Engine | ✅ COMPLETE | scheduler.py | 33 |
| 2 | Model Schema Extension | ✅ COMPLETE | model.py | 10 |
| 3 | Precondition & Dispatch | ✅ COMPLETE | monitor.py | 13 |
| 4 | Blackout Credit Logic | ✅ COMPLETE | discharge_handler.py | 10 |

## Key Deliverables

### Task 1: Scheduler Decision Engine (Pure Function)

**File:** `src/battery_math/scheduler.py` (267 LOC)

Pure stateless decision engine with no I/O, no logging, fully testable offline.

**SchedulerDecision dataclass:**
- `action`: 'propose_test', 'defer_test', 'block_test'
- `test_type`: 'deep', 'quick', or None
- `reason_code`: Human-readable decision reason (e.g., 'soh_floor_55%', 'sulfation_0.65_roi_0.34')
- `next_eligible_timestamp`: ISO8601 for deferred/blocked tests

**evaluate_test_scheduling() function:**
- Accepts 12 parameters (sulfation score, ROI, SoH, days since test, blackout history, cycle budget, thresholds)
- Enforces 7 guard clauses in order:
  1. **SoH floor gate (SCHED-05):** Blocks if SoH < 60% (configurable)
  2. **Rate limiting gate (SCHED-01):** Defers if <7 days since last test (configurable)
  3. **Blackout credit gate (SCHED-03):** Defers if active 7-day credit
  4. **Grid stability gate (SCHED-06, configurable):** Defers if blackout within last 4h (disables entirely if cooldown=0)
  5. **Cycle budget gate:** Blocks if <5 cycles remaining
  6. **ROI threshold gate:** Defers if ROI < 0.2 and >20 cycles remaining
  7. **Sulfation threshold:** Proposes deep (>0.65), quick (>0.40), or defers (low sulfation)

**Test coverage:** 33 tests covering:
- All 7 guard clauses (SoH floor, rate limit, blackout credit, grid stability, cycle budget, ROI, sulfation)
- Boundary conditions at exact thresholds
- Configurable grid stability cooldown (disabled at 0.0)
- Decision tree precedence (SoH first, sulfation last)
- Real-world scenarios (good condition, approaching EOL, recent natural blackout)

### Task 2: Model Schema Extension

**File:** `src/model.py` (82 LOC added)

Phase 17 scheduling state fields initialized to None on load, persisted via atomic writes.

**New fields:**
- `last_upscmd_timestamp`: ISO8601 of last command dispatch
- `last_upscmd_type`: Command sent (test.battery.start.deep|quick)
- `last_upscmd_status`: 'OK' or error message
- `scheduled_test_timestamp`: Next proposed test time (informational)
- `scheduled_test_reason`: Decision reason code
- `test_block_reason`: If test blocked, the blocking reason
- `blackout_credit`: Dict with 'active', 'credit_expires', 'credited_event_timestamp', 'desulfation_credit'

**New methods:**
- `set_blackout_credit(credit_dict)` → Grants 7-day credit
- `clear_blackout_credit()` → Expires credit
- `update_scheduling_state(timestamp, reason, block_reason)` → Updates scheduled info
- `update_upscmd_result(timestamp, type, status)` → Records dispatch result
- `get_last_upscmd_timestamp()` → Retrieves last timestamp
- `get_blackout_credit()` → Retrieves current credit

**Backward compatibility verified:**
- Phase 16 model.json loads without errors (missing fields default to None)
- Phase 17 fields persisted through save/reload cycles
- Forward compatible: model.save() doesn't strip unknown future fields

**Test coverage:** 10 tests covering schema initialization, persistence, backward/forward compatibility

### Task 3: Precondition Validator & Dispatch Integration

**File:** `src/monitor.py` (176 LOC added)

Precondition validation before upscmd dispatch, daily scheduler evaluation in polling loop.

**validate_preconditions_before_upscmd() function:**
- Checks UPS online (OL, no OB/CAL)
- Checks SoC ≥95% (prevents low-battery dispatch)
- Checks grid stable (≤2 transitions in 4h)
- Checks no test already running
- Returns (can_proceed: bool, reason_if_blocked: str)

**dispatch_test_with_audit() function:**
- Validates preconditions before dispatch
- Calls nut_client.send_instcmd(f'test.battery.start.{test_type}')
- Updates model.json on success (timestamp, type, status='OK')
- Updates model.json on failure (error message)
- Logs to journald: test_dispatched, test_precondition_blocked, test_dispatch_failed

**Daily scheduler evaluation (MonitorDaemon.run()):**
- Evaluates once daily at 08:00 UTC (one-minute window)
- Gathers current state: sulfation score, ROI, SoH, days since test, cycle budget
- Calls evaluate_test_scheduling() pure function
- Logs decision to journald (all three actions: propose/defer/block with reason codes)
- Updates model.json scheduling state via update_scheduling_state()
- If decision='propose_test', attempts dispatch_test_with_audit()
- Config defaults (all retrievable from Config object):
  - soh_floor_threshold: 0.60
  - min_days_between_tests: 7.0
  - roi_threshold: 0.2
  - grid_stability_cooldown_hours: 4.0

**Helper methods in MonitorDaemon:**
- `_calculate_days_since_last_test()` → Days elapsed since last upscmd, or inf if never tested
- `_get_last_natural_blackout()` → Returns {timestamp, depth} of most recent natural discharge event

**Test coverage:** 13 tests covering:
- All 4 precondition guard clauses (UPS online, SoC, grid, test running)
- Boundary conditions (SoC=95%, glitches=2)
- Dispatch success (updates model.json, logs to journald)
- Dispatch precondition blocked (returns False, no NUT command sent)
- Dispatch NUT failure (updates model with error message)
- Integration with real CurrentMetrics fixture

### Task 4: Blackout Credit Logic

**File:** `src/discharge_handler.py` (56 LOC added)

Test-initiated vs natural discharge classification and blackout credit granting.

**_classify_event_reason() implementation:**
- Compares discharge start time to last_upscmd_timestamp
- If discharge within 60 seconds of upscmd → 'test_initiated'
- Otherwise → 'natural' (also for missing upscmd record or invalid timestamps)

**Blackout credit granting in update_battery_health():**
- After discharge completion, checks: event_reason='natural' AND DoD ≥0.90
- Grants 7-day credit with expiry = now + 7 days
- Calls battery_model.set_blackout_credit() to persist
- Logs to journald: event_type='blackout_credit_granted'
- Test-initiated discharges do NOT grant credit (avoids reinforcing cycle where test→discharge→credit→defer_next_test)

**Event logging enhancement:**
- Discharge complete events include event_reason field ('natural' or 'test_initiated')
- Blackout credit events logged with credit_expires timestamp

**Test coverage:** 10 tests covering:
- Discharge classification (no upscmd, recent upscmd, old upscmd, no buffer)
- Blackout credit (deep natural discharge, shallow discharge, test-initiated)
- Credit expiry (7-day window)
- Manual credit clearing (clear_blackout_credit())
- Journald event logging

## Requirements Satisfied

| Req | Title | Satisfied By |
|-----|-------|--------------|
| SCHED-01 | Rate limiting (≤1 test/week) | Task 1 & 3: min_days_between_tests gate, last_upscmd_timestamp tracking |
| SCHED-03 | Blackout credit (7-day deferral) | Task 2 & 4: blackout_credit field, set_blackout_credit() method, granting on natural ≥90% DoD |
| SCHED-04 | Sulfation threshold (0.40–0.65 zones) | Task 1: sulfation_score gate with deep/quick decision boundaries |
| SCHED-05 | SoH floor (≥60%) | Task 1: hard block gate on SoH < threshold |
| SCHED-06 | Grid stability (configurable cooldown) | Task 1: grid_stability_cooldown_hours=0 fully disables gate |
| SCHED-08 | Precondition validation (SoC ≥95%, no glitches, no test running) | Task 3: validate_preconditions_before_upscmd() guards |

## Deviations from Plan

None. Plan executed exactly as specified. All 4 tasks completed, all guard clauses implemented, all requirements satisfied, all tests passing.

## Architecture Decisions

1. **Pure scheduler function:** Phase 17 scheduler is stateless, no I/O, no daemon coupling. Fully testable offline. Reduces integration complexity and risk.

2. **Grid stability gate configurable:** Per user feedback, `grid_stability_cooldown_hours=0` completely disables the grid stability gate. Allows deployment flexibility for different operational contexts (stable grid vs high-fault environments).

3. **No systemd timer masking in code:** Deployment checklist item (manual). Keeps daemon code focused on business logic, not infrastructure concerns.

4. **Blackout credit only for natural discharges:** Test-initiated discharges (daemon-controlled) do NOT grant credit. Prevents self-reinforcing cycle where test→discharge→credit→defer_next_test→natural_discharge→credit again.

5. **Daily scheduler evaluation at 08:00 UTC:** One-minute window per day to minimize overhead while maintaining consistent scheduling. Configurable via Config object for future customization.

6. **Precondition validation after decision:** Scheduler decides test candidacy globally (sulfation, ROI, rate limit), then preconditions validate dispatch readiness locally (SoC, grid, test running). Two-stage gating improves transparency.

## Self-Check

✅ **All files created:**
- ✅ src/battery_math/scheduler.py (267 LOC)
- ✅ tests/test_scheduler.py (528 LOC)
- ✅ tests/test_dispatch.py (230 LOC)
- ✅ tests/test_discharge_handler.py (268 LOC)

✅ **All files modified:**
- ✅ src/model.py (phase 17 schema + methods)
- ✅ src/monitor.py (scheduler integration + dispatch functions)
- ✅ src/discharge_handler.py (classification + blackout credit)
- ✅ tests/test_model.py (10 backward-compat tests)

✅ **All tests passing:** 66/66 tests

✅ **All commits created:**
- ✅ f2a0cc5: feat(17-01): scheduler decision engine
- ✅ 2363224: feat(17-02): model schema extension
- ✅ c22ad51: feat(17-03): precondition validator and dispatch
- ✅ e278a67: feat(17-04): blackout credit logic

## Next Phase

Phase 17 Plan 02 (Wave 2) → Systemd timer migration and deployment validation (manual disable of legacy timers, verification of daemon-controlled scheduling behavior, integration testing with real UPS events).
