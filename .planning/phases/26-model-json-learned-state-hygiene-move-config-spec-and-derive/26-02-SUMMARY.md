---
phase: 26-model-json-learned-state-hygiene-move-config-spec-and-derive
plan: "02"
subsystem: model-persistence
tags: [hygiene, model-json, derived-cache, replacement-due, convergence-gate, refactor]
dependency_graph:
  requires: [26-01]
  provides: [slim-ModelState, live-replacement-due, convergence-gate, shared-baseline-helper, deploy-strip-documented]
  affects: [src/model.py, src/monitor.py, src/discharge_handler.py, src/scheduler_manager.py, src/virtual_ups_exporter.py, src/motd_status.py, scripts/battery-health.py, tests/test_model.py, tests/test_health_endpoint_v16.py, tests/test_motd_status.py, tests/test_monitor_integration.py]
tech_stack:
  added: []
  patterns: [live-recompute-on-read, config-injection, shared-baseline-helper, convergence-gate-mirror]
key_files:
  created: []
  modified:
    - src/model.py
    - src/monitor.py
    - src/discharge_handler.py
    - src/scheduler_manager.py
    - scripts/battery-health.py
    - tests/test_model.py
    - tests/test_motd_status.py
    - tests/test_monitor_integration.py
decisions:
  - "compute_replacement_due() returns None when get_convergence_status().converged is False — mirrors the OLD discharge_handler gate (discharge_handler.py:218-219); soh_history and capacity_estimates are INDEPENDENT arrays, so non-converged capacity does not mean no soh_history (cycle-2 HIGH)"
  - "latest_capacity_ah_ref() is a module-level helper shared by compute_replacement_due() and battery-health.py — single baseline selector prevents mixed-baseline divergence (review HIGH #3)"
  - "soh_threshold injected into BatteryModel (default 0.80); monitor.py passes config.soh_alert_threshold so the live recompute reproduces the old persisted value for ALL configured thresholds, not only 0.80 (review HIGH #2)"
  - "Scheduling output (scheduled_test_timestamp/reason/test_block_reason) is health.json-only; no model.json mirror; comment in scheduler_manager rewritten accordingly"
  - "ACCEPTED: standalone MOTD (motd_status.py) uses default soh_threshold 0.80 — for non-default soh_alert the MOTD replacement date may differ from the daemon NUT value; accepted (MOTD is read-only, 0.80 is the IEEE standard, YAGNI per CONTEXT.md)"
  - "ACCEPTED: battery-health.py reports at the 0.80 default threshold (no config.toml loader added — YAGNI; consistent with pre-refactor behavior)"
  - "Deploy strip applied on 2026-06-04: removed replacement_due, scheduled_test_reason, scheduled_test_timestamp, test_block_reason from deployed model.json; full_capacity_ah_ref was already absent (stripped in wave-1 deploy); daemon restarted successfully"
metrics:
  duration: "16 min"
  completed: "2026-06-04"
  tasks_completed: 3
  tasks_total: 3
  files_changed: 8
---

# Phase 26 Plan 02: derived-cache keys removed from model.json — live recompute with convergence gate Summary

Stopped persisting five derived-cache keys (`scheduled_test_timestamp`, `scheduled_test_reason`, `test_block_reason`, `capacity_converged`, `replacement_due`) from model.json. Added `compute_replacement_due()` with a convergence gate mirroring the old discharge_handler path, a `soh_threshold`-injected BatteryModel, and a `latest_capacity_ah_ref()` shared baseline helper. Proved equivalence for non-0.80 thresholds and mixed-baseline histories. Deployed the one-time key strip on the production model.json.

## Tasks Completed

| Task | Description | Commit | Files |
|------|-------------|--------|-------|
| 1 | Drop scheduled_test_*/test_block_reason/capacity_converged persistence (HYG-03) | a12c4e1 | model.py, scheduler_manager.py, discharge_handler.py |
| 2 | Recompute replacement_due live with configured threshold + shared baseline (HYG-03/04) | 7bd3dcf | model.py, monitor.py, discharge_handler.py, battery-health.py, test_motd_status.py |
| 3 | Dead test removal + equivalence/baseline/loader gate tests (HYG-05) | 331966c | test_model.py, test_monitor_integration.py, battery-health.py |

## What Was Built

