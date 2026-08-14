# Cross-AI Result: glm-5-3

Execution attempts:

- shared: /home/j2h4u/.opencode/bin/opencode run --model zai-coding-plan/glm-5.3 --agent plan --format json --variant max --attach http://127.0.0.1:40193 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-keyguard-rerun/20260814T222100Z/inputs/natural-blackout-learning-implementation.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-keyguard-rerun/20260814T222100Z/inputs/deepseek-v4-pro.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-keyguard-rerun/20260814T222100Z/inputs/glm-5-3.md --title cross-ai-glm-5-3 -- 'Goal / decision to support: Standard final re-review after fixing DeepSeek M-1. Inspect current tree. Verify live model.json and retained backup are both rejected unless they have exactly the documented 13 legacy keys before jq or service stop; verify the guard precedes conversion, cd/PYTHONPATH is correct, the degraded RuntimeDirectory window is explicit, and prior Release A safety/capture-only/partial-NUT fixes remain intact. Full local gate passed: 631 tests, 82.93% coverage, ruff, pyright, vulture, shellcheck, compileall. Return GO only if no actionable Critical, High, or Medium finding remains. Do not edit files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'

Command:

```bash
/home/j2h4u/.opencode/bin/opencode run --model zai-coding-plan/glm-5.3 --agent plan --format json --variant max --attach http://127.0.0.1:40193 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-keyguard-rerun/20260814T222100Z/inputs/natural-blackout-learning-implementation.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-keyguard-rerun/20260814T222100Z/inputs/deepseek-v4-pro.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260815-standard-keyguard-rerun/20260814T222100Z/inputs/glm-5-3.md --title cross-ai-glm-5-3 -- 'Goal / decision to support: Standard final re-review after fixing DeepSeek M-1. Inspect current tree. Verify live model.json and retained backup are both rejected unless they have exactly the documented 13 legacy keys before jq or service stop; verify the guard precedes conversion, cd/PYTHONPATH is correct, the degraded RuntimeDirectory window is explicit, and prior Release A safety/capture-only/partial-NUT fixes remain intact. Full local gate passed: 631 tests, 82.93% coverage, ruff, pyright, vulture, shellcheck, compileall. Return GO only if no actionable Critical, High, or Medium finding remains. Do not edit files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'
```

Output:

# Release A final re-review — verdict: **GO**

Static verification of the current tree (bash restricted to git read commands; live `~/.config` state and retained backup are outside the readable boundary — their keysets are exercised indirectly by the doc-contract tests, and the full gate is reported passed by the operator).

## DeepSeek M-1 — CLOSED

- **Both files key-guarded before anything destructive**: `docs/RELEASE-A-DEPLOYMENT.md:59-61` runs an exact-keyset assertion (`set(data) == expected`) against the live `$model` **and** the retained `$backup_model` with exactly the 13 documented legacy keys. The jq transform (:88-106) and `systemctl stop` (:196) come strictly later; contract test pins this ordering (`tests/test_release_a_deployment_doc.py:141-144`).
- **The guard is load-bearing, not decorative**: the four jq-hardcoded keys (`capacity_estimates`, `capacity_ah_measured`, `battery_epoch_id`, `new_battery_detected_timestamp`) do not exist in the 13-key legacy schema (verified against `c6c5980:src/model.py` — the old loader's setdefaults produce exactly those 13), so with the guard the overwrite cannot discard learned state; if the old daemon somehow wrote any Release-A key, the guard aborts.
- **TOCTOU covered**: sha is captured immediately after the guard (:62-63), re-verified pre-stop (:128-129) and post-stop (:198-199) — any write between guard and conversion aborts the runbook.
- **Guard is really executed in tests**: `test_legacy_guard_accepts_exact_copy_and_rejects_schema_changes` (test file:174-201) runs the exact guard snippet against a private copy of the real retained backup and proves rejection of each of the 4 added keys, a removed key, and an unknown key; `test_documented_transform_validates_private_copy_of_retained_backup` (:150-171) runs the real jq transform and proves strict `BatteryModel` acceptance.

## L-1 (cd/PYTHONPATH) — fixed

`cd "$repo"; or exit 1` (runbook:125) precedes the strict-loader import (:126); `python3 -c` puts cwd on `sys.path`. Ordering pinned by the contract test (:145).

## L-2 (degraded RuntimeDirectory window) — explicit

Runbook:149-152 now states plainly that stop removes `RuntimeDirectory`, virtual UPS/shutdown protection are unavailable, and protection is degraded only within the `<30s` stop-to-start window, which is why the OL preflight immediately before stop (:195) and the pre-staged rollback are mandatory. Strings pinned by the test (:121-124).

## Prior Release A fixes — intact (direct re-check)

| Fix | Evidence |
|---|---|
| Partial-NUT false-OL | `_physical_reply_is_valid` (src/monitor.py:874-906) gating at :1110 before any pipeline work; exporter `_has_usable_status` (src/virtual_ups_exporter.py:44-62) refuses writes; no `OL` fallback remains |
| Watchdog/READY | `WATCHDOG=1` in `finally` (monitor.py:1089); READY only after valid poll + both outputs (:1203-1215) |
| Strict 17-key loader | `_require_current_schema` + full nested validation, UUID epoch (src/model.py:382-574); `KNOWN_STATE_KEYS` = exactly the 17 keys the jq transform emits |
| Rollback never runs strict code on old state | restore paths install rollback unit + `daemon-reload` **before** returning the 13-key file (runbook:180-191, 247-254) |
| Capture-only | `_validate_application_input` requires `controlled_capacity_test` (src/discharge_handler.py:328) — no producer in `src/` |
| Scheduler blocking | `SchedulerModeError` at the single dispatch boundary + fails-closed on missing poll values (src/scheduler_manager.py:111-125) |
| Single writer | `flock LOCK_EX\|LOCK_NB` (monitor.py:299) |

## Residual Low (non-blocking)

1. Contract test pins the guard's *first* occurrence before jq/stop but doesn't assert **both** guard invocations (`$model` and `$backup_model`) exist — a future edit deleting only the live-model line would pass the test. Runbook is correct today; test-hardening only.
2. Restore paths use `len(d) == 13` rather than keyset equality — acceptable secondary check since the backup was keyset-verified and sha-pinned at prep, and the old loader is lenient.
3. Previously acknowledged Lows unchanged: `render_motd` propagates `ModelLoadError`, dead delegates deferred to plan commit 18, `_recv_until` silent partial reply.

**No Critical/High/Medium findings remain — GO.** The one-time 13→17-key conversion and rollback procedure are executable as documented, and strict code never runs against old state.
