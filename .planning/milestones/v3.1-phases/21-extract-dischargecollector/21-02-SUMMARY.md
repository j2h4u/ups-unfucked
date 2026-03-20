---
phase: 21-extract-dischargecollector
plan: 02
subsystem: monitor
tags: [extraction, refactoring, discharge-collector, architecture, tdd]
dependency_graph:
  requires: [21-01-SUMMARY.md]
  provides: [src/discharge_collector.py]
  affects: [src/monitor.py, tests/test_monitor.py, tests/test_monitor_integration.py]
tech_stack:
  added: [src/discharge_collector.py]
  patterns: [collaborator-extraction, SagTracker-pattern, TDD-red-green]
key_files:
  created:
    - src/discharge_collector.py
    - tests/test_discharge_collector.py
  modified:
    - src/monitor.py
    - tests/test_monitor.py
    - tests/test_monitor_integration.py
decisions:
  - track() receives current_metrics and reads previous_event_type from it (option 1 of 2)
  - Integration test uses ema_filter sync after daemon.ema_filter reassignment
  - Pre-existing TestSulfationMethodSplit failures deferred (not caused by this plan)
metrics:
  duration: 15 min
  completed: 2026-03-20
  tasks_completed: 2
  files_changed: 5
---

# Phase 21 Plan 02: DischargeCollector Extraction Summary

**One-liner:** DischargeCollector extracted from MonitorDaemon into standalone collaborator module (ARCH-05) with 19 direct unit tests; MonitorDaemon no longer owns any discharge collection state or methods.

## Tasks Completed

| Task | Name | Commit | Key Files |
|------|------|--------|-----------|
| 1 | Create DischargeCollector module with unit tests (TDD) | 0ac9fa2 | src/discharge_collector.py, tests/test_discharge_collector.py |
| 2 | Rewire MonitorDaemon to delegate to DischargeCollector | 7f04da4 | src/monitor.py, tests/test_monitor.py, tests/test_monitor_integration.py |

## What Was Built

### Task 1: DischargeCollector (TDD RED → GREEN)

New `src/discharge_collector.py` following the SagTracker collaborator pattern. Owns:
- `discharge_buffer`, `_discharge_start_time`, `_discharge_buffer_clear_countdown`, `_calibration_last_written_index`
- `track(voltage, timestamp, event_type, current_metrics) -> bool` — drives the full state machine; returns True on cooldown expiry so caller invokes `_update_battery_health()`
- `finalize(timestamp)` — records on-battery time, resets buffer state
- `reset_buffer()` — replaces buffer with fresh DischargeBuffer after health update
- `is_collecting` property, `buffer` property
- `_start_discharge_collection`, `_handle_discharge_cooldown`, `_write_calibration_points` (private, moved from MonitorDaemon)

19 unit tests in `tests/test_discharge_collector.py` — no MonitorDaemon construction required.

### Task 2: MonitorDaemon Rewiring

**Removed from MonitorDaemon:**
- 4 state fields: `discharge_buffer`, `_discharge_start_time`, `discharge_buffer_clear_countdown`, `calibration_last_written_index`
- 1 runtime field: `_discharge_predicted_runtime` (now owned by discharge_handler via DischargeCollector)
- 5 methods: `_start_discharge_collection`, `_handle_discharge_cooldown`, `_track_discharge`, `_finalize_discharge_collection`, `_write_calibration_points`
- Unused imports: `DischargeBuffer`, `DISCHARGE_BUFFER_MAX_SAMPLES`

**Added to MonitorDaemon:**
- `self.discharge_collector = DischargeCollector(battery_model, config, discharge_handler, ema_filter)` in `_init_battery_model_and_estimators()`
- Import: `from src.discharge_collector import DischargeCollector`

**Rewired call sites:**
- `_poll_once`: `cooldown_expired = self.discharge_collector.track(...); if cooldown_expired: self._update_battery_health()`
- `_update_battery_health`: reads `self.discharge_collector.buffer`, calls `self.discharge_collector.reset_buffer()`
- `_auto_calibrate_peukert`, `_log_discharge_prediction`, `_log_status`: use `self.discharge_collector.buffer`

