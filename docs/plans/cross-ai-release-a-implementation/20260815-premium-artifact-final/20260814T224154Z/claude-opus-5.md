# Cross-AI Result: claude-opus-5

Execution attempts:

- direct: /home/j2h4u/.local/bin/claude -p 'Goal / decision to support: Final premium gate for committed Release A tag release-a-20260815. Adversarially verify application correctness, UPS shutdown safety, durable capture, strict state conversion/deployment rollback, and the two attached standard GO reviews. Return GO only if this exact artifact is ready for controlled deployment; otherwise provide actionable blockers.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.

Review the following copied context files in addition to repository evidence:
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-premium-artifact-final/20260814T224154Z/inputs/natural-blackout-learning-implementation.md
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-premium-artifact-final/20260814T224154Z/inputs/deepseek-v4-pro.md
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-premium-artifact-final/20260814T224154Z/inputs/glm-5-3.md' --model opus --permission-mode plan --output-format json --effort low

Command:

```bash
/home/j2h4u/.local/bin/claude -p 'Goal / decision to support: Final premium gate for committed Release A tag release-a-20260815. Adversarially verify application correctness, UPS shutdown safety, durable capture, strict state conversion/deployment rollback, and the two attached standard GO reviews. Return GO only if this exact artifact is ready for controlled deployment; otherwise provide actionable blockers.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.

Review the following copied context files in addition to repository evidence:
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-premium-artifact-final/20260814T224154Z/inputs/natural-blackout-learning-implementation.md
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-premium-artifact-final/20260814T224154Z/inputs/deepseek-v4-pro.md
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-premium-artifact-final/20260814T224154Z/inputs/glm-5-3.md' --model opus --permission-mode plan --output-format json --effort low
```

Output:

## Premium gate — `release-a-20260815` (a443b77)

I independently re-ran the gate (both prior reviews could not): `just check` → ruff format/lint, pyright, vulture, **631 passed, 82.93% coverage** (gate 80%), exit 0. The tree is clean at the tag; only untracked files are the review-artifact dirs under `docs/plans/`, which the runbook's preflight explicitly permits (it scopes untracked-check to `src systemd tests scripts`).

### Critical
None.

### High
None.

### Medium

**M-1 — Runbook has no preflight for the state directory mode, and Release A tightens it.**
`DischargeJournal` is a **new file in this commit** (does not exist at `c6c5980`), and `_validate_parent` hard-rejects any parent with group/other bits: `src/discharge_journal.py:536-540` (`if stat.S_IMODE(...) & 0o077: raise JournalPathError`). The only thing that sets that mode is `scripts/install.sh:184-185` (`mkdir -p`, `chmod 700`), and the runbook deliberately **does not run install.sh in the critical window** (`docs/RELEASE-A-DEPLOYMENT.md:14-16`). The global server convention is 755 for directories.

Failure mode: the daemon still starts (`src/monitor.py:106-112` swallows `JournalError` into `journal_error`, `self.journal = None`), READY is still sent, but health reports `journal_healthy: false` — which the acceptance step requires to be true. Result is an avoidable rollback inside the 30 s stop-to-start window, i.e. an extra unnecessary stop/start of shutdown protection.

Fix: add to the pre-stop block, next to the existing symlink/realpath checks (`RELEASE-A-DEPLOYMENT.md:71-79`):
```fish
test (stat -c %a "$state_real") = 700; or exit 1
test (stat -c %U "$state_real") = (id -un); or exit 1
```
I could not verify the live mode of `~/.config/ups-battery-monitor` — it is outside my read boundary. If it is already 0700, this is a documentation hardening only; if not, it is a real aborted deployment.

### Low (non-blocking, no change required for this window)

