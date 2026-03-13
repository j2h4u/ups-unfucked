# Phase 4 Planning Summary — Health Monitoring & Battery Degradation

**Date:** 2026-03-14
**Status:** Planning Complete
**Plans Created:** 2 (Wave 0 TDD, Wave 1 Integration)
**Requirements Covered:** HLTH-01, HLTH-02, HLTH-03, HLTH-04, HLTH-05

---

## Overview

Phase 4 implements battery health monitoring through three independent calculation modules and integrates them into the monitor.py daemon and MOTD display. The phase builds entirely on Python stdlib (no external dependencies) and follows established patterns from Phases 1–3.

**Core concept:** After each discharge event (OB→OL transition), calculate State of Health (SoH) using area-under-curve voltage integration, predict replacement date via linear regression, and alert operators via journald when thresholds breached. Display status in MOTD on login.

---

## Plan Structure

### Plan 01 — Wave 0: Test Infrastructure & Module Implementations (TDD)

**Type:** TDD (Red-Green-Refactor cycle)

**Modules Created:**
1. `src/soh_calculator.py` — Calculate SoH from discharge voltage profile using trapezoidal rule area-under-curve
2. `src/replacement_predictor.py` — Linear regression over SoH history to predict replacement date
3. `src/alerter.py` — journald structured logging for SoH and runtime threshold breaches

**Test Coverage:**
- `tests/test_soh_calculator.py` — 8 unit tests covering trapezoidal rule, edge cases, anchor voltage trimming
- `tests/test_replacement_predictor.py` — 8 unit tests covering least-squares regression, R² validation, insufficient data handling
- `tests/test_alerter.py` — 8 unit tests covering SysLogHandler integration, structured fields, independent thresholds

**Key Design Decisions:**
- Manual least-squares regression (not scipy) — avoids 50MB dependency for 100 lines of stdlib-equivalent code
- Trapezoidal rule for area-under-curve — well-understood, handles non-uniform time intervals
- SysLogHandler for journald — stdlib integration, automatic rotation, queryable timestamps
- Configurable thresholds (SoH %, Time_rem minutes) — passed as function parameters, no environment variables initially

**Deliverables:**
- 3 production-ready modules (100+ lines each)
- 24 unit tests (all passing)
- No regressions in Phases 1–3

---

### Plan 02 — Wave 1: Integration into Monitor Loop & MOTD (Auto)

**Type:** Auto (standard implementation tasks)

**Task 1: Monitor.py Integration**

Integrate `_update_battery_health()` method into monitor.py polling loop:

1. After OB→OL transition (discharge event completion):
   - Extract voltage/time series from discharge_buffer
   - Call `soh_calculator.calculate_soh_from_discharge()`
   - Append {date, SoH} entry to model.json soh_history
   - Call `replacement_predictor.linear_regression_soh()` with 3-point minimum
   - Alert if SoH < 80% (threshold configurable)
   - Alert if Time_rem@100% < 20 min (threshold configurable)

2. Imports and logger setup:
   - Import soh_calculator, replacement_predictor, alerter
   - Logger created via `alerter.setup_ups_logger("ups-battery-monitor")`

3. Error handling:
   - Discharge buffer with <2 points: skip SoH update (no discharge detected)
   - Insufficient history for regression (<3 points): return None, show "insufficient data"
   - High scatter (R² < 0.5): reject prediction, log warning

**Task 2: MOTD Script (scripts/motd/51-ups-health.sh)**

Create bash script that displays single-line health status:

1. Read virtual UPS via `upsc cyberpower-virtual@localhost` (Phase 3 infrastructure)
2. Read model.json for SoH and replacement_date
3. Format output: `[icon] UPS: [status] · charge X% · runtime Xm · load X% · health Y% [replacement YYYY-MM]`
4. Colors: green (SoH ≥80%), yellow (60–79%), red (<60%)
5. Handles missing fields gracefully

**Integration points:**
- MOTD runner calls script on every SSH login
- Script executes in <100ms (no performance regression)
- Compatible with existing 51-ups.sh color conventions

**Deliverables:**
- Updated monitor.py with discharge event handler
- Functional MOTD script
- No regressions in test suite
- All Phase 1–3 tests still passing

---

## Requirements Mapping

| Requirement | Plan | Task | Implementation |
|-------------|------|------|-----------------|
| HLTH-01 | 01 | Task 1 | `calculate_soh_from_discharge()` with trapezoidal rule area-under-curve |
| HLTH-02 | 01 | Task 2 | `linear_regression_soh()` with least-squares fit and R² validation |
| HLTH-03 | 02 | Task 2 | MOTD script displaying charge, runtime, load, SoH%, replacement date |
| HLTH-04 | 01 | Task 3 | `alert_soh_below_threshold()` journald structured logging |
| HLTH-05 | 02 | Task 1 | `alert_runtime_below_threshold()` in monitor loop |

---

## Wave Structure

