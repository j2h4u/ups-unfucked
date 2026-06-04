---
phase: 26-model-json-learned-state-hygiene-move-config-spec-and-derive
reviewed: 2026-06-04T00:00:00Z
depth: standard
files_reviewed: 10
files_reviewed_list:
  - scripts/battery-health.py
  - src/battery_math/constants.py
  - src/discharge_handler.py
  - src/model.py
  - src/monitor.py
  - src/scheduler_manager.py
  - tests/test_model.py
  - tests/test_monitor_integration.py
  - tests/test_motd.py
  - tests/test_motd_status.py
findings:
  critical: 0
  warning: 4
  info: 4
  total: 8
status: issues_found
---

# Phase 26: Code Review Report

**Reviewed:** 2026-06-04T00:00:00Z
**Depth:** standard
**Files Reviewed:** 10
**Status:** issues_found

## Summary

Phase 26 removes derived/config keys (`replacement_due`, `capacity_converged`,
`scheduled_test_timestamp`/`reason`, `test_block_reason`, `full_capacity_ah_ref`,
physics `nominal_*`) from the persisted `model.json` and replaces them with live
recomputation. The core mechanism is sound:

- `compute_replacement_due()` (model.py:545) gates on `get_convergence_status().converged`
  first, exactly mirroring the old `discharge_handler._predict_replacement` gate, then
  feeds `self.soh_threshold` (the configured value, NOT a hardcoded 0.80) into
  `linear_regression_soh`. The parametrized equivalence test (test_model.py:1213) proves
  the t=0.75 case would diverge if 0.80 were hardcoded — good coverage.
- The shared `latest_capacity_ah_ref` helper (model.py:64) is used by BOTH `model.py` and
  `battery-health.py`, closing the mixed-baseline divergence (HIGH #3). Verified against
  the mixed-baseline test (test_model.py:1332).
- The strict loader (`_reject_unknown_state_keys`) correctly rejects every removed key, and
  `save()` emits only schema-compliant keys, so a freshly regenerated `model.json` round-trips
  clean (test_model.py:1406 / :1426).
- No remaining production reads of the removed persisted keys: the only surviving references
  are docstrings, comments, and the runtime `HealthSnapshot.capacity_converged` field
  (monitor_config.py:301) which is a per-poll health-endpoint output, not persisted model state.

The defects below are correctness-adjacent inconsistencies and quality issues, not blockers.
The most material is the threshold divergence between the daemon's live recompute and the
two read-only reporting paths (battery-health.py, motd_status.py), which the task explicitly
flagged for scrutiny.

## Warnings

### WR-01: battery-health.py replacement date diverges from the daemon for non-default `soh_alert_threshold`

**File:** `scripts/battery-health.py:206` (also `src/motd_status.py:72` via the same 0.80 default)
**Issue:** The daemon's `compute_replacement_due()` uses `self.soh_threshold`, injected from
`config.soh_alert_threshold` (model.py:571, monitor.py:131). `battery-health.py` hardcodes
`threshold_soh=0.80` (line 206). For any operator who sets `soh_alert_threshold` to a
non-default value (e.g. 0.75 — and the project's own equivalence test parametrizes exactly
that), the CLI report and the daemon's exported `battery.replacement.due` will print
DIFFERENT replacement dates from identical state. The phase's stated goal is "live recompute
reproduces the OLD persisted value for ALL configured soh_alert thresholds, not only 0.80"
(model.py:558) — battery-health.py does not meet that bar.

The inline comment (battery-health.py:192-194) acknowledges this and defers it as "YAGNI: no
config loader added to CLI/MOTD per CONTEXT.md scope." That is a deliberate scope decision,
but it leaves a user-visible inconsistency between two surfaces that both claim to "mirror the
daemon exactly" (battery-health.py:189, :202). At minimum the divergence should be surfaced to
the operator, or the threshold read from the health endpoint the daemon already writes.

**Fix:** Read the threshold from the daemon's health endpoint (which is already loaded as
`health_data`) instead of hardcoding, so the CLI tracks whatever the daemon computed:
```python
# daemon writes soh_alert_threshold into the health snapshot; fall back to 0.80 only
# when the endpoint is unavailable (standalone/no-daemon run)
threshold = health_data.get("soh_alert_threshold", 0.80)
replacement_prediction = linear_regression_soh(
    soh_history,
    threshold_soh=threshold,
    capacity_ah_ref=latest_capacity_ah_ref(soh_history),
)
```
If keeping the hardcode is intentional, change the docstrings at lines 189 and 202 to stop
claiming the path "mirrors the daemon exactly" and add a printed caveat when a measured
threshold cannot be confirmed.

