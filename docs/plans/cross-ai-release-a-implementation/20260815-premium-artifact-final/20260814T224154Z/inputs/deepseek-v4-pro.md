# Cross-AI Result: deepseek-v4-pro

Execution attempts:

- shared: /home/j2h4u/.opencode/bin/opencode run --model deepseek/deepseek-v4-pro --agent plan --format json --variant max --attach http://127.0.0.1:39149 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-artifact-final/20260814T223645Z/inputs/natural-blackout-learning-implementation.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-artifact-final/20260814T223645Z/inputs/claude-opus-5.md --title cross-ai-deepseek-v4-pro -- 'Goal / decision to support: Review the committed Release A implementation at tag release-a-20260815 for correctness, safety, architecture, tests, and deployment readiness. Verify that the three prior premium blockers in the attached report are resolved. Return GO only if this exact artifact is ready for controlled deployment; otherwise give actionable blockers.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'

Command:

```bash
/home/j2h4u/.opencode/bin/opencode run --model deepseek/deepseek-v4-pro --agent plan --format json --variant max --attach http://127.0.0.1:39149 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-artifact-final/20260814T223645Z/inputs/natural-blackout-learning-implementation.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-artifact-final/20260814T223645Z/inputs/claude-opus-5.md --title cross-ai-deepseek-v4-pro -- 'Goal / decision to support: Review the committed Release A implementation at tag release-a-20260815 for correctness, safety, architecture, tests, and deployment readiness. Verify that the three prior premium blockers in the attached report are resolved. Return GO only if this exact artifact is ready for controlled deployment; otherwise give actionable blockers.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'
```

Output:

# Release A artifact review — `release-a-20260815` (a443b77)

## Findings

**Critical:** none.
**High:** none.
**Medium:** none.

### Prior premium blockers — verification

| Blocker | Status | Evidence |
|---|---|---|
| H-1: `restore_after_replace` rolls back under live strict daemon | **Resolved** | `stop_and_require_inactive` runs first and accepts only `inactive`/`failed` (`docs/RELEASE-A-DEPLOYMENT.md:176-187, 222-224`); model `mv` only after verified stop (:243); ordering `restore_stop < restore_stage < restore_mv` locked by `tests/test_release_a_deployment_doc.py:156-160` |
| M-1: unbounded `systemctl start` in critical window | **Resolved** | `bounded_start` = `start --no-block` + bounded ActiveState polling, `failed`/timeout → CRITICAL (:189-205); budgets 10s forward / 30s rollback match old unit's `TimeoutStartSec=30` (verified `git show c6c5980:systemd/…`); no bare `systemctl start` remains (asserted :155) |
| M-2: forward artifact uncommitted | **Resolved** | Tag `release-a-20260815` exists and equals HEAD; runbook requires HEAD==tag, `git diff --quiet` vs tag, zero untracked files in `src/systemd/tests/scripts` (:44-56). Current tree satisfies preflight (only untracked: planning docs under `docs/`) |

### Independent code re-checks (all clear)

- **No false OL**: `_physical_reply_is_valid` gates before pipeline work (`src/monitor.py:873-906, 1110`); exporter refuses writes without usable status (`src/virtual_ups_exporter.py:44-53`) and re-asserts in `_build_virtual_metrics` (:206); OB passthrough guard (:258-268).
- **Capture-only**: `dispatch_test_with_audit` raises for non-`execute` modes (`src/scheduler_manager.py:111-118`); `model_update_mode="capture_only"`, `automatic_dispatch=False` in health (`src/virtual_ups_exporter.py:107-111`).
- **Exactly-once dispositions**: idempotent `mark_applied` (`src/discharge_journal.py:329-353`); operational events get terminal `recorded_only` at close and on replay (`src/monitor.py:440-445, 764-778`) — no eternal `pending_replay`.
- **Watchdog/READY**: `WATCHDOG=1` in `finally` (`monitor.py:1084-1089`); READY only after valid poll + both outputs (:1203-1212); `TimeoutStartSec=0` documented in unit.
- **Epoch isolation**: replay and counters use only current `battery_epoch_id` (`monitor.py:421-427, 568-574`); epoch-mismatch open events closed history-only (`src/discharge_collector.py:414-455`); every new start carries the epoch UUID (:301).
- **Strict loader**: exact 17-key schema + nested `physics`/`rls`/`lut` validation, no rename/backup/clamp/rewrite (`src/model.py:382-574`); malformed → startup error.
- **Frozen IR**: `SagTracker` is observation-only (`src/sag_tracker.py:160-220`); golden test locks persisted-`ir_k` direction and LB envelope with unchanged model hash (`tests/test_release_a_ir_golden.py`).
- **13-key guard**: keyset equality on both `$model` and `$backup_model` before any mutation (`RELEASE-A-DEPLOYMENT.md:81-82`); rollback unit installed before 13-key file return in both restore paths.
- **Single writer**: `O_NOFOLLOW` + `flock LOCK_EX|LOCK_NB` held from construction to shutdown (`monitor.py:291-321`); journal enforces 0600/0700, torn-tail `ftruncate`+`fdatasync`, middle corruption = hard error (`src/discharge_journal.py:475-528`).
- **`reset_baseline`**: one transaction, fresh LUT/physics/RLS, single `SoH=1` entry, preserves `last_upscmd_*` audit, rollback on save failure (`src/model.py:712-773`).
- **Runbook covered by tests**: `tests/test_release_a_deployment_doc.py` asserts ordering, `--no-block`, bounded budgets, keyset guard before jq, no `//` fallback.

### Lows (non-blocking)

1. Restore paths still check `len(d) == 13` instead of keyset equality (`RELEASE-A-DEPLOYMENT.md:234, 306`) — weaker than the prep guard it mirrors.
2. Dead legacy API `_auto_calibrate_peukert`/`_log_discharge_prediction` remain (`src/monitor.py:804-812`), covered only through tests (plan commit #18 cleanup deferred).
3. `mark_applied` conflict (different hash/disposition) degrades the journal permanently (`discharge_journal.py:345-347` → `monitor.py:539-547`); unreachable on capture-only paths, but inverts "fail-visible, keep capturing" once applications become real.
4. No freshness contract on virtual `.dev` when polls are invalid/NUT is down — pre-existing; `STATUS=degraded` mitigates observability.
5. `render_motd` propagates `ModelLoadError` to the login banner (`src/motd_status.py:83`).
6. **Process**: CI runs only on main pushes/PRs; a443b77 lives on `feat/natural-blackout-learning`, so no GitHub CI record exists for this artifact. Local `just check` mirrors CI gates exactly and the pre-push hook enforces it, but the CI badge for the release tag is absent.

### Limitations

This session's command policy allowed only `git diff/status/log/show`; I could not re-run `pytest`/`just check` myself. Test figures (631 tests, 82.93%, `--cov-fail-under=80`) come from the prior premium review and the enforced pre-push hook.

## Verdict: **GO**

The three prior premium blockers are genuinely fixed and locked by tests; no Critical/High/Medium correctness, shutdown-safety, data-integrity, architecture, deployment, or test-validity finding remains in the artifact at tag `release-a-20260815`. Recommended follow-ups before/around the window: L-1 (reuse the keyset guard in restore paths) and L-6 (merge/push to main so CI runs on the tag).
