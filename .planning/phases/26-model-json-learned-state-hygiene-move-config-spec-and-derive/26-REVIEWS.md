---
phase: 26
reviewers: [codex, opencode]
reviewed_at: 2026-06-04T03:20:00+06:00
cycles:
  - cycle: 1
    reviewed_at: 2026-06-04T03:20:00+06:00
    reviewers: [codex, opencode]
    high_raised: 3
    high_remaining: 3
  - cycle: 2
    reviewed_at: 2026-06-04T03:30:00+06:00
    reviewers: [codex, opencode]
    plans_revised_in: b0af9e3
    cycle1_high_resolved: 3   # rated_ah propagation, configurable replacement threshold, shared baseline filter
    high_new: 1               # compute_replacement_due drops the convergence gate
    high_remaining: 1
plans_reviewed: [26-01-PLAN.md, 26-02-PLAN.md]
---

# Cross-AI Plan Review — Phase 26

## Codex Review

I reviewed both plans against the current checkout. Overall: the phase decomposition is good and the sequential wave ordering is justified because both plans touch `src/model.py:22`. The main gaps are not in the core schema-removal idea, but in live-value equivalence: rated capacity propagation, replacement prediction threshold/baseline semantics, and tests for the actual stripped old file.

### 26-01-PLAN.md

**Summary**
Strong plan for removing category ① config/spec from persisted state. Constructor injection for `capacity_ah` and constant-backed nominal voltage/power are the right shape. One important gap: `get_convergence_status()` still hardcodes `RATED_CAPACITY_AH`, and the plan does not explicitly move health/MOTD rated-capacity output to the injected config value.

**Strengths**
- Correctly identifies `full_capacity_ah_ref` as redundant because monitor currently overwrites it from config on startup at `src/monitor.py:128`.
- Keeps getter signatures stable, which limits blast radius for `sag_tracker`, `discharge_handler`, `monitor`, and estimators.
- Splits the mixed `physics` blob without dropping learned fields.
- Good final gates: full pytest, ruff, vulture, and grep for removed persisted keys.

**Concerns**
- **HIGH:** `ConvergenceStatus.rated_ah` is still hardcoded to `RATED_CAPACITY_AH` at `src/model.py:789` and `src/model.py:806`; health uses that value at `src/virtual_ups_exporter.py:49`. If `capacity_ah=9.0`, health/MOTD can still show 7.2 unless this is changed.
- **MEDIUM:** Test updates are under-scoped. Old `full_capacity_ah_ref` fixtures also exist in `tests/test_motd.py`, `tests/test_monitor.py`, and `tests/test_monitor_integration.py`, not only `tests/test_model.py`.
- **LOW:** The plan says `NOMINAL_VOLTAGE` becomes the single source of truth, but defaults still hardcode `12.0` in math modules. That is not a persistence blocker, but the wording overpromises.

**Suggestions**
- Add `get_convergence_status()` coverage: `BatteryModel(path, capacity_ah=9.0).get_convergence_status().rated_ah == 9.0`.
- Add/update a health/MOTD capacity-rated test for non-default config or explicitly document that MOTD remains default-only.
- Expand Plan 01's test inventory to include `tests/test_motd.py`, `tests/test_monitor.py`, and `tests/test_monitor_integration.py`.

**Risk Assessment**
**MEDIUM.** The implementation path is straightforward, but missing rated-capacity propagation would violate the "config/spec from runtime config" goal for non-default batteries.

### 26-02-PLAN.md

**Summary**
The derived-cache removal is mostly well designed. `scheduled_test_*`, `test_block_reason`, and `capacity_converged` are genuinely redundant for health output. `replacement_due` is the hard part, and the plan handles the right general mechanism, but needs tighter equivalence rules around `soh_alert` and `capacity_ah_ref`.

**Strengths**
- Correctly keeps `last_upscmd_*` persisted while dropping scheduler output caches.
- Correctly preserves health `capacity_converged` as a live output rather than removing the health key.
- Removes dead persistence tests instead of preserving obsolete behavior.
- Adds the right kind of replacement prediction regression test and keeps vulture in the final gate.

