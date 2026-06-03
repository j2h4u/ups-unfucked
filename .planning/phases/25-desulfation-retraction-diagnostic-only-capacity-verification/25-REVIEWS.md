---
phase: 25
cycle: 3
reviewers: [codex, opencode]
reviewed_at: 2026-06-04T00:55:00+05:00
plans_reviewed: [25-01-PLAN.md, 25-02-PLAN.md, 25-03-PLAN.md]
prior_cycle_high_count: 1
current_high_count: 0
revision_commit: a785bb5
---

# Cross-AI Plan Review — Phase 25 (Convergence Cycle 3, FINAL)

Cycle 1 raised 5 HIGH concerns (all verified resolved in cycle 2). Cycle 2 raised 1 NEW HIGH: a
single `days_since_last_test` value fed BOTH the 7-day rate-limit gate and the ~365-day cadence
gate, so a failed dispatch — whose cadence input was forced to `inf` to avoid deferring the annual
clock — became invisible to the rate-limit gate (`inf < 7` → False) and could be retried on every
daily run, hammering the UPS.

The plans were revised in commit `a785bb5` (changes confined to 25-01-PLAN.md; 25-02/25-03
unchanged). This cycle verifies the two-input split actually landed and asks both external
reviewers whether the cycle-2 HIGH is fully resolved and whether any HIGH was newly introduced.

## Pre-review fix verification (against current 25-01-PLAN.md text)

| Check | Landed? | Evidence (25-01-PLAN.md) |
|-------|---------|--------------------------|
| `_calculate_days_since_last_test` split into two methods | YES | :165-182 (split action), :167-173 attempt body, :174-182 success body |
| Gate 2 (rate-limit) keys off `days_since_last_attempt` (ANY attempt, OK or ERR) | YES | :106-108 ("rate-limit defer … keyed off ANY attempt so a failed dispatch is still rate-limited") |
| Gate 5 (cadence) keys off `days_since_last_test_success` (inf unless last status OK) | YES | :110 ("cadence: if `days_since_last_test_success >= DIAGNOSTIC_TEST_INTERVAL_DAYS`"); :174-182 success body returns inf when `last_upscmd_status != "OK"` |
| Ordering: gate 2 (attempt) runs BEFORE gate 5 (success) so a failed dispatch is rate-limited before cadence sees inf | YES | :116-118 ("Because gate 2 (attempt) runs before gate 5 (success), a failed dispatch 1d ago is caught by rate-limit before the cadence gate ever sees the inf success age") |
| Two independent inputs, never collapsed into one value | YES | must-have :20; signature :94-101 takes both `days_since_last_test_success` and `days_since_last_attempt`, no single `days_since_last_test` |
| Boundary test (a): recent failed attempt (1d, ERR) → defer_test/rate_limit | YES | :185-188 |
| Boundary test (b): old failed attempt (8d, ERR, never succeeded) → propose_test/diagnostic_cadence | YES | :189-191 |
| Boundary test (c): successful test (30d, OK) → defer_test/within_cadence | YES | :192-193 |
| `days_since_last_test\b` grep returns nothing (single collapsed name gone) | YES | verification :230 |

