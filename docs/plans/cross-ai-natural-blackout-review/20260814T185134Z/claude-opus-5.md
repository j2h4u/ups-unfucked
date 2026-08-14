# Cross-AI Result: claude-opus-5

Execution attempts:

- direct: /home/j2h4u/.local/bin/claude -p 'Goal / decision to support: Premium final gate after both standard reviewers returned GO. Adversarially verify the complete implementation plan against current source, with special attention to the single-host one-way state migration, battery_epoch_id stamped in start records and rotated by BaselineReset, preservation of valuable legacy journal evidence without old-binary compatibility obligations, scientific fingerprint, capture-only safety, battery model update gates, transaction/replay behavior, runtime prediction invariants, and staged lightweight domain model. Return GO only if implementation can proceed without actionable Critical/High/Medium findings. Do not edit repository files outside the cross-ai output directory.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.

Review the following copied context files in addition to repository evidence:
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T185134Z/inputs/natural-blackout-learning-implementation.md
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T185134Z/inputs/deepseek-v4-pro.md
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T185134Z/inputs/glm-5-3.md' --model opus --permission-mode plan --output-format json --effort low

Command:

```bash
/home/j2h4u/.local/bin/claude -p 'Goal / decision to support: Premium final gate after both standard reviewers returned GO. Adversarially verify the complete implementation plan against current source, with special attention to the single-host one-way state migration, battery_epoch_id stamped in start records and rotated by BaselineReset, preservation of valuable legacy journal evidence without old-binary compatibility obligations, scientific fingerprint, capture-only safety, battery model update gates, transaction/replay behavior, runtime prediction invariants, and staged lightweight domain model. Return GO only if implementation can proceed without actionable Critical/High/Medium findings. Do not edit repository files outside the cross-ai output directory.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.

Review the following copied context files in addition to repository evidence:
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T185134Z/inputs/natural-blackout-learning-implementation.md
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T185134Z/inputs/deepseek-v4-pro.md
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T185134Z/inputs/glm-5-3.md' --model opus --permission-mode plan --output-format json --effort low
```

Output:

# Verdict: **GO** — no actionable Critical/High/Medium findings

Verdict written to `/home/j2h4u/.claude/plans/goal-decision-to-curious-castle.md`. No repository files edited.

## First, a caveat about the two prior GOs

Both standard reviewers verified a **994-line** revision whose epoch key was `battery_epoch_started_at` (an RFC 3339 timestamp). The current plan (996 lines) uses a **UUID `battery_epoch_id`** with a `"legacy-pre-epoch"` sentinel (plan:368, 445, 471, 506, 607, 611, 678). Their GO does not cover the shipped mechanism, so I verified it independently. It is strictly stronger: it eliminates DeepSeek's Low-3 (a forward NTP step admitting a pre-reset event into a cohort — the epoch axis is no longer wall-clock at all), and their Low-1 (epoch key now in `scientific_fingerprint`, plan:471) and Low-2 (commit 17 renamed "derived-SoH-guarded", plan:956) are closed in this revision.

## Source-verified

- **Learning gap is real**: `model_processing_eligible=False` at `src/discharge_collector.py:222,291,471,523`; `_validate_application_input` rejects all but `controlled_capacity_test` (`src/discharge_handler.py:405-414`); no producer of such an event exists.
- **`pending_replay` trap is real**: `src/monitor.py:271-280` — legacy `evidence_class="operational"` events reach `apply_completed_discharge`, return `skipped`, and set `_pending_replay=True` forever.
- **Capture-only safety holds**: the only live scientific writer during a normal blackout is `sag_tracker.set_ir_k` (`src/sag_tracker.py:198-203`), frozen by Stage 0.2. `handle_discharge_complete` / `_handle_capacity_convergence` (which sets `new_battery_detected`) and `update_battery_health` / `_auto_calibrate_peukert` have **no production caller** — only `tests/test_monitor_integration.py`. That matches commit 18's dead-pipeline cleanup.
- **Fingerprint list** (plan:468-472) matches real `ModelState` keys (`src/model.py:24-58`), with `cycle_count`/scheduler state correctly excluded; the fail-fast loader (`src/model.py:391`) accommodates adding `battery_epoch_id` to the TypedDict.
- **Journal/transaction**: v1 schema, 64 KiB line envelope, existing `monotonic_ns`, LUT pruned ≤200 — the 48 KiB snapshot budget is feasible; `mark_applied` idempotency is per-event only (`src/discharge_journal.py:325-338`), consistent with "existing markers are terminal, never re-compared".
- **Readiness/watchdog gaps real** (`src/monitor.py:812,836` vs `Type=notify`/`WatchdogSec=120`); **unconditional `new_battery_detected=False`** at `src/monitor.py:224`; `_reset_battery_baseline` (`src/monitor.py:514-551`) touches neither that flag nor `battery_install_date` — the Stage 3 gap is correctly identified.

## Low findings (non-blocking)

1. **Legacy mapping precedence unstated** — existing shutdown records carry `evidence_class="operational_partial"` *and* `lifecycle="closed_shutdown_requested"` with no safety context (`src/discharge_collector.py:214-222`), while Stage 1 asserts both rules (plan:526-531). State explicitly: missing safety context ⇒ `operational_partial`.
2. **`collector.shutdown()` fires on any SIGTERM during OB**, not only policy shutdown — make the `shutdown_imminent=true` condition normative so scenarios 8 and 25 can't collide.
3. **Migration vs fingerprint-baseline ordering** — the deploy migration writes a fingerprint field. Sequence it: snapshot → migration → startup repair → baseline fingerprint → arm alarm.
4. **"persisted hash" is a whole-file SHA-256** (`src/model.py:696-706`) that changes on every save; §9's invariant wording should say "hash as of marker write, never re-compared", or use `scientific_fingerprint` there.

Implementation can proceed; these four are wording/sequencing clarifications, not blockers.
