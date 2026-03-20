---
plan: 22-01
phase: 22-naming-docs-sweep
status: complete
started: 2026-03-20
completed: 2026-03-20
---

# Plan 22-01: Rename BatteryModel.data to state + rls/d cleanup — Summary

## What Was Built

Renamed `BatteryModel.data` attribute to `BatteryModel.state` across 12 files (~230 occurrences). Renamed `_sync_physics_from_data`/`_sync_physics_to_data` methods to `_sync_physics_from_state`/`_sync_physics_to_state`. Cleaned up opaque local variables `rls` → `rls_state` and `d` → `stored_params` in the deserialization method.

## Tasks Completed

| # | Task | Status |
|---|------|--------|
| 1 | Rename BatteryModel.data to state across all source and test files | ✅ |
| 2 | Rename rls/d local variables in _sync_physics_from_state | ✅ |

## Key Files

### Modified
- `src/model.py` — 80 `.data` → `.state`, method renames, rls/d cleanup
- `src/discharge_handler.py` — 10 `.data` → `.state`
- `src/scheduler_manager.py` — 3 `.data` → `.state`
- `src/monitor.py` — 8 `.data` → `.state`
- `tests/test_model.py` — 37 `.data` → `.state`
- `tests/test_monitor.py` — 23 `.data` → `.state`
- `tests/test_sulfation_persistence.py` — 17 `.data` → `.state`
- `tests/test_discharge_event_logging.py` — 16 `.data` → `.state`
- `tests/test_monitor_integration.py` — 13 `.data` → `.state`
- `tests/test_scheduler_manager.py` — 10 `.data` → `.state`
- `tests/test_dispatch.py` — 6 `.data` → `.state`
- `tests/test_discharge_handler.py` — 7 `.data` → `.state`

## Verification

- `rg 'self\.data\b' src/model.py` → 0 hits ✅
- `rg '\.data\b' src/discharge_handler.py src/scheduler_manager.py src/monitor.py` → 0 hits ✅
- `rg '_sync_physics_from_data' src/` → 0 hits ✅
- `rg 'rls_state = {}' src/model.py` → 1 hit ✅
- `rg 'stored_params = rls_data' src/model.py` → 1 hit ✅
- `python3 -m pytest -x -q` → 555 passed ✅

## Self-Check: PASSED

All acceptance criteria verified. Zero stale references. model.json schema unchanged.

## Deviations

None.
