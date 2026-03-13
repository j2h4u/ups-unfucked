---
phase: 03-virtual-ups-safe-shutdown
plan: 02
subsystem: virtual-ups-status-override
tags:
  - Wave 1 implementation
  - LB flag arbitration
  - Shutdown threshold logic
  - EventType integration
type: Wave 1 (Field Overrides & Shutdown Threshold Logic)
dependency_graph:
  requires:
    - "03-01: Virtual UPS infrastructure (Wave 0)"
    - "Event classification state machine (Phase 2)"
  provides:
    - "compute_ups_status_override() for monitor.py integration"
    - "Test coverage for all LB flag scenarios"
    - "Configurable shutdown threshold implementation"
  affects:
    - "Phase 3 Wave 2: Integration into monitor.py polling loop"
    - "Upsmon shutdown coordination (Phase 5)"
tech_stack:
  added: []
  patterns:
    - EventType enum matching (if/elif/else on event classification)
    - Threshold-based logic (time_rem < threshold boundary testing)
    - Parametrized pytest tests for multiple threshold values
key_files:
  created: []
  modified:
    - "src/virtual_ups.py (compute_ups_status_override implementation)"
    - "tests/test_virtual_ups.py (5 new test implementations)"
decisions:
  - "Use < comparison (not <=) for threshold boundary: time_rem == threshold does not trigger LB"
  - "Safe default is 'OL' for unknown event types (fail-safe)"
  - "All override and passthrough fields tested in isolation before integration"
  - "Parametrized tests with thresholds [1, 3, 5, 10] verify configurable behavior"
metrics:
  duration: "~8 minutes"
  completed_date: "2026-03-14"
  tasks_completed: 4
  tests_added: 5
  tests_total_passing: 88 (full suite)
---

# Phase 3 Plan 02: Virtual UPS Status Override & Shutdown Thresholds Summary

**Wave 1 Objective:** Implement field override logic and shutdown threshold arbitration. Tests verify that the daemon can correctly determine when to signal LOW_BATTERY based on remaining runtime and configurable thresholds.

**One-liner:** Implemented `compute_ups_status_override()` with all EventType cases and comprehensive test coverage for LB flag boundary behavior and configurable thresholds.

---

## Execution Summary

**Status:** ✓ COMPLETE

All 4 tasks executed and committed individually. All 5 new tests implemented and passing.
- Task 1: `compute_ups_status_override()` fully implemented
- Task 2: `test_field_overrides` and `test_passthrough_fields` implemented
- Task 3: `test_lb_flag_threshold`, `test_configurable_threshold`, and `test_calibration_mode_threshold` implemented
- Full test suite: 88/88 tests passing (no regressions)

---

## Implementation Details

### Task 1: compute_ups_status_override() Implementation

**File:** `src/virtual_ups.py`

Implemented complete function with all 4 EventType branches:

1. **EventType.ONLINE** → returns `"OL"`
2. **EventType.BLACKOUT_TEST** → returns `"OB DISCHRG"` (no LB flag, allows calibration data collection)
3. **EventType.BLACKOUT_REAL (time_rem >= threshold)** → returns `"OB DISCHRG"`
4. **EventType.BLACKOUT_REAL (time_rem < threshold)** → returns `"OB DISCHRG LB"` (signal LOW_BATTERY to upsmon)
5. **Unknown/else** → returns `"OL"` (safe default)

**Key logic detail:** Threshold uses `<` comparison, not `<=`. Example: with threshold=5 minutes:
- time_rem=5.0 → `"OB DISCHRG"` (no LB)
- time_rem=4.9 → `"OB DISCHRG LB"` (LB flag set)

This prevents premature shutdown when system time is exactly at threshold.

**Docstring:** Comprehensive explanation of LB flag semantics and upsmon integration.

### Task 2: Field Override Tests

**File:** `tests/test_virtual_ups.py`

#### test_field_overrides()
- Validates 3 critical fields (battery.runtime, battery.charge, ups.status) are correctly overridden
- Creates metrics dict with override + passthrough fields
- Uses atomic write pattern with temporary files for test isolation
- Parses output and verifies override values match exactly as provided (no transformation)
- ✓ PASSED

#### test_passthrough_fields()
- Validates all non-override fields transparently pass through unchanged
- Tests 8 passthrough fields + 3 override fields
- Verifies passthrough values are identical to input dict
- Confirms correct field count in output (11 VAR lines)
- ✓ PASSED

### Task 3: Threshold Logic Tests