### WR-02: battery-health.py convergence gate silently diverges from the daemon when an estimate lacks `ah_estimate`

**File:** `scripts/battery-health.py:197-198`
**Issue:** The CLI recomputes convergence as
`ah_values = [e['ah_estimate'] for e in capacity_estimates if 'ah_estimate' in e]` then
`len(ah_values) >= 3 and compute_cov(ah_values) < 0.10`. The daemon's
`get_convergence_status()` (model.py:846) instead does `[e["ah_estimate"] for e in estimates]`
with NO `if 'ah_estimate' in e` filter — a missing key raises `KeyError` there, whereas the
CLI silently drops the entry. If a corrupt/partial estimate entry exists, the daemon and the
CLI will disagree on `converged` (daemon errors/excludes-by-crash vs CLI quietly shrinks the
sample set), producing inconsistent "Replace battery" output versus the daemon's exported
value. The two convergence definitions are reimplemented independently rather than sharing a
single function, so they can drift in exactly this way.

**Fix:** Extract the convergence predicate into a shared helper (alongside
`latest_capacity_ah_ref` in model.py) and call it from both `get_convergence_status()` and
battery-health.py, so the sample-selection and `cov < 0.10` rule have one definition:
```python
# src/model.py
def is_capacity_converged(estimates: list) -> bool:
    ah = [e["ah_estimate"] for e in estimates if "ah_estimate" in e]
    return len(ah) >= 3 and compute_cov(ah) < 0.10
```
and reuse it in `get_convergence_status()` so both paths handle malformed entries identically.

### WR-03: `add_capacity_estimate` swallows a real persistence failure as a logged error

**File:** `src/model.py:792-799`
**Issue:** `add_capacity_estimate` calls `self.save()` inside a `try/except (OSError, TypeError,
ValueError)` that logs and returns. The capacity estimate has already been appended to
`self.state["capacity_estimates"]` in memory (line 790) BEFORE the save attempt. On a save
failure the in-memory model now contains an estimate that is not on disk; the next successful
`save()` (e.g. from a later poll) will persist it, but if the daemon restarts first the estimate
is lost — and `has_converged()` replay on restart (monitor.py:149) will see a different sample
count than the running daemon did. This is a silent in-memory/on-disk divergence in learned
state that this phase is specifically trying to keep clean. The docstring even notes "may
silently fail on OSError" (line 779) as if expected.

