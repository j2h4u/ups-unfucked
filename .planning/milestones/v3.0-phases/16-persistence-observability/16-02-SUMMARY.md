---
phase: 16-persistence-observability
plan: 02
type: execute
subsystem: battery-model-persistence
tags: [persistence, sulfation, discharge-events, schema-extension, backward-compat]
completed: "2026-03-17T14:30:00Z"
duration_minutes: 45
task_count: 3
test_count: 14
dependency_graph:
  requires: [16-01]
  provides: [model.json-schema-extension, BatteryModel-persistence-methods]
  affects: [16-03-wave-2, 16-04-wave-3]
key_files:
  created: []
  modified:
    - src/model.py
    - tests/test_sulfation_persistence.py
    - tests/test_discharge_event_logging.py
tech_stack:
  added: []
  patterns: [setdefault-initialization, history-pruning, atomic-writes]
decisions:
  - Backward compatibility: All Phase 16 arrays (sulfation_history, discharge_events, roi_history, natural_blackout_events) initialize to empty arrays in load() for seamless v2.0 model.json loading
  - Pruning strategy: Keep last 30 entries per array (mirrors existing v2.0 pruning for soh_history, r_internal_history). Prevents unbounded growth while maintaining 1+ month trend data
  - Initialization timing: Phase 16 arrays initialized in load() (not __init__) to ensure proper backward compatibility behavior on every model load
---

# Phase 16 Plan 02: Model Persistence Foundation Summary

**Wave 1 of Persistence-Observability** — Extend BatteryModel with sulfation and discharge event persistence.

## Overview

Plan 02 establishes the foundational persistence layer for Phase 16 observability. Three new methods added to BatteryModel enable downstream discharge handler integration to persist sulfation scoring and cycle ROI metrics. Model.json schema extended with 4 new top-level arrays while maintaining full backward compatibility with v2.0.

**Primary outcome:** BatteryModel ready to receive sulfation_history and discharge_event appends from discharge handler (Wave 2, Plan 03).

## Completion Status

✅ **All 3 tasks complete | 14 tests passing | 0 deviations**

### Task Summary

| Task | Name | Tests | Status | Commit |
|------|------|-------|--------|--------|
| 1 | Add sulfation history methods | 5 | ✅ PASS | ee6e6df |
| 2 | Add discharge event methods | 7 | ✅ PASS | 1066353 |
| 3 | Backward compatibility & integration | 2 | ✅ PASS | 4fce3e8 |

## Test Coverage (14 Total)

### Sulfation Persistence Tests (7)
- ✅ `test_append_sulfation_history_single_entry` — Single entry append, verify model.data['sulfation_history'][0]
- ✅ `test_append_sulfation_history_multiple_entries` — 5 appends, verify len == 5
- ✅ `test_sulfation_history_saved_to_model_json` — Append, save(), reload, verify persistence
- ✅ `test_prune_sulfation_history_keeps_last_30` — 50 entries, prune to 30, verify length
- ✅ `test_append_discharge_event` — Discharge event append within sulfation test file (validates dual-array support)
- ✅ `test_discharge_event_schema_correctness` — Verify 6 required fields present
- ✅ `test_backward_compatibility_missing_keys` — Load v2.0 model.json (no Phase 16 keys), verify empty arrays initialize

### Discharge Event Tests (7)
- ✅ `test_append_discharge_event_to_model` — Basic append, verify presence
- ✅ `test_discharge_event_schema_required_fields` — Validate required field names
- ✅ `test_discharge_event_reason_values` — Accept 'natural' and 'test_initiated'
- ✅ `test_discharge_event_persisted_in_model_json` — Append, save(), reload, verify file I/O
- ✅ `test_discharge_event_timestamp_format` — Verify ISO8601 format (YYYY-MM-DDTHH:MM:SSZ)
- ✅ `test_prune_discharge_events_keeps_last_30` — 50 events, keep 30, verify length
- ✅ `test_discharge_events_queryable_by_reason` — Filter mixed reasons, verify counts (3 natural, 2 test_initiated)

**Full test run output:**
```
======================= 14 passed, 19 warnings in 0.05s ========================
```

## Implementation Details

### src/model.py Changes

**4 new methods added to BatteryModel class:**

1. **`append_sulfation_history(entry: dict)`** (line ~347)
   - Appends to `self.data['sulfation_history']` array
   - Creates array if not present (setdefault pattern)
   - Entry schema: timestamp, event_type, sulfation_score, days_since_deep, ir_trend_rate, recovery_delta, temperature_celsius, confidence_level