**Concerns**
- **HIGH:** Old replacement prediction uses configurable `self.soh_threshold` at `src/discharge_handler.py:220`; the plan hardcodes `0.80`. That is equivalent only when `soh_alert=0.80`.
- **HIGH:** `scripts/battery-health.py` already computes replacement prediction directly at `scripts/battery-health.py:190`, but without the latest `capacity_ah_ref` filter. Mixed-baseline SoH history can diverge from the proposed model method.
- **MEDIUM:** The plan tests stale old-file rejection, but does not clearly add a "strip old file, then load succeeds and learned keys survive" test. That is part of the phase acceptance criteria.
- **MEDIUM:** `latest soh_history capacity_ah_ref` is reasonable, but `add_soh_history_entry()` rounds it to two decimals. If config capacity has more precision, live recompute may not exactly match the old persisted path.
- **LOW:** Plan 01 defers stale-file deploy handling to Plan 02. Fine for one combined phase, but the Plan 01 summary should say "do not deploy after wave 1 without stripping wave 1 keys."

**Suggestions**
- Prefer `compute_replacement_due(threshold_soh: float = 0.80)` and inject/pass the configured threshold from daemon paths. If hardcoded `0.80` is intentional, document that `replacement_due` is independent of `soh_alert`.
- Centralize latest-baseline selection in one helper and use it in `BatteryModel` and `battery-health.py`.
- Add three tests: non-default threshold equivalence, mixed `capacity_ah_ref` history, and stripped-old-file-loads-clean with learned keys preserved.
- Expand grep/test gates to include `tests` and `scripts`, not only `src`, for removed persisted keys.

**Risk Assessment**
**MEDIUM-HIGH.** The schema cleanup itself is low risk, but `replacement_due` is user-visible through NUT and MOTD. Without threshold/baseline tightening, the plan can produce silent value drift.

### Overall

The plans are directionally correct and should achieve Phase 26 after tightening the live-output equivalence tests. I would approve execution after adding explicit coverage for `capacity_ah_rated`, configurable replacement threshold behavior, `battery-health.py` baseline filtering, and stripped-file load success.

---

## OpenCode Review

### 1. Summary

Both plans implement a well-scoped hygiene refactor: removing non-learned data (config/spec and derived caches) from persisted `model.json` while preserving byte-equivalent outputs. The plans are grounded in precise `file:line` references from the canonical codebase, task ordering is correct (Wave 1 → Wave 2 due to shared `model.py` edits), and the Nyquist-critical `replacement_due` equivalence verification is correctly identified as the highest-risk signal. However, there are two substantive concerns: a silent behavior change risk around the `compute_replacement_due` threshold, and the battery-health.py capacity sourcing strategy that needs clarification on whether it pulls from config or falls back to a constant.

### 2. Strengths

- **Excellent code-groundedness**: Every task cites exact `src/model.py:NNN`, `src/monitor.py:NNN` lines, matching the actual codebase (verified against live reads — e.g., `_sync_physics_from_state` lines 310-311, `update_scheduling_state` at 875-877, `_predict_replacement` at 230-232).
- **Correct dependency ordering**: Wave 2 declares `depends_on: [26-01]` — both plans touch `src/model.py` so sequential execution is mandatory and correctly specified.
- **Nyquist validation properly identified**: The `replacement_due` equivalence test is the only signal whose live-recompute path is non-trivial.
- **Thorough grep gate inventory**: The verification sections are precise and complete — they cover all 6 top-level + 2 physics keys to be removed.
- **Deploy mechanic matches Phase 25 precedent**: Stop → strip-keys → start, backup exists, no migration code.
- **Physics blob split handled**: Correctly distinguishes learned physics keys from spec keys.

### 3. Concerns

#### 3.1 [MEDIUM] `compute_replacement_due` hardcodes `threshold_soh=0.80` — silent divergence if user configures `soh_alert != 0.80`

The old path in `_predict_replacement` (discharge_handler.py:222) used `self.soh_threshold` = `config.soh_alert`. If a user sets `soh_alert = 0.75`, the old persisted `replacement_due` was at 75%; after refactor the live recompute returns a value at 80% — a different date. The equivalence test uses 0.80 for both sides, so it passes, but doesn't catch the user-custom-threshold case. If hardcoding 0.80 is intentional, document it as a design decision in the SUMMARY. `motd_status.py` (standalone, no config) implicitly uses 0.80 — correct since MOTD has no config context.

#### 3.2 [MEDIUM] `battery-health.py` capacity sourcing strategy underspecified

The plan says to "Add a tiny helper that loads config.toml (reuse the same search order as monitor_config.load_config)" — 20-30 lines of mostly-duplicated logic for one field. YAGNI suggests: default to `RATED_CAPACITY_AH` with a comment, OR import `load_config` from `src.monitor_config`. Pick one concrete approach; don't leave it ambiguous. The replacement must produce a non-None value (current code can return None → empty).

