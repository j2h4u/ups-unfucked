# Cross-AI Result: deepseek-v4-pro

Execution attempts:

- shared: /home/j2h4u/.opencode/bin/opencode run --model deepseek/deepseek-v4-pro --agent plan --format json --variant max --attach http://127.0.0.1:51861 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T210309Z-standard-final/20260814T210327Z/inputs/natural-blackout-learning-implementation.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T210309Z-standard-final/20260814T210327Z/inputs/deepseek-v4-pro.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T210309Z-standard-final/20260814T210327Z/inputs/glm-5-3.md --title cross-ai-deepseek-v4-pro -- 'Goal / decision to support: Final standard re-review of the complete uncommitted Release A implementation. The previous GLM review found one Medium: healthy ticks after READY emitted STATUS=degraded. Verify the new explicit truth table and regression sequence first, then inspect for any remaining Critical/High/Medium regression. Return GO only when none remain. Also preserve verification of all earlier closed findings listed in attached reports. Do not edit repository source files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'

Command:

```bash
/home/j2h4u/.opencode/bin/opencode run --model deepseek/deepseek-v4-pro --agent plan --format json --variant max --attach http://127.0.0.1:51861 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T210309Z-standard-final/20260814T210327Z/inputs/natural-blackout-learning-implementation.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T210309Z-standard-final/20260814T210327Z/inputs/deepseek-v4-pro.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T210309Z-standard-final/20260814T210327Z/inputs/glm-5-3.md --title cross-ai-deepseek-v4-pro -- 'Goal / decision to support: Final standard re-review of the complete uncommitted Release A implementation. The previous GLM review found one Medium: healthy ticks after READY emitted STATUS=degraded. Verify the new explicit truth table and regression sequence first, then inspect for any remaining Critical/High/Medium regression. Return GO only when none remain. Also preserve verification of all earlier closed findings listed in attached reports. Do not edit repository source files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'
```

Output:

## Final Release A re-review — verdict: GO

Static review only; bash in this session is restricted to `git status/diff/log/show`, so `pytest`/`ruff`/`mypy` were not executed — the full test/lint gate remains an operator pre-flight.

### Prior Medium (glm M-1: healthy post-READY ticks emitted degraded) — CLOSED

- Explicit truth table at src/monitor.py:1159-1171: first fresh poll → `READY=1\nSTATUS=ready` once (:1167); healthy ticks after READY → `STATUS=ready` only (:1163); not-fresh tick → `STATUS=degraded` (:1171). Partial response degrades early at :1080, socket errors at :1225.
- Regression sequence exists in tests/test_monitor_integration.py:960-1030: incomplete poll → degraded/no READY; failed virtual-UPS write → no READY; complete poll → single READY; next healthy tick asserts `STATUS=ready` (:1009); partial after READY → degraded (:1019); recovery → `STATUS=ready` (:1030).

### All earlier closed findings re-verified intact

- Watchdog heartbeat in `finally` outside validation branches (monitor.py:1033-1046); tests test_monitor.py:1810/1829/1855.
- One journal projection per tick shared by both exporters (monitor.py:1039-1045); counters from projection, no per-event replay (test_monitor_integration.py:225).
- BaselineReset: fresh estimator/RLS/handler tracking/sag RLS constructed before commit, replaced only after `reset_baseline` (monitor.py:805-852); model transaction rolls back on save failure and preserves `last_upscmd_*` audit (model.py:679-746).
- Foreign-epoch open event closed terminal `closed_epoch_mismatch`/`history_only`, never learned (discharge_collector.py:428-467).
- Strict current schema: exact 17-key set + UUID + ranges, no rename/backup/clamp/default-injection/rewrite (model.py:388-441); monitor `_validate_model` raises instead of self-healing (monitor.py:630-651).
- Terminal dispositions: exactly-once `mark_applied` with idempotent same-hash/same-disposition re-mark and conflict error (discharge_journal.py:339-365); operational events close to `recorded_only` immediately (monitor.py:738-791); legacy markers without disposition stay raw evidence.
- One-writer lock: `flock LOCK_EX|LOCK_NB` + `O_NOFOLLOW`, mode 0600, released on constructor failure/`__del__`/shutdown (monitor.py:283-318); two-writer test at test_monitor.py:1921.
- 1s poll / 10s durable samples / 60s reporting (monitor_config.py:46, discharge_collector.py:396); torn-tail truncation under exclusive lock with `ftruncate`+`fdatasync` (discharge_journal.py:425-437, 493-494).
- IR golden direction test (tests/test_release_a_ir_golden.py); SagTracker frozen — no model writes remain in sag_tracker.py.
- Deploy runbook exists outside app code (docs/RELEASE-A-DEPLOYMENT.md), no runtime migration; scheduler dispatch structurally blocked by `SchedulerModeError` at the single command boundary (scheduler_manager.py:110-117), default `proposal_only`; systemd unit has `TimeoutStartSec=0`, `WatchdogSec=120`, `Restart=on-failure`.

### Residual Lows (non-blocking)

1. `STATUS=ready` is re-sent every ~1s healthy tick — harmless but chatty on the notify socket.
2. The degraded message at monitor.py:1171 conflates physical-poll and output-write failures; operators must read health JSON to distinguish.
3. scheduler_manager.py:104 docstring still says `capture_only` is the default while the constructor default is `proposal_only`.
4. Dead code (`update_battery_health`/`handle_discharge_complete`, `calibration_write`) remains, deferred to plan commit 18; no production callers.

No Critical, High, or Medium findings remain. Release A is ready to ship pending the standard pre-flight: full test/lint/type gate (not executable in this session), the one-time deploy conversion per docs/RELEASE-A-DEPLOYMENT.md, and the documented 24h acceptance window.