**Task 1 — Drop scheduling/convergence persistence:**
- `ModelState`: removed `capacity_converged`, `replacement_due`, `scheduled_test_timestamp`, `scheduled_test_reason`, `test_block_reason`
- `_apply_defaults`: removed setdefaults for the three scheduling keys
- `_validate_and_clamp_fields`: removed `scheduled_test_*` / `test_block_reason` from string-field loop; kept `last_upscmd_*`
- `update_scheduling_state()` method deleted entirely; no callers remain
- `scheduler_manager.py`: removed `update_scheduling_state()` call; rewritten NOT-A-BUG comment to state "health.json-only scheduling, no model.json mirror — nothing to reconcile"
- `discharge_handler._handle_capacity_convergence`: removed `state["capacity_converged"] = True` write; health endpoint reads live `get_convergence_status().converged`

**Task 2 — Live replacement_due recompute:**
- `BatteryModel.__init__`: added `soh_threshold: float = 0.80` parameter; sets `self.soh_threshold` before `load()`
- `latest_capacity_ah_ref(soh_history)`: module-level helper returning `capacity_ah_ref` of the last entry, or None; used by both `compute_replacement_due()` and `battery-health.py`
- `compute_replacement_due()`: returns None when `get_convergence_status().converged` is False (cycle-2 HIGH gate); otherwise calls `linear_regression_soh(soh_hist, threshold_soh=self.soh_threshold, capacity_ah_ref=latest_capacity_ah_ref(soh_hist))` and returns the date
- `get_replacement_due()`: delegates to `compute_replacement_due()` — name kept for call-site compatibility; `set_replacement_due()` removed
- `monitor.py`: `BatteryModel(..., soh_threshold=config.soh_alert_threshold)` — live recompute uses the same configured threshold the old discharge_handler path used
- `discharge_handler._predict_replacement`: removed `set_replacement_due()` calls; still returns the prediction tuple for `_check_alerts`; docstring updated
- `battery-health.py`: added convergence gate (`compute_cov` on `capacity_estimates`, same formula as `get_convergence_status`); uses shared `latest_capacity_ah_ref` helper; removed `scheduled_test_timestamp` fallback (health.json-only)

**Task 3 — Test cleanup + new gates:**
- Removed dead tests: `scheduled_test_*` round-trip/validation (8 tests), `set_replacement_due` persistence test, `capacity_converged` from `bool_keys`; fixed `test_lifecycle_round_trip` and `test_validate_valid_fields_no_warnings`
- `test_monitor_integration.py`: removed `state["capacity_converged"] = True` seed (live from estimates now)
- `test_motd_status.py`: updated `test_soh_and_replacement_due_surface` to use converged fixture (live-compute path) instead of seeded persisted key
- New: `TestComputeReplacementDueEquivalence` — parametrized over `[0.80, 0.75]`; catches hardcoded-0.80 regression
- New: `TestComputeReplacementDueConvergenceGate` (T-26-08) — non-converged fixture self-validates `converged=False`, asserts None; converged twin asserts not None
- New: `TestLatestCapacityAhRefBaseline` — mixed-baseline fixture with converged estimates; proves `latest_capacity_ah_ref` filter produces different date than all-entries
- New: `TestRegenLoaderGates` — fresh-save-loads-clean + strip-then-loads-clean-with-learned-keys (peukert_exponent and soh_history survive the strip)

## Deploy Note (HYG-05) — ONE-TIME STRIP APPLIED 2026-06-04

The strict loader rejects any model.json carrying the removed keys. Deploy sequence (no migration code):

```bash
sudo systemctl stop ups-battery-monitor

python3 - <<'EOF'
import json
from pathlib import Path
model_path = Path.home() / ".config" / "ups-battery-monitor" / "model.json"
data = json.loads(model_path.read_text())
for key in ("full_capacity_ah_ref", "replacement_due", "capacity_converged",
            "scheduled_test_timestamp", "scheduled_test_reason", "test_block_reason"):
    data.pop(key, None)
physics = data.get("physics", {})
physics.pop("nominal_voltage", None)
physics.pop("nominal_power_watts", None)
model_path.write_text(json.dumps(data, indent=2))
print("Strip complete")
EOF

sudo systemctl start ups-battery-monitor
```

**This strip was applied on 2026-06-04.** The pre-existing backup is at `~/.config/ups-battery-monitor/model.json.pre-v3.2-cleanup.bak`. Learned state (`soh`, `soh_history`, `capacity_estimates`, `physics.peukert_exponent/ir_compensation/rls_state`, `capacity_ah_measured`, `battery_install_date`, counters) is preserved by the surgical strip.

## Threshold Design Decision

`replacement_due` now recomputes live at `self.soh_threshold` (= `config.soh_alert`). For a customized `soh_alert`, the FIRST poll after deploy may shift the predicted date versus the last persisted value (which was computed at whatever threshold was configured when the discharge completed) — this is correct, not drift, because the live value tracks the current configured threshold.

## Convergence Gate Decision (cycle-2 HIGH, T-26-08)

