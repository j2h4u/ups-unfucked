# Phase 20: Extract SchedulerManager - Context

**Gathered:** 2026-03-20
**Status:** Ready for planning

<domain>
## Phase Boundary

SchedulerManager logic lives in its own module, MonitorDaemon delegates scheduling decisions to it. SchedulerManager owns all scheduler state, daily evaluation logic, precondition checks, and test dispatch. Has direct unit tests without constructing a MonitorDaemon. All existing tests pass with no regressions.

Requirements: ARCH-04.

</domain>

<decisions>
## Implementation Decisions

### Module placement
- `src/scheduler_manager.py` — top-level under src/, not under battery_math/
- SchedulerManager is a daemon collaborator (stateful, interacts with model, nut_client, metrics), not a pure math function
- Consistent with SagTracker (Phase 19) and DischargeHandler (existing) placement

### Public interface
- Constructor takes dependencies: `battery_model`, `nut_client`, `scheduling_config`, `discharge_handler` (for sulfation/ROI/budget reads)
- Single `run_daily(now, current_metrics)` entry point — MonitorDaemon calls from `_poll_once()` (replaces `self._run_daily_scheduler(now)`)
- Properties `last_scheduling_reason` and `last_next_test_timestamp` exposed for health snapshot reads (lines 912-913 in monitor.py)
- No return value from `run_daily()` — side effects are logging, model persistence, and test dispatch

### State ownership
- SchedulerManager owns all three state fields: `scheduler_evaluated_today`, `last_scheduling_reason`, `last_next_test_timestamp`
- MonitorDaemon no longer holds scheduler state — SchedulerManager is the sole owner
- `scheduling_config` reference held by SchedulerManager, not MonitorDaemon

### Dispatch function placement
- `validate_preconditions_before_upscmd()` and `dispatch_test_with_audit()` move into `scheduler_manager.py` as module-level functions
- They're only called from `_execute_scheduler_decision()` which moves to SchedulerManager
- Keeps all scheduling-related code co-located; no cross-module dispatch calls
- Existing tests in `test_dispatch.py` update imports from `src.scheduler_manager` instead of `src.monitor`

### Helper methods that move
- `_calculate_days_since_last_test()` — reads `battery_model.get_last_upscmd_timestamp()`
- `_get_last_natural_blackout()` — reads `battery_model.data['discharge_events']`
- `_gather_scheduler_inputs()` — reads discharge_handler metrics + battery_model
- `_execute_scheduler_decision()` — logs, persists, dispatches
- `_should_run_scheduler()` — daily flag + hour check
- `_run_daily_scheduler()` → becomes the orchestrator inside SchedulerManager

### Pure function stays in battery_math
- `evaluate_test_scheduling()` in `battery_math/scheduler.py` stays where it is — it's a pure function, correctly placed
- SchedulerManager imports and calls it, same as MonitorDaemon does now

### Claude's Discretion
- Exact constructor signature details (whether discharge_handler or individual metric accessors)
- Whether to expose a `reset()` method for testing/restart scenarios
- Internal method decomposition within SchedulerManager
- Unit test structure and fixture design

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Scheduler methods in MonitorDaemon (extraction source)
- `src/monitor.py` lines 437-574 — All 6 scheduler methods to extract (`_calculate_days_since_last_test`, `_get_last_natural_blackout`, `_gather_scheduler_inputs`, `_execute_scheduler_decision`, `_should_run_scheduler`, `_run_daily_scheduler`)
- `src/monitor.py` lines 213-218 — Scheduler state fields (`scheduler_evaluated_today`, `last_scheduling_reason`, `last_next_test_timestamp`, `scheduling_config`)
- `src/monitor.py` lines 49-167 — Module-level functions to move (`validate_preconditions_before_upscmd`, `dispatch_test_with_audit`)
- `src/monitor.py` line 962 — Call site in `_poll_once()`: `self._run_daily_scheduler(datetime.now(timezone.utc))`
- `src/monitor.py` lines 892-917 — `_write_health_snapshot()` reads `self.last_scheduling_reason` and `self.last_next_test_timestamp`

### Pure scheduler function (stays in place)
- `src/battery_math/scheduler.py` — `evaluate_test_scheduling()` pure function + `SchedulerDecision` dataclass (NOT being moved)

### Existing tests
- `tests/test_scheduler.py` — 30+ tests for pure `evaluate_test_scheduling()` (no changes expected)
- `tests/test_dispatch.py` — 13 tests for `validate_preconditions_before_upscmd` and `dispatch_test_with_audit` (import path changes)
- `tests/test_monitor.py` lines 70, 114, 148 — Mocks `_run_daily_scheduler` (will change to mock `scheduler_manager.run_daily`)

### Extraction pattern reference
- `.planning/phases/19-extract-sagtracker/19-CONTEXT.md` — SagTracker extraction pattern (Phase 19, same approach)

### Config
- `src/monitor_config.py` — `SchedulingConfig` dataclass and `get_scheduling_config()` function

### Requirements
- `.planning/REQUIREMENTS.md` — ARCH-04 definition

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `SagTracker` (src/sag_tracker.py): Just-completed extraction — exact same pattern to follow
- `DischargeHandler` (src/discharge_handler.py): Established daemon collaborator — constructor injection, called from _poll_once
- `SchedulingConfig` (monitor_config.py): Already a separate dataclass — pass directly to SchedulerManager
- `evaluate_test_scheduling()` (battery_math/scheduler.py): Pure decision engine — SchedulerManager calls it
- `SchedulerDecision` (battery_math/scheduler.py): Immutable result type — no changes needed

### Established Patterns
- Daemon collaborators constructed in `_init_battery_model_and_estimators()` (SagTracker, DischargeHandler)
- Dependencies injected via constructor — no globals, no singletons
- Properties for read-only state access (SagTracker.is_measuring, SagTracker.ir_k)
- Module-level functions for stateless operations (validate_preconditions, dispatch_test)

### Integration Points
- `MonitorDaemon.__init__()` — construct SchedulerManager, pass dependencies
- `MonitorDaemon._poll_once()` line 962 — replace `self._run_daily_scheduler(now)` with `self.scheduler_manager.run_daily(now, self.current_metrics)`
- `MonitorDaemon._write_health_snapshot()` lines 912-913 — read `self.scheduler_manager.last_scheduling_reason` and `self.scheduler_manager.last_next_test_timestamp`
- `tests/test_dispatch.py` — update import path from `src.monitor` to `src.scheduler_manager`

</code_context>

<specifics>
## Specific Ideas

No specific requirements — the extraction is well-defined by the existing code structure and the SagTracker/DischargeHandler pattern already established. Follow the same dependency injection and delegation pattern from Phase 19.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 20-extract-schedulermanager*
*Context gathered: 2026-03-20*