1. `model_update_mode` is hardcoded `"capture_only"` in the health snapshot regardless of the actual scheduler mode (`src/virtual_ups_exporter.py:107-111`), while `automatic_dispatch` right below it is correctly derived. Correct today; it becomes a false operator-facing assertion the moment a later release flips modes, and the acceptance check `jq '.model_update_mode == "capture_only"'` therefore proves nothing.
2. Restore paths still assert `len(d) == 13` (`RELEASE-A-DEPLOYMENT.md:234, 306`) rather than the keyset equality used in prep (:81-82). Risk is low because the same backup already passed the strict keyset check pre-stop, but the weaker check is what runs when things are going wrong.
3. `mark_applied` conflict permanently degrades the journal (`src/discharge_journal.py:329-353` → `src/monitor.py:539-547`). Unreachable in capture-only — there is no production producer of `controlled_capacity_test` (confirmed: the only occurrences in `src/` are the two rejection reasons in `discharge_collector.py:561,582` and the gate at `discharge_handler.py:328`) — but it inverts "fail visible, keep capturing" once applications become real in Release C/D.
4. Physical raw `LB` is never passed through to the virtual device: `compute_ups_status_override` (`src/virtual_ups.py`) derives status purely from the model, and the only raw passthrough is the OB guard at `virtual_ups_exporter.py:258-268`. This is the deliberate premise of the project (firmware lies ~2×) and the 2-minute `SAFETY_LB_FLOOR_MINUTES` backstops it, so it is not a regression — but it means the sole shutdown trigger remains model-derived, and the plan's own §6 wants raw LB as an independent terminal marker. Worth stating explicitly in the acceptance notes rather than leaving implicit.
5. `main()` runs `--new-battery` reset and then falls into the poll loop (`src/monitor.py:1340-1347`); it also cannot run while the service is up (writer lock at `:289-306`). Correct behaviour, undocumented ergonomics.
6. Dead legacy API `_auto_calibrate_peukert`/`_log_discharge_prediction` survives with test-only callers (plan commit #18 cleanup deferred).

### Independently re-verified (clear)

- **Watchdog cannot be starved by NUT.** `sd_notify("WATCHDOG=1")` is in the `finally` of `_poll_once` (`monitor.py:1076-1089`), covering both the invalid-reply early returns and exceptions from `get_ups_vars()`; the NUT socket has a 2 s timeout with a monotonic read deadline (`nut_client.py:41,145-148`), so a single tick cannot approach `WatchdogSec=120`.
- **READY is a genuine postcondition.** Sent only when `physical_poll_valid and virtual_ups_ok and health_ok` (`monitor.py:1196-1212`); degraded paths set `startup_degraded` and emit `STATUS=degraded` without ever fabricating `OL`. `TimeoutStartSec=0` in the unit makes this safe, and `bounded_start 10` in the runbook means the operator's wait is bounded even though systemd's is not — with `Type=notify`, `ActiveState=active` *is* proof of a valid poll plus both output writes. That is a stronger acceptance signal than the runbook claims for itself.
- **Capture-only is structural, not configured.** `dispatch_test_with_audit` raises `SchedulerModeError` for anything but `execute` (`scheduler_manager.py:111-118`); the model-application gate requires `controlled_capacity_test` and no production code produces it.
- **Single writer / durability.** `O_NOFOLLOW` + `flock(LOCK_EX|LOCK_NB)` acquired before mutable state opens, released via explicit path and `__del__` (`monitor.py:289-321`); journal enforces regular-file + 0600 + `O_EXCL`-then-open, torn-tail `ftruncate`+`fdatasync`+parent sync (`discharge_journal.py:474-528`), mid-file corruption is a hard error.
- **Epoch isolation.** Replay skips any event whose start payload epoch ≠ current persisted epoch, retaining it verbatim as raw evidence (`monitor.py:1093-1110` region / `_replay_closed_events`); operational events get a terminal `recorded_only` marker at close and on replay — no eternal `pending_replay`.
- **Rollback ordering.** `restore_after_replace` stops first and accepts only exact `inactive`/`failed`, installs the rollback unit + `daemon-reload` **before** returning the 13-key file, then `bounded_start 30` (`RELEASE-A-DEPLOYMENT.md:222-249`). The strict loader never sees the backup. Ordering is locked by `tests/test_release_a_deployment_doc.py:156-160`.
- **`reset_baseline`** refuses to run with an open journal or collector event, builds all replacement runtime objects before the commit boundary, and re-baselines the capture-only fingerprint (`monitor.py:818-860`).

### Verdict: **GO**, conditional on one pre-window action

The artifact is correct, safe, and deployable as-is. Before opening the window, run `stat -c '%a %U' ~/.config/ups-battery-monitor` and confirm `700` + the service user (M-1); if it is not 0700, `chmod 700` it before the stop, or the new journal will refuse to open and the acceptance check will force an otherwise unnecessary rollback. Everything else above is post-window follow-up.

Not verified inside my boundary: live filesystem state under `~/.config`, actual UPS/NUT behaviour, and GitHub CI status for this commit (CI runs on `main` only; the artifact sits on `feat/natural-blackout-learning`, so the local gate I ran is the only green record for this tag).
