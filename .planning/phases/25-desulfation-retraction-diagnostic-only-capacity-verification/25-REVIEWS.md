---
phase: 25
cycle: 2
reviewers: [codex, opencode]
reviewed_at: 2026-06-04T00:37:46+05:00
plans_reviewed: [25-01-PLAN.md, 25-02-PLAN.md, 25-03-PLAN.md]
prior_cycle_high_count: 5
current_high_count: 1
---

# Cross-AI Plan Review — Phase 25 (Convergence Cycle 2)

Cycle 1 raised 5 HIGH concerns. The plans were revised in commit `7c57fc1` to close them. This
cycle verifies each claimed fix actually landed in the current PLAN.md files and asks the external
reviewers whether any HIGH remains unresolved or was newly introduced.

## Pre-review fix verification (against current PLAN.md text)

| Cycle-1 HIGH | Claimed fix | Landed? | Evidence (plan file:line) |
|--------------|-------------|---------|---------------------------|
| 1. install.sh installs deleted 55-sulfation.sh | Drop it from the MOTD `for`-loop | YES | 25-02-PLAN.md:206-210 (loop → `51-ups.sh 51-ups-health.sh`), must-have :37, verify :283 |
| 2. grep-clean gate cannot pass; broken `grep -qv` | ripgrep `rg -v` exclusion + reword comment; replace broken check | YES | 25-02-PLAN.md:245,250 (`rg -v "...aging \(sulfation"`); 25-03-PLAN.md:179-181 reword; no `grep -qv` remains |
| 3. update_battery_health/DischargeMetrics under-specified | 15-step checklist; eliminate dataclass; KEEP capacity_ah_ref as measured_capacity_ah; REMOVE confidence_level; mandate append_discharge_event survives | YES | 25-02-PLAN.md:114 ELIMINATE, :119-123 capacity_ah_ref/confidence_level decision, :125-149 enumerated 15 steps, :142 KEEP append_discharge_event |
| 4. tail-piped verify masks failures | `set -o pipefail` in every `<automated>`; remove `| tail` | YES | pipefail in 25-01 (×2 blocks), 25-02 (×3 blocks), 25-03 (line 145 block); `grep "\| tail"` across all plans → NONE |
| 5. stale model.json keys persist; no operator action | Deploy-time `rm ~/.config/ups-battery-monitor/model.json` in Plan 02 + mandatory ADR Consequences bullet | YES (see note) | 25-02-PLAN.md:162-173 deploy note; 25-03-PLAN.md:91-99 MANDATORY Consequences bullet w/ concrete `rm` step |

All five claimed fixes are present in the live plan text. Fix 5 is verified present but one
reviewer (Codex) flags the chosen disposition (delete the whole file) as operationally blunt —
tracked below as a MEDIUM, not a reopened HIGH.

---

## Codex Review

### Summary

The revised plans resolve the original convergence blockers well: the deleted MOTD script is
removed from install, grep gates are mostly corrected, the discharge persistence path is now
explicitly preserved, verification commands use `pipefail`, and stale `model.json` state has an
operator disposition. However, Cycle 2 introduces **one new HIGH** in Plan 25-01: the proposed
failed-dispatch handling cannot simultaneously make annual cadence ignore failed attempts AND keep
the 7-day rate limit on any attempt while using only one `days_since_last_test` value. As written, a
failed dispatch can be retried on the next daily scheduler run, bypassing `MIN_DAYS_BETWEEN_TESTS`.

### Prior HIGH Concerns

1. `install.sh` still installs deleted `55-sulfation.sh`: **FULLY RESOLVED** — Plan 25-02 deletes
   the script, removes it from the install loop, and verifies `scripts/install.sh`.
2. Grep-clean gate cannot pass / broken `grep -qv`: **FULLY RESOLVED** — broken gate replaced with
   `set -o pipefail` plus an explicit filtered `rg` check; Plan 25-03 removes the final token.
3. `update_battery_health` / `DischargeMetrics` slim path under-specified: **FULLY RESOLVED** —
   ordered surviving pipeline; `append_discharge_event` preserved with
   `measured_capacity_ah=capacity_ah_ref`; `confidence_level` explicitly removed.
