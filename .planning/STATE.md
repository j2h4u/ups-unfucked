---
gsd_state_version: 1.0
milestone: v1.1
milestone_name: Expert Panel Review Fixes
status: unknown
last_updated: "2026-03-14T14:22:23.355Z"
progress:
  total_phases: 5
  completed_phases: 1
  total_plans: 2
  completed_plans: 3
---

# Project State — UPS Battery Monitor v1.1

**Last Updated:** 2026-03-15 after roadmap completion
**Milestone:** v1.1 Expert Panel Review Fixes
**Current Focus:** Phase 7 — Safety-Critical Metrics (per-poll writes during blackout)

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
| Current Phase | 7: Safety-Critical Metrics |
| Status | Plan 07-01 complete, 1 of 2 plans in phase complete |
| Progress | 1/5 phases started, 2/19 requirements implemented (SAFE-01, SAFE-02) |

---

## Progress Metrics

```
Requirements Coverage: 19/19 (100%)
├─ Safety (P0): SAFE-01, SAFE-02 → Phase 7
├─ Architecture (P1): ARCH-01, ARCH-02, ARCH-03 → Phase 8
├─ Testing (P1): TEST-01, TEST-02, TEST-03, TEST-04, TEST-05 → Phase 9
├─ Code Quality (P2): QUAL-01, QUAL-02, QUAL-03, QUAL-04, QUAL-05 → Phase 10
└─ Low Priority (P3): LOW-01, LOW-02, LOW-03, LOW-04, LOW-05 → Phase 11

Phases defined: 5
Plans to create: ~15-18 (3-4 per phase)
Expected tests: +5 new critical path tests
Expected LOC growth: ~100 (dataclasses, helpers, tests)
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

**LOW-01 (history pruning):** `soh_history` and `r_internal_history` in model.json grow unbounded.
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

**Next step:** Start Phase 7 planning with `/gsd:plan-phase 7`

**Context to preserve:**
- All 5 phases mapped, 19 requirements assigned, 100% coverage
- SAFE-01/02 are P0 safety-critical, must be first phase
- ARCH-01/02/03 dataclass refactors touch monitor.py heavily — sequential commits required
- Phase 9+ tests depend on Phase 8 dataclasses being in place
- All v1.0 tests (160) must pass as regression check after refactors

**Files to review before Phase 7 planning:**
- `docs/EXPERT-PANEL-REVIEW-2026-03-15.md` (detailed findings)
- `src/monitor.py` (main daemon, focus on lines 20-30 globals, 68 imports, 90-93 event transition)
- `tests/conftest.py` (mock_socket_ok line ~40-50)
- `.planning/REQUIREMENTS.md` (phase mappings for traceability)

**Tools/Skills to refresh:**
- Python @dataclass and frozen=True semantics
- Type hints and mypy
- Mock/patch patterns for file I/O and socket operations
- Systemd journal timestamp formats

---

*State updated: 2026-03-15 after roadmap creation for v1.1*
