# Phase 21: Extract DischargeCollector - Research

**Researched:** 2026-03-20
**Domain:** Python class extraction / collaborator pattern (daemon decomposition)
**Confidence:** HIGH

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**Module placement (ARCH-05)**
- `src/discharge_collector.py` — top-level under src/, not under battery_math/
- DischargeCollector is a daemon collaborator (stateful, interacts with model during active discharge), not a pure math function
- Consistent with SagTracker (Phase 19) and SchedulerManager (Phase 20) placement

**Module boundary (ARCH-05)**
- DischargeCollector owns the "hot path" during active discharge: sample accumulation, cooldown timer, calibration point writes
- DischargeHandler keeps the post-discharge pipeline: SoH calculation, Peukert calibration, sulfation scoring, replacement prediction, alerts
- DischargeCollector does NOT process completed discharges — it collects data, DischargeHandler analyzes it
- `_update_battery_health()` stays in MonitorDaemon as the thin delegation call (same pattern as today)

**Public interface**
- Constructor takes dependencies: `battery_model`, `config` (or relevant subset: polling_interval, reporting_interval, reference_load_percent), `discharge_handler` (for predicted_runtime handoff), `ema_filter` (for current load reads), `event_classifier` (for state reads)
- Single `track(voltage, timestamp, event_type, current_metrics)` entry point — replaces `_track_discharge()` in MonitorDaemon
- `is_collecting` property — replaces `self.discharge_buffer.collecting` checks
- `buffer` property — read-only access to discharge_buffer for `_update_battery_health()` and `_write_health_snapshot()`
- `finalize()` method — for explicit end-of-discharge processing

**State ownership (ARCH-05)**
- DischargeCollector owns all four state fields: `discharge_buffer`, `_discharge_start_time`, `discharge_buffer_clear_countdown`, `calibration_last_written_index`
- MonitorDaemon no longer holds discharge collection state — DischargeCollector is the sole owner
- `DischargeBuffer` instance lifecycle managed by DischargeCollector (create, populate, reset)

**Methods that move to DischargeCollector**
- `_start_discharge_collection()` (monitor.py:365-394)
- `_handle_discharge_cooldown()` (monitor.py:396-425)
- `_track_discharge()` → becomes `track()` public method (monitor.py:427-448)
- `_finalize_discharge_collection()` (monitor.py:450-457)
- `_write_calibration_points()` (monitor.py:459-485)

**Sulfation method split (ARCH-06)**
- `_score_and_persist_sulfation` in DischargeHandler (95 lines) split into three methods:
  1. `_compute_sulfation_metrics()` — calls compute_sulfation_score() + compute_cycle_roi(), returns data dict; catches ValueError/TypeError
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

### Deferred Ideas (OUT OF SCOPE)
None — discussion stayed within phase scope.
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| ARCH-05 | DischargeCollector extracted from MonitorDaemon (sample accumulation, calibration writes) | Full source audit of extraction targets; SagTracker/SchedulerManager pattern fully documented |
| ARCH-06 | `_score_and_persist_sulfation` split into compute / persist / log methods | Full source audit of the 95-line monolith; split boundary and data-dict interface identified |
</phase_requirements>

---

## Summary

Phase 21 is a pure structural extraction: no logic changes, no behavior changes, no new features. The task is to move five methods out of MonitorDaemon into a new `DischargeCollector` class, rewire four call sites in MonitorDaemon, and split one 95-line method in DischargeHandler into three independently testable parts.

Two prior phases (19 — SagTracker, 20 — SchedulerManager) established the exact pattern to follow. The pattern is fully visible in the codebase: constructor injection of dependencies, properties for read-only state, a single public entry-point method (`track()`), module-level placement in `src/`. DischargeCollector is the third and final collaborator extraction in v3.1.

The sulfation split (ARCH-06) is the other half of this phase. `_score_and_persist_sulfation` (discharge_handler.py:230-325) currently does compute + persist + log in one method. The split decomposes it into three methods sharing a pre-computed data dict, orchestrated by the existing `update_battery_health()` caller. No logic moves; only call boundaries change.

