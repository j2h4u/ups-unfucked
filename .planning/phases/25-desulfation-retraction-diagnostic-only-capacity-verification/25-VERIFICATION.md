---
phase: 25-desulfation-retraction-diagnostic-only-capacity-verification
verified: 2026-06-04T21:15:56Z
status: passed
score: 10/10 must-haves verified
overrides_applied: 0
---

# Phase 25: Desulfation Retraction & Diagnostic-Only Capacity Verification Report

**Phase Goal:** The daemon no longer self-initiates discharges for "desulfation": the scheduler proposes only a rare diagnostic capacity/SoH test on a persistent time cadence (bootstrap deadlock gone), all disproven-premise sulfation/cycle_roi machinery is removed, and the docs reflect honest monitoring.
**Verified:** 2026-06-04T21:15:56Z
**Status:** PASSED
**Re-verification:** No — initial verification (includes post-CR-review commits c75af55, 834794b, 41b7275, 3176810)

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Scheduler proposes first diagnostic test from cold start — deadlock gone | VERIFIED | `evaluate_test_scheduling(days_since_last_test_success=inf, days_since_last_attempt=inf, soh=0.9)` returns `propose_test/quick/diagnostic_cadence`. 30/30 scheduler tests, 39/39 manager tests green. Fresh-model deadlock proof test passes in `test_scheduler_manager.py`. |
| 2 | Scheduler drives on ~365-day cadence with zero dependency on sulfation score or cycle ROI | VERIFIED | `DIAGNOSTIC_TEST_INTERVAL_DAYS = 365.0` is the sole cadence constant. Gate 5 keys off `days_since_last_test_success >= 365`. `grep sulfation\|cycle_roi src/battery_math/scheduler.py` returns nothing. |
| 3 | Daemon restart does not re-trigger a test: days_since_last_test_success just under interval defers | VERIFIED | Restart-safety proof test in `test_scheduler_manager.py`: days=30 → `defer_test/within_cadence`. Reads `last_upscmd_timestamp` from persisted model.json (SCH-02). No in-memory state dependency. |
| 4 | Two-input timing split: days_since_last_attempt (rate-limit) and days_since_last_test_success (cadence) are independent; failed dispatch does not defer cadence ~365d but IS rate-limited | VERIFIED | Three boundary tests all pass: (a) attempt=1d, status=ERR → `defer_test/rate_limit`; (b) attempt=8d, status=ERR → `propose_test/diagnostic_cadence`; (c) attempt=30d, status=OK → `defer_test/within_cadence`. `_calculate_days_since_last_attempt` is status-agnostic; `_calculate_days_since_last_test_success` returns inf when status != "OK". |
| 5 | Safety gates hold: SoH floor, rate-limit, grid stability, cycle budget | VERIFIED | Gate order in `evaluate_test_scheduling`: (1) soh_floor block, (2) rate_limit defer, (3) grid_unstable defer, (4) critical_cycle_budget block, (5) cadence. All four gate paths covered by tests. |
| 6 | sulfation.py and cycle_roi.py deleted; grep returns nothing across src/tests/scripts | VERIFIED | `test ! -f src/battery_math/sulfation.py && test ! -f src/battery_math/cycle_roi.py` → confirmed absent. Full `rg sulfation\|cycle_roi src/ tests/ scripts/` returns zero matches. Line-109 comment reworded to "plate corrosion / electrolyte loss" — literal token gone. |
| 7 | Discharge event persistence survives: append_discharge_event called every discharge with measured_capacity_ah (no cycle_roi) | VERIFIED | `src/discharge_handler.py` lines 126-133: `append_discharge_event({"timestamp": ..., "event_reason": ..., "duration_seconds": ..., "depth_of_discharge": ..., "measured_capacity_ah": capacity_ah_ref})` — no `cycle_roi` key. DischargeMetrics dataclass eliminated. |
| 8 | Health endpoint, MOTD, exporter, journald stripped of sulfation_score/cycle_roi/recovery_delta fields | VERIFIED | `grep sulfation\|cycle_roi src/monitor_config.py src/virtual_ups_exporter.py src/motd_status.py` → empty. `55-sulfation.sh` absent. `scripts/install.sh` MOTD loop: `for motd_name in 51-ups.sh 51-ups-health.sh; do` — no reference to deleted script. |
| 9 | ADR 0001 records premise reversal, evidence, no-charge-control fact, and deploy action for stale model.json | VERIFIED | `docs/adr/0001-desulfation-premise-reversal.md` exists. Confirmed content: "No charge-side control (verified live)", "IEEE-1188", and deploy action `rm ~/.config/ups-battery-monitor/model.json`. Nygard format with Context/Decision/Consequences. |
| 10 | README/ROADMAP/MILESTONES contain no active-desulfation product claims; battery-health.py prints Maintenance & schedule section | VERIFIED | `grep -Ein "fights sulfation\|free desulfation\|stretch a\|active care" README.md` → empty. MILESTONES: `[RETRACTED in v3.2 — premise reversed, see ADR 0001]` banner present. ROADMAP Phase 15 annotated retracted. `battery-health.py`: `Maintenance & schedule` section confirmed with fixture run — prints next test, "never tested", IR trend, capacity/SoH. `UPS_MODEL_PATH`/`UPS_HEALTH_PATH` env overrides functional. |

