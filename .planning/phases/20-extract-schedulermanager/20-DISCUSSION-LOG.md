# Phase 20: Extract SchedulerManager - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-20
**Phase:** 20-extract-schedulermanager
**Areas discussed:** Module placement, Public interface, State ownership, Dispatch function placement
**Mode:** --auto (all decisions auto-selected)

---

## Module Placement

| Option | Description | Selected |
|--------|-------------|----------|
| src/scheduler_manager.py | Top-level under src/, consistent with SagTracker and DischargeHandler | ✓ |
| src/battery_math/scheduler_manager.py | Under battery_math alongside pure scheduler function | |
| Merge into existing scheduler.py | Add class to battery_math/scheduler.py | |

**User's choice:** [auto] src/scheduler_manager.py (recommended default)
**Notes:** Stateful collaborator belongs in src/, not battery_math/. Consistent with Phase 19 SagTracker placement decision.

---

## Public Interface

| Option | Description | Selected |
|--------|-------------|----------|
| Single run_daily(now, current_metrics) | One entry point, mirrors sag_tracker.track() pattern | ✓ |
| Separate evaluate() + dispatch() | Two-step: evaluate returns decision, caller dispatches | |
| Full pipeline with callbacks | Register callbacks for dispatch/logging | |

**User's choice:** [auto] Single run_daily(now, current_metrics) (recommended default)
**Notes:** MonitorDaemon calls one method from _poll_once(). Properties expose last_scheduling_reason and last_next_test_timestamp for health snapshot.

---

## State Ownership

| Option | Description | Selected |
|--------|-------------|----------|
| All scheduler state moves | scheduler_evaluated_today, last_scheduling_reason, last_next_test_timestamp, scheduling_config | ✓ |
| Only evaluation state | Keep scheduling_config on MonitorDaemon | |
| Shared state via model | Persist all state in battery_model instead of instance fields | |

**User's choice:** [auto] All scheduler state moves (recommended default)
**Notes:** Sole owner pattern — SchedulerManager is the single source of truth for all scheduling state. Consistent with SagTracker owning all sag state.

---

## Dispatch Function Placement

| Option | Description | Selected |
|--------|-------------|----------|
| Move to scheduler_manager.py | Co-locate with scheduling logic, update test imports | ✓ |
| Keep in monitor.py | Leave as module-level functions, SchedulerManager imports them | |
| Make SchedulerManager methods | Instance methods instead of module-level | |

**User's choice:** [auto] Move to scheduler_manager.py (recommended default)
**Notes:** validate_preconditions_before_upscmd and dispatch_test_with_audit are only called from scheduler execution. Co-locating keeps all scheduling code together. test_dispatch.py updates import path.

---

## Claude's Discretion

- Exact constructor signature details (whether to pass discharge_handler or individual metric accessors)
- Whether to expose a reset() method for testing/restart scenarios
- Internal method decomposition within SchedulerManager
- Unit test structure and fixture design

## Deferred Ideas

None — discussion stayed within phase scope.