All elements of the two-input split are present in the live plan text. The ordering argument is
explicit and correct: rate-limit (attempt) precedes cadence (success), so the two requirements
("a failed dispatch does not defer the annual cadence" and "a failed dispatch is still
rate-limited") hold simultaneously. This is the clean fix the cycle-2 adjudication prescribed.

---

## Codex Review

### Cycle 3 Verdict

The cycle-2 HIGH is **FULLY RESOLVED**. The revised 25-01 plan separates `days_since_last_attempt`
(7-day rate-limit) from `days_since_last_test_success` (annual cadence), orders rate-limit before
cadence, and requires the three boundary tests. **No HIGH remains for cycle 3** and **no new HIGH
was introduced** by the revision.

### Prior HIGH Verdicts

| Prior HIGH | Verdict | Evidence |
|------------|---------|----------|
| Cycle 2: one `days_since_last_test` value served both rate-limit and cadence | **FULLY RESOLVED** | 25-01-PLAN.md:20-22, 95-117, 168-182, 184-193, 234-237 |
| C1: `install.sh` still installs deleted `55-sulfation.sh` | **FULLY RESOLVED** | 25-02-PLAN.md:204-210, 283 |
| C1: grep-clean gate could not pass / broken grep | **FULLY RESOLVED** | 25-02-PLAN.md:238-250; 25-03-PLAN.md:179-188, 214 |
| C1: `update_battery_health` slim path under-specified | **FULLY RESOLVED** | 25-02-PLAN.md:115-149, 179 |
| C1: verify commands masked failures via tail/pipes | **FULLY RESOLVED** | 25-01-PLAN.md:122-125, 202-205; 25-02-PLAN.md:177, 213, 250 |
| C1: stale `model.json` keys persist | **FULLY RESOLVED** (no-migration/operator-delete disposition) | 25-02-PLAN.md:162-173, 269, 284; 25-03-PLAN.md:91-99, 210 |

### 25-01 (revised) — Strengths

- The two-input timing split is the right fix and directly addresses the failed-dispatch retry
  hazard: gate 2 uses any attempt (`days_since_last_attempt`) before gate 5 ever sees cadence
  (`days_since_last_test_success`).
- Boundary tests cover recent-failed → rate-limit, old-failed → propose, successful → within-cadence.
- Explicitly removes scheduler dependency on `sulfation_score`, `cycle_roi`, and blackout credit.
- Correct gate order: SoH, attempt rate-limit, grid stability, cycle budget, cadence.
- Manager wiring AND unit tests are both updated, not just the pure function.

### 25-01 (revised) — Concerns

- **LOW** — `_calculate_days_since_last_test_success` uses "last status OK" as a proxy for "last
  successful diagnostic" because no durable success timestamp exists (25-01-PLAN.md:179-182).
  Acceptable for the current scheduler path, but a future/manual caller of `update_upscmd_result`
  could blur cadence semantics.
- **LOW** — Threat T-25-01 says rate-limit still bounds corrupted timestamps, but both split
  methods returning `inf` on a corrupt timestamp means a corrupted timestamp can allow one
  immediate quick proposal (25-01-PLAN.md:222). Dispatch then rewrites a valid timestamp, so this
  is not HIGH; the wording could be tightened.

### 25-02 / 25-03 (unchanged) — Concerns

- **LOW** — Wave 2 keeps one temporary `sulfation` grep exemption in `scripts/battery-health.py`
  until Wave 3 reword; final strict grep must run after Wave 3.
- **LOW** — Stale-state removal depends on operator action, not code (matches policy; ADR/deploy
  note explicit).
- **MEDIUM** — 25-03 Task 2 README grep verify prints `CLAIMS_REMAIN` but still exits 0
  (25-03-PLAN.md:145) — a soft acceptance gate. Suggest a failing negative grep
  (`! grep -Ein "fights sulfation|..." README.md`).
- **LOW** — 25-03 fixture-based `battery-health.py` verification references fixture files but does
  not specify creating them (25-03-PLAN.md:213).

### Risk Assessment

**LOW.** The revised design resolves the safety-relevant retry loop and has targeted tests. The
docs-verification softness in 25-03 is the only non-trivial residual, and it is MEDIUM, not HIGH.

### Final HIGH Status (Codex)

No HIGH remains for cycle 3. The prior cycle-2 HIGH is fully resolved, not partial: separate
inputs, correct gate ordering, and explicit boundary tests proving failed-dispatch behavior.

---

## OpenCode Review

### Cycle-2 HIGH Verdict

| Cycle-2 HIGH | Status | Evidence |
|--------------|--------|----------|
| Single `days_since_last_test` cannot satisfy both rate-limit (count any attempt) and cadence (ignore failed attempts) | **FULLY RESOLVED** | 25-01-PLAN.md:96-99 (split params), :106-116 (gate order 2 before 5), :165-182 (two-calc split), :184-193 (three boundary tests) |

The two-input split + gate ordering is correct. The prior HIGH is gone. No new HIGH introduced.

### 25-01 (revised) — Strengths

- Two-input split is explicit, independently verifiable, and maps 1:1 to the two scheduling
  concerns (rate-limit vs cadence) — no clever collapsing.
- Gate ordering (2 before 5) creates a natural priority: rate-limit first, cadence only if eligible.
- `_calculate_days_since_last_attempt` reuses the existing body verbatim (already restart-safe,
  already corrupt-tolerant) — zero new risk there.
- `_calculate_days_since_last_test_success` correctly handles the no-getter gap
  (`state.get("last_upscmd_status")`) without adding new API surface.
- Three boundary tests (25-01-PLAN.md:184-193) are precise and map directly to the cycle-2 objection.
- Plan explicitly removes the single `days_since_last_test` param/key and grep-verifies it (:230).

### 25-01 (revised) — Concerns

- **MEDIUM** — `_calculate_days_since_last_test_success` reads `self.battery_model.state` directly
  (`state.get("last_upscmd_status")`), while `_calculate_days_since_last_attempt` uses the public
  `get_last_upscmd_timestamp()` getter. This asymmetry couples scheduler_manager to the internal
  state dict for one path. The plan acknowledges the no-getter gap; correct per single-host policy,
  but a latent fragility if model.py refactors state-dict keys.
- **LOW** — `last_upscmd_status` is a coarse proxy for "successful diagnostic test." A manual
  `test.battery.start.quick`, or a dispatch that succeeds then later fails, both reset the cadence
  clock. The cycle-2 MEDIUM about distinguishing "last command success" from "last diagnostic
  completion" remains open — the split does not address it (nor claims to; this is explicitly a
  cadence-on-command-success design).

