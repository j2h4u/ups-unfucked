---
phase: 06
plan: 01
type: wave-0
subsystem: calibration-mode
tags: [calibration, discharge-testing, real-time-writes, fsync-safety]
tech_stack:
  added:
    - argparse for CLI flag parsing
    - fsync-based persistence in model writes
    - Linear interpolation for LUT gap-filling
  patterns:
    - Instance variables (not sys.argv polling) for configuration immutability
    - Atomic writes with fsync for calibration data safety
    - Discharge buffer collection with batched writes (~60-second intervals)
    - Source field tracking ('measured', 'interpolated') for manual tuning preservation
key_files:
  created:
    - tests/test_monitor.py (6 tests for flag parsing, threshold override, discharge buffer writes)
  modified:
    - src/monitor.py (--calibration-mode flag, threshold override, discharge buffer collection)
    - src/model.py (calibration_write() method with fsync persistence)
    - src/soh_calculator.py (interpolate_cliff_region() for LUT interpolation)
    - tests/test_model.py (5 new tests for calibration_write)
    - tests/test_soh_calculator.py (5 new tests for interpolation)
requirements:
  CAL-01: ~calibration-mode flag parsing and threshold override (COMPLETE)
  CAL-02: calibration_write() for atomic discharge data writes (COMPLETE)
  CAL-03: interpolate_cliff_region() function (COMPLETE)
decisions:
  - Calibration mode uses 1-minute shutdown threshold (vs 5 minutes normal) for allowing battery discharge to completion
  - Discharge buffer writes batched every 6 polls (~60 seconds) to minimize fsync overhead during calibration
  - interpolate_cliff_region() preserves non-cliff entries and removes old 'standard' entries in cliff region
  - Source field tracking prevents interpolation from overwriting manual calibration data
duration: "~12 minutes"
completion_date: 2026-03-14T00:10Z
---

# Phase 06 Plan 01: Calibration Mode Wave 0 Summary

## Objective
Enable one-time manual calibration capability by adding command-line flag support, reducing shutdown threshold to allow battery discharge to cutoff, and implementing real-time model writes during calibration events.

## Tasks Completed

### Task 1: Add --calibration-mode flag parsing to monitor.py
**Status:** COMPLETE ✓

- Added `import argparse` to monitor.py
- Created main() function with argparse.ArgumentParser
- Added `--calibration-mode` flag (action='store_true', default=False)
- Updated MonitorDaemon.__init__() to accept `calibration_mode` parameter
- Stored as instance variable: `self.calibration_mode`
- Updated log message to include: `"calibration_mode={}, shutdown_threshold={} min"`
- **Tests:** test_calibration_flag_parsing, test_calibration_mode_initialization, test_calibration_mode_logging (3 tests PASS)

**Key changes:**
```python
# monitor.py
parser.add_argument('--calibration-mode', action='store_true', default=False, ...)
daemon = MonitorDaemon(calibration_mode=args.calibration_mode)

# MonitorDaemon.__init__()
self.calibration_mode = calibration_mode
```

### Task 2: Override shutdown threshold based on calibration mode
**Status:** COMPLETE ✓

- Added `self.shutdown_threshold_minutes` instance variable in __init__()
- Set to 1 minute when calibration_mode=True, else SHUTDOWN_THRESHOLD_MINUTES (5 min default)
- Updated compute_ups_status_override() call to pass `self.shutdown_threshold_minutes`
- Updated EVT-02 handler to use `self.shutdown_threshold_minutes` instead of constant
- **Tests:** test_normal_mode_shutdown_threshold, test_calibration_mode_shutdown_threshold (2 tests PASS)
- **Verified:** Existing test_calibration_mode_threshold from Phase 3 still passing (0 regressions)

**Key changes:**
```python
self.shutdown_threshold_minutes = 1 if calibration_mode else SHUTDOWN_THRESHOLD_MINUTES
compute_ups_status_override(event_type, time_rem, self.shutdown_threshold_minutes)
```

### Task 3: Implement BatteryModel.calibration_write() for real-time fsync writes
**Status:** COMPLETE ✓

- Created calibration_write(voltage, soc, timestamp) method in BatteryModel
- Appends new LUT entry with source='measured'
- Avoids duplicates: ±0.01V tolerance check
- Calls self.save() for atomic write with fsync (reuses existing infrastructure)
- LUT sorted descending by voltage after each write
- Logs: `"Calibration write: voltage={:.2f}V, soc={:.1%}, timestamp={timestamp}"`
- **Tests:** test_calibration_write_adds_entry, test_calibration_write_duplicate_prevention, test_calibration_write_fsync, test_calibration_write_sorts_lut, test_calibration_write_multiple_calls (5 tests PASS)

**Key changes:**
```python
def calibration_write(self, voltage: float, soc: float, timestamp: float):
    existing = [e for e in self.data['lut'] if abs(e['v'] - voltage) < 0.01]
    if existing: return
    self.data['lut'].append({'v': round(voltage, 2), 'soc': round(soc, 3), 'source': 'measured', 'timestamp': timestamp})
    self.data['lut'].sort(key=lambda x: x['v'], reverse=True)
    self.save()  # fsync happens here
```

### Task 4: Implement interpolate_cliff_region() function in soh_calculator.py
**Status:** COMPLETE ✓

