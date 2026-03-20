# Phase 21: Extract DischargeCollector - Context

**Gathered:** 2026-03-20
**Status:** Ready for planning

<domain>
## Phase Boundary

DischargeCollector owns discharge sample accumulation (voltage/time/load buffers), cooldown management, and calibration point writes during active discharge. Sulfation scoring in DischargeHandler is split into three independently testable methods: compute, persist, and log. MonitorDaemon delegates discharge tracking to DischargeCollector. All existing tests pass with no regressions.

Requirements: ARCH-05, ARCH-06.

</domain>

<decisions>
## Implementation Decisions

### Module placement (ARCH-05)
- `src/discharge_collector.py` — top-level under src/, not under battery_math/
- DischargeCollector is a daemon collaborator (stateful, interacts with model during active discharge), not a pure math function
- Consistent with SagTracker (Phase 19) and SchedulerManager (Phase 20) placement

### Module boundary (ARCH-05)
- DischargeCollector owns the "hot path" during active discharge: sample accumulation, cooldown timer, calibration point writes
- DischargeHandler keeps the post-discharge pipeline: SoH calculation, Peukert calibration, sulfation scoring, replacement prediction, alerts
- DischargeCollector does NOT process completed discharges — it collects data, DischargeHandler analyzes it
- `_update_battery_health()` stays in MonitorDaemon as the thin delegation call (same pattern as today)

### Public interface
- Constructor takes dependencies: `battery_model`, `config` (or relevant subset: polling_interval, reporting_interval, reference_load_percent), `discharge_handler` (for predicted_runtime handoff), `ema_filter` (for current load reads), `event_classifier` (for state reads)
- Single `track(voltage, timestamp, event_type, current_metrics)` entry point — replaces `_track_discharge()` in MonitorDaemon
- `is_collecting` property — replaces `self.discharge_buffer.collecting` checks
- `buffer` property — read-only access to discharge_buffer for `_update_battery_health()` and `_write_health_snapshot()`
- `finalize()` method — for explicit end-of-discharge processing

### State ownership (ARCH-05)
- DischargeCollector owns all four state fields: `discharge_buffer`, `_discharge_start_time`, `discharge_buffer_clear_countdown`, `calibration_last_written_index`
- MonitorDaemon no longer holds discharge collection state — DischargeCollector is the sole owner
- `DischargeBuffer` instance lifecycle managed by DischargeCollector (create, populate, reset)

### Methods that move to DischargeCollector
- `_start_discharge_collection()` (monitor.py:365-394)
- `_handle_discharge_cooldown()` (monitor.py:396-425)
- `_track_discharge()` → becomes `track()` public method (monitor.py:427-448)
- `_finalize_discharge_collection()` (monitor.py:450-457)
- `_write_calibration_points()` (monitor.py:459-485)

### Sulfation method split (ARCH-06)
- `_score_and_persist_sulfation` in DischargeHandler (95 lines) split into three methods:
  1. `_compute_sulfation_metrics()` — calls compute_sulfation_score() + compute_cycle_roi(), returns data dict with all computed values; catches ValueError/TypeError like current code
  2. `_persist_sulfation_and_discharge()` — receives data dict, appends to sulfation_history + discharge_events in model, grants blackout credit
  3. `_log_discharge_complete()` — receives data dict, emits structured journald event
- Each method independently testable — no combined compute+persist+log in one call
- `update_battery_health()` orchestrates: compute → persist → log (same as current flow, just decomposed)

### Claude's Discretion
- Exact constructor signature details (whether to pass full Config or individual fields)
- Whether `track()` returns a status enum or just mutates state
- Internal method naming within DischargeCollector
- Unit test structure and fixture design
- Whether `_grant_blackout_credit` stays inline in persist method or becomes its own method

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Discharge collection code in MonitorDaemon (extraction source)
- `src/monitor.py` lines 365-394 — `_start_discharge_collection()` (buffer init, cycle count increment, prediction snapshot)
- `src/monitor.py` lines 396-425 — `_handle_discharge_cooldown()` (60s cooldown timer state machine)
- `src/monitor.py` lines 427-448 — `_track_discharge()` (main entry point, sample accumulation, buffer cap)
- `src/monitor.py` lines 450-457 — `_finalize_discharge_collection()` (on-battery time recording, buffer state reset)
- `src/monitor.py` lines 459-485 — `_write_calibration_points()` (LUT calibration batch writes)
- `src/monitor.py` lines 84-86, 94 — State fields to move (discharge_buffer, _discharge_start_time, discharge_buffer_clear_countdown, calibration_last_written_index)