### 25-02 / 25-03 (unchanged) — Concerns

- **MEDIUM** — Whole-file `model.json` delete loses learned SoH/capacity/LUT state; accepted per
  no-backward-compat policy but the operational cost (capacity estimation restarts) is real.
  Carried from cycle 2, not a blocker.
- **LOW** — `test_dispatch.py` / `test_year_simulation.py` surgical-edit guidance is thin; failure
  mode is an import error caught by `uv run pytest`.
- **LOW** — 25-03 sudo hint doesn't mention env overrides as a sudo-free testing alternative.
- **LOW** — 25-03 line-109 reword changes named aging mechanisms; verify factual accuracy.

### Overall Consensus (OpenCode)

| Plan | Prior HIGHs | Current HIGHs | Verdict |
|------|-------------|---------------|---------|
| 25-01 | 1 (cycle-2 rate-limit bypass) | **0** | FULLY RESOLVED — ready to execute |
| 25-02 | 0 | 0 | Ready to execute |
| 25-03 | 0 | 0 | Ready to execute |

### Risk Assessment

**LOW.** The two-input split is sound, verifiable, and properly gated. The only remaining MEDIUM on
25-01 is a code-style asymmetry (dict access vs getter), not a logic flaw.

---

## Consensus Summary

Both reviewers independently rate the cycle-2 HIGH **FULLY RESOLVED** and find **no new HIGH** and
**no remaining HIGH** for cycle 3. The convergence loop has converged: `current_high=0`.

The fix both verified is the same one the cycle-2 adjudication prescribed — split the single timing
value into `days_since_last_attempt` (rate-limit, counts any attempt) and
`days_since_last_test_success` (cadence, inf unless last status OK), with gate 2 (rate-limit)
ordered before gate 5 (cadence). Because `update_upscmd_result` writes `last_upscmd_timestamp` and
`last_upscmd_status` together on every attempt (OK or ERR), a failed dispatch yields
attempt-age=small (rate-limited) AND success-age=inf (cadence not deferred ~365d) — and the gate
order means the rate-limit gate fires before cadence ever inspects the inf. The two requirements
hold simultaneously. Three boundary tests lock the behavior.

### Agreed Strengths

- Two-input split maps 1:1 to the two scheduling concerns; no value collapsing — both.
- Correct gate ordering (rate-limit before cadence) — both.
- Three boundary tests directly encode the cycle-2 objection — both.
- Manager wiring AND unit tests both updated — both (Codex explicit; OpenCode via strengths).
- Wave ordering, 15-step discharge checklist, `pipefail` hardening, evidence-backed ADR — carried
  forward, both.

### Agreed Concerns (none HIGH)

- **MEDIUM (OpenCode)** — getter/state-dict asymmetry in the two calc methods (latent refactor
  fragility); acknowledged in-plan.
- **MEDIUM (Codex)** — 25-03 README grep verify is a soft gate (prints `CLAIMS_REMAIN`, exits 0);
  suggest a failing negative grep.
- **LOW (both)** — "last command OK" is a coarse proxy for "last diagnostic completed"; the
  cycle-2 MEDIUM about a dedicated `last_successful_diagnostic_timestamp` remains deferred
  (out of scope for this retraction phase, by design).

### Divergent Views

- None material. Both reviewers converge on FULLY RESOLVED / no new HIGH. They surface different
  MEDIUMs (OpenCode: code-style asymmetry on 25-01; Codex: docs-verify softness on 25-03), but
  neither rises to HIGH and neither reviewer contradicts the other.

---

## Verification coverage (source-grounding pass)