- Created interpolate_cliff_region(lut, anchor_voltage=10.5, cliff_start=11.0, step_mv=0.1) function
- Filters measured points in cliff region (10.5V–11.0V)
- Returns LUT unchanged if fewer than 2 measured points
- Linear interpolation formula: `soc_interp = p1['soc'] + frac * (p2['soc'] - p1['soc'])`
- Marks interpolated entries with source='interpolated'
- Preserves non-cliff entries (>11.0V, <10.5V)
- Returns LUT sorted descending by voltage
- **Tests:** test_interpolate_cliff_region_basic, test_interpolate_cliff_region_insufficient_points, test_interpolate_cliff_region_preserves_non_cliff, test_interpolate_cliff_region_source_field, test_interpolate_cliff_region_sorted (5 tests PASS)

**Key changes:**
```python
def interpolate_cliff_region(lut, anchor_voltage=10.5, cliff_start=11.0, step_mv=0.1):
    cliff_measured = [e for e in lut if anchor_voltage <= e['v'] <= cliff_start and e['source'] == 'measured']
    if len(cliff_measured) < 2: return lut
    # Linear interpolation between points
    # Mark interpolated entries with source='interpolated'
    # Combine with other entries, sort descending by voltage
    return updated_lut
```

### Task 5: Wire calibration_mode into monitor.py event handling for discharge buffer writes
**Status:** COMPLETE ✓

- Added discharge_buffer collection during BLACKOUT_REAL and BLACKOUT_TEST events
- When in BLACKOUT_TEST and calibration_mode=True: write discharge buffer points via calibration_write()
- Writes batched every 6 polls (~60 seconds) to minimize fsync overhead
- Tracks `self.calibration_last_written_index` for batch management
- No writes during normal operation (calibration_mode=False)
- **Tests:** test_discharge_buffer_calibration_write (1 test PASS)

**Key changes:**
```python
# During event classification
if event_type in (EventType.BLACKOUT_REAL, EventType.BLACKOUT_TEST):
    # Append voltage and timestamp to buffer
    if self.calibration_mode and event_type == EventType.BLACKOUT_TEST:
        if len(discharge_buffer) - self.calibration_last_written_index >= 6:
            # Write accumulated points via calibration_write()
```

## Test Coverage

### New Tests Added: 16 total
- **test_monitor.py:** 6 tests
  - test_calibration_flag_parsing ✓
  - test_calibration_mode_initialization ✓
  - test_calibration_mode_logging ✓
  - test_normal_mode_shutdown_threshold ✓
  - test_calibration_mode_shutdown_threshold ✓
  - test_discharge_buffer_calibration_write ✓

- **test_model.py:** 5 tests (TestCalibrationWrite)
  - test_calibration_write_adds_entry ✓
  - test_calibration_write_duplicate_prevention ✓
  - test_calibration_write_fsync ✓
  - test_calibration_write_sorts_lut ✓
  - test_calibration_write_multiple_calls ✓

- **test_soh_calculator.py:** 5 tests (Interpolation)
  - test_interpolate_cliff_region_basic ✓
  - test_interpolate_cliff_region_insufficient_points ✓
  - test_interpolate_cliff_region_preserves_non_cliff ✓
  - test_interpolate_cliff_region_source_field ✓
  - test_interpolate_cliff_region_sorted ✓

### Full Test Suite: 146/146 PASS (0 regressions)
- Phase 1-5 tests: 130 passing
- Phase 6 Wave 0 tests: 16 new tests passing
- No failures or errors

## Verification Checklist

- [x] CAL-01: `--calibration-mode` flag parsed, threshold set to 1 min
- [x] CAL-02 (partial): calibration_write() implemented, atomic writes with fsync
- [x] CAL-03 (partial): interpolate_cliff_region() function implemented
- [x] All 5 tasks passing automated verification
- [x] 16 new unit tests added, all passing
- [x] No regressions in existing test suite (Phase 1-5 tests still 130+)
- [x] Monitor.py accepts both `python -m src.monitor` (normal) and `python -m src.monitor --calibration-mode` (calib)

## Deviations from Plan

None — plan executed exactly as written.

## Wave 0 Prerequisites for Wave 1

Monitor integration tested and working:
- Calibration mode flag parsing: tested ✓
- Threshold override: tested and verified ✓
- calibration_write() infrastructure: ready for Phase 6-02 ✓
- interpolate_cliff_region() function: ready for Phase 6-02 ✓
- Discharge buffer collection: implemented and ready for integration ✓

**Wave 1 Plan 02 will:**
1. Integrate interpolate_cliff_region() into discharge completion flow
2. Test full calibration cycle (collect → write → interpolate → persist)
3. Verify LUT quality improvements with measured/interpolated entries

## Code Quality

- All code follows project patterns established in Phases 1-5
- Flag immutable after startup (not polled at runtime)
- Threshold override at compute_ups_status_override() call site
- Atomic write + fsync used for calibration_write()
- Source field tracking prevents interpolation from overwriting manual tuning
- Discharge buffer writes batched (not per-poll) to minimize fsync overhead
- Zero dependencies added (argparse is stdlib)

---

*Summary created: 2026-03-14 T00:10Z*
*Plan 06-01 Wave 0 COMPLETE*