`compute_replacement_due()` returns None unless `get_convergence_status().converged`, exactly mirroring the old `discharge_handler` gate (`discharge_handler.py:218-219`). `soh_history` and `capacity_estimates` are INDEPENDENT arrays (model.py:36/37), so a model with regression-quality `soh_history` but non-converged `capacity_estimates` (reachable for the default-config user) returns None — matching the old persisted None even for the default user.

## Accepted Reporting-Tool Divergences (non-default configs only)

Both affect only non-default configurations; the operator runs the default 7.2Ah battery at the default 0.80 threshold, so neither applies to this host:

1. **Standalone MOTD** (`motd_status.py` builds `BatteryModel` with no config): uses default `soh_threshold=0.80`. For a customized `soh_alert != 0.80` the MOTD banner's replacement date differs from the daemon's NUT value — accepted (MOTD is read-only, 0.80 is the IEEE standard).
2. **battery-health.py** reports rated capacity from `RATED_CAPACITY_AH` constant (7.2) and predicts at the 0.80 default. For a non-default `capacity_ah`/`soh_alert` the operator report can differ from NUT — accepted (YAGNI: no config-loader added to CLI/MOTD per CONTEXT.md scope; battery-health already read 7.2 pre-refactor, so this is not a regression).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] test_motd_status.test_soh_and_replacement_due_surface seeded removed key**
- **Found during:** Task 2 verification
- **Issue:** Test wrote `replacement_due` directly into model.json fixture — that key is now rejected by the strict loader
- **Fix:** Rewrote test to use converged capacity_estimates + regression-quality soh_history so `compute_replacement_due()` returns a live date; asserts `fields["replacement_due"] != ""`
- **Files modified:** tests/test_motd_status.py
- **Commit:** 7bd3dcf

**2. [Rule 1 - Bug] test_monitor_integration.py seeded state["capacity_converged"]=True (removed key)**
- **Found during:** Task 3
- **Issue:** Line 291 set `state["capacity_converged"] = True` — key no longer in schema; would cause ModelLoadError on any reload
- **Fix:** Removed the assignment; added comment that the three converged estimates (CoV << 0.10) already make `converged=True` live
- **Files modified:** tests/test_monitor_integration.py
- **Commit:** 331966c

**3. [Rule 1 - Bug] TestLatestCapacityAhRefBaseline: all_entries_result was None (subscript error)**
- **Found during:** Task 3 test run
- **Issue:** Mixed-baseline history with old-declining + new-healthy entries fails R²<0.5 (V-shaped pattern), so `linear_regression_soh(..., capacity_ah_ref=None)` returns None — subscripting `None[3]` raised TypeError
- **Fix:** Changed comparison to `all_entries_date = all_entries_result[3] if all_entries_result is not None else None`; the assertion still holds (latest-only date != None proves filter is applied)
- **Files modified:** tests/test_model.py
- **Commit:** 331966c

**4. [Rule 3 - Blocking] Import order + unused variable caught by ruff**
- **Found during:** Task 3 ruff check
- **Issues:** (a) `from src import replacement_predictor` was placed after third-party imports (I001); (b) `import copy` in test was unused (F401); (c) `m2 = BatteryModel(...)` in test was unused (F841)
- **Fix:** Reordered import; removed unused import and variable
- **Files modified:** src/model.py, tests/test_model.py
- **Commit:** 331966c

## Known Stubs

None. All callers receive live values from `compute_replacement_due()` / `get_replacement_due()`. The MOTD and battery-health.py use the 0.80 default (documented accepted divergence above, not a stub).

## Threat Flags

No new security-relevant surface introduced. This is a pure in-place refactor removing persistence of derived-cache values and adding a live-recompute path. All threat mitigations from the plan's STRIDE register were applied:
- T-26-03 (strict loader rejects stale file): deployed strip applied 2026-06-04; daemon active
- T-26-04 (soh_threshold equivalence): parametrized test at [0.80, 0.75] gates regression
- T-26-07 (mixed-baseline divergence): shared `latest_capacity_ah_ref` helper; mixed-baseline test
- T-26-08 (convergence gate divergence): gate implemented; T-26-08 test with self-validating fixture

## Self-Check: PASSED

- `26-02-SUMMARY.md`: FOUND (this file)
- Commit a12c4e1 (Task 1): FOUND
- Commit 7bd3dcf (Task 2): FOUND
- Commit 331966c (Task 3): FOUND
- `rg "update_scheduling_state|set_replacement_due" src/`: CLEAN
- `rg "state\[.capacity_converged" src/`: CLEAN
- `uv run pytest -q`: 535 passed
- `uvx ruff check src tests scripts`: All checks passed
- Daemon status: active