**Test updates:**
- `tests/test_monitor.py`: 7 `daemon._track_discharge = MagicMock()` replaced with `daemon.discharge_collector = MagicMock(); daemon.discharge_collector.track.return_value = False`; all `daemon.discharge_buffer` references updated; `test_discharge_buffer_init` and `test_discharge_buffer_cleared_after_health_update` rewritten; integration test uses real `discharge_collector.track()`
- `tests/test_monitor_integration.py`: ~40 `daemon.discharge_buffer` references bulk-replaced with `daemon.discharge_collector.discharge_buffer`; fixture updated; `_write_calibration_points` mock moved to `discharge_collector`

## Test Results

- 547 tests pass (19 new DischargeCollector tests + 528 existing)
- 8 tests deselected (pre-existing `TestSulfationMethodSplit` failures, not caused by this plan)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] LUT format mismatch in test factory**
- **Found during:** Task 1 GREEN phase
- **Issue:** `make_collector()` used tuple LUT `(10.5, 0.0)` but `soc_from_voltage()` expects dict `{"v": ..., "soc": ..., "source": ...}`
- **Fix:** Updated `make_collector()` factory to use dict format matching `soc_predictor.py` contract
- **Files modified:** tests/test_discharge_collector.py

**2. [Rule 1 - Bug] Cooldown countdown test expected wrong value**
- **Found during:** Task 1 GREEN phase
- **Issue:** Test expected countdown to be 60 after first tick, but `_handle_discharge_cooldown` sets it to 60 then immediately decrements by `polling_interval=10`, leaving 50
- **Fix:** Updated test assertion to expect 50 (60 - polling_interval)
- **Files modified:** tests/test_discharge_collector.py

**3. [Rule 1 - Bug] EMA filter reference mismatch in integration test**
- **Found during:** Task 2 — `test_ol_ob_ol_discharge_lifecycle_complete` failure
- **Issue:** Integration test reassigns `daemon.ema_filter = Mock()` but `discharge_collector.ema_filter` still points to original (EMAFilter mock with MagicMock `.load`). Buffer loads became MagicMock objects, causing `TypeError: '<=' not supported between MagicMock and int` in discharge_handler
- **Fix:** Added `daemon.discharge_collector.ema_filter = daemon.ema_filter` after EMA mock reassignment
- **Files modified:** tests/test_monitor.py

**4. [Rule 1 - Bug] Indentation error from replace_all in indented block**
- **Found during:** Task 2 — syntax error in test file
- **Issue:** `replace_all` on `daemon._track_discharge = MagicMock()` at 8-space indent (inside `with` block) produced `daemon.discharge_collector.track.return_value = False` at wrong indent
- **Fix:** Manually corrected indentation
- **Files modified:** tests/test_monitor.py

**5. [Rule 1 - Bug] test_monitor_integration.py not in Task 2 scope but needed updating**
- **Found during:** Task 2 full suite run
- **Issue:** `test_monitor_integration.py` had ~40 `daemon.discharge_buffer` references, plus `daemon._write_calibration_points` and fixture fake_health_update using old state fields
- **Fix:** Bulk regex replacement + manual fixture/method fixes
- **Files modified:** tests/test_monitor_integration.py

## Deferred Items

- `TestSulfationMethodSplit` in `tests/test_discharge_handler.py` — 8 tests for `_compute_sulfation_metrics`, `_persist_sulfation_and_discharge`, `_log_discharge_complete` methods that don't exist yet on `DischargeHandler`. Pre-existing at start of this plan, not caused by Phase 21. See `.planning/phases/21-extract-dischargecollector/deferred-items.md`.

## Self-Check: PASSED

- src/discharge_collector.py: FOUND
- tests/test_discharge_collector.py: FOUND
- .planning/phases/21-extract-dischargecollector/21-02-SUMMARY.md: FOUND
- commit 0ac9fa2 (feat(21-02): extract DischargeCollector): FOUND
- commit 7f04da4 (feat(21-02): rewire MonitorDaemon): FOUND