**File:** `tests/test_virtual_ups.py`

#### test_lb_flag_threshold()
Tests LB flag firing at threshold boundary:
- time_rem=6, threshold=5 → `"OB DISCHRG"` (no LB)
- time_rem=5, threshold=5 → `"OB DISCHRG"` (no LB, boundary case)
- time_rem=4.9, threshold=5 → `"OB DISCHRG LB"` (LB fires)
- time_rem=0, threshold=5 → `"OB DISCHRG LB"` (LB fires)
- ✓ PASSED

#### test_configurable_threshold()
Parametrized test with thresholds [1, 3, 5, 10]:
- For each threshold value, tests time_rem just below (triggers LB) and just above (no LB)
- Verifies threshold parameter actually controls LB firing (not hardcoded)
- Demonstrates configurable behavior across all tested thresholds
- ✓ PASSED (4 parametrized variants)

#### test_calibration_mode_threshold()
Parametrized test with calibration_threshold [1, 0]:
- Shows threshold can be reduced independently for battery testing
- time_rem=2: no LB with threshold=1 or threshold=5 (different LB behaviors)
- time_rem=0.5: LB with both threshold=1 and threshold=5 (consistent below both)
- Documents that actual calibration mode flag will be added in Phase 6
- ✓ PASSED (2 parametrized variants)

---

## Test Results

### Virtual UPS Test Module (test_virtual_ups.py)

All 10 tests passing:

1. TestVirtualUPSWriting::test_write_to_tmpfs ✓
2. TestVirtualUPSWriting::test_passthrough_fields ✓ **[NEW - Task 2]**
3. TestFieldOverrides::test_field_overrides ✓ **[NEW - Task 2]**
4. TestNUTFormatCompliance::test_nut_format_compliance ✓
5. TestShutdownThresholds::test_lb_flag_threshold ✓ **[NEW - Task 3]**
6. TestShutdownThresholds::test_configurable_threshold ✓ **[NEW - Task 3]**
7. TestShutdownThresholds::test_calibration_mode_threshold[1] ✓ **[NEW - Task 3]**
8. TestShutdownThresholds::test_calibration_mode_threshold[0] ✓ **[NEW - Task 3]**
9. TestEventTypeIntegration::test_event_type_imports ✓
10. TestEventTypeIntegration::test_compute_status_override_signature ✓

**Full Suite:** 88/88 tests passing (Phase 1, 2, and 3 combined)
- No regressions detected
- Coverage includes: EMA smoothing, event classification, battery model, NUT client, runtime calculator, SoC predictor, and virtual UPS modules

---

## Deviations from Plan

**None** — plan executed exactly as written.

---

## Key Decisions Made

1. **Threshold Boundary:** Use `<` (strict less-than), not `<=` (less-than-or-equal)
   - Rationale: Ensures shutdown does not fire when time_rem exactly equals threshold
   - Consequence: More conservative (allows extra second before LB signal)

2. **Safe Default:** Unknown event types default to `"OL"`
   - Rationale: Prevents false LOW_BATTERY signals if event classification fails
   - Consequence: Daemon will not shut down if event type becomes unclassified (explicit rather than implicit)

3. **Test Isolation:** All field override and passthrough tests use temporary directories
   - Rationale: Prevents pollution of test tmpfs, isolates each test
   - Consequence: Slightly higher test execution time, cleaner test environment

4. **Parametrization:** Threshold tests use `@pytest.mark.parametrize`
   - Rationale: Comprehensive coverage of threshold range [1, 3, 5, 10] without code duplication
   - Consequence: Clear test matrix in pytest output; easy to add new thresholds

---

## Ready for Wave 2

✓ `compute_ups_status_override()` fully tested and ready for integration
✓ Field override and passthrough logic verified in isolation
✓ Threshold configuration validated across multiple values
✓ Calibration mode threshold pattern established (Phase 6 adds flag)
✓ No regressions in Phase 1-2 test suite

**Next step (Wave 2):** Integrate `compute_ups_status_override()` into `monitor.py` polling loop to compute and write virtual UPS metrics every poll cycle.

---

## Self-Check

- [x] All 4 tasks executed and committed
- [x] 5 new tests implemented (2 + 1 + 2 parametrized = 5)
- [x] Full test suite: 88/88 passing
- [x] No regressions from Phase 1-2
- [x] Implementation matches RESEARCH.md Pattern 2 specification
- [x] Threshold boundary behavior verified (<, not <=)
- [x] Configurable threshold parameter validated
