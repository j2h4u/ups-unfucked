# Phase 19: Extract SagTracker - Context

**Gathered:** 2026-03-20
**Status:** Ready for planning

<domain>
## Phase Boundary

SagTracker logic lives in its own module, fully decoupled from MonitorDaemon internals. MonitorDaemon instantiates SagTracker and delegates sag-related calls to it. SagTracker has direct unit tests without constructing a MonitorDaemon. All existing tests pass with no regressions.

Requirements: ARCH-03.

</domain>

<decisions>
## Implementation Decisions

### Module placement
- `src/sag_tracker.py` — top-level under src/, not under battery_math/
- SagTracker is a daemon collaborator (stateful, interacts with model and RLS), not a pure math function
- Consistent with future extractions (SchedulerManager, DischargeCollector will also be top-level)

### Public interface
- Constructor takes dependencies: `battery_model`, `config` (or relevant subset), initial `ir_k` value, `rls_ir_k` (ScalarRLS instance)
- Single `track(voltage, event_type, transition_occurred, current_load)` entry point — drives the IDLE → MEASURING → COMPLETE state machine
- `is_measuring` property for poll interval decision (replaces `self.sag_state == SagState.MEASURING` checks in _poll_once and sleep)
- `reset_idle()` method for error recovery (replaces `self.sag_state = SagState.IDLE` in error handler)
- Internal methods `_record_voltage_sag()` stays private inside SagTracker

### RLS ownership
- SagTracker owns `rls_ir_k` — it's only used in sag context (voltage sag → R_internal → ir_k calibration)
- Constructed from model's persisted RLS state at init
- SagTracker updates `ir_k` on model directly (via `battery_model.set_ir_k()`, `battery_model.set_rls_state()`)
- MonitorDaemon no longer holds `self.rls_ir_k` or `self.ir_k` — SagTracker is the sole owner

### State reset
- SagTracker exposes `reset_rls(theta, P)` method for battery baseline reset
- MonitorDaemon._reset_battery_baseline() calls `self.sag_tracker.reset_rls(theta=0.015, P=1.0)` instead of directly resetting rls_ir_k
- SagState enum stays in monitor_config.py (used by SagTracker internally)

### Claude's Discretion
- Exact constructor signature details (whether to pass full Config or just SAG_SAMPLES_REQUIRED)
- Whether to pass load as a callable or a direct value to track()
- Internal method decomposition within SagTracker
- Unit test structure and fixture design

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Sag tracking implementation (extraction source)
- `src/monitor.py` lines 573-622 — `_record_voltage_sag()` and `_track_voltage_sag()` (the code to extract)
- `src/monitor.py` lines 219-221 — Sag state fields (`sag_state`, `v_before_sag`, `sag_buffer`)
- `src/monitor.py` lines 281-285 — `ir_k` and `rls_ir_k` initialization (moves to SagTracker)
- `src/monitor.py` lines 1014, 1037, 1063 — Call sites in `_poll_once()`, sleep, error handler

### Sag state machine
- `src/monitor_config.py` lines 50, 207-211 — `SAG_SAMPLES_REQUIRED` constant and `SagState` enum

### Dependencies
- `src/battery_math/rls.py` — `ScalarRLS` class (used for ir_k auto-calibration)
- `src/model.py` lines 360-363, 405-425, 561 — Model methods used by sag tracking (get_nominal_voltage, set_ir_k, set_rls_state, add_r_internal_entry)

### Battery reset interaction
- `src/monitor.py` lines 393-417 — `_reset_battery_baseline()` that resets rls_ir_k (needs to delegate to SagTracker)

### Requirements
- `.planning/REQUIREMENTS.md` — ARCH-03 definition

### Prior phase context
- `.planning/phases/18-unify-coulomb-counting/18-CONTEXT.md` — Phase 18 extraction pattern (battery_math pure function extraction as reference)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `ScalarRLS` (battery_math/rls.py): Already extracted pure math class — SagTracker will use it directly
- `SagState` enum (monitor_config.py): Already defined separately from MonitorDaemon — import as-is
- `SAG_SAMPLES_REQUIRED` constant (monitor_config.py): Already in config module

### Established Patterns
- Phase 18 extraction pattern: pure function extracted to battery_math/, exported via __init__.py. Phase 19 differs — SagTracker is stateful, lives in src/ not battery_math/
- DischargeHandler pattern (src/discharge_handler.py): Existing extracted collaborator that MonitorDaemon delegates to. SagTracker should follow the same delegation pattern — constructed in MonitorDaemon.__init__(), methods called from _poll_once()
- Dependencies injected via constructor (battery_model, config, rls instances) — consistent with DischargeHandler pattern

### Integration Points
- `MonitorDaemon.__init__()` — construct SagTracker, pass dependencies
- `MonitorDaemon._poll_once()` line 1014 — replace `self._track_voltage_sag(voltage)` with `self.sag_tracker.track(...)`
- `MonitorDaemon._poll_once()` line 1037 — replace `self.sag_state == SagState.MEASURING` with `self.sag_tracker.is_measuring`
- `MonitorDaemon.run()` error handler line 1063 — replace `self.sag_state = SagState.IDLE` with `self.sag_tracker.reset_idle()`
- `MonitorDaemon._reset_battery_baseline()` line 415 — replace direct rls_ir_k reset with `self.sag_tracker.reset_rls()`

</code_context>

<specifics>
## Specific Ideas

No specific requirements — the extraction is well-defined by the existing code structure and the DischargeHandler pattern already established in the codebase. Follow the same dependency injection and delegation pattern.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 19-extract-sagtracker*
*Context gathered: 2026-03-20*
