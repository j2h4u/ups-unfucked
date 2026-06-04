---
phase: 26-model-json-learned-state-hygiene-move-config-spec-and-derive
plan: "01"
subsystem: model-persistence
tags: [hygiene, model-json, constants, capacity, refactor]
dependency_graph:
  requires: []
  provides: [NOMINAL_VOLTAGE-constant, capacity_ah-injection, rated_ah-propagation, slim-physics-persistence]
  affects: [src/model.py, src/monitor.py, src/battery_math/constants.py, scripts/battery-health.py]
tech_stack:
  added: []
  patterns: [config-injection, constants-as-source-of-truth, schema-as-code-via-TypedDict]
key_files:
  created: []
  modified:
    - src/battery_math/constants.py
    - src/model.py
    - src/monitor.py
    - scripts/battery-health.py
    - tests/test_model.py
    - tests/test_motd.py
    - tests/test_monitor_integration.py
decisions:
  - "NOMINAL_VOLTAGE added to constants.py as single source of truth for 12.0 nominal battery voltage in getters and physics persistence only; runtime_calculator/peukert/calibration still carry local 12.0 defaults (out of scope, not a persistence blocker)"
  - "battery-health.py uses RATED_CAPACITY_AH constant directly — no config.toml loader helper added (YAGNI; script is a read-only operator report, constant is the deployed default)"
  - "Pre-existing mock-leak failures in test suite (TypeError '>=' not supported between int and MagicMock) confirmed pre-existing via git stash comparison — 71 failures before and after our changes with same test ordering; all target tests pass when run isolated or in non-conflicting order"
metrics:
  duration: "10 min"
  completed: "2026-06-04"
  tasks_completed: 3
  tasks_total: 3
  files_changed: 7
---

# Phase 26 Plan 01: model.json Config/Spec Hygiene — NOMINAL_VOLTAGE, capacity_ah injection, slim physics Summary

Moved three config/spec values out of persisted model.json: stopped persisting `full_capacity_ah_ref` (now sourced from config at runtime via `BatteryModel.capacity_ah`), `physics.nominal_voltage`, and `physics.nominal_power_watts` (now sourced from constants.py). The persisted physics dict now carries only the three learned keys (peukert_exponent, ir_compensation, rls_state). The injected `capacity_ah` also drives `get_convergence_status().rated_ah` at both return sites so health/MOTD correctly reflects non-default capacity batteries.

## Tasks Completed

| Task | Description | Commit | Files |
|------|-------------|--------|-------|
| 1 | Add NOMINAL_VOLTAGE; source nominal voltage/power from constants; slim physics persistence | 5a03480 | constants.py, model.py |
| 2 | Inject capacity_ah from config; drop full_capacity_ah_ref; propagate rated_ah | 1a2e1ec | model.py, monitor.py |
| 3 | Update battery-health.py capacity source; fix/update tests | 607da49 | battery-health.py, test_model.py, test_motd.py, test_monitor_integration.py |

## What Was Built

**Task 1 — NOMINAL_VOLTAGE constant + slim physics persistence:**
- Added `NOMINAL_VOLTAGE = 12.0` to `src/battery_math/constants.py` as single source of truth
- Removed `nominal_voltage` and `nominal_power_watts` fields from `PhysicsParams` dataclass
- `get_nominal_voltage()` and `get_nominal_power_watts()` now return constants directly
- `_sync_physics_from/to_state` only handle the three learned keys; `_default_vrla_lut` no longer seeds spec keys in physics

**Task 2 — capacity_ah injection:**
- `BatteryModel.__init__` gains `capacity_ah: float = RATED_CAPACITY_AH` parameter; sets `self.capacity_ah` before `load()`
- `get_capacity_ah()` returns `self.capacity_ah` (no state dict lookup)
- `get_convergence_status().rated_ah` uses `self.capacity_ah` at both return sites (empty-estimates branch and populated branch)
- Removed `full_capacity_ah_ref` from `ModelState` TypedDict, `_validate_and_clamp_fields`, and `_default_vrla_lut`
- `monitor.py` passes `capacity_ah=config.capacity_ah` to constructor; removed the now-dead `state["full_capacity_ah_ref"] = config.capacity_ah` assignment

