---
phase: 11
plan: 02
subsystem: EMA filtering & logging infrastructure
tags: [refactoring, metrics, logging, backward-compatible]
dependency_graph:
  requires: []
  provides: [MetricEMA generic class, simplified logging pattern]
  affects: [future temperature sensor support (v2)]
tech_stack:
  added: [MetricEMA base class]
  patterns: [dataclass-like encapsulation, generic metric tracking, standard Python logging]
key_files:
  created: []
  modified: [src/ema_filter.py, src/alerter.py, src/monitor.py, tests/test_ema.py, tests/test_alerter.py, tests/test_logging.py, tests/test_monitor.py]
decisions: []
metrics:
  duration: "~2m total execution"
  completed_date: "2026-03-14"
  requirements_met: [LOW-03, LOW-04]
  tests_passed: 33 (19 EMA + 10 alerter + 4 logging)
---

# Phase 11 Plan 02: EMA Decoupling & Logger Cleanup

## Summary

Refactored EMA metric tracking into a generic `MetricEMA` base class and removed the thin `setup_ups_logger()` wrapper, simplifying the logging pattern to standard Python conventions. All changes are backward-compatible with existing code.

**Key achievement:** Decoupled per-metric EMA tracking enables independent monitoring of voltage, load, and future temperature sensors (v2).

---

## Tasks Completed

### Task 1: Extract MetricEMA Generic Class & Refactor EMAFilter

**Objective:** Create a generic `MetricEMA` class for single metric tracking, then refactor `EMAFilter` to use it.

**Implementation:**
- Created `MetricEMA` class (lines 5-62 in src/ema_filter.py):
  - Accepts `metric_name` parameter for identifying the metric
  - Tracks single value with adaptive alpha smoothing
  - Exposes `value` and `stabilized` properties
  - Identical adaptive alpha logic as original EMAFilter

- Refactored `EMAFilter` to use MetricEMA instances:
  - `voltage_ema = MetricEMA("voltage", ...)`
  - `load_ema = MetricEMA("load", ...)`
  - Maintains backward-compatible properties: `ema_voltage`, `ema_load`, `voltage`, `load`
  - Delegates `add_sample(v, l)` calls to both MetricEMA instances

**Tests Added (4 new):**
1. `test_metric_ema_single_metric()` — MetricEMA initialization and update
2. `test_metric_ema_multiple_independent()` — voltage, load, temperature track separately
3. `test_ema_filter_backward_compatible()` — .ema_voltage, .ema_load properties work
4. `test_metric_ema_stabilized_flag()` — Stabilized flag behavior

**Verification:**
- All 19 EMA tests pass (15 existing + 4 new)
- No regressions in existing behavior
- Backward compatibility confirmed: `buf.ema_voltage == buf.voltage_ema.value`

**Commits:**
- `537fc46` refactor(11-02): extract MetricEMA generic class; prepare for v2 temperature sensor

---

### Task 2: Remove setup_ups_logger Wrapper; Use Standard logging.getLogger()

**Objective:** Remove the thin wrapper function and use Python's standard logging pattern directly.

**Implementation:**
- Removed `setup_ups_logger()` function from src/alerter.py (was lines 8-15)
- Added module-level logger instance: `logger = logging.getLogger("ups-battery-monitor")`
- Updated src/monitor.py (line 184): Changed from `alerter.setup_ups_logger("ups-battery-monitor")` to `logging.getLogger("ups-battery-monitor")`
- Updated all test imports and usages to use `logging.getLogger()` directly

**Files Modified:**
1. src/alerter.py — Removed setup_ups_logger, added module logger
2. src/monitor.py — Use logging.getLogger() directly
3. tests/test_alerter.py — Updated all 8 existing tests + added 2 new tests
4. tests/test_logging.py — Removed test_setup_ups_logger_returns_logger test
5. tests/test_monitor.py — Removed patch('src.monitor.alerter.setup_ups_logger') from 2 test fixtures

**Tests Added (2 new):**
1. `test_setup_ups_logger_removed()` — Verifies ImportError when trying to import removed function
2. `test_alerter_uses_standard_logging()` — Confirms logging.getLogger pattern in alerter module

**Verification:**
- `from src.alerter import setup_ups_logger` → ImportError (expected)
- No remaining references to setup_ups_logger in src/ or tests/ (except test name)
- All 10 alerter tests pass
- All 4 logging tests pass
- No regressions

**Commits:**
- `9f2fe93` refactor(11-02): remove setup_ups_logger wrapper; use standard logging.getLogger()

---

## Verification Results

### Automated Tests
```
tests/test_ema.py:        19 passed ✓
tests/test_alerter.py:    10 passed ✓
tests/test_logging.py:     4 passed ✓
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total:                     33 passed ✓
```

### Code Checks

**Import Check:**
```bash
$ python3 -c "from src.alerter import setup_ups_logger"
ImportError: cannot import name 'setup_ups_logger' ✓
```

**Grep Check (setup_ups_logger references):**
- Only in test_alerter.py as test function names and comments ✓
- No imports or usages in actual code ✓

**Grep Check (MetricEMA usage):**
```
src/ema_filter.py:  class MetricEMA        (definition)
src/ema_filter.py:  voltage_ema = MetricEMA("voltage", ...)
src/ema_filter.py:  load_ema = MetricEMA("load", ...)
```
Total: 2 uses in EMAFilter.__init__ ✓

**Backward Compatibility Check:**
- `EMAFilter.ema_voltage` property → accesses `voltage_ema.value` ✓
- `EMAFilter.ema_load` property → accesses `load_ema.value` ✓
- `EMAFilter.add_sample(v, l)` → calls both MetricEMA instances ✓
- `EMAFilter.stabilized` → checks both EMAs ✓

---

## Deviations from Plan

None — plan executed exactly as written.

---

## Requirements Met

- **LOW-03:** MetricEMA generic class created, enables independent voltage/load/temperature tracking
- **LOW-03:** EMAFilter refactored to use MetricEMA; backward-compatible
- **LOW-04:** setup_ups_logger() removed from alerter.py
- **LOW-04:** All logging in alerter.py and monitor.py uses logging.getLogger("ups-battery-monitor") directly

---

## Impact Summary

### Code Metrics
- Lines added: 158 (MetricEMA implementation + tests)
- Lines removed: 15 (setup_ups_logger function)
- Net impact: +143 lines
- Test coverage: 33 tests, 100% passing

### Maintainability
- **Simplified logging:** No custom wrapper — standard Python pattern
- **Extensibility:** MetricEMA enables v2 temperature sensor without code changes
- **Clarity:** Each metric (voltage, load, temp) has independent EMA instance

### Architecture
- Prepares for multi-sensor v2 architecture
- Reduces technical debt (removed wrapper)
- Improves testability (generic MetricEMA easier to unit test)

---

## Future Considerations (v2)

With MetricEMA in place, adding temperature sensor support is straightforward:
```python
temp_ema = MetricEMA("temperature", window_sec=120, poll_interval_sec=10)
# Update in polling loop:
temp_ema.update(temperature_value)
```

No changes needed to EMAFilter or existing voltage/load tracking.

---

*Completed: 2026-03-14 16:12 UTC*
