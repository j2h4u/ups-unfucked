---
phase: 25-desulfation-retraction-diagnostic-only-capacity-verification
plan: "02"
subsystem: battery-math-retraction
tags: [retraction, deletion, sulfation, cycle-roi, discharge-pipeline, health-endpoint, motd]
dependency_graph:
  requires: [25-01]
  provides: [sulfation-modules-deleted, discharge-pipeline-slimmed, health-endpoint-clean, motd-clean, tests-clean]
  affects: [discharge_handler, model, monitor, monitor_config, virtual_ups_exporter, motd_status]
tech_stack:
  added: []
  patterns: [inline-slim-path-rewrite, surgical-test-cleanup]
key_files:
  created: []
  modified:
    - src/battery_math/__init__.py
    - src/discharge_handler.py
    - src/model.py
    - src/monitor.py
    - src/monitor_config.py
    - src/virtual_ups_exporter.py
    - src/motd_status.py
    - scripts/install.sh
    - tests/test_model.py
    - tests/test_health_endpoint_v16.py
    - tests/test_motd_status.py
    - tests/test_discharge_event_logging.py
    - tests/test_dispatch.py
    - tests/test_monitor.py
    - tests/test_scheduler_manager.py
    - tests/test_year_simulation.py
decisions:
  - "DischargeMetrics dataclass eliminated entirely — existed only to carry data between deleted compute/persist/log trio; no surviving callers"
  - "soh_before/soh_delta removed from update_battery_health — only used to feed deleted sulfation scoring; not needed by surviving pipeline"
  - "No migration code for stale model.json — single-host no-backward-compat policy; operator deletes model.json at deploy (state regenerates from live discharges)"
  - "test_discharge_handler.py deleted wholesale — 100% of tests exercised deleted sulfation/blackout machinery"
  - "Pre-existing vulture findings in calibration.py/types.py/discharge_collector.py left as deferred items — not caused by this removal"
metrics:
  duration: "~40 minutes"
  completed: "2026-06-03T20:41:01Z"
  tasks_completed: 3
  tasks_total: 3
  files_changed: 23
  commits: 3
---

# Phase 25 Plan 02: Sulfation/Cycle-ROI Retraction Summary

Deleted the two disproven-premise kernel modules (`sulfation.py`, `cycle_roi.py`), eliminated
the DischargeMetrics dataclass and its entire compute-persist-log trio from the discharge
pipeline, stripped all sulfation/cycle_roi fields from the health endpoint, MOTD, exporter,
and installer, and cleaned the test suite — leaving the codebase honest and grep-clean.

## What Was Built

**Deleted kernel modules:**
- `src/battery_math/sulfation.py` — SulfationState dataclass, compute_sulfation_score, estimate_recovery_delta
- `src/battery_math/cycle_roi.py` — compute_cycle_roi

**src/battery_math/__init__.py** — removed sulfation/cycle_roi imports and `__all__` entries

**src/discharge_handler.py** — complete pipeline rewrite:
- Eliminated: `DischargeMetrics` dataclass, `_score_and_persist_sulfation`, `_compute_sulfation_metrics`, `_persist_sulfation_and_discharge`, `_log_discharge_complete`, `_assess_sulfation_confidence`, `_grant_blackout_credit`
- Eliminated: `last_sulfation_score`, `last_sulfation_confidence`, `last_recovery_delta`, `last_cycle_roi` instance fields
- Eliminated: `soh_before`/`soh_delta` (used only by deleted scoring); `BLACKOUT_CREDIT_DAYS` constant
- Preserved: `update_battery_health` with inline slim path — all 15 pipeline steps in order (SoH, Peukert, replacement, alerts, trigger classification, discharge event, journald log, safe_save)
- `append_discharge_event` called every discharge with `measured_capacity_ah` and no `cycle_roi`

**src/model.py** — removed:
- `setdefault` calls for `sulfation_history`, `roi_history`, `blackout_credit`
- `append_sulfation_history`, `set_blackout_credit`, `clear_blackout_credit`, `get_blackout_credit` methods
- `sulfation_history`/`roi_history` from list-type validation
- `sulfation_history` cap in `save()`
- Blackout credit dict-type validation block
- Updated `save()` docstring and `update_scheduling_state` docstring

**src/monitor.py** — removed `clear_blackout_credit()` call and desulfation comment

**src/monitor_config.py:**
- HealthSnapshot: removed `sulfation_score`, `sulfation_confidence`, `recovery_delta`, `cycle_roi` fields
- `write_health_endpoint`: removed those four keys from `health_data`
- SchedulingConfig docstring updated

**src/virtual_ups_exporter.py** — removed four sulfation kwargs from HealthSnapshot construction

**src/motd_status.py** — removed `sulfation_pct` mapping and updated docstring

**scripts/motd/55-sulfation.sh** — deleted

**scripts/install.sh** — removed `55-sulfation.sh` from MOTD install loop

**Test suite — deleted wholesale (exercised only deleted code):**
- `tests/test_sulfation.py`
- `tests/test_sulfation_persistence.py`
- `tests/test_sulfation_offline_harness.py`
- `tests/test_cycle_roi.py`
- `tests/test_discharge_handler.py`

**Test suite — surgical cleanup:**
- `test_model.py`: removed blackout_credit test methods and sulfation_history validation test
- `test_health_endpoint_v16.py`: removed sulfation field test, rewrote ROI test as diagnostic fields test
- `test_motd_status.py`: removed sulfation_pct assertions, renamed tests
- `test_discharge_event_logging.py`: removed cycle_roi from sample event and required_fields
- `test_dispatch.py`: replaced sulfation reason_code strings with diagnostic_cadence
- `test_monitor.py`: removed test_battery_replaced_clears_blackout_credit
- `test_scheduler_manager.py`: renamed/updated sulfation-referencing test
- `test_year_simulation.py`: renamed sulfation rest-period test, cleaned comment language

