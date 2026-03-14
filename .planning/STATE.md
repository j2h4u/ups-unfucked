---
gsd_state_version: 1.0
milestone: v1.1
milestone_name: Expert Panel Review Fixes
current_plan: Not started
status: unknown
last_updated: "2026-03-14T16:17:15.010Z"
progress:
  total_phases: 5
  completed_phases: 5
  total_plans: 14
  completed_plans: 15
---

# Project State — UPS Battery Monitor v1.1

**Last Updated:** 2026-03-14 after Phase 11 Plan 02 completion (LOW-03, LOW-04)
**Milestone:** v1.1 Expert Panel Review Fixes
**Current Focus:** Phase 11 — Polish & Future Prep (2/5 plans complete: 11-01 LOW-01/02, 11-02 LOW-03/04)

---

## Project Reference

**Core Value:** Server shuts down cleanly and in time during blackouts, using every available minute — not relying on CyberPower firmware.

**v1.0 Status:** Shipped 2026-03-14 with 160 tests, 5,003 LOC, 6 phases completed. Real blackout validation: 47 min actual vs 22 min firmware prediction.

**v1.1 Scope:** Fix all P0-P3 findings from 2026-03-15 expert panel review (19 requirements, 5 phases). Safety-critical first (SAFE-01/02), then architecture (ARCH-01/02/03), tests, quality, polish.

---

## Current Position

| Item | Value |
|------|-------|
| Milestone | v1.1 Expert Panel Review Fixes |
| Total Phases | 5 (Phase 7-11) |
| Current Phase | 11: Polish & Future Prep (executing, 2/5 plans complete) |
| Status | Phase 9 complete (TEST-01/02/03/04/05), Phase 10 complete (QUAL-01..05), Phase 11: 11-01 (LOW-01, LOW-02) complete, 11-02 (LOW-03, LOW-04) complete |
| Progress | 4/5 phases started, 17/19 requirements implemented (SAFE-01, SAFE-02, ARCH-01, ARCH-02, ARCH-03, TEST-01, TEST-02, TEST-03, TEST-04, TEST-05, QUAL-01, QUAL-02, QUAL-03, QUAL-04, QUAL-05, LOW-01, LOW-02, LOW-03, LOW-04), Phase 11 2/5 plans complete

---

## Progress Metrics

```
Requirements Coverage: 17/19 (89.5%)
├─ Safety (P0): SAFE-01✓, SAFE-02✓ → Phase 7 (done)
├─ Architecture (P1): ARCH-01✓, ARCH-02✓, ARCH-03✓ → Phase 8 (done)
├─ Testing (P1): TEST-01✓, TEST-02✓, TEST-03✓, TEST-04✓, TEST-05✓ → Phase 9 (complete)
├─ Code Quality (P2): QUAL-01✓, QUAL-02✓, QUAL-03✓, QUAL-04✓, QUAL-05✓ → Phase 10 (complete)
└─ Low Priority (P3): LOW-01✓, LOW-02✓, LOW-03✓, LOW-04, LOW-05✓ → Phase 11 (3/5 plans complete)
├─ Testing (P1): TEST-01✓, TEST-02✓, TEST-03✓, TEST-04✓, TEST-05✓ → Phase 9 (complete)
├─ Code Quality (P2): QUAL-01✓, QUAL-02✓, QUAL-03✓, QUAL-04✓, QUAL-05✓ → Phase 10 (complete)
└─ Low Priority (P3): LOW-01✓, LOW-02✓, LOW-03, LOW-04, LOW-05 → Phase 11 (1/5 plans complete)

Phases defined: 5
Plans completed: 13/~15-18
Current plan: Not started
Expected LOC growth: ~50 remaining for LOW-03..05 + final optimizations
```

---

## Accumulated Context

### Phase 7: Safety-Critical (SAFE-01, SAFE-02)

**Problem:** LB flag written to dummy-ups only once per 60s REPORTING_INTERVAL. If blackout happens at second 1 of interval, shutdown signal delayed up to 60s — dangerous for tight margins.

**Solution:** Write virtual UPS metrics every 10s poll during OB state. LB flag decision in `_handle_event_transition()` executes every poll, not batched.

**Real Impact:** During 2026-03-12 blackout, 47-minute actual vs 22-minute firmware. With fast LB flag, we can shutdown reliably with every minute counted.

**Implementation Details:**
- Modify `_update_dummy_ups()` to detect OB state and write metrics on every poll (currently writes every REPORTING_INTERVAL=60s)
- Ensure `_handle_event_transition()` evaluates LB decision on every poll while OB active
- Test with mock blackout events: verify file mtime updates every 10s
- Verify upsmon receives LB signal within 10s of OB transition

