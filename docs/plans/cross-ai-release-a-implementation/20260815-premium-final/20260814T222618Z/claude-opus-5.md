# Cross-AI Result: claude-opus-5

Execution attempts:

- direct: /home/j2h4u/.local/bin/claude -p 'Goal / decision to support: Premium final gate for complete uncommitted Release A after two standard GO verdicts and full local release gate. Inspect current source/tests/runbook directly. Return GO only if no actionable Critical, High, or Medium correctness, UPS shutdown-safety, data-integrity, architecture, deployment, or test-validity finding remains. Re-check prior premium H-1 false OL, prior M-1 deploy procedure, exact live+backup 13-key guard, rollback ordering, capture-only journaling/exactly-once dispositions, watchdog/READY, epoch isolation, dead legacy API removal, and whether 631-test 82.93% gate meaningfully covers failure paths. Do not edit files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.

Review the following copied context files in addition to repository evidence:
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-premium-final/20260814T222618Z/inputs/natural-blackout-learning-implementation.md
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-premium-final/20260814T222618Z/inputs/deepseek-v4-pro.md
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-premium-final/20260814T222618Z/inputs/glm-5-3.md' --model opus --permission-mode plan --output-format json --effort low

Command:

```bash
/home/j2h4u/.local/bin/claude -p 'Goal / decision to support: Premium final gate for complete uncommitted Release A after two standard GO verdicts and full local release gate. Inspect current source/tests/runbook directly. Return GO only if no actionable Critical, High, or Medium correctness, UPS shutdown-safety, data-integrity, architecture, deployment, or test-validity finding remains. Re-check prior premium H-1 false OL, prior M-1 deploy procedure, exact live+backup 13-key guard, rollback ordering, capture-only journaling/exactly-once dispositions, watchdog/READY, epoch isolation, dead legacy API removal, and whether 631-test 82.93% gate meaningfully covers failure paths. Do not edit files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.

Review the following copied context files in addition to repository evidence:
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-premium-final/20260814T222618Z/inputs/natural-blackout-learning-implementation.md
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-premium-final/20260814T222618Z/inputs/deepseek-v4-pro.md
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-premium-final/20260814T222618Z/inputs/glm-5-3.md' --model opus --permission-mode plan --output-format json --effort low
```

Output:

## Verdict: **NO-GO (qualified)** — one High and two Mediums remain, all in the deployment runbook, not in the application code

The source and test tree hold up under re-inspection; every prior premium item I was asked to re-check is genuinely intact. The blockers are in `docs/RELEASE-A-DEPLOYMENT.md`'s failure-handling, and they interact with a unit-file change made in this same release.

---

### HIGH — `restore_after_replace` can "roll back" while the new strict daemon is still alive, and report success
`docs/RELEASE-A-DEPLOYMENT.md:166-193`

The function never stops the service before installing the rollback unit, `mv`-ing the 13-key backup over `$model`, and calling `systemctl start`. The manual rollback section at :240-254 *does* stop first — the in-window automatic path contradicts the procedure the same document declares correct.

Failure scenario (reachable, not theoretical): `sudo systemctl start` at :207 returns non-zero, or `systemctl is-active --quiet` at :208 fails because the unit is in **`activating`** — `is-active` only succeeds for `active`/`reloading`, and a `Type=notify` unit that has not yet sent `READY=1` sits in `activating`. `READY=1` is a postcondition of a valid physical poll *plus* both output writes (`src/monitor.py:1203-1215`), so a few seconds of NUT flakiness at switchover puts the unit exactly there. Then:

1. rollback unit installed + `daemon-reload` — does not touch the running process;
2. 13-key `model.json` written **under a live strict daemon** (which may still save 17-key state over it);
3. `systemctl start` on an already-activating unit is a no-op/blocks → the old checkout never runs;
4. final `systemctl is-active --quiet` then passes → **no `CRITICAL` printed**.

The operator ends the window believing rollback succeeded while the new code is live over legacy state, on a headless host. Fix is the same three lines the manual path already uses: `stop`, verify not active (treating `activating` as active), then proceed.

### MEDIUM — `systemctl start` in the critical window is unbounded, and this release is what made it so
`systemd/ups-battery-monitor.service:24` (`TimeoutStartSec=0`) vs `git show c6c5980:systemd/…` (`TimeoutStartSec=30`); runbook :207, :253