4. Tail-piped verify commands mask failures: **FULLY RESOLVED** — `set -o pipefail` / non-piped logic.
5. Stale `model.json` keys persist with no operator action: **PARTIALLY RESOLVED** — an operator
   action and ADR consequence are now documented. It does resolve stale keys, but deleting the whole
   model file is operationally blunt and loses learned SoH/capacity/LUT state; a targeted purge would
   be safer while still avoiding migration code. (Codex treats this as MEDIUM, not a reopened HIGH.)

### Strengths

- Strong dependency ordering: scheduler is detached from sulfation/ROI before deletion.
- The installer breakage is now handled at the exact failure point.
- The discharge rewrite has a clear preservation checklist for SoH, Peukert, alerts, measured
  capacity, journald, and `safe_save`.
- Plan 25-03 moves next-test visibility from the deleted MOTD sulfation UI to `battery-health.py`.
- ADR requirement is concrete and includes the no-charge-control fact plus evidence basis.
- Verification is much more failure-sensitive than Cycle 1.

### Concerns

- **HIGH: Plan 25-01 failed-dispatch rate limiting is internally inconsistent.**
  `_calculate_days_since_last_test` is changed to return `inf` when `last_upscmd_status != "OK"` so
  annual cadence is not reset. But `evaluate_test_scheduling` uses that SAME `days_since_last_test`
  value for the 7-day rate-limit gate (gate 2). If the value is `inf`, the rate-limit gate
  (`inf < MIN_DAYS_BETWEEN_TESTS` → False) cannot apply to the recent failed attempt. This
  contradicts the plan's own must-have (line 20: "rate-limiting still applies to any attempt") and
  its verification (line 190: "while the rate-limit gate still sees the recent attempt"). With a
  single value the two requirements are unsatisfiable; a failed dispatch could be retried on the next
  daily run, hammering the UPS.

- **MEDIUM: Delete-whole-`model.json` deploy action may regress safety data.** It removes stale keys
  but also drops learned capacity, SoH, LUT, RLS state, and history. Prefer a targeted purge of only
  `sulfation_history`, `roi_history`, `blackout_credit`, and nested `cycle_roi` fields.

- **MEDIUM: Annual cadence probably should distinguish last successful diagnostic from last command
  success.** `last_upscmd_status="OK"` means command accepted, not that a diagnostic discharge
  completed. Existing `discharge_events` with a test `event_reason` may be a stronger source.

- **LOW: Docs grep checks focus mostly on README.** MILESTONES preserves history under a banner
  (acceptable), but verification should assert the banner is adjacent to the preserved v3.0 block.

### Suggestions

- Split scheduler timing inputs: `days_since_last_test_success` for annual cadence,
  `days_since_last_test_attempt` for `MIN_DAYS_BETWEEN_TESTS`. This resolves the new HIGH cleanly.
- Add explicit tests: failed attempt 1d ago / no success ever → `defer_test/rate_limit`; failed
  attempt 8d ago / no success ever → `propose_test/diagnostic_cadence`; success 30d ago →
  `defer_test/within_cadence`.
- Replace `rm ~/.config/.../model.json` with a targeted operator command that deletes only obsolete
  keys and strips `cycle_roi` from `discharge_events`, preserving learned battery state.
- Keep the `battery-health.py` env-override requirement.

### Risk Assessment

**MEDIUM overall.** The convergence work is substantially improved; the original HIGHs are resolved
or explicitly addressed. The remaining blocker is narrow but safety-relevant: failed-dispatch retry
semantics can bypass the intended rate limit unless the plan separates "last successful diagnostic"
from "last command attempt." No other new HIGH concern is introduced.

---

## OpenCode Review

### Overall Assessment

The three plans are substantially improved over Cycle 1. OpenCode rates all 5 prior HIGH concerns
**FULLY RESOLVED** and identifies **no new HIGH**. The plans form a coherent, dependency-ordered
sequence (01→02→03). Two MEDIUM concerns remain (a `DischargeMetrics`/DischargeCollector scoping
question and `inf` formatting), plus minor LOWs.

### Resolution of Cycle-1 HIGH Concerns

