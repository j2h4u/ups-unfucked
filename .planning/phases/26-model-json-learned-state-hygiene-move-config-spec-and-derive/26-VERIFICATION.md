---
phase: 26-model-json-learned-state-hygiene-move-config-spec-and-derive
verified: 2026-06-04T09:58:42Z
status: passed
score: 8/8 must-haves verified
overrides_applied: 0
---

# Phase 26: model.json Learned-State Hygiene Verification Report

**Phase Goal:** model.json persists ONLY learned per-battery state (category ③). Configuration/spec (category ①) is read from config.toml/constants.py at runtime and not persisted; derived caches (category ②) are recomputed and not persisted. The ModelState schema shrinks to learned-state-only, and the strict loader still passes against a real on-disk file after a one-time key strip.

**Verified:** 2026-06-04T09:58:42Z
**Status:** passed
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `full_capacity_ah_ref`, `physics.nominal_voltage`, `physics.nominal_power_watts` removed from persisted schema (HYG-01) | VERIFIED | `rg "full_capacity_ah_ref" src/ scripts/` → exit 1 (clean); runtime save() shows top-level and physics keys contain none of these; ModelState TypedDict confirmed clean |
| 2 | `get_capacity_ah()` returns config-injected `self.capacity_ah`; `get_nominal_voltage()` / `get_nominal_power_watts()` return constants (HYG-02) | VERIFIED | Runtime: `BatteryModel(path, capacity_ah=9.0).get_capacity_ah()` → 9.0; `get_nominal_voltage()` → 12.0; `get_nominal_power_watts()` → 425.0. `NOMINAL_VOLTAGE=12.0` in constants.py:10; imported in model.py:15 |
| 3 | `get_convergence_status().rated_ah` uses `self.capacity_ah` at both return sites (HYG-01/02 rated_ah propagation) | VERIFIED | Runtime: `BatteryModel(path, capacity_ah=9.0).get_convergence_status().rated_ah` → 9.0 (empty-estimates branch). All 4 propagation tests pass: `test_rated_ah_propagation_empty_model`, `test_rated_ah_propagation_populated_model`, `test_rated_ah_propagation_default` |
| 4 | `scheduled_test_timestamp`, `scheduled_test_reason`, `test_block_reason`, `capacity_converged`, `replacement_due` removed from persisted schema (HYG-03) | VERIFIED | `rg "scheduled_test_timestamp|scheduled_test_reason|test_block_reason|update_scheduling_state|set_replacement_due" src/` → only a comment line (not code); `rg "state\[.capacity_converged" src/` → clean; KNOWN_STATE_KEYS runtime check confirms all 5 absent |
| 5 | `compute_replacement_due()` with convergence gate recomputes live; `get_replacement_due()` delegates to it; `set_replacement_due` removed (HYG-03/04) | VERIFIED | `hasattr(model, 'set_replacement_due')` → False; `compute_replacement_due()` returns None on empty data; `get_replacement_due()` delegates to compute; runtime verified |
| 6 | `compute_replacement_due()` returns None when `get_convergence_status().converged` is False — mirrors old discharge_handler gate (T-26-08, HYG-04) | VERIFIED | Runtime test: regression-quality soh_history + non-converged capacity_estimates → `converged=False`, `compute_replacement_due()=None`. `TestComputeReplacementDueConvergenceGate` both cases pass |
| 7 | Equivalence: live `compute_replacement_due()` matches direct `linear_regression_soh` at both 0.80 and 0.75 thresholds; `battery-health.py` uses shared `latest_capacity_ah_ref` helper and same convergence gate (HYG-04) | VERIFIED | `TestComputeReplacementDueEquivalence` parametrized [0.80, 0.75] passes; `TestLatestCapacityAhRefBaseline.test_mixed_baseline_selects_latest` passes; `battery-health.py` imports `latest_capacity_ah_ref` from `src.model` (line 15) and uses `compute_cov` convergence gate (lines 14, 190-200) |
| 8 | Strict loader passes on freshly-regenerated file; old-schema file raises on load; strip-then-load preserves learned keys; deploy strip applied (HYG-05) | VERIFIED | `TestRegenLoaderGates.test_fresh_save_loads_clean` PASS; `test_old_schema_raises_on_load` PASS; `test_strip_then_load_clean_and_learned_keys_survive` PASS; 26-02-SUMMARY.md documents strip applied on 2026-06-04 with daemon restarted successfully |