2. **`append_discharge_event(event: dict)`** (line ~367)
   - Appends to `self.data['discharge_events']` array
   - Creates array if not present
   - Event schema: timestamp, event_reason, duration_seconds, depth_of_discharge, measured_capacity_ah, cycle_roi

3. **`_prune_sulfation_history(keep_count: int = 30)`** (line ~382)
   - Keeps only last 30 entries (oldest discarded)
   - Called from save() before atomic write

4. **`_prune_discharge_events(keep_count: int = 30)`** (line ~389)
   - Keeps only last 30 entries
   - Called from save() before atomic write

**Updated existing methods:**

- **`save()`** (line ~385) — Added pruning calls:
  ```python
  self._prune_sulfation_history()
  self._prune_discharge_events()
  ```

- **`load()`** (line ~90) — Added Phase 16 array initialization:
  ```python
  self.data.setdefault('sulfation_history', [])
  self.data.setdefault('discharge_events', [])
  self.data.setdefault('roi_history', [])
  self.data.setdefault('natural_blackout_events', [])
  ```

### Model.json Schema Extension

**New top-level arrays** (persisted atomically):

```json
{
  "sulfation_history": [
    {
      "timestamp": "2026-03-17T10:30:00Z",
      "event_type": "natural" | "test_initiated",
      "sulfation_score": 0.45,
      "days_since_deep": 7.2,
      "ir_trend_rate": 0.008,
      "recovery_delta": 0.12,
      "temperature_celsius": 35.0,
      "confidence_level": "high" | "medium" | "low"
    }
  ],
  "discharge_events": [
    {
      "timestamp": "2026-03-17T10:30:00Z",
      "event_reason": "natural" | "test_initiated",
      "duration_seconds": 1200,
      "depth_of_discharge": 0.75,
      "measured_capacity_ah": 6.8,
      "cycle_roi": 0.52
    }
  ],
  "roi_history": [],
  "natural_blackout_events": []
}
```

**Backward compatibility:** v2.0 model.json files load without error; missing Phase 16 keys initialize to empty arrays.

## Deviations from Plan

**None** — plan executed exactly as written.

All acceptance criteria met:
- ✅ `append_sulfation_history()` defined, accepts dict with 8 fields
- ✅ `append_discharge_event()` defined, accepts dict with 6 fields
- ✅ `_prune_sulfation_history()` and `_prune_discharge_events()` defined
- ✅ `save()` calls pruning methods in correct order
- ✅ Backward compatibility: v2.0 model.json loads, missing keys → empty arrays
- ✅ 14 integration tests all passing
- ✅ grep counts: "sulfation_history" appears ≥5 times, "discharge_event" appears ≥5 times

## Next Steps (Wave 2)

**Plan 03 (next):** Discharge handler integration
- Import sulfation/cycle_roi modules from Phase 15
- Call append_sulfation_history() and append_discharge_event() on discharge completion
- Integrate journald structured event logging
- Health.json metric export (sulfation_score, cycle_roi, next_test_eta)

**Plan 04:** Health endpoint observability
- Extend write_health_endpoint() with Phase 16 metrics
- MOTD module for sulfation display

**Plan 05:** Journald event logging
- Structured events for discharge completion
- Event aggregation for Grafana

## Requirement Traceability

| Requirement | Status | Evidence |
|-------------|--------|----------|
| SULF-05 | ✅ SATISFIED | sulfation_history persisted in model.json, 7 tests pass |
| RPT-03 | ✅ SATISFIED | discharge_events persisted in model.json, 7 tests pass |

## Self-Check

**File integrity:**
- ✅ `/home/j2h4u/repos/j2h4u/ups-battery-monitor/src/model.py` exists, contains 4 new methods
- ✅ `/home/j2h4u/repos/j2h4u/ups-battery-monitor/tests/test_sulfation_persistence.py` exists, 7 tests
- ✅ `/home/j2h4u/repos/j2h4u/ups-battery-monitor/tests/test_discharge_event_logging.py` exists, 7 tests

**Commit verification:**
- ✅ ee6e6df — feat(16-02): add sulfation history persistence methods — files: src/model.py, tests/test_sulfation_persistence.py
- ✅ 1066353 — test(16-02): implement full discharge event logging tests — files: tests/test_discharge_event_logging.py
- ✅ 4fce3e8 — test(16-02): improve backward compatibility test — files: tests/test_sulfation_persistence.py

**Test runs:**
- ✅ pytest exit code 0 (14 passed)
- ✅ No FAILED or ERROR in output
- ✅ All required fields validated
- ✅ Persistence verified (save/reload cycles)

---

*Executed: 2026-03-17 | Model persistence foundation complete, ready for discharge handler integration*