| # | Concern | Verdict | Evidence |
|---|---------|---------|----------|
| 1 | install.sh installs deleted `55-sulfation.sh` | FULLY RESOLVED | Plan 02 Task 2: explicit removal, loop leaves `51-ups.sh 51-ups-health.sh`. |
| 2 | grep-clean gate cannot pass | FULLY RESOLVED | Plan 02 Task 3 `rg -v` exemption; Plan 03 Task 3 rewrites the comment to drop the token. |
| 3 | update_battery_health / DischargeMetrics under-specified | FULLY RESOLVED | Plan 02 Task 1: 15-step checklist; `capacity_ah_ref`→KEPT as `measured_capacity_ah`; `confidence_level`→REMOVED; dataclass→ELIMINATED. |
| 4 | tail-piped verify masks failures | FULLY RESOLVED | Every verify block leads with `set -o pipefail`. |
| 5 | stale model.json keys persist | FULLY RESOLVED | Plan 02 + Plan 03 ADR: explicit `rm ~/.config/.../model.json` deploy step; no migration code per policy. |

### Strengths (per plan)

- **25-01**: deadlock fix is crisp (`inf` days → propose on cold start); failed-dispatch cadence
  safety praised as defense-in-depth; gate ordering logical; signature cleaned; constants reorganized.
- **25-02**: 15-step enumerated pipeline removes ambiguity; explicit `DischargeMetrics` elimination;
  explicit `install.sh` fix; concrete stale-state operator action; rigorous grep gate with documented
  exemption; vulture integration; sound test strategy (4 wholesale delete, 8 surgical).
- **25-03**: comprehensive ADR (IEEE-1188/BU-804b/Vertiv, no-charge-control, deploy step); retraction
  banner preserves history; env overrides for testability; graceful degradation with sudo hint.

### Concerns

- **MEDIUM** — `DischargeMetrics` elimination may also require touching `src/discharge_collector.py`
  (Phase 21 extraction) if it constructs/returns/stores instances; Plan 02 `files_modified` doesn't
  list it. *(Adjudicated below: VERIFIED FALSE — DischargeMetrics is referenced only in
  discharge_handler.py.)*
- **MEDIUM** — Plan 01 only verifies scheduler tests; full pytest deferred to Plan 02, so
  `test_dispatch.py` (old signature) could break silently between waves. Non-blocking (Plan 02 catches).
- **LOW** — `inf` formatted as `infd` in reason strings; cosmetic.
- **LOW** — `test_year_simulation.py` strip guidance is thin (executor inspects).
- **LOW** — line-109 reword changes the named aging mechanisms; verify factual accuracy.
- **LOW** — sudo hint doesn't mention the env overrides as an alternative.

### Suggestions

- Add `src/discharge_collector.py` as a check-and-clean item in Plan 02 Task 1.
- Add `tests/test_dispatch.py` to Plan 01's verify, or document that Plan 02 surfaces signature breakage.
- Special-case `inf` → "never tested" in `reason_detail` to avoid `infd`.
- Mention `UPS_MODEL_PATH`/`UPS_HEALTH_PATH` in the sudo hint text.

### Risk Assessment: **LOW**

All 5 Cycle-1 HIGH concerns fully resolved; no new HIGH identified. The two MEDIUMs are within what a
competent executor handles inline. The plans are ready for execution.

---

## Consensus Summary

Both reviewers agree the cycle-2 revisions **fully resolve cycle-1 HIGH concerns #1–#4**, and both
praise the wave ordering, the 15-step discharge-pipeline checklist, and the more failure-sensitive
verification. They diverge on two points:

1. **The failed-dispatch rate-limit split (cycle-1 MEDIUM that the revision attempted to close).**
   Codex raises this as a **NEW HIGH**: the plan funnels a single `days_since_last_test` value into
   both the rate-limit gate (gate 2) and the cadence-propose gate (gate 5), then asks
   `_calculate_days_since_last_test` to return `inf` on a failed status — which makes the rate-limit
   gate blind to the recent failed attempt, directly contradicting the plan's own must-have and
   verification text. OpenCode praised the same split as sound defense-in-depth and did **not** notice
   the single-value contradiction.