**Score:** 8/8 truths verified

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/battery_math/constants.py` | NOMINAL_VOLTAGE = 12.0 constant | VERIFIED | Line 10: `NOMINAL_VOLTAGE = 12.0` |
| `src/model.py` | BatteryModel with capacity_ah + soh_threshold injection; slim ModelState; compute_replacement_due; latest_capacity_ah_ref helper | VERIFIED | __init__ takes both params; ModelState TypedDict has no removed keys; compute_replacement_due exists (line 545); latest_capacity_ah_ref module-level helper (line 67 area) |
| `src/monitor.py` | BatteryModel construction with capacity_ah=config.capacity_ah AND soh_threshold=config.soh_alert_threshold | VERIFIED | Lines 128-132 confirmed; soh_alert_threshold at lines 89, 131, 176 |
| `src/virtual_ups_exporter.py` | replacement_due from live get_replacement_due() | VERIFIED | Line 77: `replacement_due = self.battery_model.get_replacement_due() or ""` |
| `src/motd_status.py` | replacement_due from live get_replacement_due() | VERIFIED | Line 72: `model.get_replacement_due() or ""` |
| `src/scheduler_manager.py` | No update_scheduling_state call; last_* properties kept | VERIFIED | rg clean; last_scheduling_reason/last_next_test_timestamp properties intact (lines 192-207); comment updated |
| `src/discharge_handler.py` | No capacity_converged write; no set_replacement_due calls | VERIFIED | rg clean on both; comment at line 461 documents the removal |
| `scripts/battery-health.py` | Uses RATED_CAPACITY_AH constant; latest_capacity_ah_ref helper; convergence gate with compute_cov | VERIFIED | Lines 13-15 imports; line 97: `capacity_ah = RATED_CAPACITY_AH`; lines 196-200: convergence gate; line 201-206: shared baseline helper |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `src/monitor.py` | `src/model.py` | `BatteryModel(model_path, capacity_ah=config.capacity_ah, soh_threshold=config.soh_alert_threshold)` | WIRED | Lines 128-132 confirmed |
| `src/model.py` | `src/battery_math/constants.py` | `get_nominal_voltage/get_nominal_power_watts` return `NOMINAL_VOLTAGE`/`NOMINAL_POWER_WATTS` | WIRED | `NOMINAL_VOLTAGE` imported line 15; returned at line 516 |
| `src/virtual_ups_exporter.py` | `src/model.py` | `compute_replacement_due()` via `get_replacement_due()` | WIRED | Line 77 calls `get_replacement_due()`; that method delegates to `compute_replacement_due()` (line 582) |
| `src/scheduler_manager.py` | `src/model.py` | `last_scheduling_reason`/`last_next_test_timestamp` updated; no model.json mirror | WIRED | Lines 159, 402 confirmed; `update_scheduling_state` call removed |
| `scripts/battery-health.py` | `src/model.py` | `latest_capacity_ah_ref` shared helper | WIRED | Line 15 import; line 206 call |

---

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|--------------|--------|--------------------|--------|
| `src/virtual_ups_exporter.py` | `replacement_due` | `self.battery_model.get_replacement_due()` → `compute_replacement_due()` → `linear_regression_soh(live soh_history)` | Yes — live soh_history from model state | FLOWING |
| `src/motd_status.py` | `replacement_due` | `model.get_replacement_due()` → same compute path | Yes — standalone model with default soh_threshold=0.80 | FLOWING |
| `scripts/battery-health.py` | `capacity_ah` (rated) | `RATED_CAPACITY_AH` constant (7.2) | Yes — constant, always non-None | FLOWING |
| `scripts/battery-health.py` | replacement date | `linear_regression_soh(soh_history, ..., latest_capacity_ah_ref(soh_history))` gated on convergence | Yes — live from model.json soh_history | FLOWING |

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| battery-health.py runs without error and shows rated capacity from constant | `python3 scripts/battery-health.py` | Prints "Rated capacity: 7.2 Ah" with exit 0 | PASS |
| Saved model.json has no removed keys | Runtime: save() then inspect JSON keys | Top-level: no removed keys; physics: only {ir_compensation, peukert_exponent, rls_state} | PASS |
| capacity_ah injection flows through to rated_ah | Runtime: `BatteryModel(path, capacity_ah=9.0).get_convergence_status().rated_ah` | 9.0 | PASS |
| compute_replacement_due returns None without convergence | Runtime: regression-quality soh_history + non-converged estimates | None | PASS |

---

### Probe Execution

Step 7c: SKIPPED — no `scripts/*/tests/probe-*.sh` files exist for this phase; phase is a pure persistence refactor with no standalone probe scripts.

---

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| HYG-01 | 26-01-PLAN | Remove full_capacity_ah_ref, physics.nominal_voltage, physics.nominal_power_watts from persisted schema; split physics blob | SATISFIED | ModelState TypedDict clean; save() output confirmed; rg gates clean |
| HYG-02 | 26-01-PLAN | get_capacity_ah() from config injection; get_nominal_voltage/power from constants.py; all callers unaffected | SATISFIED | Runtime getters return correct values; NOMINAL_VOLTAGE in constants; 535 tests green |
| HYG-03 | 26-02-PLAN | Stop persisting scheduled_test_*, test_block_reason, capacity_converged, replacement_due | SATISFIED | All removed from ModelState; discharge_handler/scheduler writes removed; rg gates clean |
| HYG-04 | 26-02-PLAN | Recompute category-② values live; same values for fixed inputs | SATISFIED | compute_replacement_due() with convergence gate; battery-health.py with same gate and helper; equivalence tests at 0.80 and 0.75 pass |
| HYG-05 | 26-02-PLAN | Update/remove dead tests; strict loader passes on regenerated file; deploy note documented | SATISFIED | TestRegenLoaderGates and TestComputeReplacementDueEquivalence/ConvergenceGate added; dead persistence tests removed; strip applied 2026-06-04 documented in SUMMARY |

---

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/battery_math/calibration.py` | 15 | `unused variable 'current_exponent'` (vulture 100%) | INFO | Pre-existing, unrelated to phase 26 |
| `src/model.py` | 37,44,46,47,48,54,55,56 | vulture 60% "unused variable" on TypedDict fields | INFO | False positives — vulture cannot see TypedDict field access patterns; these are category-③ learned keys that were intentionally kept; not a phase-26 issue |

Note: vulture exit code 3 reflects these pre-existing / false-positive hits. The plan's `uvx vulture src` gate is defined as "clean" for production dead code; TypedDict false positives at 60% confidence are a known limitation and do not constitute blockers. No TBD/FIXME/XXX markers found in phase-modified files.

---

### Human Verification Required

None. All observable truths were verified programmatically.

---

### Gaps Summary

No gaps. All 8 must-haves verified. The phase goal is fully achieved:

- All 8 removed keys (6 top-level + 2 physics spec sub-keys) are absent from ModelState, KNOWN_STATE_KEYS, save() output, and src/ source.
- Config/spec values (capacity_ah, nominal_voltage, nominal_power_watts) sourced live from constructor injection and constants — not persisted.
- Derived caches (replacement_due, capacity_converged, scheduling) computed live each poll — not persisted.
- Equivalence proven at non-default threshold (0.75) and mixed baseline.
- Convergence gate (T-26-08) prevents live/persisted divergence for default-config users.
- Full suite: 535 passed. ruff: all checks passed. Deploy strip applied and daemon active.

---

_Verified: 2026-06-04T09:58:42Z_
_Verifier: Claude (gsd-verifier)_