```
Wave 0 (Plan 01):
  - TDD cycle: RED → GREEN → REFACTOR
  - 3 modules × 8 tests each = 24 unit tests
  - No dependencies between modules (parallel testability)
  - All tests passing before moving to Wave 1

Wave 1 (Plan 02):
  - Depends on Wave 0 (modules must exist)
  - Task 1: Monitor.py integration (depends on soh_calculator, replacement_predictor, alerter from Wave 0)
  - Task 2: MOTD script (standalone, no code dependencies)
  - Both tasks can run in parallel if needed
  - Full test suite (Phases 1–4) passing before completion
```

---

## Verification Strategy

**Per-task verification:**
- `pytest tests/test_soh_calculator.py -v` — 8 tests
- `pytest tests/test_replacement_predictor.py -v` — 8 tests
- `pytest tests/test_alerter.py -v` — 8 tests
- `pytest tests/test_monitor.py -x` — Phase 2 integration tests unchanged
- `bash scripts/motd/51-ups-health.sh` — script runs without error

**Phase gate:**
- `pytest tests/ -x` — all phases (1, 2, 3, 4) passing, 150+ tests
- Manual MOTD check: `ssh server` → verify health line displays correctly
- Journald check: `journalctl -u ups-battery-monitor -p warning` → no spam, structured fields present

---

## Open Questions (Deferred to Execution)

1. **Area-under-curve baseline:** What is the reference discharge curve for a new UT850EG?
   - Current: Empirical 47-min baseline from 2026-03-12 blackout (12.0V avg × 2820 sec)
   - Phase 6 (calibration mode) will refine this with measured data

2. **SoH threshold:** 80% recommended, but configurable. Tune after 3 months of real data.

3. **Runtime@100% threshold:** Proposed 20 min (rough 40% of observed 47-min baseline). Configurable; exact value TBD per user decision during execution.

4. **Replacement date format:** YYYY-MM recommended (e.g., "2028-03"); less detail than day-level for uncertainty acknowledgment.

---

## Architecture Notes

**Stateless design:**
- soh_calculator: Pure function, no state
- replacement_predictor: Pure function, no state
- alerter: Fire-and-forget logging, no state
- Integration in monitor.py: Reads discharge buffer, writes to model.json (existing atomic pattern)

**Data flow:**
```
Discharge event (OB→OL)
    ↓ (voltage/time samples in buffer)
soh_calculator.calculate_soh_from_discharge()
    ↓
model.add_soh_history_entry()
model.save()
    ↓
replacement_predictor.linear_regression_soh()
    ↓
alerter.alert_soh_below_threshold() / alert_runtime_below_threshold()
    ↓ (journald)
MOTD script reads model.json, displays status on login
```

**Failure modes:**
- Discharge buffer empty: SoH update skipped, no error
- Insufficient history: Regression returns None, prediction deferred
- R² < 0.5: Prediction rejected as unreliable
- Alerting disabled: System continues (non-critical path)

---

## Testing Approach

**TDD in Wave 0:**
- Write failing tests first (RED)
- Implement modules to pass tests (GREEN)
- Refactor for clarity (REFACTOR)
- Verify no regressions in existing test suite

**Integration in Wave 1:**
- Monitor.py integration tests reuse Phase 2 patterns
- MOTD script tested via `bash -n` (syntax check) and manual SSH
- Full suite passing gate before moving to Phase 5

---

## Dependencies & Constraints

**Dependencies:**
- Phases 1–3 (all prior infrastructure)
- Python stdlib only (statistics, math, datetime, json, logging)
- Existing model.py, monitor.py, runtime_calculator.py

**Constraints:**
- No external packages (avoid scipy, numpy, etc.)
- model.json atomic writes (established in Phase 1)
- journald availability (SysLogHandler with /dev/log fallback)
- Thresholds configurable via function parameters

**Compatibility:**
- Compatible with existing virtual UPS infrastructure (Phase 3)
- MOTD script works with existing color conventions
- No changes to systemd service or NUT configuration (deferred to Phase 5)

---

## Success Criteria

✅ **All Phase 4 requirements satisfied:**
1. SoH recalculated after discharge (HLTH-01)
2. Linear regression predicts replacement date (HLTH-02)
3. MOTD displays health status (HLTH-03)
4. SoH threshold alerting (HLTH-04)
5. Runtime threshold alerting (HLTH-05)

✅ **Quality gates:**
- 24 unit tests passing (Wave 0)
- Monitor.py integration verified (Wave 1)
- MOTD script operational (Wave 1)
- Zero regressions in Phases 1–3 tests
- Full test suite passing (150+)

✅ **Ready for execution:** `/gsd:execute-phase 04-health-monitoring-battery-degradation`

---

## Next Steps

1. **Execute Plan 01 (Wave 0):** TDD implementation of 3 modules + 24 tests
2. **Execute Plan 02 (Wave 1):** Integration into monitor.py + MOTD script
3. **Verify Phase 4:** Full test suite + manual MOTD check
4. **Plan Phase 5:** Operational setup (install script, systemd service)

---

**Planning completed:** 2026-03-14 at 19:30 UTC
**Plans ready for execution**