2. **Cycle-1 HIGH #5 (stale model.json).** OpenCode: FULLY RESOLVED. Codex: PARTIALLY RESOLVED at the
   disposition level (delete-whole-file is blunt), but Codex still scores it MEDIUM, not a reopened
   HIGH.

### Adjudication (grep authority)

- **Codex's new HIGH is CONFIRMED against the plan text.** 25-01-PLAN.md:89-90 defines a single
  `days_since_last_test` keyword param; lines 97 (gate 2) and 99 (gate 5) both consume it; lines
  143-152 instruct `_calculate_days_since_last_test` to return `inf` on non-OK status; yet line 20
  (must-have) and line 190 (verification) require "rate-limiting still applies to any attempt" /
  "the rate-limit gate still sees the recent attempt." These are mutually unsatisfiable with one
  value. The contradiction is internal to the plan and is safety-relevant (rate-limit bypass on
  repeated failed dispatches). **This is the one unresolved HIGH for cycle 2.** The clean fix is
  Codex's suggested two-input split (`days_since_last_test_success` for cadence;
  `days_since_last_attempt` for rate-limit).
- **OpenCode's DischargeCollector MEDIUM is DISMISSED.** `grep -rln "DischargeMetrics" src/` returns
  only `src/discharge_handler.py`; `src/discharge_collector.py` contains no `DischargeMetrics`
  reference. Eliminating the dataclass touches only the file Plan 02 already lists. No action needed,
  though adding a one-line "no other module references DischargeMetrics" note would preempt the worry.

### Agreed Strengths

- Wave ordering (reframe → delete → docs) prevents dangling callers — both.
- 15-step discharge-pipeline checklist resolves the cycle-1 data-integrity ambiguity — both.
- `set -o pipefail` verification hardening across all plans — both.
- ADR is concrete, evidence-backed, and records the no-charge-control fact — both.

### Agreed Concerns (highest priority)

- **HIGH — Failed-dispatch rate-limit vs cadence cannot both hold with one `days_since_last_test`
  value** (Codex HIGH; the mechanism OpenCode endorsed without catching the contradiction).
  Remediation: split into two scheduler inputs — cadence keys off last *successful* test;
  rate-limit keys off *any* attempt — and add the three boundary tests Codex listed.

### Divergent Views

- Codex flags the whole-file `model.json` delete as a MEDIUM regression risk (loses learned state)
  and prefers a targeted key purge; OpenCode considers the deploy step fully sufficient.
- OpenCode flags a DischargeCollector scoping risk that grep proves is a non-issue.

---

## Verification coverage (source-grounding pass)

Every symbol the plans cite that should already exist (excluding artifacts the plans declare they
produce — ADR 0001, the new `print_maintenance`/`HEALTH_PATH`, the `DIAGNOSTIC_TEST_INTERVAL_DAYS`
constant, the reframed engine, env overrides in battery-health.py) was resolved against live source
via ripgrep/Read.

### VERIFIED