With `Type=notify` + `TimeoutStartSec=0`, `systemctl start` blocks **indefinitely** whenever NUT does not yield a valid reply — which is precisely the cold-start/driver-race condition this host is known to hit. The runbook states the target stop-to-start window is <30s and has no bounded wait (`timeout`, `--no-block` + poll) anywhere. Practical protection is not lost (the daemon polls and recovers on its own), but the operator's session hangs, the documented window is void, and an impatient `Ctrl-C` + `is-active` check funnels straight into the High above. The `TimeoutStartSec=0` rationale in the unit comment is sound for steady-state; the runbook needs a bounded start check to match.

### MEDIUM — the artifact being deployed is an uncommitted working tree; only the rollback side is pinned
`git status` (36 modified + 12 untracked files at HEAD `c6c5980`); runbook :72-75

The runbook rigorously pins the rollback commit (`git worktree add --detach … c6c5980`, HEAD re-verified at :242) but the *forward* deployment is `$repo` itself — a dirty tree. The live service therefore has no version identity, any later `git checkout`/`stash`/`just check` in `$repo` silently mutates production, and re-upgrade after a rollback depends on that uncommitted diff surviving. Commit (or tag) the branch before the window and pin `release_a_commit` symmetrically with `rollback_commit`; this also makes the newly-added worktree operations unambiguous.

---

### Re-checks requested — all clear

| Item | Evidence |
|---|---|
| Prior H-1 false OL | `_physical_reply_is_valid` (`src/monitor.py:872-905`) gates before any pipeline work at :1110; exporter `_has_usable_status` (`src/virtual_ups_exporter.py:43-52`) refuses writes and `_build_virtual_metrics` re-asserts it; OB passthrough guard :255-266; no `"OL"` default |
| Prior M-1 13-key guard | exact `set(data) == expected` on **both** `$model` and `$backup_model` (:59-61), strictly before worktree add, jq (:88), and stop (:196); 13 keys match `c6c5980:src/model.py`; no `//` fallback in the transform |
| Rollback ordering | rollback unit + `daemon-reload` precede returning the 13-key file in both restore paths — strict loader never sees legacy state (subject to the High above) |
| Capture-only / exactly-once | `_validate_application_input` requires `controlled_capacity_test` (`src/discharge_handler.py:325`), no producer in `src/`; dispositions terminal and idempotent (`discharge_journal.py:329-353`); `_replay_closed_events` skips events already marked |
| Watchdog / READY | `WATCHDOG=1` in `finally` regardless of validation branch (`monitor.py:1084-1089`); READY only after valid poll + both writes (:1203-1215) |
| Epoch isolation | replay and open-event recovery both drop non-matching `battery_epoch_id` (`monitor.py:421-427`, `discharge_collector.py:414-455`), retaining evidence as history-only |
| Journal durability | `fdatasync` per append, parent dir fsync, `O_NOFOLLOW`/regular-file/0600 enforcement, torn-tail `ftruncate`+`fdatasync`, middle corruption = hard error |
| 631 tests / 82.93% | Meaningful on failure paths, not just happy paths: torn tail vs unknown schema, middle corruption, sync-call assertions, byte caps, save-failure rollback, post-commit failure semantics, replay-degradation. Note the enforced gate is `--cov-fail-under=80` (`justfile:26`, `ci.yml:45`); 82.93% is the measured value, not a floor. |

### Lows (non-blocking)
- **Dead legacy API not removed:** `monitor._auto_calibrate_peukert` / `_log_discharge_prediction` (`src/monitor.py:804-811`) have no production caller, yet ~20 tests in `test_monitor.py` / `test_monitor_integration.py` reach the handler *through* them — coverage that looks like production coverage but isn't.
- **`mark_applied` conflicts degrade the journal permanently:** any `JournalError` — including the pure-bookkeeping "different applied hash or disposition" — flows into `_journal_degraded` → `journal.mark_degraded`, and `start_event` refuses while unhealthy, so all future capture stops until restart. Not reachable on any Release-A capture-only path I could trace, but it inverts the ADR's "fail-visible, keep capturing" intent once applications become real.
- **Restore paths use `len(d) == 13`, not keyset equality** (:176, :249) — weaker than the prep guard it mirrors.
- **No freshness contract on the virtual `.dev`:** when a poll is rejected or NUT errors, nothing is written and `dummy-ups` keeps serving the last values indefinitely. Pre-existing architecture, not a regression; I could not verify whether `upsmon` also monitors the physical UPS (`/etc/nut` is outside the review boundary).
- `render_motd` still propagates `ModelLoadError` to the login banner (`src/motd_status.py:83`).

**Bottom line:** the code is ship-ready; the runbook is not. Fix the `restore_after_replace` stop-and-verify, add a bounded start check, and commit the release artifact — then this is a GO. No files were edited.
