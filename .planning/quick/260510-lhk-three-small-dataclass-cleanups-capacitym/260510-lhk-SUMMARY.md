---
phase: 260510-lhk-quick
plan: "01"
subsystem: capacity_estimator, model, monitor_config
tags: [dataclass, refactor, immutability, cleanup]
dependency_graph:
  requires: []
  provides: [frozen-CapacityMeasurement, frozen-HealthSnapshot, RLSParams-asdict]
  affects: [src/capacity_estimator.py, src/model.py, src/monitor_config.py]
tech_stack:
  added: []
  patterns: [frozen-dataclass, dataclasses.asdict]
key_files:
  modified:
    - src/capacity_estimator.py
    - src/model.py
    - src/monitor_config.py
decisions:
  - "NamedTuple → frozen dataclass: preserves immutability, gains named-field safety and FrozenInstanceError on mutation attempts"
  - "dataclasses.asdict over hand-rolled to_dict: stdlib, zero maintenance, guaranteed parity for flat scalar fields"
  - "ScalarRLS.to_dict() in battery_math/rls.py intentionally untouched (different class, used by test_rls.py)"
metrics:
  duration: "~5 min"
  completed: "2026-05-10"
  tasks_completed: 1
  files_modified: 3
---

# Phase 260510-lhk Plan 01: Three Dataclass Cleanups Summary

One-liner: Frozen CapacityMeasurement (NamedTuple→dataclass) + frozen HealthSnapshot + RLSParams.to_dict() replaced by dataclasses.asdict().

## What Changed

### src/capacity_estimator.py
- Import: removed `NamedTuple` from typing, added `from dataclasses import dataclass`
- `CapacityMeasurement`: `NamedTuple` → `@dataclass(frozen=True)`; positional construction still works
- `has_converged()` line 293: `m[1]` → `m.ah` (named field access)
- `get_weighted_estimate()` lines 321–324: tuple-unpacking loop `for timestamp, ah, confidence, metadata in ...` → `for m in ...:` with `m.metadata`, `m.ah` named access

### src/model.py
- Added `import dataclasses` (module-level, alongside existing `from dataclasses import dataclass, field`)
- `RLSParams.to_dict()` method removed (7 lines deleted)
- 3 caller sites updated:
  - line 290: `rls.to_dict()` → `dataclasses.asdict(rls)` (dict comprehension)
  - line 512: `RLSParams().to_dict()` → `dataclasses.asdict(RLSParams())`
  - line 513: `rls.to_dict()` → `dataclasses.asdict(rls)`

### src/monitor_config.py
- `@dataclass` → `@dataclass(frozen=True)` on `HealthSnapshot`
- No other changes; zero mutation sites confirmed by pre-task grep

## Test Results

569 passed, 0 failed, 1 warning (unrelated pytest config option)

No tests needed updating — no `isinstance(m, tuple)` assertions existed for CapacityMeasurement, no direct `RLSParams.to_dict()` calls in test suite.

## ScalarRLS Verification

`grep -n "to_dict" src/battery_math/rls.py` → `ScalarRLS.to_dict()` still present at line 50 (untouched). `tests/test_rls.py` exercises it normally.

## Deviations from Plan

None — plan executed exactly as written.

## Self-Check

- [x] src/capacity_estimator.py modified (NamedTuple removed, frozen dataclass, m[1] fixed, tuple-unpack fixed)
- [x] src/model.py modified (to_dict removed, 3 callers updated, import dataclasses added)
- [x] src/monitor_config.py modified (frozen=True added)
- [x] Commit 19098c8 exists
- [x] 569 tests pass
- [x] ScalarRLS.to_dict() untouched
- [x] Daemon restarted and active

## Self-Check: PASSED
