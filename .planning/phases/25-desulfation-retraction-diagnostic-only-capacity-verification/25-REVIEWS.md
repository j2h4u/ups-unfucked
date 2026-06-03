---
phase: 25
reviewers: [codex, opencode]
reviewed_at: 2026-06-03T21:40:00+05:00
plans_reviewed: [25-01-PLAN.md, 25-02-PLAN.md, 25-03-PLAN.md]
---

# Cross-AI Plan Review — Phase 25: Desulfation Retraction → Diagnostic-Only Capacity Verification

## Codex Review

## Summary

The three-plan sequence is directionally sound and mostly matches the live checkout: Wave 1 correctly removes the scheduler's dependency on `sulfation_score` / `cycle_roi`, Wave 2 targets the right production surfaces, and Wave 3 covers the public narrative and operator report. The main gaps are execution-quality issues: a deleted MOTD script is still installed by `scripts/install.sh`, stale fields in an existing `model.json` will not disappear just because defaults/validators are removed, and several verification commands can hide failing tests because they pipe through `tail` without `pipefail`.

## Strengths

- Wave ordering is good: scheduler reframe first, deletion second, docs last.
- The bootstrap deadlock is correctly identified in `src/scheduler_manager.py:244` and `src/battery_math/scheduler.py:193`.
- The persistent timestamp path is real: `get_last_upscmd_timestamp()` and `update_upscmd_result()` exist in `src/model.py:870`.
- The deletion inventory is mostly accurate for `discharge_handler`, `model`, `monitor_config`, exporter, MOTD, and tests.
- Keeping SoH floor, rate limit, grid cooldown, and cycle budget ahead of any test proposal is the right safety posture.
- The ADR requirement is well scoped and valuable; it prevents re-litigating the battery-chemistry reversal.

## Concerns

- **HIGH:** Wave 2 deletes `scripts/motd/55-sulfation.sh`, but `scripts/install.sh:240` still installs it. With `set -euo pipefail`, install will fail when the file is gone.
- **HIGH:** Existing `model.json` stale keys will persist. Removing `setdefault()` / validation does not remove loaded `sulfation_history`, `roi_history`, `blackout_credit`, or old `cycle_roi` entries from `discharge_events`; `save()` writes `self.state` back as-is. See `src/model.py:299` and `src/model.py:593`.
- **HIGH:** Verification commands like `uv run pytest ... | tail -20` and `uvx vulture ... | tail -10` can return success even when pytest/vulture failed. Use `set -o pipefail` or avoid piping.
- **HIGH:** `grep -rn "sulfation\|cycle_roi" src tests scripts` cannot pass in Wave 2 as written because `scripts/battery-health.py:109` still contains `sulfation`, and `scripts/install.sh` still contains `55-sulfation.sh`.
- **MEDIUM:** Failed `upscmd` attempts count as `last_upscmd_timestamp`. After one transient error, the cadence logic may defer the next diagnostic for ~365 days even though no test actually ran.
- **MEDIUM:** Wave 3 misses active anti-sulfation claims in `docs/internal/CONTEXT.md:117` and the README tagline at `README.md:3`.
- **MEDIUM:** The `DischargeMetrics` rewrite should explicitly keep `capacity_ah_ref`; otherwise discharge event persistence/logging can lose measured-capacity context.
- **MEDIUM:** `battery-health.py` has no fixture-friendly path override. Testing "against a fixture model.json + health.json" will be awkward unless you add env overrides or function parameters.
- **LOW:** `days_since_last_test = inf` will produce awkward `infd since last test` text unless special-cased as `never tested`.
- **LOW:** ADR evidence is named but not linked. Acceptable for internal docs, but source URLs would make the decision record stronger.

## Suggestions