**Primary recommendation:** Follow the SagTracker module structure exactly. Copy the constructor-injection pattern, property style, and test fixture factory from `test_sag_tracker.py`. For the sulfation split, extract methods in place (no new file) and use a plain `dict` as the data-passing contract between compute/persist/log.

---

## Standard Stack

This phase uses no new libraries. All dependencies are already present in the codebase.

### Core (already in codebase)
| Module | Purpose | Used by DischargeCollector |
|--------|---------|---------------------------|
| `src/model.py` — `BatteryModel` | Persist calibration points, cycle count, on-battery time | `calibration_write`, `calibration_batch_flush`, `increment_cycle_count`, `add_on_battery_time` |
| `src/monitor_config.py` — `DischargeBuffer` | Dataclass holding voltages/times/loads/collecting flag | Owned and managed by DischargeCollector |
| `src/monitor_config.py` — `Config` | Polling interval, reporting interval | Cooldown timer arithmetic, calibration batch threshold |
| `src/soc_predictor.py` — `soc_from_voltage` | Voltage → SoC for calibration LUT writes | `_write_calibration_points` |
| `src/monitor_config.py` — `DISCHARGE_BUFFER_MAX_SAMPLES` | Cap at 1000 samples | Buffer overflow guard |
| `src/event_classifier.py` — `EventType` | BLACKOUT_REAL / BLACKOUT_TEST detection | Discharge state machine |

**Installation:** No new packages needed.

---

## Architecture Patterns

### Established Collaborator Pattern (from Phases 19–20)

The pattern is already proven in this codebase. DischargeCollector must follow it exactly.

**Constructor:**
```python
# Source: src/sag_tracker.py (reference pattern)
class DischargeCollector:
    def __init__(
        self,
        battery_model: BatteryModel,
        config,            # Config or relevant subset — Claude's discretion
        discharge_handler, # For predicted_runtime handoff on OL→OB
        ema_filter,        # For current load reads in _start_discharge_collection
        event_classifier,  # Not needed — event_type passed into track() directly
    ):
        self.battery_model = battery_model
        # ... store deps
        self.discharge_buffer = DischargeBuffer()
        self._discharge_start_time = None
        self.discharge_buffer_clear_countdown = None
        self.calibration_last_written_index = 0
```

Note: `event_classifier` is NOT needed — `track()` receives `event_type` and `current_metrics` as parameters (same as SagTracker.track() receives `event_type` directly). The `ema_filter` is needed because `_start_discharge_collection` reads `ema_filter.stabilized` and `current_metrics.time_rem_minutes`, and `_track_discharge` reads `ema_filter.load`.

**Public interface:**
```python
@property
def is_collecting(self) -> bool:
    """True while discharge buffer is accumulating samples."""
    return self.discharge_buffer.collecting

@property
def buffer(self) -> DischargeBuffer:
    """Read-only access to discharge buffer (for MonitorDaemon delegation calls)."""
    return self.discharge_buffer

def track(self, voltage, timestamp, event_type, current_metrics) -> None:
    """Drive the discharge collection state machine for one poll tick."""
    # Contains the full logic of _track_discharge (which calls _handle_discharge_cooldown,
    # _start_discharge_collection, sample append, _write_calibration_points,
    # and _finalize_discharge_collection)

def finalize(self, timestamp) -> None:
    """Explicit end-of-discharge: record on-battery time and reset buffer."""
    # Contents of _finalize_discharge_collection
```

### MonitorDaemon Rewire Points

All changes in MonitorDaemon are mechanical substitutions:

| Before | After |
|--------|-------|
| `self.discharge_buffer = DischargeBuffer()` (line 84) | removed — DischargeCollector owns it |
| `self._discharge_start_time = None` (line 85) | removed |
| `self.discharge_buffer_clear_countdown = None` (line 86) | removed |
| `self.calibration_last_written_index = 0` (line 94) | removed |
| `self._track_discharge(voltage, timestamp)` (line 682) | `self.discharge_collector.track(voltage, timestamp, self.current_metrics.event_type, self.current_metrics)` |
| `self.discharge_handler.update_battery_health(self.discharge_buffer)` (line 259) | `self.discharge_handler.update_battery_health(self.discharge_collector.buffer)` |
| `self.discharge_buffer = DischargeBuffer()` (line 260) | `self.discharge_collector.discharge_buffer = DischargeBuffer()` OR a `reset_buffer()` method |
| `len(self.discharge_buffer.voltages)` (line 557) | `len(self.discharge_collector.buffer.voltages)` |
| `self.discharge_handler._auto_calibrate_peukert(current_soh, self.discharge_buffer)` (line 268) | `self.discharge_handler._auto_calibrate_peukert(current_soh, self.discharge_collector.buffer)` |
| `self.discharge_handler._log_discharge_prediction(self.discharge_buffer, ...)` (line 273) | `self.discharge_handler._log_discharge_prediction(self.discharge_collector.buffer, ...)` |

**Construction site** — add after `self.discharge_handler =` in `_init_battery_model_and_estimators()`:
```python
self.discharge_collector = DischargeCollector(
    battery_model=self.battery_model,
    config=config,
    discharge_handler=self.discharge_handler,
    ema_filter=self.ema_filter,
)
```

### Sulfation Method Split Pattern (ARCH-06)

The 95-line `_score_and_persist_sulfation` in `discharge_handler.py:230-325` splits into three private methods orchestrated by its caller. The data contract between them is a plain `dict`.

**Current flow:**
```
update_battery_health() → _score_and_persist_sulfation() [compute + persist + log + credit]
```

**New flow:**
```
update_battery_health()
  → _compute_sulfation_metrics()       # returns data dict (or None on failure)
  → _persist_sulfation_and_discharge() # receives data dict, writes to model
  → _log_discharge_complete()          # receives data dict, emits journald event
```

**Data dict keys** (pre-computed shared values):
```python
{
    'now_iso': str,
    'sulfation_state': SulfationState | None,
    'roi': float | None,
    'sulfation_score_r': float | None,
    'days_since_deep_r': float | None,
    'ir_trend_r': float,
    'recovery_delta_r': float,
    'discharge_duration': float,
    'dod_r': float,
    'roi_r': float | None,
    'soh_new': float,
    'soh_delta': float,
    'discharge_trigger': str,
    'capacity_ah_ref': float | None,
    'confidence_level': str,
}
```

`_grant_blackout_credit` is already a separate method (line 327) — it stays separate and is called from `_persist_sulfation_and_discharge`.

### Recommended Project Structure

No new directories. One new file:
```
src/
├── discharge_collector.py    # NEW — Phase 21
├── sag_tracker.py            # Phase 19 reference
├── scheduler_manager.py      # Phase 20 reference
├── discharge_handler.py      # ARCH-06 split (in-place)
└── monitor.py                # rewired, state fields removed
```

### Anti-Patterns to Avoid

- **Passing MonitorDaemon `self` into DischargeCollector:** All prior extractions inject specific dependencies. Never pass the daemon itself.
- **Calling `_update_battery_health()` from within DischargeCollector:** The cooldown expiry path already calls `self._update_battery_health()` inline in monitor.py line 422. This call must stay in MonitorDaemon — DischargeCollector signals via its state, daemon calls health update. The `track()` method cannot invoke MonitorDaemon callbacks.
- **Returning a callback from track():** The cooldown expiry currently calls `_update_battery_health()` inside `_handle_discharge_cooldown()`. After extraction, `track()` must signal this somehow. Options: return a bool `cooldown_expired`, or use an `is_cooldown_expired` property. MonitorDaemon checks and calls `_update_battery_health()` itself. This is the trickiest rewire point.
- **Splitting sulfation across files:** ARCH-06 is an in-place split within discharge_handler.py. Do not create a new file.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Batch calibration flush | Custom persistence | `battery_model.calibration_batch_flush()` | Already implemented, handles atomicity |
| SoC from voltage | Custom LUT lookup | `soc_from_voltage(v, model.get_lut())` | Already used in `_write_calibration_points` |
| Discharge buffer dataclass | Custom dict | `DischargeBuffer` from monitor_config.py | Already defined with voltages/times/loads/collecting |