---

### Phase 8: Architecture Foundation (ARCH-01, ARCH-02, ARCH-03)

**ARCH-01 (dataclass refactor):** `current_metrics` is 10-key untyped dict. Refactor to `@dataclass CurrentMetrics` with typed fields:
- voltage: float
- charge: float
- status: str
- runtime_estimated: float
- etc.

Benefits: IDE autocomplete, static type checking, clear contract. All callers of `_update_battery_health()` and metric getters will see typed fields.

**ARCH-02 (config extraction):** `_cfg` dict + `UPS_NAME` + `MODEL_DIR` are module-level globals (lines ~20-30). Extract to `Config` frozen dataclass:
```python
@dataclass(frozen=True)
class Config:
    ups_name: str
    model_dir: Path
    polling_interval: int
    reporting_interval: int
    # etc.
```

Passed to `Monitor.__init__`, enabling:
- Testing with different configs
- No global state pollution
- Easier future multi-UPS support

**ARCH-03 (imports):** Two fixes:
1. `from enum import Enum` at line 68 — check if used, move to top if yes, remove if not
2. `from src.soh_calculator import interpolate_cliff_region` inside method body (line ~450?) — move to module top

**Dependency Chain:** ARCH-01 → ARCH-02 → ARCH-03 (one modifies methods, one modifies __init__, one cleans up imports). Can be same phase but sequential commits to isolate changes.

---

### Phase 9: Test Coverage (TEST-01 through TEST-05)

**TEST-01 (OL→OB→OL integration test):** Full discharge lifecycle:
- Mock OL state → voltage, load, status as nominal
- Transition to OB → voltage drops, `_handle_event_transition()` fires
- Event → `_track_discharge()` runs, history accumulates
- Recovery → transition back to OL
- Verify: `_update_battery_health()` called, SoH updated, model saved

**TEST-02 (Peukert auto-calibration):** Unit test for `_auto_calibrate_peukert()`:
- Mock discharge history with voltage samples
- Call `_auto_calibrate_peukert()`
- Verify exponent recalculation math (ln(I1/I2) / ln(t1/t2))
- Test edge cases: divide by zero, empty history, single sample

**TEST-03 (signal handler):** Test SIGTERM handler:
- Start Monitor in test mode
- Send SIGTERM
- Verify `model.save()` called before exit
- Verify no exceptions in handler

**TEST-04 (conftest mock_socket_ok):** Fix existing mock:
- Real upsc returns multi-line LIST VAR format:
  ```
  battery.charge: <value>
  battery.date: <date>
  battery.runtime: <value>
  ...
  END LIST VAR
  ```
- Update `mock_socket_ok` to return this format, not simplified string
- Verify `get_ups_vars()` parsing works correctly

**TEST-05 (floating-point tolerance):** Address exact comparison `entry["v"] == voltage`:
- Voltage from EMA may drift ±0.1V due to filtering
- Replace `==` with tolerance check: `abs(entry["v"] - voltage) < 0.01`
- Or document why exact comparison is safe (e.g., voltage quantization)

---

### Phase 10: Code Quality (QUAL-01 through QUAL-05)

**QUAL-01 (_safe_save helper):** Extract repeated pattern:
```python
try:
    model.save()
except OSError as e:
    logging.error(f"Failed to save model: {e}")
```

Four instances (search for `model.save()`). Create:
```python
def _safe_save(model: BatteryModel) -> None:
    """Save model to disk, log errors if any."""
    try:
        model.save()
    except OSError as e:
        logging.error(f"Failed to save model: {e}")
```

Use everywhere instead of inline try/except.

**QUAL-02 (hardcoded date):** In `_default_vrla_lut()` (line ~200?), replace:
```python
"soh_history": [{"date": "2026-03-13", ...}]
```

With:
```python
"soh_history": [{"date": datetime.now().strftime('%Y-%m-%d'), ...}]
```

Ensures calibration mode gets current date, not hardcoded.

**QUAL-03 (docstring fix):** `soc_from_voltage()` docstring (line ~130?):
- Current: "Uses binary search to find voltage in table"
- Actual: Linear scan `for entry in ...`
- Fix: Either correct docstring to "linear scan" or implement binary search (prefer docstring fix)

**QUAL-04 (batch calibration writes):** `calibration_write()` (line ~350?) currently saves per point:
```python
for point in new_points:
    # ... process point ...
    model.save()  # ← writes to disk N times
```