#### 3.3 [LOW] `_reject_unknown_state_keys` doesn't reject `physics.nominal_voltage` / `physics.nominal_power_watts`

`_reject_unknown_state_keys` (model.py:334-350) only checks top-level keys. The physics sub-keys are inside the `physics` dict (which stays in KNOWN_STATE_KEYS) and will be silently ignored, not rejected. The old-file-rejected test still passes because the old file has removed top-level keys — but the test description implies physics sub-keys cause rejection when they don't. Harmless (sync ignores them after refactor); document the distinction.

#### 3.4 [LOW] `PhysicsParams` field removal may leave dead import references

The plan's verification only runs `sag_tracker` and `runtime_calculator` tests. A broader `rg "self\.physics\.nominal" src/` grep should be part of automated verification, not assumed safe. Currently confirmed only model.py accesses these — shift left into the verification step.

#### 3.5 [LOW] `update_scheduling_state` deletion vs `scheduler_manager` comment update

After deletion, the NOT-A-BUG comment's premise becomes moot — there's no longer a model.json copy to diverge from. The updated comment should simply explain "scheduling output is health.json-only."

### 4. Suggestions

4.1 Document the `threshold_soh=0.80` design decision explicitly in 26-02-SUMMARY.md: `replacement_due` now predicts at IEEE-standard 80% SoH independently of `soh_alert`; note first-poll shift if `soh_alert` was customized.
4.2 Simplify battery-health.py capacity sourcing: import `RATED_CAPACITY_AH` as fallback, or import `load_config`. Pick one.
4.3 Strengthen Plan 01 automated verification with `rg "self\.physics\.nominal" src/`.
4.4 Add a `compute_replacement_due() is None` test for soh_history with all-identical SoH (no variance → None).
4.5 Clarify the old-file-rejected test description: rejection is caused by top-level removed keys, not physics sub-keys.
4.6 Consider keeping `set_replacement_due` as a deprecated stub (zero-cost, YAGNI-optional).

### 5. Risk Assessment

**Overall: LOW.** Both plans are pure-refactor with no new behavioral paths — all computations deterministic, all callers identified and accounted for, the verification architecture (equivalence test + strict-loader gate + grep fence) is sound. The two MEDIUM concerns (hardcoded 0.80 threshold and battery-health.py capacity source ambiguity) are design-preference issues, not correctness bugs. Deploy strip is battle-tested from Phase 25. Highest-risk change has a dedicated regression test. No security exposure, no performance regression, no data loss.

---

## Consensus Summary

Both reviewers agree the phase decomposition is sound, the code-grounded `file:line` references are accurate, the Wave 1 → Wave 2 sequential ordering is correctly justified (shared `model.py` edits), and the `replacement_due` live-recompute is the highest-risk signal correctly flagged for equivalence testing. Both also independently flagged the two core equivalence gaps: the hardcoded `0.80` replacement threshold and the `battery-health.py` capacity sourcing strategy. They diverge on severity: Codex rates the threshold/baseline issues HIGH (replacement_due is user-visible via NUT + MOTD → silent value drift) and adds a third HIGH (rated_ah propagation in ConvergenceStatus); OpenCode rates the same threshold/capacity issues MEDIUM and the overall phase LOW risk.

### Agreed Strengths
- Excellent code-groundedness — every task cites exact, verified `file:line` references.
- Correct dependency ordering — Wave 2 `depends_on: [26-01]` is mandatory and specified.
- `replacement_due` correctly identified as the Nyquist-critical equivalence signal.
- Physics blob correctly split (learned keys kept, spec keys removed) rather than dropped wholesale.
- Deploy mechanic (stop → strip → start, backup present, no migration code) matches Phase 25 precedent.
- Thorough grep-gate inventory covering all 8 removed keys.

### Agreed Concerns (highest priority)
- **Replacement threshold hardcoded to 0.80** (Codex HIGH / OpenCode MEDIUM): old path used configurable `self.soh_threshold` = `config.soh_alert`; live recompute at fixed 0.80 diverges when `soh_alert != 0.80`. The equivalence test uses 0.80 on both sides so it won't catch this. Either pass the configured threshold through, or document the intentional decoupling (alert-threshold vs prediction-threshold) in the SUMMARY.
- **`battery-health.py` capacity sourcing underspecified** (Codex HIGH on baseline filter / OpenCode MEDIUM on config-load ambiguity): the script computes replacement prediction directly without the latest `capacity_ah_ref` filter (mixed-baseline divergence), and the config-loading helper is left ambiguous. Centralize the baseline-selection helper and pick one concrete capacity source.