---

## Common Pitfalls

### Pitfall 1: Cooldown Expiry Calls Back Into MonitorDaemon

**What goes wrong:** `_handle_discharge_cooldown()` currently calls `self._update_battery_health()` on line 422 directly. After extraction, `track()` lives in DischargeCollector — it cannot call back into MonitorDaemon.

**Why it happens:** The original code had all methods in the same class. The circular dependency only becomes visible at extraction time.

**How to avoid:** `track()` returns a `bool` indicating cooldown expiry (or a status enum). MonitorDaemon checks: `if self.discharge_collector.track(...): self._update_battery_health()`. This is the cleanest inversion and keeps DischargeCollector dependency-free of MonitorDaemon.

**Warning signs:** If you see `discharge_collector` holding a reference to `monitor_daemon`, the design is wrong.

### Pitfall 2: Buffer Reset After `_update_battery_health()`

**What goes wrong:** After `_update_battery_health()` processes the buffer, MonitorDaemon currently resets it: `self.discharge_buffer = DischargeBuffer()` (line 260). After extraction, MonitorDaemon holds `self.discharge_collector.buffer` — reassigning `self.discharge_buffer` on MonitorDaemon won't work.

**How to avoid:** Either (a) add a `reset_buffer()` method to DischargeCollector, or (b) have `_update_battery_health()` call `self.discharge_collector.discharge_buffer = DischargeBuffer()` directly. Option (a) is cleaner — aligns with the collaborator pattern.

### Pitfall 3: `_start_discharge_collection` Reads `current_metrics`

**What goes wrong:** `_start_discharge_collection()` (line 387-390) reads `self.ema_filter.stabilized` and `self.current_metrics.time_rem_minutes` to snapshot `discharge_predicted_runtime`. After extraction, `current_metrics` must be passed in — it cannot be a constructor dependency since it mutates every poll.

**How to avoid:** Pass `current_metrics` as a parameter to `track()` (already specified in the locked interface: `track(voltage, timestamp, event_type, current_metrics)`). DischargeCollector reads what it needs from the passed-in object.

### Pitfall 4: Existing Tests Reference `daemon._track_discharge` and `daemon.discharge_buffer` Directly

**What goes wrong:** test_monitor.py mocks `daemon._track_discharge = MagicMock()` in 7 places and asserts on `daemon.discharge_buffer` directly in several tests. After extraction these will fail.

**How to avoid:** Update the mocking to `daemon.discharge_collector.track = MagicMock()` and update assertions to read from `daemon.discharge_collector.buffer`. The buffer-state tests (lines 552, 583-584, 589-590, 616, 638-639) will need to be updated to read from `daemon.discharge_collector.buffer` — OR, if a backward-compat `discharge_buffer` property is added to MonitorDaemon that proxies to the collector, fewer test lines change. Per project policy (no backward compat shims), the tests should be updated to use the new path.

### Pitfall 5: Data Dict in Sulfation Split Must Include All Shared Pre-computed Values

**What goes wrong:** The current monolith pre-computes `sulfation_score_r`, `days_since_deep_r`, `ir_trend_r`, etc. on lines 281-287 and uses them in both the persistence block (lines 289-308) and the log block (lines 310-323). If the split forgets to include any of these in the data dict, the persist or log method will re-compute or be missing values.

**How to avoid:** The data dict must capture everything computed between the try/except and the three blocks. Verify the dict includes: `now_iso`, the scored state, roi, all `_r` rounded values, `discharge_duration`, `dod_r`, confidence_level.

---

## Code Examples

