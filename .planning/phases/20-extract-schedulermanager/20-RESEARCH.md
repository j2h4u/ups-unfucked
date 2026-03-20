# Phase 20: Extract SchedulerManager — Research

**Researched:** 2026-03-20
**Phase:** 20 — Extract SchedulerManager
**Requirement:** ARCH-04

## RESEARCH COMPLETE

## 1. Current Scheduler Code in MonitorDaemon

### State Fields (lines 213-218)
```python
self.scheduler_evaluated_today = False    # Daily flag
self.last_scheduling_reason: str = 'observing'
self.last_next_test_timestamp: str | None = None
self.scheduling_config = config.scheduling or SchedulingConfig()
```

### Methods to Extract (lines 437-574)
| Method | Lines | Purpose |
|--------|-------|---------|
| `_calculate_days_since_last_test()` | 437-447 | Reads model timestamp, returns float days |
| `_get_last_natural_blackout()` | 449-458 | Scans discharge_events for natural events |
| `_gather_scheduler_inputs()` | 460-474 | Collects all inputs for evaluate_test_scheduling() |
| `_execute_scheduler_decision()` | 476-517 | Logs, persists, dispatches test |
| `_should_run_scheduler()` | 519-531 | Daily hour + minute check + evaluated flag |
| `_run_daily_scheduler()` | 533-574 | Orchestrator: gather → evaluate → execute |

### Module-Level Functions to Move (lines 49-167)
| Function | Lines | Purpose |
|----------|-------|---------|
| `validate_preconditions_before_upscmd()` | 49-84 | 4 guard clauses before upscmd |
| `dispatch_test_with_audit()` | 87-167 | Send upscmd + model persistence + logging |

### Pure Function (stays in place)
- `battery_math/scheduler.py` — `evaluate_test_scheduling()` pure function (200 LOC, 7 safety gates)
- `SchedulerDecision` frozen dataclass — already correctly placed in battery_math

## 2. Dependencies Analysis

### SchedulerManager Dependencies
| Dependency | Source | Used By |
|-----------|--------|---------|
| `battery_model` | Constructor injection | `_calculate_days_since_last_test`, `_get_last_natural_blackout`, `_gather_scheduler_inputs`, `_execute_scheduler_decision` |
| `nut_client` | Constructor injection | `dispatch_test_with_audit` |
| `scheduling_config` | Constructor injection | `_should_run_scheduler`, `_run_daily_scheduler` |
| `discharge_handler` | Constructor injection | `_gather_scheduler_inputs` (reads last_sulfation_score, last_cycle_roi, last_cycle_budget_remaining) |
| `current_metrics` | Method parameter | `dispatch_test_with_audit` (reads ups_status_override, soc) |

### What MonitorDaemon Reads from SchedulerManager
- `last_scheduling_reason` — used in `_write_health_snapshot()` (line 912)
- `last_next_test_timestamp` — used in `_write_health_snapshot()` (line 913)

## 3. Extraction Pattern (from Phase 19 SagTracker)

### Pattern Established
1. New file: `src/scheduler_manager.py` (top-level, not battery_math/)
2. Constructor: inject dependencies (battery_model, nut_client, scheduling_config, discharge_handler)
3. Single entry point: `run_daily(now, current_metrics)` — called from `_poll_once()`
4. Properties for read-only state: `last_scheduling_reason`, `last_next_test_timestamp`
5. MonitorDaemon only holds `self.scheduler_manager` — no scheduler state/methods remain

### SagTracker Pattern Reference
```python
# In MonitorDaemon.__init__() / _init_battery_model_and_estimators():
self.sag_tracker = SagTracker(
    battery_model=self.battery_model,
    rls_ir_k=ScalarRLS.from_dict(...),
    ir_k=self.battery_model.get_ir_k(),
)

# In _poll_once():
self.sag_tracker.track(voltage, event_type=..., transition_occurred=..., current_load=...)

# In error handler:
self.sag_tracker.reset_idle()

# In _write_health_snapshot() equivalent:
# Access via property: self.sag_tracker.ir_k
```

## 4. Test Impact

### Existing Tests (no behavioral changes)
| File | Tests | Impact |
|------|-------|--------|
| `test_scheduler.py` | 30+ | None — tests pure `evaluate_test_scheduling()` |
| `test_dispatch.py` | 13 | Import path changes: `src.monitor` → `src.scheduler_manager` |
| `test_monitor.py` | 3 mocks | `_run_daily_scheduler` mock → `scheduler_manager.run_daily` mock |

### New Tests Needed
- `test_scheduler_manager.py` — Unit tests for SchedulerManager class:
  - `_should_run_scheduler()` with various hour/minute/flag combinations
  - `_calculate_days_since_last_test()` with valid/invalid/missing timestamps
  - `_get_last_natural_blackout()` with various discharge event lists
  - `_gather_scheduler_inputs()` with various model states
  - `run_daily()` integration: gather → evaluate → execute flow
  - Property access: `last_scheduling_reason`, `last_next_test_timestamp`

## 5. Risk Assessment

### Low Risk
- Mechanical extraction — no logic changes
- Pattern proven by Phase 19 (SagTracker)
- Well-isolated code block (lines 437-574 in monitor.py)
- Existing test coverage for pure scheduler and dispatch logic
- Module-level functions (`validate_preconditions`, `dispatch_test_with_audit`) already stateless

### Potential Issues
- `_gather_scheduler_inputs()` reads from `discharge_handler` — ensure SchedulerManager receives discharge_handler reference
- `dispatch_test_with_audit()` uses `safe_save()` from monitor_config — import needed
- `_execute_scheduler_decision()` accesses `self.nut_client` and `self.current_metrics` — clean up parameter passing

## 6. Validation Architecture

### Requirement: ARCH-04
> SchedulerManager extracted from MonitorDaemon into own module

### Validation Checks
1. **File exists:** `src/scheduler_manager.py` contains `class SchedulerManager`
2. **MonitorDaemon clean:** No scheduler methods remain in `src/monitor.py` (grep for `_run_daily_scheduler`, `_should_run_scheduler`, `_gather_scheduler_inputs`, `_execute_scheduler_decision`, `_calculate_days_since_last_test`, `_get_last_natural_blackout`)
3. **State clean:** MonitorDaemon doesn't hold `scheduler_evaluated_today`, `last_scheduling_reason`, `last_next_test_timestamp`
4. **Delegation:** MonitorDaemon calls `self.scheduler_manager.run_daily(now, current_metrics)`
5. **Properties work:** `self.scheduler_manager.last_scheduling_reason` used in health snapshot
6. **Module-level functions moved:** `validate_preconditions_before_upscmd` and `dispatch_test_with_audit` in `scheduler_manager.py`
7. **Tests pass:** All 476+ tests pass
8. **New tests:** `test_scheduler_manager.py` exercises SchedulerManager without MonitorDaemon