**Fix:** Either let the save error propagate (the caller `handle_discharge_complete` already
runs inside the daemon's loop-level error handling), or roll back the in-memory append on
failure so memory and disk stay consistent:
```python
self.state["capacity_estimates"].append(entry)
self._cap_history_entries("capacity_estimates")
try:
    self.save()
except (OSError, TypeError, ValueError):
    self.state["capacity_estimates"].pop()  # keep memory == disk
    raise
```

### WR-04: `_reset_battery_baseline` and `_handle_capacity_convergence` format `.2f` on values that could be None on corrupt state

**File:** `src/monitor.py:385-387` and `src/discharge_handler.py:469`
**Issue:** In `_reset_battery_baseline`, `old_capacity = state.get("capacity_ah_measured")`
(monitor.py:363) and the f-string `f"...from {old_capacity:.2f}Ah..."` (line 385) is guarded by
`if old_capacity is not None`, so that one is safe. But `_handle_capacity_convergence`
(discharge_handler.py:469) formats `convergence_status.latest_ah:.2f` and
`current_measured = convergence_status.latest_ah` (line 486) is later used in
`abs(current_measured - stored_baseline)` (line 490). `latest_ah` is `None` whenever
`capacity_estimates` is empty (model.py:838). The path is reached only after
`self.capacity_estimator.has_converged()` (line 453), and convergence requires >=3 estimates,
so in normal operation `latest_ah` is non-None. However, `has_converged()` (the estimator's own
tracker) and `get_convergence_status()` (the model's recompute) are two INDEPENDENT sources of
truth — if they ever disagree (estimator says converged, model's `capacity_estimates` is empty
because the model was reset/reloaded between the two calls), line 469 raises
`TypeError: unsupported format string passed to NoneType`. This is fragile coupling introduced
by deriving convergence live in two places.

**Fix:** Guard the format / fail fast with a clear message rather than a format TypeError:
```python
if convergence_status.latest_ah is None:
    logger.error("convergence/estimator disagreement: latest_ah is None at lock",
                 extra={"event_type": "convergence_state_mismatch"})
    return
```
Or assert the invariant explicitly so the disagreement is diagnosable.

## Info

### IN-01: Duplicate hardcoded VRLA voltage constants instead of using `constants.py`

**File:** `src/discharge_handler.py:650-651`
**Issue:** `_estimate_dod_from_buffer` hardcodes `v_nominal = 12.0` and `v_floor = 10.5` with
the comment "CyberPower UT850: nominal voltage 12V". Phase 26 just introduced
`NOMINAL_VOLTAGE = 12.0` in `constants.py` (line 10) explicitly to replace "the scattered magic
12.0". This is exactly such a scattered literal that was missed by the consolidation.
**Fix:** Import and use `from src.battery_math.constants import NOMINAL_VOLTAGE` for `v_nominal`;
keep 10.5 as a named local (e.g. `V_CUTOFF`) or pull from the LUT anchor.

### IN-02: `get_replacement_due` is a pure pass-through retained only for call-site naming

**File:** `src/model.py:575-582`
**Issue:** `get_replacement_due()` does nothing but `return self.compute_replacement_due()`.
The docstring says the name is "kept for backward call-site compatibility," but the project's
own policy (MEMORY: `feedback_no_backward_compat`) is "no compat shims, single-user daemon."
Two public methods that are exact synonyms invite confusion about which is canonical.
**Fix:** Rename the two call sites (virtual_ups_exporter.py:77, motd_status.py:72) to call
`compute_replacement_due()` directly and delete `get_replacement_due()`.

### IN-03: Stale docstring references a removed fallback in `print_maintenance`

**File:** `scripts/battery-health.py:28-29`
**Issue:** The `print_maintenance` docstring still says next_test_timestamp "falls back to
model.json scheduled_test_timestamp" (lines 28-29), but the code body was changed to read
health.json only (line 42) and the comment at lines 40-41 correctly documents the removal. The
docstring contradicts the code.
**Fix:** Update the docstring bullet to "next_test_timestamp: health.json only (model.json
scheduled_test_timestamp removed in HYG-03)."

### IN-04: `_estimate_dod_from_buffer` voltage-swing heuristic can under-report DoD, feeding `discharge_events`

**File:** `src/discharge_handler.py:646-654`
**Issue:** DoD is computed as `(v_max - v_min) / (v_nominal - v_floor)` using the buffer's raw
voltage extremes. `v_max` is whatever the highest sampled voltage was during the discharge
buffer, not the resting/float voltage, so a discharge that begins already sagged reports a
smaller swing and thus a smaller DoD. This `depth_of_discharge` is persisted into
`discharge_events` (discharge_handler.py:131) and later drives `_get_last_natural_blackout`
depth and `_calculate_days_since_deep`'s `> 0.7` gate (line 529) — an under-reported DoD can
cause a genuinely deep discharge to be misclassified as shallow, deferring the deep-discharge
cadence. The docstring honestly labels it "heuristic, not true DoD," so this is documented
debt, not a regression, but it interacts with scheduling decisions.
**Fix (optional, out of phase scope):** Compute DoD via SoC lookup against the LUT (the model
instance is available) rather than the voltage-swing proxy, or anchor `v_max` to the LUT
full-charge voltage rather than the observed buffer max.

---

_Reviewed: 2026-06-04T00:00:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