### SagTracker factory fixture (reference for DischargeCollector test factory)
```python
# Source: tests/test_sag_tracker.py:15-22
def make_tracker(ir_k=0.015, rls_theta=0.015, rls_P=1.0, nominal_voltage=13.0, nominal_power_watts=425.0):
    """Build a SagTracker with a mocked BatteryModel and real ScalarRLS."""
    mock_model = MagicMock()
    mock_model.get_nominal_voltage.return_value = nominal_voltage
    mock_model.get_nominal_power_watts.return_value = nominal_power_watts
    rls = ScalarRLS(theta=rls_theta, P=rls_P, forgetting_factor=0.97)
    tracker = SagTracker(battery_model=mock_model, rls_ir_k=rls, ir_k=ir_k)
    return tracker, mock_model
```

Equivalent factory for DischargeCollector tests will mock BatteryModel and use minimal Config stubs — no MonitorDaemon construction needed.

### DischargeCollector construction site in MonitorDaemon
```python
# Source: src/monitor.py:174-179 (SchedulerManager as reference)
self.scheduler_manager = SchedulerManager(
    battery_model=self.battery_model,
    nut_client=self.nut_client,
    scheduling_config=config.scheduling or SchedulingConfig(),
    discharge_handler=self.discharge_handler,
)
# DischargeCollector will follow same pattern, constructed after discharge_handler
```

### Cooldown return-value pattern for MonitorDaemon
```python
# In _poll_once(), the line:
self._track_discharge(voltage, timestamp)
# becomes approximately:
cooldown_expired = self.discharge_collector.track(
    voltage, timestamp, self.current_metrics.event_type, self.current_metrics)
if cooldown_expired:
    self._update_battery_health()
```

### Sulfation split orchestrator
```python
# Source: src/discharge_handler.py:230-325 (current monolith → new orchestration)
def update_battery_health(self, discharge_buffer):
    # ... existing guard clauses ...
    data = self._compute_sulfation_metrics(soh_new, soh_delta, discharge_buffer, discharge_trigger, capacity_ah_ref)
    self._persist_sulfation_and_discharge(data)
    self._log_discharge_complete(data)
```

---

## State of the Art

| Old Approach | Current Approach | Notes |
|--------------|-----------------|-------|
| All discharge state inline in MonitorDaemon | Extracted collaborator (this phase) | Same pattern as SagTracker (Phase 19) and SchedulerManager (Phase 20) |
| `_score_and_persist_sulfation` monolith | Three independent methods (this phase) | Enables unit-testing compute/persist/log independently |

---

## Open Questions

1. **Buffer reset method: `reset_buffer()` vs direct attribute assignment**
   - What we know: After `_update_battery_health()`, MonitorDaemon currently does `self.discharge_buffer = DischargeBuffer()`. After extraction, this must go through DischargeCollector.
   - What's unclear: Whether to expose `reset_buffer()` method or allow `discharge_collector.discharge_buffer = DischargeBuffer()` from outside.
   - Recommendation: Add `reset_buffer()` to DischargeCollector — keeps encapsulation consistent with SagTracker pattern.