### Divergent Views
- **Overall severity:** Codex → MEDIUM-HIGH (replacement_due is user-visible; silent drift without threshold/baseline tightening). OpenCode → LOW (pure deterministic refactor; the gaps are design-preference, daemon runs correctly in all cases).
- **`ConvergenceStatus.rated_ah` propagation (HIGH, Codex only):** Codex flags that `rated_ah` stays hardcoded to `RATED_CAPACITY_AH` at model.py:789/806, so health/MOTD show 7.2 even when `capacity_ah=9.0` is injected — a goal violation for non-default batteries. OpenCode did not raise this. Worth verifying against the code before execution.
- **`add_soh_history_entry()` 2-decimal rounding (MEDIUM, Codex only):** could break exact equivalence if config capacity carries more precision.

---
---

# Cross-AI Plan Review — Phase 26 — CYCLE 2

**Reviewed:** 2026-06-04T03:30:00+06:00 · **Reviewers:** codex, opencode · **Plans revised in:** b0af9e3

Cycle-2 context: the plans were revised after cycle 1 to address the 3 HIGH concerns — (1) rated_ah
propagation from injected capacity_ah, (2) configurable replacement threshold via `config.soh_alert`
with a parametrized equivalence test, (3) shared `latest_capacity_ah_ref` baseline filter for
battery-health.py. Both reviewers were asked to confirm those are fully resolved and to surface any new
HIGH concerns.

## Cycle-1 HIGH Resolution — Adjudicated

| Cycle-1 HIGH | Codex | OpenCode | Adjudication (verified vs source) |
|---|---|---|---|
| #1 rated_ah propagation | PARTIAL | FULLY | **FULLY RESOLVED.** Plan 01 sets `rated_ah=self.capacity_ah` at both return sites (truths + verify gate `rg rated_ah=RATED_CAPACITY_AH src/` empty) and adds a both-branches + default propagation test (T-26-06). Codex's "partial" caveat is the *reporting-tool* divergence (MOTD/battery-health), which is a separate documented MEDIUM design decision, not a failure of the rated_ah mechanism itself. Note: MOTD `capacity_rated_ah` already showed 7.2 pre-refactor because the old `rated_ah` was hardcoded — so this is not an MOTD regression. |
| #2 configurable replacement threshold | PARTIAL | FULLY | **FULLY RESOLVED (daemon path).** Plan 02 injects `soh_threshold=config.soh_alert_threshold` into `BatteryModel`; `compute_replacement_due()` uses `self.soh_threshold`; the parametrized `[0.80, 0.75]` equivalence test fails on a hardcoded 0.80 (T-26-04). Codex's "partial" is again the standalone-reporting path (MOTD/battery-health intentionally use 0.80) — documented MEDIUM, not the targeted defect. |
| #3 shared baseline filter | FULLY | FULLY | **FULLY RESOLVED.** Single `latest_capacity_ah_ref()` helper consumed by both `compute_replacement_due()` and battery-health.py; mixed-baseline test asserts latest-only fit differs from all-entries (T-26-07). |

All three cycle-1 HIGHs are **fully resolved** (verified against `src/model.py`, `src/discharge_handler.py`,
`src/replacement_predictor.py`, `src/motd_status.py`, `src/virtual_ups_exporter.py`).

## New HIGH (cycle 2) — `compute_replacement_due()` drops the convergence gate

**Raised by Codex; VERIFIED REAL against source; UNRESOLVED.**

The OLD persisted path (`src/discharge_handler.py:212-234` `_predict_replacement`) only computes/persists a
replacement date **`if convergence.converged`** (line 219); otherwise it persists `None`. Plan 02's new
`compute_replacement_due()` calls `replacement_predictor.linear_regression_soh(self.get_soh_history(), …)`
**unconditionally** — there is no `get_convergence_status().converged` guard in the plan's truths, action,
or tests.

`soh_history` (model.py:36, appended by `add_soh_history_entry`) and `capacity_estimates` (model.py:37,
the input to `get_convergence_status`) are **independent arrays**. A model can therefore hold a
regression-quality `soh_history` (≥3 points, R²≥0.5, negative slope) while `capacity_estimates` is
non-converged. In that reachable state:

- OLD persisted `replacement_due` = `None` → MOTD/NUT `battery.replacement.due` showed nothing.
- NEW live `compute_replacement_due()` = a real date.

This is a **value divergence that violates the HYG-04 "same values" contract for the DEFAULT-config user**
(the common case), not just non-default batteries. The cycle-2 equivalence test does not catch it because
its fixture is built converged. This HIGH is independent of the documented reporting-threshold MEDIUMs and
is not addressed anywhere in the revised plans.

**Fix direction (for replan):** make `compute_replacement_due()` return `None` when
`get_convergence_status().converged` is False (mirroring the old gate), and add a regression test with
`soh_history` ≥3 points but non-converged `capacity_estimates` asserting the live value equals the old
`None`. Decide explicitly whether `get_replacement_due` / NUT export should reflect the gate (recommended,
to preserve equivalence) — and confirm the standalone MOTD model, which has no capacity estimates injected,
behaves identically before/after.

## Codex Cycle-2 Review (verbatim)

**Cycle-1 HIGH Resolution Assessment**

- **HIGH #1 rated_ah propagation: PARTIALLY RESOLVED.** The model-level fix is well specified: 26-01 requires `rated_ah=self.capacity_ah` at both branches, and the test plan covers empty, populated, and default cases. Gap: end-to-end MOTD/reporting is not fully proven. `src/motd_status.py` constructs `BatteryModel(model_path)` without config, and Plan 01 does not update that path. `battery-health.py` is moved to the `RATED_CAPACITY_AH` constant, so non-default `capacity_ah=9.0` still diverges there.
- **HIGH #2 configurable replacement threshold: PARTIALLY RESOLVED.** The daemon/model path is fixed: 26-02 adds `self.soh_threshold`, monitor injects `config.soh_alert_threshold`, and the `[0.80, 0.75]` equivalence test would catch a hardcoded model method. Gap: reporting paths are still default-threshold (MOTD standalone 0.80, battery-health keeps 0.80), so customized `soh_alert=0.75` is not preserved end-to-end.
- **HIGH #3 shared baseline filter: FULLY RESOLVED for the tagged latest-baseline case.** One `latest_capacity_ah_ref()` helper, used in both `compute_replacement_due()` and `battery-health.py`, plus a mixed-baseline regression test proving latest-baseline-only differs from all-entry output.

**Summary** — The revisions close the narrow hardcoded-return and baseline-filter defects in the core model. Not fully converged: two HIGH output-equivalence risks remain — config-less reporting paths fall back to defaults, and `replacement_due` recomputation appears to drop the old capacity-convergence precondition.

**Concerns**
- **HIGH (NEW/latent):** Non-default config values lost in reporting tools. `motd_status.py` builds `BatteryModel` without `capacity_ah`/`soh_threshold`; Plan 01 makes `battery-health.py` use `RATED_CAPACITY_AH`; Plan 02 keeps battery-health at `threshold_soh=0.80`. Violates "computed outputs identical" for `capacity_ah != 7.2` or `soh_alert != 0.80`.
- **HIGH (NEW/latent):** `replacement_due` live recompute omits the old convergence gate. `discharge_handler.py:218-219` only persisted a prediction when `get_convergence_status().converged`; `compute_replacement_due()` runs regression directly on `soh_history`. With 3+ SoH points but non-converged capacity estimates, old persisted value was `None`; new live value may emit a date.
- **MEDIUM:** `latest_capacity_ah_ref()` returns `None` when the latest entry lacks `capacity_ah_ref`; `linear_regression_soh()` treats missing tags as the 7.2 baseline when filtering. Mixed untagged+tagged history can use all entries instead of the latest effective baseline.
- **LOW:** Plan 01's `rg full_capacity_ah_ref scripts/ tests/` gate conflicts with Plan 02's old-schema strip test, which intentionally needs the removed-key literals in tests.

**Suggestions** — source report-only capacity from `health.json capacity_ah_rated` (or a lightweight config reader for CLI/MOTD); add `replacement_due` to `health.json` from the daemon's configured model OR make MOTD/battery-health load the same `soh_alert`; add end-to-end tests for `capacity_ah=9.0` and `soh_alert=0.75` across health/MOTD/battery-health; add a 3+-points-but-non-converged regression test for `replacement_due`; make `latest_capacity_ah_ref()` treat a missing latest tag as `RATED_CAPACITY_AH` or test/document why `None` is correct.