Every pre-existing symbol cited by the plans — EXCLUDING artifacts the plans declare they produce
(the reframed `evaluate_test_scheduling` body/signature, `DIAGNOSTIC_TEST_INTERVAL_DAYS`, the new
`_calculate_days_since_last_attempt` / `_calculate_days_since_last_test_success` methods, the split
`days_since_last_test_success` / `days_since_last_attempt` inputs, ADR 0001, `print_maintenance` /
`HEALTH_PATH`, env overrides in battery-health.py) — was resolved against live source via
ripgrep/Read at HEAD `a785bb5`. This cycle re-grounds the symbols load-bearing for the revised
two-input split; the broader symbol set was already VERIFIED in the cycle-2 pass (carried forward).

### VERIFIED (revised-plan load-bearing symbols)

| Symbol | Verdict | Evidence (file:line) |
|--------|---------|----------------------|
| `evaluate_test_scheduling` (pre-existing fn, being reframed) | VERIFIED | src/battery_math/scheduler.py:71 |
| `SchedulerDecision` (dataclass, kept) | VERIFIED | src/battery_math/scheduler.py:34 |
| `_decision` helper (kept) | VERIFIED | src/battery_math/scheduler.py:108 |
| `_parse_iso_or_warn` helper (kept) | VERIFIED | src/battery_math/scheduler.py:54 |
| `SOH_FLOOR` / `MIN_DAYS_BETWEEN_TESTS` / `CRITICAL_CYCLE_BUDGET` (keep) | VERIFIED | scheduler.py:25, 26, 28 |
| `ROI_THRESHOLD` / `DEEP_SULFATION_THRESHOLD` / `QUICK_SULFATION_THRESHOLD` (remove) | VERIFIED | scheduler.py:27, 29, 30 |
| `evaluate_test_scheduling` import in scheduler_manager | VERIFIED | src/scheduler_manager.py:14 |
| `_calculate_days_since_last_test` (current body reads ONLY `get_last_upscmd_timestamp()`; status read is the planned MOD → renamed to `_calculate_days_since_last_attempt`) | VERIFIED | src/scheduler_manager.py:289-301 |
| `_gather_scheduler_inputs` (returns single `days_since_last_test` key today; plan splits it) | VERIFIED | src/scheduler_manager.py:317, key at :327 |
| `evaluate_test_scheduling(...)` call site passing `days_since_last_test=` (the kwarg being split) | VERIFIED | src/scheduler_manager.py:244-248 |
| verbose-log line formatting `days_since_last_test` (plan updates) | VERIFIED | src/scheduler_manager.py:238 |
| `_get_last_natural_blackout` (kept, feeds grid stability) | VERIFIED | src/scheduler_manager.py:303 |
| `_execute_scheduler_decision` (structured-log call site) | VERIFIED | src/scheduler_manager.py:333 |
| `run_daily` (deadlock end-to-end proof target) | VERIFIED | src/scheduler_manager.py:213 |
| `get_last_upscmd_timestamp` | VERIFIED | src/model.py:885 |
| `update_upscmd_result` (writes `last_upscmd_timestamp` AND `last_upscmd_status` together on OK *and* error — the success-proxy mechanism) | VERIFIED | src/model.py:870, ts write :880, status write :882; docstring ":873 called after successful dispatch or error" |
| `last_upscmd_status` state key (setdefault + validation-list; **no dedicated getter** — confirms plan's "read state.get directly" claim) | VERIFIED | src/model.py:304 (setdefault), :338 (validation list), :882 (write); no `get_last_upscmd_status` defined |
| `append_discharge_event` | VERIFIED | src/model.py:578 |

### MISSING

- None. Every cited pre-existing symbol resolved at grep/Read authority.

### AMBIGUOUS

- None. The plan's claim "there is no dedicated getter — read the state key directly" for
  `last_upscmd_status` is positively confirmed: only setdefault/validation/write references exist,
  no `get_last_upscmd_status` method. The chosen `state.get("last_upscmd_status")` access is the
  only available read path.

### UNCHECKABLE / skipped

- The full src/tests/scripts symbol set cited by 25-02/25-03 (unchanged plans) was VERIFIED in the
  cycle-2 source-grounding pass and is carried forward; not re-grounded here since those plans did
  not change.
- Runtime truth of the rate-limit-before-cadence ordering (that gate 2 fires before gate 5 at
  execution time) is reasoned statically from the plan's explicit gate-order spec
  (25-01-PLAN.md:106-118); the three boundary tests are the executable proof and run at execute time.
- IEEE-1188 / BU-804b / Vertiv BattCon evidence citations — external, not verifiable against this
  repo; live in the ADR the plan produces.