| Symbol | Evidence |
|--------|----------|
| `evaluate_test_scheduling` | `src/battery_math/scheduler.py:71` |
| `SchedulerDecision` (dataclass) | `src/battery_math/scheduler.py:34` |
| `_decision` helper | `src/battery_math/scheduler.py:108` |
| `_parse_iso_or_warn` helper | `src/battery_math/scheduler.py:54` |
| `SOH_FLOOR` / `MIN_DAYS_BETWEEN_TESTS` / `CRITICAL_CYCLE_BUDGET` (keep) | `scheduler.py:25,26,28` |
| `ROI_THRESHOLD` / `DEEP_SULFATION_THRESHOLD` / `QUICK_SULFATION_THRESHOLD` (remove) | `scheduler.py:27,29,30` |
| `compute_sulfation_score` / `estimate_recovery_delta` / `SulfationState` | `src/battery_math/sulfation.py:42,115,19` |
| `compute_cycle_roi` | `src/battery_math/cycle_roi.py:16` |
| battery_math `__init__` exports of the above | `src/battery_math/__init__.py:2,7,16-19` |
| `DischargeMetrics` (16 fields incl. `capacity_ah_ref`, `confidence_level`) | `src/discharge_handler.py:44,66,67` |
| `DischargeMetrics` referenced ONLY in discharge_handler.py (NOT discharge_collector.py) | `grep -rln "DischargeMetrics" src/` → single file; dismisses OpenCode MEDIUM |
| `update_battery_health` | `src/discharge_handler.py:119` |
| `_compute_soh` returns `(soh_after, capacity_ah_ref)` | `src/discharge_handler.py:152,206` |
| `_predict_replacement` / `_avg_load` / `_check_alerts` / `_auto_calibrate_peukert` / `_classify_discharge_trigger` / `_log_discharge_prediction` | `discharge_handler.py:208,267,232,485,800,557` |
| `_score_and_persist_sulfation` / `_compute_sulfation_metrics` / `_persist_sulfation_and_discharge` / `_assess_sulfation_confidence` / `_grant_blackout_credit` | `discharge_handler.py:273,293,397,872,461` |
| `last_sulfation_*` / `last_cycle_roi` instance fields | `discharge_handler.py:110-117` |
| `append_sulfation_history` / `append_discharge_event` (cycle_roi in docstring) | `src/model.py:561,578,588` |
| `set_/clear_/get_blackout_credit` | `src/model.py:835,849,889` |
| `get_last_upscmd_timestamp` / `update_upscmd_result` (writes ts+status on OK *and* error) | `src/model.py:885,870` |
| `setdefault("sulfation_history"/"roi_history"/"blackout_credit")` | `src/model.py:299,301,308` |
| `_cap_history_entries("sulfation_history")` (no roi_history cap — asymmetry confirmed) | `src/model.py:607` |
| blackout_credit validation block | `src/model.py:367` |
| `clear_blackout_credit` call + "desulfation credit" comment | `src/monitor.py:376,379` |
| `HealthSnapshot` sulfation/cycle_roi fields + health_data keys | `src/monitor_config.py:303,304,307,308,344,345,348,349` |
| `HEALTH_ENDPOINT_PATH = /run/ups-battery-monitor/ups-health.json` | `src/monitor_config.py:53` |
| exporter HealthSnapshot kwargs `sulfation_score=`/`cycle_roi=` etc. | `src/virtual_ups_exporter.py:53,54,57,58` |
| `motd_status.py` `sulfation_pct` mapping (:82) + `UPS_MODEL_PATH`/`UPS_HEALTH_PATH` env pattern (:94,95) | `src/motd_status.py:82,94,95` |
| `scripts/motd/55-sulfation.sh` (exists, to delete) | file present |
| `scheduler_manager` inputs/call-site/log-lines/calc-days | `src/scheduler_manager.py:235-236,244-246,289,303,317,324-329,355-356` |
| `_calculate_days_since_last_test` current body reads only `last_upscmd_timestamp` (status read is the planned MOD) | `src/scheduler_manager.py:289-301` |
| `evaluate_test_scheduling` import in scheduler_manager | `src/scheduler_manager.py:14` |
| `scripts/install.sh:240` installs `55-sulfation.sh` (the loop the plan edits) | `scripts/install.sh:240` |
| `scripts/battery-health.py:109` factual "sulfation" comment; MODEL_PATH json.loads(read_text) | `scripts/battery-health.py:109,14,25` |
| `docs/internal/CONTEXT.md` active-desulfation claims (now enumerated by Plan 03) | `docs/internal/CONTEXT.md:120-125` |
| `README.md:3` "active care" tagline + line 13 pitch (now enumerated by Plan 03) | `README.md:3,13` |

### MISSING

- None. Every cited pre-existing symbol resolved at grep authority.

### AMBIGUOUS

- None blocking. The cycle-1 AMBIGUOUS items (`capacity_ah_ref` / `confidence_level` keep-or-remove)
  are now explicitly resolved in 25-02-PLAN.md:119-123.

### UNCHECKABLE / skipped

- Some `<read_first>` line offsets drift a few lines from current source after refactors; all
  *symbols* resolve, so executors locate by symbol. Non-blocking.
- IEEE-1188 / BU-804b / Vertiv BattCon evidence citations — external, not verifiable against this
  repo; to live in the ADR the plan produces.
- Runtime truth of the `inf >= 365.0` deadlock fix and the `inf < 7` rate-limit-bypass — reasoned
  about statically (no test executed in a review pass); the bypass is the basis for the cycle-2 HIGH.