- Add `scripts/install.sh` to Wave 2 and remove `55-sulfation.sh` from the MOTD install loop and comments.
- In `BatteryModel` load/default handling, explicitly purge retired keys with `pop()` and remove `cycle_roi` from existing `discharge_events`, or explicitly require deleting/regenerating the local model file as part of deployment.
- Replace verification with commands that fail correctly: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright src`, `uv run vulture`, and `! rg -n "sulfation|cycle_roi" src tests scripts`.
- Fix the incorrect Wave 3 check `grep -qv "sulfation"`; use `! grep -q "sulfation\|cycle_roi" scripts/battery-health.py`.
- Add `docs/internal/CONTEXT.md` to Wave 3 or narrow the acceptance wording to public docs only.
- Add a test for the "failed dispatch timestamp" behavior, or split cadence from rate limiting: rate-limit all attempts, but compute annual diagnostic cadence from the last successful test.
- Add `UPS_MODEL_PATH` / `UPS_HEALTH_PATH` env overrides to `scripts/battery-health.py`, mirroring `src.motd_status`, then test the new maintenance section with temp files.

## Risk Assessment

**MEDIUM**, with a few **HIGH-severity plan gaps**. The architecture and phase split are sound, and the intended behavior aligns with v3.2. The risk comes from acceptance drift: installer breakage, stale runtime state, and weak verification could let the implementation look complete while still failing on the real host. Fixing those items before execution should bring the phase down to low-to-medium risk.

---

## OpenCode Review

# Cross-AI Plan Review — Phase 25: Desulfation Retraction

## Summary

The three plans form a well-ordered refactor sequence (reframe → delete → docs) that correctly diagnoses a real bootstrap deadlock, defines a sound diagnostic cadence engine, and comprehensively inventories every sulfation/cycle_roi reference across a 5,000+ LOC codebase. All cited symbols and line numbers are **accurate** against the live source. The plans will achieve the phase goals. The primary risk is in Plan 02 where the discharge pipeline (`update_battery_health`) must be restructured without breaking critical discharge event persistence — the plan correctly identifies this but provides less implementation detail than the other tasks.

## Plan 25-01: Scheduler Reframe + Catch-22 Fix

### Strengths

- **Deadlock diagnosis is precise:** `_gather_scheduler_inputs` (line 317–331) sources `last_sulfation_score`/`last_cycle_roi` from `DischargeHandler`, which are `None`/`0.0` until a discharge completes — and no discharge happens without a proposed test. Root cause correctly traced through gates 6/7.
- **Persistent trigger reuse:** `last_upscmd_timestamp` (written by `update_upscmd_result` at `model.py:870`) already provides restart-safe state. `_calculate_days_since_last_test` already returns `inf` for "never tested", and `float("inf") >= 365.0` is `True`, killing the deadlock with zero new infrastructure.
- **Gate ordering is sound:** SoH floor → rate limit → grid stability → cycle budget → cadence propose. Rate limit runs before cadence, preventing a restart just-under-interval from re-triggering.
- **TDD coverage specified:** Seven explicit test scenarios cover all code paths.

### Concerns

- **MEDIUM — DischargeMetrics field list incomplete in "keep" enumeration.** The plan lists 9 fields to keep but the actual dataclass has 16. Two fields (`capacity_ah_ref: Optional[float]`, `confidence_level: str`) are omitted from both the "remove" and "keep" lists. Ambiguous whether to retain a slimmed `DischargeMetrics` or eliminate it. Doesn't block but costs implementation time.
- **MEDIUM — `_execute_scheduler_decision` reads `scheduler_inputs['sulfation_score']` (line 355).** After removing keys from the dict, this raises `KeyError`. The plan instructs updating these log lines, but the executor must find ALL references, not just the cited lines.
- **LOW — `test_type` Literal retains `"deep"`.** Engine emits only `"quick"`; vulture may flag `"deep"` as dead. Cosmetic.
- **LOW — `_parse_iso_or_warn` retention.** Still used by grid stability after blackout-credit gate removal; correctly kept.

### Suggestions

- Clarify whether `DischargeMetrics` is retained slimmed or removed entirely; move a clear decision to 25-02-PLAN.md.
- Add a verification: `python -c 'from src.battery_math.scheduler import evaluate_test_scheduling; help(...)'` to confirm the new signature.

## Plan 25-02: Delete Sulfation/Cycle-ROI Machinery

### Strengths

- **Inventory is exhaustive:** Every file, field, method, import correctly cited (100% accurate across 22 files).
- **Kernel deletion unblocks naturally:** Wave 1 already removed scheduler dependency, so `git rm` is safe.
- **Discharge event persistence identified as critical:** `append_discharge_event` must still be called without `cycle_roi` — the single most important correctness gate.
- **Test cleanup scoped correctly:** Four files deleted wholesale; eight surgically edited.

### Concerns

- **HIGH — `update_battery_health` restructuring under-specified.** At `discharge_handler.py:119-150`, this calls `_score_and_persist_sulfation` which orchestrates metrics → `append_sulfation_history` + `append_discharge_event` + `_grant_blackout_credit` → logging. The plan says "replace with a slim persist path" but doesn't detail which helpers survive, whether the slim path constructs a new `DischargeMetrics`, or the new `_log_discharge_complete` signature. **If the executor removes `_score_and_persist_sulfation` without re-adding `append_discharge_event`, discharge events stop being persisted, breaking SoH/capacity/replacement prediction.** Recommendation: add an explicit checklist of surviving pipeline steps.
- **MEDIUM — `_execute_scheduler_decision` log keys accessed after dict key removal** (lines 355-356). Handled in Plan 01 Task 2; grep gate catches misses but adds round-trip friction.
- **MEDIUM — `save()` cap list has `sulfation_history` but not `roi_history`.** Plan says remove both; only `sulfation_history` is capped at line 607. Removing a nonexistent `roi_history` cap is a no-op; note the asymmetry.
- **LOW — `active_credit` vs `active_blackout_credit` naming** inconsistency between dict key and param.
- **LOW — `scripts/battery-health.py` already has no sulfation reads** (only a factual comment at line 109).
- **LOW — vulture may produce false positives** after dataclass field removal; results must be triaged.

### Suggestions

- Add explicit pseudo-code for the slimmed `update_battery_health` path enumerating: compute days_since_deep/ir_trend/cycle_budget/dod → update last_* fields → `append_discharge_event({...})` WITHOUT cycle_roi → log WITHOUT sulfation keys.
- Confirm `_log_discharge_complete` is the only consumer of `DischargeMetrics.sulfation_*` fields after restructuring.

## Plan 25-03: Docs, ADR, and Operator Report

### Strengths

- **ADR format correctly chosen** (Nygard Context/Decision/Consequences).
- **Evidence citations specific and verifiable** (BU-804b, Sibex, Vertiv BattCon, Schneider/APC, Lifeline, IEEE-1188).
- **MILESTONES history preservation:** annotate not erase.
- **`battery-health.py` patterns reused** (`json.loads(path.read_text())` with graceful degradation).
- **GLOSSARY line preserved** (factual physics note, not a product claim).

### Concerns

- **MEDIUM — `/run/ups-battery-monitor/ups-health.json` may be root-only.** `battery-health.py` currently reads user-owned `~/.config/...`; the health endpoint is in `/run/` (typically root-owned). Non-root run gets `PermissionError`. Graceful degradation prints "unavailable" (acceptable), but the user should be warned `sudo` may be needed.
- **LOW — MILESTONES.md v3.0 block has ~12 lines of active-desulfation claims;** "soften wording" is subjective. A concrete retraction template would help.
- **LOW — `battery-health.py` flat key access** for `last_upscmd_*` is correct (written directly to `state`).
- **LOW — `battery-health.py:109`** factual sulfation comment should be explicitly exempted from grep-clean, mirroring the GLOSSARY exemption.

### Suggestions

- Add `sudo` note to the maintenance section graceful-degradation message.
- Concrete MILESTONES template: prepend `[RETRACTED in v3.2 — see ADR 0001]` to the v3.0 block, keeping original text.
- Explicitly exempt `scripts/battery-health.py:109` from the grep-clean requirement.

## Risk Assessment

**Overall: MEDIUM.** Correctness HIGH confidence (all symbols verified, deadlock diagnosis precise, gate ordering sound). Completeness MEDIUM — Plan 02's discharge pipeline restructuring is under-specified (the most critical data-integrity path). Safety HIGH. Testability HIGH. Scope discipline HIGH. The phase goals will be achieved if Plan 02's discharge pipeline receives careful implementation attention; primary remediation is an enumerated checklist of surviving persist/log steps in 25-02 Task 1.

---

## Consensus Summary

Both reviewers independently rate the phase **MEDIUM overall** with HIGH-severity execution gaps, agree the wave ordering (reframe → delete → docs) is correct, and confirm the deadlock diagnosis and all cited line references are accurate against live source. The plans will achieve the goals once the gaps below are closed.

### Agreed Strengths

- Wave ordering (reframe → delete → docs) prevents dangling callers mid-phase — both reviewers.
- Deadlock root cause correctly traced; `last_upscmd_timestamp` persistent trigger is restart-safe and reused with zero new infrastructure — both.
- Safety gate posture (SoH floor / rate limit / grid cooldown / cycle budget ahead of propose; first test `quick`) is sound — both.
- Deletion inventory is accurate/exhaustive across the codebase — both.
- ADR is well-scoped and prevents re-litigating the chemistry reversal — both.

### Agreed Concerns (highest priority)

- **HIGH — `DischargeMetrics` keep/remove list is incomplete.** `capacity_ah_ref` and `confidence_level` (verified at `discharge_handler.py:66-67`) appear in neither list. Codex flags `capacity_ah_ref` loss as MEDIUM; OpenCode flags the omission as MEDIUM. Combined with the `update_battery_health` restructuring HIGH (OpenCode), this is the central data-integrity risk: a careless slim-path rewrite can drop `append_discharge_event` or measured-capacity context, breaking SoH/capacity/replacement prediction. **Remediation: 25-02 must enumerate the surviving persist/log steps and the exact slimmed `DischargeMetrics`/`_log_discharge_complete` shape.**
- **HIGH — grep-clean gate cannot pass as written.** Both flag that `scripts/battery-health.py:109` (factual comment, verified) is caught by `grep -rn "sulfation\|cycle_roi" src tests scripts`. Codex additionally finds `scripts/install.sh` references `55-sulfation.sh`. **Remediation: scope the grep to exclude the factual comment (or reword it), fix the install.sh reference, and replace Plan 03's broken `grep -qv "sulfation"` check.**

### Divergent / Unique Views

- **Codex-only HIGH — `scripts/install.sh:240` installs the deleted `55-sulfation.sh`** (verified). Under `set -euo pipefail`, install breaks. Neither plan touches install.sh. This is the single most actionable un-covered gap and should be added to Wave 2. (OpenCode did not inspect install.sh.)
- **Codex-only HIGH — stale `model.json` keys persist** (no `pop()` on load; `save()` round-trips `self.state`). Per project no-backward-compat policy (REQUIREMENTS Out of Scope: "state regenerates"), this is **accept** — but the plan/ADR should state the operator action (delete or regenerate the local model.json) rather than leaving stale `sulfation_history`/`blackout_credit`/`cycle_roi` silently resident.
- **Codex-only HIGH — `tail`-piped verify commands mask failures** (no `set -o pipefail`). Real CI-hygiene gap across all three plans' `<automated>` blocks.
- **Codex-only MEDIUM — failed `upscmd` writes `last_upscmd_timestamp`,** so a transient dispatch error could defer the next diagnostic ~365 days. Worth a test or splitting cadence (last *successful* test) from rate-limit (any attempt).
- **Codex-only MEDIUM — README:3 tagline ("active care") and `docs/internal/CONTEXT.md:120-125`** carry active-desulfation claims Plan 03 does not enumerate (verified). Plan 03 cites README lines 13/48-52/66/82/160 but not line 3.
- **OpenCode-only MEDIUM — `/run/.../ups-health.json` is root-owned;** non-root `battery-health.py` hits `PermissionError`. Graceful degradation covers it but a `sudo` hint helps.

---

## Verification coverage (source-grounding pass)

Every symbol the plans cite that should already exist (excluding artifacts the plans declare they produce) was resolved against live source via ripgrep/Read. Verdicts:

### VERIFIED

| Symbol | Verdict | Evidence |
|--------|---------|----------|
| `evaluate_test_scheduling` (function) | VERIFIED | `src/battery_math/scheduler.py:71` |
| `SchedulerDecision` (dataclass) | VERIFIED | `src/battery_math/scheduler.py:34`; `test_type: Optional[Literal["deep","quick"]]` at :48 |
| `_decision` helper | VERIFIED | `src/battery_math/scheduler.py:108` |
| `_parse_iso_or_warn` helper | VERIFIED | `src/battery_math/scheduler.py:54` |
| `SOH_FLOOR` | VERIFIED | `src/battery_math/scheduler.py:25` |
| `MIN_DAYS_BETWEEN_TESTS` | VERIFIED | `src/battery_math/scheduler.py:26` |
| `CRITICAL_CYCLE_BUDGET` | VERIFIED | `src/battery_math/scheduler.py:28` |
| `ROI_THRESHOLD` (to remove) | VERIFIED | `src/battery_math/scheduler.py:27` |
| `DEEP_SULFATION_THRESHOLD` (to remove) | VERIFIED | `src/battery_math/scheduler.py:29` |
| `QUICK_SULFATION_THRESHOLD` (to remove) | VERIFIED | `src/battery_math/scheduler.py:30` |
| `compute_sulfation_score` | VERIFIED | `src/battery_math/sulfation.py:42` |
| `estimate_recovery_delta` | VERIFIED | `src/battery_math/sulfation.py:115` |
| `SulfationState` | VERIFIED | `src/battery_math/sulfation.py:19` |
| `compute_cycle_roi` | VERIFIED | `src/battery_math/cycle_roi.py:16` |
| battery_math `__init__` exports of above | VERIFIED | `src/battery_math/__init__.py:2,7,16-19` |
| `DischargeMetrics` (dataclass) | VERIFIED | `src/discharge_handler.py:44`; includes `capacity_ah_ref` (:66), `confidence_level` (:67) — NOT in plan keep/remove list |
| `update_battery_health` | VERIFIED | `src/discharge_handler.py:119` |
| `_score_and_persist_sulfation` | VERIFIED | `src/discharge_handler.py:273` |
| `_compute_sulfation_metrics` | VERIFIED | `src/discharge_handler.py:293` |
| `_persist_sulfation_and_discharge` | VERIFIED | `src/discharge_handler.py:397` |
| `_grant_blackout_credit` | VERIFIED | referenced at `src/discharge_handler.py:431` |
| `last_sulfation_score` / `last_cycle_roi` / `last_cycle_budget_remaining` | VERIFIED | `src/discharge_handler.py:110,115,116` |
| `append_sulfation_history` | VERIFIED | `src/model.py:561` |
| `append_discharge_event` | VERIFIED | `src/model.py:578` |
| `set_blackout_credit` / `clear_blackout_credit` / `get_blackout_credit` | VERIFIED | `src/model.py:835,849,889` |
| `get_last_upscmd_timestamp` | VERIFIED | `src/model.py:885` |
| `update_upscmd_result` | VERIFIED | `src/model.py:870` |
| `setdefault("sulfation_history"/"roi_history")` | VERIFIED | `src/model.py:299,301` |
| `_cap_history_entries("sulfation_history")` | VERIFIED | `src/model.py:607` (no `roi_history` cap — confirms OpenCode asymmetry note) |
| `clear_blackout_credit` call + "desulfation credit" comment | VERIFIED | `src/monitor.py:376,379` |
| `HealthSnapshot` fields `sulfation_score`/`sulfation_confidence`/`recovery_delta`/`cycle_roi` | VERIFIED | `src/monitor_config.py:303,304,307,308` + health_data at :344,345,348,349 |
| `HEALTH_ENDPOINT_PATH = /run/ups-battery-monitor/ups-health.json` | VERIFIED | `src/monitor_config.py:53` (confirms OpenCode root-owned concern) |
| exporter HealthSnapshot kwargs `sulfation_score=`/`cycle_roi=` etc. | VERIFIED | `src/virtual_ups_exporter.py:53,54,57,58` |
| `motd_status.py` `sulfation_pct` mapping + docstring | VERIFIED | `src/motd_status.py:82,12` |
| `scripts/motd/55-sulfation.sh` | VERIFIED (exists) | file present |
| `_gather_scheduler_inputs` keys + call site + log lines | VERIFIED | `src/scheduler_manager.py:317,324-329,235-236,244-250,355-356` |
| `evaluate_test_scheduling` import in scheduler_manager | VERIFIED | `src/scheduler_manager.py:14` |
| `scripts/install.sh` installs `55-sulfation.sh` | VERIFIED | `scripts/install.sh:240` (confirms Codex HIGH — NOT covered by any plan) |
| `scripts/battery-health.py:109` "sulfation" comment | VERIFIED | factual aging comment (confirms grep-gate conflict) |
| `docs/internal/CONTEXT.md` active-desulfation claims | VERIFIED | `docs/internal/CONTEXT.md:120-125` (not enumerated by Plan 03) |
| `README.md:3` "active care" tagline | VERIFIED | `README.md:3` (not enumerated by Plan 03) |

### MISSING

- None. Every cited pre-existing symbol resolved. (Note: `scripts/install.sh:240` and `docs/internal/CONTEXT.md` are present-but-uncovered by the plans, not missing — they are gaps in plan scope, surfaced as concerns above.)

### AMBIGUOUS

- `DischargeMetrics.capacity_ah_ref` / `confidence_level` — exist in source but the plans neither keep nor remove them explicitly. Resolution deferred to replan (must be enumerated).

### UNCHECKABLE / skipped

- Exact internal line numbers cited in some `<read_first>` blocks (e.g. "lines 448-451", "lines 110-117") drift by a few lines from current source after recent refactors (e.g. `evaluate_test_scheduling` is at :71, research narrative implies a "7-gate" layout). The *symbols* all resolve; only some line offsets are stale. Non-blocking — executors locate by symbol.
- IEEE-1188 / BU-804b / Vertiv BattCon evidence citations — external sources, not verifiable against this repo; treated as the planners' asserted domain basis (to live in the ADR they produce).
- Runtime behavior of `float("inf") >= 365.0` deadlock fix — logically sound and asserted by both reviewers; not executed here (no test run in a review pass).