### Sulfation scoring in DischargeHandler (split target)
- `src/discharge_handler.py` lines 230-325 — `_score_and_persist_sulfation()` (compute + persist + log monolith to split)
- `src/discharge_handler.py` lines 327-347 — `_grant_blackout_credit()` (called from _score_and_persist_sulfation)
- `src/discharge_handler.py` lines 570-631 — Helper methods used by sulfation scoring (_calculate_days_since_deep, _estimate_ir_trend)
- `src/discharge_handler.py` lines 673-714 — More helpers (_estimate_dod_from_buffer, _estimate_cycle_budget, _assess_sulfation_confidence)

### MonitorDaemon call sites (rewire points)
- `src/monitor.py` line 682 — `_track_discharge(voltage, timestamp)` call in `_poll_once()` → becomes `self.discharge_collector.track(...)`
- `src/monitor.py` lines 257-260 — `_update_battery_health()` reads `self.discharge_buffer` → reads from `self.discharge_collector.buffer`
- `src/monitor.py` line 557 — `_log_status()` reads `len(self.discharge_buffer.voltages)` → reads from discharge_collector
- `src/monitor.py` lines 84-86 — State fields referenced throughout (search for `discharge_buffer`, `_discharge_start_time`, `discharge_buffer_clear_countdown`)

### Extraction pattern references
- `src/sag_tracker.py` — SagTracker extraction (Phase 19) — same collaborator pattern
- `src/scheduler_manager.py` — SchedulerManager extraction (Phase 20) — same delegation pattern
- `.planning/phases/19-extract-sagtracker/19-CONTEXT.md` — Phase 19 decisions (constructor injection, properties, delegation)
- `.planning/phases/20-extract-schedulermanager/20-CONTEXT.md` — Phase 20 decisions (state ownership, call site rewiring)

### Requirements
- `.planning/REQUIREMENTS.md` — ARCH-05, ARCH-06 definitions

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `SagTracker` (src/sag_tracker.py): Just-completed extraction — exact same collaborator pattern to follow
- `SchedulerManager` (src/scheduler_manager.py): Another completed extraction — same delegation pattern
- `DischargeHandler` (src/discharge_handler.py): Existing collaborator that DischargeCollector will complement (not replace)
- `DischargeBuffer` dataclass (monitor_config.py): Already a separate data structure — DischargeCollector will own instances of it
- `soc_from_voltage()` (soc_predictor.py): Used in `_write_calibration_points()` — DischargeCollector will import it

### Established Patterns
- Daemon collaborators constructed in `_init_battery_model_and_estimators()` (SagTracker, SchedulerManager, DischargeHandler)
- Dependencies injected via constructor — no globals, no singletons
- Properties for read-only state access (SagTracker.is_measuring, SchedulerManager.last_scheduling_reason)
- Module-level functions for stateless operations
- `_update_battery_health()` already delegates to DischargeHandler — same thin wrapper pattern continues

### Integration Points
- `MonitorDaemon.__init__()` — construct DischargeCollector after DischargeHandler (DischargeCollector needs discharge_handler for predicted_runtime)
- `MonitorDaemon._poll_once()` line 682 — replace `self._track_discharge(voltage, timestamp)` with `self.discharge_collector.track(...)`
- `MonitorDaemon._update_battery_health()` — read buffer from `self.discharge_collector.buffer`, call reset after processing
- `MonitorDaemon._log_status()` line 557 — read buffer length from discharge_collector
- `MonitorDaemon._poll_once()` line 704 — sleep decision currently reads `self.sag_tracker.is_measuring` (no change needed)

</code_context>

<specifics>
## Specific Ideas

No specific requirements — the extraction is well-defined by the existing code structure and the SagTracker/SchedulerManager pattern already established. This is the third and final MonitorDaemon collaborator extraction in v3.1, completing the decomposition of the god class. Follow the same dependency injection and delegation pattern from Phases 19-20.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

### Reviewed Todos (not folded)
- "Anti-sulfation deep discharge scheduling for battery longevity" — already implemented in v3.0 (Phase 17); todo is stale.

</deferred>

---

*Phase: 21-extract-dischargecollector*
*Context gathered: 2026-03-20*