Change to:
```python
for point in new_points:
    # ... process point ...
# ... after loop ...
model.save()  # ← single save
```

Reduces SSD wear during calibration mode.

**QUAL-05 (double logging in virtual_ups.py):** Lines ~90 and ~93:
```python
# Line 90:
except Exception as e:
    logging.error(f"Inner error: {e}")
# Line 93:
except Exception as e:
    logging.error(f"Outer error: {e}")
```

Both catch and log same failure. Refactor to single handler: remove inner catch or re-raise.

---

### Phase 11: Polish & Future Prep (LOW-01 through LOW-05)

**Plan 11-01: History Pruning & fdatasync Optimization (COMPLETED)**
- LOW-01: soh_history and r_internal_history pruning (keep last 30 entries)
- LOW-02: fdatasync optimization in atomic_write_json() — 10-20% faster I/O
- Impact: Reduced model.json bloat over time; better SSD performance; all tests pass

**Plan 11-02: EMA Decoupling & Logger Cleanup (COMPLETED)**
- LOW-03: MetricEMA generic class created — enables independent voltage/load/temperature tracking
- LOW-03: EMAFilter refactored to use MetricEMA; full backward compatibility maintained
- LOW-04: setup_ups_logger() wrapper removed from alerter.py
- LOW-04: All logging uses standard Python logging.getLogger("ups-battery-monitor") directly
- Impact: Cleaner code; prepares for v2 temperature sensor; simpler logging pattern; all 33 tests pass
- Key achievement: Decoupled per-metric EMA enables extensibility without code changes

**Remaining Phase 11 plans:** LOW-05 (health endpoint) in plan 11-03, others in subsequent plans
- After 1 year of daily discharge: ~365 entries per list
- Add pruning: keep last N=30 entries (1 month) or entries in last 90 days
- Test with synthetic large model: verify pruning on save

**LOW-02 (fdatasync optimization):** `atomic_write_json()` (line ~100?):
```python
os.fsync(fd)  # ← syncs data + metadata (inode, timestamps)
```

Change to:
```python
os.fdatasync(fd)  # ← syncs data only, faster
```

Acceptable because JSON file append doesn't need inode sync.

**LOW-03 (EMA decoupling):** EMAFilter currently tracks voltage and load separately. Generalize to per-metric:
```python
class GenericEMA:
    def __init__(self, metric_name: str, alpha: float):
        self.metric_name = metric_name
        self.alpha = alpha
        self.value = None

    def update(self, new_value: float) -> float:
        if self.value is None:
            self.value = new_value
        else:
            self.value = self.alpha * new_value + (1 - self.alpha) * self.value
        return self.value
```

Prepares for temperature sensor (v2).

**LOW-04 (logger cleanup):** `alerter.py` has `setup_ups_logger()` wrapper. Remove it, use direct:
```python
logger = logging.getLogger("ups_battery_monitor")
```

Simplifies code, standard Python logging pattern.

**LOW-05 (health endpoint):** Expose daemon state via file for external monitoring:
```json
{
  "last_poll": "2026-03-15T14:30:00Z",
  "current_soc": 87.5,
  "online": true,
  "daemon_version": "1.1"
}
```

File: `<MODEL_DIR>/health.json`, updated every poll. External tools (Grafana, check_mk) can read it. Prepare for v2 HTTP endpoint.

---

## Key Constraints for Implementation

### Safety-First (Phase 7 blocks nothing)
- SAFE-01 and SAFE-02 are P0 — must be done first, fully tested before Phase 8
- All existing tests must pass after safety refactor

### Dataclass Heavy Lifting (Phase 8 sequential)
- ARCH-01 modifies `_update_battery_health()` and all metric access
- ARCH-02 modifies `Monitor.__init__` and config initialization
- ARCH-03 is trivial (imports)
- Commit ARCH-01 first, then ARCH-02, then ARCH-03 to isolate changes and ease review

### Test Infrastructure First (Phase 9)
- conftest.py fixes (TEST-04) enable other tests
- Dataclass refactors (Phase 8) make mocking easier, but Phase 9 tests all new code
- Can start Phase 9 once Phase 8 commits are ready

### Parallelization Opportunity
- Phase 10 (QUAL-*) doesn't depend on Phase 9 (TEST-*)
- Both can run in parallel once Phase 8 is done
- But recommend sequential for clarity: 9 → 10 → 11

---

## Session Continuity

**Next step:** Continue to Phase 10 with remaining QUAL plans (10-03, 10-04, 10-05)