## Commits

| Hash | Type | Description |
|------|------|-------------|
| d423858 | feat | Task 1 — delete sulfation/cycle_roi kernels, slim discharge pipeline + model |
| b039481 | feat | Task 2 — strip sulfation/cycle_roi from health endpoint, exporter, MOTD, installer |
| 26ac1ca | feat | Task 3 — delete sulfation/cycle_roi tests, clean mixed tests, prove grep+vulture |

## Verification Results

- `test ! -f src/battery_math/sulfation.py && test ! -f src/battery_math/cycle_roi.py` → FILES_GONE
- `uv run pytest` → **518/518 passed** (down from ~568 before wave; 50 tests deleted with dead code)
- `uvx vulture src` → 3 pre-existing findings (calibration.py, types.py, discharge_collector.py); none from this removal
- `uvx ruff check src` → All checks passed
- Grep gate: `rg "sulfation|cycle_roi" src tests scripts | rg -v "scripts/battery-health.py:.*aging \(sulfation"` → GREP_CLEAN
- health.json schema no longer asserts sulfation/cycle_roi keys (test_health_endpoint_v16 passes)
- `append_discharge_event` still called per discharge with `measured_capacity_ah` (no `cycle_roi`)
- `scripts/install.sh` MOTD loop no longer lists `55-sulfation.sh`

## Decisions Made

1. **DischargeMetrics eliminated** — the dataclass existed solely to carry data between the deleted compute-persist-log trio. All 16 fields were either sulfation-derived or inline-computable in 3 lines. Eliminated rather than pruned to 5 fields.

2. **No migration code for model.json** — per project no-backward-compat / single-host policy. `save()` round-trips `self.state` as-is, so stale `sulfation_history`/`roi_history`/`blackout_credit`/per-event `cycle_roi` already resident in a deployed model.json are NOT auto-purged. Operator must delete and regenerate.

3. **test_discharge_handler.py deleted wholesale** — 100% of the test file exercised deleted sulfation/blackout machinery. No non-sulfation coverage was lost.

4. **soh_before/soh_delta removed** — after removing sulfation scoring, these variables had zero remaining callers. Vulture correctly flagged them; removed.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Removed unused soh_before/soh_delta variables**
- **Found during:** Task 3 (vulture run)
- **Issue:** After eliminating the sulfation pipeline, `soh_before = self.battery_model.get_soh()` and `soh_delta = soh_after - soh_before` had no remaining callers
- **Fix:** Removed both assignments from `update_battery_health`
- **Files modified:** src/discharge_handler.py
- **Commit:** 26ac1ca

**2. [Rule 1 - Bug] Cleaned comment tokens referencing deleted code**
- **Found during:** Task 3 (grep gate)
- **Issue:** Inline comments and docstrings across src/ and tests/ still contained literal "sulfation" and "cycle_roi" tokens (in model.py docstring, discharge_handler.py comments, test_scheduler_manager.py test names)
- **Fix:** Updated all comment/docstring language to remove the literal tokens
- **Files modified:** src/discharge_handler.py, src/model.py, tests/test_scheduler_manager.py, tests/test_dispatch.py, tests/test_year_simulation.py, tests/test_motd_status.py
- **Commit:** 26ac1ca

## Known Stubs

None.

## Deployment Note

After deploying this wave, delete `~/.config/ups-battery-monitor/model.json` and re-run install so stale `sulfation_history` / `roi_history` / `blackout_credit` / per-event `cycle_roi` keys are purged — state regenerates from live discharges.

**Operational cost:** deleting model.json also discards the learned SoH / measured-capacity / LUT state, so capacity estimation restarts from scratch and re-warms over subsequent discharges. This is accepted per the no-backward-compat policy (single-host, state regenerates), but is a real cost. The ADR (Plan 03 Task 1) will mirror this as a Consequences bullet.

## Deferred Items

The following pre-existing vulture findings (present at the wave-1 baseline) are out of scope for this plan and logged for future attention:
- `src/battery_math/calibration.py:15` — `current_exponent` unused variable (100% confidence)
- `src/battery_math/types.py:32` — `cumulative_on_battery_sec` unused variable (60% confidence)
- `src/discharge_collector.py:72` — `is_collecting` unused property (60% confidence)

## Threat Flags

None — no new network endpoints, auth paths, file access patterns, or schema changes introduced. The removal only deletes persisted fields; existing consumers (Grafana/MOTD/battery-health.py) that read the removed health.json keys will see the field absent and should handle gracefully (the fields were optional).

## Self-Check

Files exist:
- src/battery_math/__init__.py: FOUND
- src/discharge_handler.py: FOUND
- src/model.py: FOUND
- src/monitor.py: FOUND
- src/monitor_config.py: FOUND
- src/virtual_ups_exporter.py: FOUND
- src/motd_status.py: FOUND
- scripts/install.sh: FOUND
- tests/test_model.py: FOUND

Commits exist:
- d423858: FOUND
- b039481: FOUND
- 26ac1ca: FOUND

Files deleted (confirmed absent):
- src/battery_math/sulfation.py: GONE
- src/battery_math/cycle_roi.py: GONE
- scripts/motd/55-sulfation.sh: GONE
- tests/test_sulfation.py: GONE
- tests/test_sulfation_persistence.py: GONE
- tests/test_sulfation_offline_harness.py: GONE
- tests/test_cycle_roi.py: GONE
- tests/test_discharge_handler.py: GONE

## Self-Check: PASSED
