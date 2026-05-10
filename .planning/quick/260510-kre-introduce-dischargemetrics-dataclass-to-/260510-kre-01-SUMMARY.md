---
phase: 260510-kre
plan: "01"
subsystem: discharge_handler
tags: [refactor, dataclass, type-safety]
dependency_graph:
  requires: []
  provides: [DischargeMetrics dataclass]
  affects: [src/discharge_handler.py, tests/test_discharge_handler.py]
tech_stack:
  added: [dataclasses.dataclass(frozen=True)]
  patterns: [frozen dataclass as typed handoff between pipeline stages]
key_files:
  modified:
    - src/discharge_handler.py
    - tests/test_discharge_handler.py
decisions:
  - _REQUIRED_DATA_KEYS assertions kept and strengthened via dataclasses.fields() + isinstance check
  - SulfationState imported explicitly (was previously transitive via compute_sulfation_score)
  - sulfation_state field in test_persist_* dicts set to None (no truthy sentinel object needed — DischargeMetrics is typed, sentinel pattern is unnecessary)
metrics:
  duration: "174 seconds (~3 min)"
  completed: "2026-05-10"
  tasks: 1
  files: 2
---

# Phase 260510-kre Plan 01: Introduce DischargeMetrics dataclass — Summary

**One-liner:** Replaced 16-key opaque dict between `_compute_sulfation_metrics` / `_persist_sulfation_and_discharge` / `_log_discharge_complete` with a `@dataclass(frozen=True) DischargeMetrics`, eliminating stringly-typed key access at all three call sites.

## What Was Built

`DischargeMetrics` frozen dataclass with 16 typed fields placed in `src/discharge_handler.py` above `class DischargeHandler`. The three pipeline methods now use attribute access (`data.field`) instead of dict subscript (`data["field"]`).

## Migrations Performed

**`data["k"]` → `data.k` count: 17 substitutions** across the two consumer methods:

- `_persist_sulfation_and_discharge`: 9 dict accesses replaced (now_iso × 2, discharge_trigger × 2, sulfation_score_r, days_since_deep_r, ir_trend_r, recovery_delta_r, confidence_level, discharge_duration, dod_r, capacity_ah_ref, roi_r, depth_of_discharge)
- `_log_discharge_complete`: 8 dict accesses replaced (discharge_trigger, discharge_duration, dod_r, sulfation_score_r, recovery_delta_r, roi_r, capacity_ah_ref, now_iso)

## Test Updates

- `_REQUIRED_DATA_KEYS` assertions: kept and strengthened — both occurrences now use `set(f.name for f in dataclasses.fields(data)) >= self._REQUIRED_DATA_KEYS` plus an `isinstance(data, DischargeMetrics)` check. Coverage is stronger than the former dict-key check.
- 5 dict literals replaced with `DischargeMetrics(...)` constructions (3 in `_persist_*` tests, 2 in `_log_*` tests).
- The two `_log_discharge_complete` test dicts that previously omitted several fields now supply all 16 fields with sensible values matching each test scenario.

## Test Results

568 tests pass (0 failures, 0 regressions vs. the 476 baseline — additional tests from prior phases were already committed).

## Deviations from Plan

None — plan executed exactly as written, with one minor observation:

- The `_persist_sulfation_and_discharge` test dicts previously used `object()` as a truthy `sulfation_state` sentinel. The dataclass `SulfationState` type annotation on the field means the field accepts `None | SulfationState`. The test scenarios for the persist path don't exercise sulfation_state directly, so `sulfation_state=None` is the correct value (persist path only reads `sulfation_score_r`, `days_since_deep_r`, etc. — it doesn't branch on `sulfation_state` itself). This is correct behavior, not a deviation.

## Operator Note

After merging to production, restart the daemon to pick up the change:

```
sudo systemctl restart ups-battery-monitor
```

## Self-Check: PASSED

- `grep -n 'class DischargeMetrics' src/discharge_handler.py` → line 40 (1 hit)
- `grep -nE 'data\["' src/discharge_handler.py` → 0 hits
- `grep -n 'DischargeMetrics(' tests/test_discharge_handler.py` → 5 hits (lines 421, 460, 499, 533, 565)
- `python3 -m pytest tests/ -x -q` → 568 passed
- Commit 8476d56 verified in git log