**Score:** 10/10 truths verified

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/battery_math/scheduler.py` | Cadence engine, no sulfation/cycle_roi params, DIAGNOSTIC_TEST_INTERVAL_DAYS | VERIFIED | Contains `DIAGNOSTIC_TEST_INTERVAL_DAYS = 365.0`, split `days_since_last_test_success`/`days_since_last_attempt` params, 5-gate structure, no sulfation/cycle_roi references |
| `src/scheduler_manager.py` | Split timing methods, no sulfation/cycle_roi inputs, public getter calls | VERIFIED | `_calculate_days_since_last_attempt` and `_calculate_days_since_last_test_success` both use public getters only; `_gather_scheduler_inputs` returns split keys; `evaluate_test_scheduling` call passes split kwargs |
| `src/model.py` | New `get_last_upscmd_status()` getter; no sulfation_history/roi_history/blackout_credit | VERIFIED | Getter defined at line 835 returning `self.state.get("last_upscmd_status")`. No `append_sulfation_history`, `set_blackout_credit`, `clear_blackout_credit`, `get_blackout_credit` methods found. |
| `src/battery_math/__init__.py` | No sulfation/cycle_roi exports | VERIFIED | `grep sulfation\|cycle_roi src/battery_math/__init__.py` → empty |
| `src/discharge_handler.py` | No sulfation scoring, no blackout credit, DischargeMetrics gone, append_discharge_event intact | VERIFIED | No `_grant_blackout_credit`, `_score_and_persist_sulfation`, `DischargeMetrics`, `last_sulfation_score` found. `append_discharge_event` called with `measured_capacity_ah`. |
| `src/monitor_config.py` | HealthSnapshot without sulfation fields | VERIFIED | `grep sulfation\|cycle_roi` → empty |
| `scripts/install.sh` | MOTD loop without 55-sulfation.sh | VERIFIED | Loop: `for motd_name in 51-ups.sh 51-ups-health.sh; do` |
| `docs/adr/0001-desulfation-premise-reversal.md` | ADR with IEEE-1188, no-charge-control, model.json deploy action | VERIFIED | All three content checks confirmed |
| `scripts/battery-health.py` | Maintenance & schedule section, read_text, no sulfation, UPS_HEALTH_PATH env override | VERIFIED | All checks confirmed; fixture run prints complete section |
| `README.md` | No active-desulfation claims | VERIFIED | grep clean |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `src/scheduler_manager.py` | `src/battery_math/scheduler.py` | `evaluate_test_scheduling(...)` with split kwargs | VERIFIED | Call at line 239 passes `days_since_last_test_success=`, `days_since_last_attempt=` — no sulfation/cycle_roi kwargs |
| `src/scheduler_manager.py` | `src/model.py` | `get_last_upscmd_timestamp()` + `get_last_upscmd_status()` via public getters | VERIFIED | `_calculate_days_since_last_attempt` calls `get_last_upscmd_timestamp()`; `_calculate_days_since_last_test_success` calls both getters. `grep state\[.*last_upscmd_status` in scheduler_manager → empty. |
| `src/virtual_ups_exporter.py` | `src/monitor_config.py` | `HealthSnapshot(...)` without sulfation fields | VERIFIED | `grep sulfation\|cycle_roi src/virtual_ups_exporter.py` → empty |
| `src/discharge_handler.py` | `src/model.py` | `append_discharge_event` without cycle_roi | VERIFIED | Call at lines 126-133; no cycle_roi key |
| `scripts/battery-health.py` | health endpoint | `json.loads(HEALTH_PATH.read_text())` | VERIFIED | Line 214: `health_data = json.loads(HEALTH_PATH.read_text())` with `(OSError, ValueError)` guard |

---

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `scripts/battery-health.py` | `model_data`, `health_data` | `json.loads(MODEL_PATH.read_text())` / `json.loads(HEALTH_PATH.read_text())` | Yes — reads daemon-written JSON; fixture run confirmed real output | FLOWING |
| `src/scheduler_manager.py` | `days_since_last_test_success`, `days_since_last_attempt` | `get_last_upscmd_timestamp()` + `get_last_upscmd_status()` from model.json state | Yes — reads persisted `last_upscmd_timestamp`/`last_upscmd_status` written by `update_upscmd_result` on every dispatch | FLOWING |

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| battery-health.py prints Maintenance & schedule with fixture data | `UPS_MODEL_PATH=/tmp/fixture-model2.json UPS_HEALTH_PATH=/tmp/fixture-health2.json uv run python scripts/battery-health.py` | Prints "Next diagnostic test: 2027-06-01  (+362 days)  (within_cadence)", "Last test run: never tested", "IR trend rate: +1.0000 mΩ/day", "Capacity / SoH: SoH=87%" | PASS |
| Full test suite | `uv run pytest -q` | 517 passed in 2.11s | PASS |
| Scheduler unit tests (69 tests) | `uv run pytest tests/test_scheduler.py tests/test_scheduler_manager.py -q` | 69 passed | PASS |
| No sulfation/cycle_roi anywhere in src/tests/scripts | `rg sulfation\|cycle_roi src/ tests/ scripts/` | Zero matches | PASS |
| CR-01 regression: test_running not persisted | `grep test_running src/scheduler_manager.py src/model.py` | No production code writes test_running; regression tests assert `"test_running" not in model.state` | PASS |
| Vulture dead code | `uvx vulture src` | 3 pre-existing findings (calibration.py, types.py, discharge_collector.py) — none from this removal | PASS |

---

### Probe Execution

Step 7c: SKIPPED — no probe scripts declared in PLAN frontmatter; no `scripts/*/tests/probe-*.sh` files found for this phase.

---

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| SCH-01 | 25-01 | evaluate_test_scheduling driven by time-based cadence (~365d), not sulfation/cycle_roi | SATISFIED | Function signature has no sulfation/cycle_roi params; gate 5 keys off `days_since_last_test_success >= DIAGNOSTIC_TEST_INTERVAL_DAYS` |
| SCH-02 | 25-01 | Trigger reads persistent last_upscmd_timestamp from model.json; restart-safe; no bootstrap deadlock | SATISFIED | `_calculate_days_since_last_attempt` reads `get_last_upscmd_timestamp()` from persisted state; fresh-model (no prior timestamp) → inf → propose_test |
| SCH-03 | 25-01 | Safety gates retained; default test is quick | SATISFIED | SoH floor, rate-limit, grid stability, cycle budget gates present; engine only proposes `test_type="quick"` |
| RET-01 | 25-02 | Remove sulfation.py and all production callers | SATISFIED | File absent; no callers in any src/ file |
| RET-02 | 25-02 | Remove cycle_roi.py and all production callers | SATISFIED | File absent; no callers in any src/ file |
| RET-03 | 25-02 | Remove blackout-credit-as-desulfation and recovery_delta-as-evidence | SATISFIED | No `_grant_blackout_credit`, `clear_blackout_credit`, `get_blackout_credit`, `recovery_delta` anywhere in src/ |
| RET-04 | 25-02 | Remove sulfation_score/cycle_roi fields from health endpoint, model schema, MOTD, journald; remove dead tests | SATISFIED | HealthSnapshot has no sulfation fields; 55-sulfation.sh deleted; 5 test files deleted wholesale; journald log keys clean |
| DOC-01 | 25-03 | Remove desulfation product claims from README/ROADMAP/MILESTONES | SATISFIED | README: no "active care"/"fights sulfation"/"free desulfation"/"stretch a" claims. MILESTONES: retraction banner on v3.0 block. ROADMAP: Phase 15 line annotated retracted. CONTEXT.md: v3.0 section annotated superseded with ADR 0001 reference. |
| DOC-02 | 25-03 | ADR recording premise reversal, evidence, no-charge-control fact | SATISFIED | `docs/adr/0001-desulfation-premise-reversal.md` exists with all required content |
| RPT-01 | 25-03 | Maintenance & schedule section in battery-health.py reading model.json + health endpoint | SATISFIED | `print_maintenance()` defined; reads via `json.loads(read_text())`; UPS_HEALTH_PATH override functional; fixture run confirmed output |

All 10 requirement IDs from PLAN frontmatter accounted for. No orphaned requirements in REQUIREMENTS.md for this phase.

---

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/battery_math/calibration.py` | 15 | `unused variable 'current_exponent'` (vulture, 100% confidence) | Info | Pre-existing before Phase 25; not caused by this removal |
| `src/battery_math/types.py` | 32 | `unused variable 'cumulative_on_battery_sec'` (vulture, 60% confidence) | Info | Pre-existing before Phase 25 |
| `src/discharge_collector.py` | 72 | `unused property 'is_collecting'` (vulture, 60% confidence) | Info | Pre-existing before Phase 25 |

No TBD/FIXME/XXX markers found in Phase 25-modified files. No stubs. No debt markers. The three vulture findings are documented in 25-02-SUMMARY.md as pre-existing deferred items.

---

### CR-01 Post-Review Fix Verification

The code review found an un-clearable `test_running` flag that would permanently block future diagnostics. Fix applied in commit c75af55:

- `src/scheduler_manager.py`: no `test_running` write anywhere in production code (confirmed by grep)
- `src/model.py`: no `test_running` state key written (confirmed by grep)
- Regression tests in `tests/test_dispatch.py:122` and `tests/test_scheduler_manager.py:622-623` assert `"test_running" not in model.state` — both pass in the 517-test suite

Additional fixes:
- `834794b`: `battery-health.py` reads `capacity_ah_measured` (top-level model key) not `measured_capacity_ah` (per-event key) — confirmed at line 96
- `41b7275` + `3176810`: timestamp corruption tolerance in `_calculate_days_since_last_attempt` and `_calculate_days_since_last_test_success` — `except (ValueError, TypeError)` guards present at lines 297 and 332

---

### Human Verification Required

None. All must-haves are verifiable programmatically. The phase is daemon-internal (scheduler logic, data pipeline cleanup, docs) with no UI behavior to assess.

---

### Gaps Summary

No gaps. All 10 must-haves verified. All 10 requirement IDs satisfied. The post-CR fixes (CR-01 test_running removal, capacity_ah_measured key correction, timestamp tolerance) are confirmed applied and regression-tested. Full suite: 517 passed.

---

_Verified: 2026-06-04T21:15:56Z_
_Verifier: Claude (gsd-verifier)_