2. **Full Config vs field subset in constructor**
   - What we know: Claude's discretion. `_write_calibration_points` needs `reporting_interval` and `polling_interval`. `_handle_discharge_cooldown` needs `polling_interval`. `_start_discharge_collection` needs nothing from Config directly.
   - Recommendation: Pass full `Config` — consistent with SchedulerManager which takes `SchedulingConfig`. Simpler than extracting individual fields. Config is frozen (immutable), safe to share.

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (no version pin detected; standard install) |
| Config file | none in root — pytest discovers via `tests/` |
| Quick run command | `pytest tests/test_discharge_collector.py -x` |
| Full suite command | `pytest tests/ -x` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| ARCH-05 | DischargeCollector accumulates samples on OB event | unit | `pytest tests/test_discharge_collector.py::test_track_accumulates_samples -x` | ❌ Wave 0 |
| ARCH-05 | DischargeCollector starts collection on OL→OB transition | unit | `pytest tests/test_discharge_collector.py::test_track_starts_collection_on_ob -x` | ❌ Wave 0 |
| ARCH-05 | DischargeCollector cooldown: OB→OL→OB within 60s is single event | unit | `pytest tests/test_discharge_collector.py::test_cooldown_continuation -x` | ❌ Wave 0 |
| ARCH-05 | DischargeCollector writes calibration points every reporting_interval polls | unit | `pytest tests/test_discharge_collector.py::test_calibration_write_batch -x` | ❌ Wave 0 |
| ARCH-05 | DischargeCollector finalize records on-battery time | unit | `pytest tests/test_discharge_collector.py::test_finalize_records_on_battery_time -x` | ❌ Wave 0 |
| ARCH-05 | MonitorDaemon no longer has discharge_buffer attribute | unit | `pytest tests/test_monitor.py -x -k discharge` | ✅ (needs update) |
| ARCH-06 | _compute_sulfation_metrics returns dict with all required keys | unit | `pytest tests/test_discharge_handler.py::test_compute_sulfation_metrics_returns_dict -x` | ❌ Wave 0 |
| ARCH-06 | _compute_sulfation_metrics returns None-scored dict on ValueError | unit | `pytest tests/test_discharge_handler.py::test_compute_sulfation_metrics_handles_error -x` | ❌ Wave 0 |
| ARCH-06 | _persist_sulfation_and_discharge appends to model history | unit | `pytest tests/test_discharge_handler.py::test_persist_sulfation_appends_history -x` | ❌ Wave 0 |
| ARCH-06 | _log_discharge_complete emits journald event | unit | `pytest tests/test_discharge_handler.py::test_log_discharge_complete_emits_event -x` | ❌ Wave 0 |
| ARCH-05/06 | All existing tests pass with no regressions | regression | `pytest tests/ -x` | ✅ |

### Sampling Rate
- **Per task commit:** `pytest tests/test_discharge_collector.py tests/test_discharge_handler.py tests/test_monitor.py -x`
- **Per wave merge:** `pytest tests/ -x`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_discharge_collector.py` — new file; covers ARCH-05 accumulation, cooldown, calibration, finalize, is_collecting property
- [ ] New test methods in `tests/test_discharge_handler.py` — covers ARCH-06 three-method split (_compute_sulfation_metrics, _persist_sulfation_and_discharge, _log_discharge_complete)
- [ ] Update `tests/test_monitor.py` — replace 7x `daemon._track_discharge = MagicMock()` with `daemon.discharge_collector.track = MagicMock()`, update `daemon.discharge_buffer` reads to `daemon.discharge_collector.buffer`

---

## Sources

### Primary (HIGH confidence)
- Direct source reading: `src/monitor.py` lines 80-94, 124-182, 215-275, 365-485, 540-570, 630-704
- Direct source reading: `src/discharge_handler.py` lines 225-347
- Direct source reading: `src/sag_tracker.py` (full file — reference pattern)
- Direct source reading: `src/scheduler_manager.py` (full file — reference pattern)
- Direct source reading: `tests/test_sag_tracker.py` (full file — test pattern reference)
- Direct source reading: `tests/test_scheduler_manager.py` (fixture pattern)
- Direct source reading: `tests/test_monitor.py` lines 540-649 (discharge buffer test coverage)
- `.planning/phases/21-extract-dischargecollector/21-CONTEXT.md` (locked decisions)
- `.planning/REQUIREMENTS.md` (ARCH-05, ARCH-06 definitions)

### Secondary (MEDIUM confidence)
- None required — all research is from direct codebase inspection.

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all dependencies already in codebase, directly verified
- Architecture patterns: HIGH — pattern proven in Phases 19 and 20, source read directly
- Pitfalls: HIGH — identified from direct code reading of the exact extraction targets
- Test coverage: HIGH — existing test file inspected, Wave 0 gaps identified precisely

**Research date:** 2026-03-20
**Valid until:** Stable — no external dependencies, only internal code structure