**Risk Assessment: HIGH** until the reporting/default-threshold and convergence-gate issues are fixed. Core direction sound, but the remaining gaps can change user-visible values — exactly what Phase 26 forbids.

## OpenCode Cycle-2 Review (verbatim)

**Cycle-1 HIGH Resolution** — HIGH #1 **FULLY RESOLVED** (rated_ah=self.capacity_ah at both sites + propagation test both branches + default; verify gate; T-26-06). HIGH #2 **FULLY RESOLVED** (`soh_threshold` param injected from `config.soh_alert_threshold`; `compute_replacement_due()` uses `self.soh_threshold`; parametrized `[0.80, 0.75]` test; T-26-04). HIGH #3 **FULLY RESOLVED** (shared `latest_capacity_ah_ref()` helper in both paths; mixed-baseline test; T-26-07).

**Summary** — All three cycle-1 HIGHs fully resolved with specific code changes and Nyquist-level regression tests. No new HIGH concerns introduced. One MEDIUM around standalone MOTD diverging from daemon threshold for non-default `soh_alert` users (documented, low-impact — MOTD is a read-only banner, 0.80 is the IEEE standard).

**Concerns**
- **MEDIUM:** Standalone MOTD threshold diverges from daemon for non-default `soh_alert`. `motd_status.py` builds `BatteryModel(model_path)` → default 0.80; old MOTD read the persisted `replacement_due` computed at `config.soh_alert`. For `soh_alert=0.75`, old MOTD showed the 75% date, new shows 80%. Documented as intentional; bounded (read-only banner, default case unaffected).
- **MEDIUM:** battery-health.py shows the default rated capacity (7.2) for non-default batteries. Plan 01 hardcodes `RATED_CAPACITY_AH`; for `capacity_ah=9.0` the operator report shows 7.2 while NUT shows 9.0. Not new (battery-health already read 7.2 from model.json); YAGNI-justified and documented.
- **LOW:** `latest_capacity_ah_ref` returns `None` for backward-compat entries lacking the field → falls back to all-entries (matches pre-field behavior; acceptable).
- **LOW:** Plan 01 Task 1 verify only runs sag/runtime tests; the final `uv run pytest -q` in Task 3 covers the broader call paths.

**Suggestions** — document the MOTD-vs-daemon threshold divergence in 26-02-SUMMARY; add a clarifying comment near the battery-health `RATED_CAPACITY_AH` import; expand Plan 01 Task 1 verify with discharge/capacity tests; consider documenting/checking the physics sub-key loader nuance in a future phase.

**Risk Assessment: LOW.** All three cycle-1 HIGHs fully resolved; the two MEDIUMs are documented, bounded design decisions; sequential wave ordering enforced; deploy strip tested. Execution can proceed.

## Cycle-2 Consensus

### Agreed (both reviewers)
- All three cycle-1 HIGH concerns are addressed by the targeted mechanisms in the revised plans
  (rated_ah at both sites + test; injected `soh_threshold` + parametrized `[0.80,0.75]` test; shared
  `latest_capacity_ah_ref` helper + mixed-baseline test). HIGH #3 is FULLY RESOLVED by both; #1 and #2
  are fully resolved for the daemon/model path (Codex's "partial" reduces to the reporting-tool MEDIUMs).
- The standalone reporting paths (MOTD, battery-health.py) intentionally use defaults (capacity 7.2,
  threshold 0.80) and can diverge from the daemon for non-default `capacity_ah`/`soh_alert` — both flag
  this; both treat it as bounded (Codex folds it into a HIGH, OpenCode rates it MEDIUM and documented).

### Divergence
- **Overall risk:** Codex → HIGH (two unresolved equivalence risks). OpenCode → LOW (all resolved, only
  documented MEDIUMs).
- **Convergence gate (Codex only, OpenCode silent):** the decisive item. Verified against source as a
  **REAL, unaddressed value divergence even for the default-config user** → counts as the one remaining
  cycle-2 HIGH. OpenCode missed it.

### Cycle-2 Verdict
- Cycle-1 HIGHs: **3 raised → 3 FULLY RESOLVED.**
- New HIGH this cycle: **1** (`compute_replacement_due()` drops the convergence gate).
- **HIGH remaining (unresolved): 1.** Convergence has NOT been reached — one more replan + review cycle
  is warranted to gate `compute_replacement_due()` on `convergence.converged` and add the
  non-converged-history equivalence test. The reporting-tool divergences (MOTD/battery-health) should be
  resolved or explicitly accepted-and-documented in the same replan (MEDIUM).