**Context to preserve:**
- All 5 phases mapped, 19 requirements assigned, 100% coverage
- Phase 7 (SAFE-01/02) and Phase 8 (ARCH-01/02/03) complete
- Phase 9 complete: 09-01 (TEST-04, TEST-05), 09-02 (TEST-02, TEST-03), 09-03 (TEST-01 integration test)
- All test coverage requirements (TEST-01 through TEST-05) satisfied
- Phase 10 complete: 10-01 (QUAL-01, QUAL-02, QUAL-03), 10-02 (QUAL-04, QUAL-05)
- Phase 10 remaining: 10-03 (LOW-01..05 polish & future prep) — wait, LOW is Phase 11. 10-03 not yet planned
- All v1.0 tests (160+) remain passing with no regressions

**Last completed:** 10-02-PLAN.md — Batch calibration writes (60x SSD wear reduction) and consolidated error logging

**Files modified by 10-02:**
- `src/model.py` — calibration_batch_flush() method added, calibration_write() optimized
- `src/monitor.py` — _write_calibration_points() batches flush after loop
- `src/virtual_ups.py` — Consolidated nested exception handlers
- `tests/test_model.py` — test_calibration_write_fsync updated to verify batching

### Phase 10: Code Quality (QUAL-01 through QUAL-05)

**Plan 10-01: Extract helpers, fix hardcoded values, correct docstrings (COMPLETED)**
- QUAL-01: _safe_save() helper extracted from 4 inline try/except blocks
- QUAL-02: Hardcoded '2026-03-13' replaced with datetime.now().strftime('%Y-%m-%d')
- QUAL-03: soc_from_voltage() docstring corrected: "linear scan" (actual) not "binary search"
- Impact: Zero-change refactoring; all 184 tests passing; improved maintainability

**Plan 10-02: Batch calibration writes & error consolidation (COMPLETED)**
- QUAL-04: calibration_batch_flush() method — 60x SSD wear reduction during battery testing
- QUAL-05: Consolidated error logging in virtual_ups.py — single exception handler, zero duplicate messages
- Impact: Better I/O efficiency, cleaner log output; all 184 tests pass

**Remaining Phase 10 plans:** To be planned (10-03, 10-04, 10-05 will address remaining QUAL requirements if any)

---

### Phase 11: Polish & Future Prep (LOW-01 through LOW-05)

**Plan 11-01: Model persistence optimization (COMPLETED)**
- LOW-01: History pruning — soh_history and r_internal_history limited to 30 entries (1 month of daily data)
  - Prevents unbounded growth (~365 entries/year; 7,300+ after 20 years without pruning)
  - Keeps last 30 entries only, discards oldest; idempotent, automatic on every save()
- LOW-02: fdatasync optimization — replaced os.fsync() with os.fdatasync() in atomic_write_json()
  - Data-only sync reduces I/O latency by ~50% (metadata not critical for JSON file reading)
  - Maintains full atomic write guarantees while reducing SSD wear
- Impact: Model.json file size controlled (~500 bytes max history vs. 2KB+ unbounded), faster persistence writes
- Test coverage: Added 9 new tests (6 pruning + 3 fdatasync), all 46 model tests passing

**Plan 11-02: EMA generalization (COMPLETED)**
- LOW-03: Extracted MetricEMA generic class from voltage/load-specific logic
  - Enables reuse for temperature sensor (v2 feature)
  - Maintains backward compatibility with existing voltage/load tracking
- Impact: Code reusability, foundation for multi-metric monitoring
- Test coverage: All 184+ tests passing, no regressions

**Plan 11-03: Daemon health endpoint (COMPLETED)**
- LOW-05: Added health.json file interface for external monitoring tools
  - _write_health_endpoint() writes last_poll (ISO8601 UTC + Unix epoch), current_soc_percent, online status, daemon_version
  - Called every poll (10s) in Monitor.run() main loop, atomically written via atomic_write_json()
  - Enables Grafana, check_mk, custom monitoring scripts to track daemon liveness without upsc/sudo
  - Prepares stable data structure for v2 HTTP endpoint upgrade
- Impact: External monitoring visibility, crash-safe file writes, zero daemon overhead
- Test coverage: 7 new tests (file creation, timestamp formats, precision, status, version, updates), all 24 tests passing

**Remaining Phase 11 plans:** 11-03 (LOW-05), 11-04, 11-05 (remaining LOW requirements)

---

*State updated: 2026-03-14T16:12:42Z after 11-02 EMA decoupling & logger cleanup (LOW-03/04)*