**Task 3 — Test and script updates:**
- `battery-health.py`: imported `RATED_CAPACITY_AH` from constants; replaced both `model_data.get("full_capacity_ah_ref")` lookups with the constant; fixed 4 pre-existing ruff F-string warnings (lines shifted by our edits)
- `test_model.py`: removed `full_capacity_ah_ref` from fixtures; cleaned `nominal_voltage`/`nominal_power_watts` from `_base_model_data()` physics; added 4 new tests: `test_capacity_ah_injection`, `test_rated_ah_propagation_empty_model`, `test_rated_ah_propagation_populated_model`, `test_rated_ah_propagation_default`
- `test_motd.py`: removed `full_capacity_ah_ref` from 3 JSON fixtures (BatteryModel load now rejects it as unknown key)
- `test_monitor_integration.py`: removed `battery_model.state["full_capacity_ah_ref"] = 7.2` assignment from fixture setup

## Verification Results

All plan gates pass:
- `rg "full_capacity_ah_ref" src/ scripts/` → nothing
- `rg "rated_ah=RATED_CAPACITY_AH" src/` → nothing
- `rg "self\.physics\.nominal" src/` → nothing
- `uvx ruff check src tests scripts` → all checks passed
- `uv run pytest tests/test_model.py tests/test_motd.py tests/test_monitor_integration.py::test_health_endpoint_capacity_persistence` → 83 passed

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed pre-existing ruff F-string warnings in battery-health.py**
- **Found during:** Task 3
- **Issue:** 4 f-strings with no interpolation in scripts/battery-health.py (lines shifted after our edits revealed them in scope)
- **Fix:** `uvx ruff check --fix` removed extraneous `f` prefix from 4 strings
- **Files modified:** scripts/battery-health.py
- **Commit:** 607da49

### Notes

**Pre-existing test isolation issue (out of scope):** When pytest runs `tests/test_model.py` before `tests/test_monitor.py`, a MagicMock leaks into the logger's handler list causing 67 `TypeError: '>=' not supported between int and MagicMock` failures. This was confirmed pre-existing via `git stash` comparison (71 failures before and after our changes with the same test ordering). The 4 target failures we fixed (test_motd ×2, test_monitor_integration ×1, test_lifecycle_round_trip ×1) now pass; all 78 test_model.py tests pass in isolation. This pre-existing isolation bug is out of scope per deviation rules.

**NOTE (scope boundary):** `NOMINAL_VOLTAGE` is the source of truth for the getters and the persisted physics blob only. Other math modules (runtime_calculator, peukert, calibration) still carry local `12.0` defaults — that is out of scope and NOT a persistence blocker. No project-wide 12.0 sweep was done.

## Deploy Caveat

Wave-1 keys (`full_capacity_ah_ref`, `physics.nominal_voltage`, `physics.nominal_power_watts`) are removed from the schema here but the stale-file strip is documented in Plan 02. **Do NOT deploy after wave 1 alone without stripping wave-1 keys** — the strict loader would reject the deployed model.json. Full deploy note lives in 26-02-SUMMARY.

## Known Stubs

None. All callers receive correct values. `get_capacity_ah()`, `get_nominal_voltage()`, `get_nominal_power_watts()`, and `get_convergence_status().rated_ah` all return live values sourced from constructor injection or constants.

## Threat Flags

No new security-relevant surface introduced. This is a pure in-place refactor removing persistence of config/spec values. The threat model mitigations for T-26-02 (dead callers) and T-26-06 (hardcoded rated_ah) were both applied: rg/pytest gate confirms no callers broken; rated_ah propagation tests confirm both return sites use self.capacity_ah.

## Self-Check: PASSED

- SUMMARY.md: FOUND
- Commit 5a03480 (Task 1): FOUND
- Commit 1a2e1ec (Task 2): FOUND
- Commit 607da49 (Task 3): FOUND
