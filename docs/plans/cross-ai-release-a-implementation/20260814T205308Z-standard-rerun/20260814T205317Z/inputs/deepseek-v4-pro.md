# Cross-AI Result: deepseek-v4-pro

Execution attempts:

- shared: /home/j2h4u/.opencode/bin/opencode run --model deepseek/deepseek-v4-pro --agent plan --format json --variant max --attach http://127.0.0.1:56453 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T201608Z-standard/20260814T201619Z/inputs/natural-blackout-learning-implementation.md --title cross-ai-deepseek-v4-pro -- 'Goal / decision to support: Review the complete uncommitted Release A implementation against docs/plans/natural-blackout-learning-implementation.md and the current source diff. Inspect source and tests, not only the plan. Return GO only when there are no actionable Critical, High, or Medium findings. Focus on capture-only scientific immutability; strict current schema with no runtime migration, fallback, self-heal, or startup rewrite; current battery epoch isolation; durable append-only journal, replay and torn-tail recovery; exactly-once recorded_only terminal markers; one-writer locking; READY/watchdog/systemd behavior; one-second physical polling and ten-second durable sampling; explicit output paths; structural scheduler command prohibition; transactional BaselineReset; installer stop-before-state-mutation; and regression risk. Do not edit repository source files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'

Command:

```bash
/home/j2h4u/.opencode/bin/opencode run --model deepseek/deepseek-v4-pro --agent plan --format json --variant max --attach http://127.0.0.1:56453 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T201608Z-standard/20260814T201619Z/inputs/natural-blackout-learning-implementation.md --title cross-ai-deepseek-v4-pro -- 'Goal / decision to support: Review the complete uncommitted Release A implementation against docs/plans/natural-blackout-learning-implementation.md and the current source diff. Inspect source and tests, not only the plan. Return GO only when there are no actionable Critical, High, or Medium findings. Focus on capture-only scientific immutability; strict current schema with no runtime migration, fallback, self-heal, or startup rewrite; current battery epoch isolation; durable append-only journal, replay and torn-tail recovery; exactly-once recorded_only terminal markers; one-writer locking; READY/watchdog/systemd behavior; one-second physical polling and ten-second durable sampling; explicit output paths; structural scheduler command prohibition; transactional BaselineReset; installer stop-before-state-mutation; and regression risk. Do not edit repository source files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'
```

Output:

# Release A implementation review — verdict: NO-GO

**Limitation:** the Bash tool in this session allowed only `git diff/status/log/show`, so I could not run `pytest`/`ruff`/`mypy`. This review is static (source + test inspection) only. All findings below are from reading code and tests inside the repo boundary.

## Critical
None.

## High

**H1. Watchdog heartbeat starves on partial/unusable NUT responses → systemd kill → restart loop → monitor down.**
`_poll_once` returns early when `battery.voltage`/`ups.load` is missing or out of range (`src/monitor.py:872-878`), before the only success-path `sd_notify("WATCHDOG=1")` at `src/monitor.py:963`. The exception handler that does send WATCHDOG (`src/monitor.py:1017`) fires only when `get_ups_vars` *raises*. A NUT server answering with a partial `LIST VAR` (missing `battery.voltage`, e.g. during driver re-init) or a stuck out-of-range voltage produces no heartbeat for >120 s (`WatchdogSec=120`, `systemd/ups-battery-monitor.service:12`), systemd SIGABRTs the process, `Restart=on-failure` (`:21`) restarts it, and `StartLimitBurst=3` (`:7`) then leaves the unit failed — monitoring and the virtual UPS/LB path dead. This directly contradicts plan §0.6 ("Watchdog heartbeat … независимо от успешности NUT poll; NUT outage дольше WatchdogSec=120 не вызывает restart") and the scenario-21 partial-response branch. No test covers it (no `ready_sent`/`STATUS=degraded`/partial-response tests exist). Fix: emit `WATCHDOG=1` per loop iteration regardless of data validity (keep READY gating as-is), and add the scenario-21 tests.

## Medium

**M1. A foreign/unknown-epoch open journal event wedges journaling forever.**
`_recover_open_event` deliberately leaves an open event whose start lacks the current `battery_epoch_id` unclosed (`src/discharge_collector.py:422-434`), but the journal constructor keeps `_open_event_id` set (`src/discharge_journal.py:216`), so every subsequent `start_event` raises "already open" (`src/discharge_journal.py:284-285`) and capture silently degrades after one warning log. Trap is easy to hit: daemon crashes mid-OB, operator "repairs" by deleting a malformed `model.json` → fresh defaults mint a new UUID → wedge (BaselineReset itself is correctly blocked while open, so the sanctioned path can't cause it, but manual recovery can). Fix: close such open events with an explicit terminal end marker (e.g. `closed_epoch_mismatch`) instead of ignoring them.

**M2. `_journal_health`/`_journal_counters` do O(events) full-file replays, ~2× per second.**
`_journal_counters` calls `journal.observed_duration(event_id)` per current-epoch event (`src/monitor.py:486-489`), and each call runs a complete `replay()` — full file read, parse, validate (`src/discharge_journal.py:241-243`). The provider is invoked twice per poll (health snapshot `src/virtual_ups_exporter.py:102` and virtual-metrics build `:183`), i.e. ~2×(N+1) full-journal parses per second, growing quadratically with journal size — this sits on the safety-relevant output path and delays virtual UPS/health freshness as the journal grows. Fix: derive counters from one `replay()` projection (samples are already grouped by `boot_id`) and cache per poll.

**M3. `BaselineReset` inherits in-memory learned state into the new epoch.**
`_reset_battery_baseline` resets model state and RLS trackers but not `self.capacity_estimator` or the handler tracking fields (`src/monitor.py:659-680`). `apply_completed_discharge` deep-copies the stale estimator (`src/discharge_handler.py:212`) and uses `has_converged()`/`latest_ah` to trigger baseline-lock or `new_battery_detected` (`:305-314`) — old-battery samples could contaminate the first controlled event of the new epoch, violating the plan's "новая эпоха не наследует learned state" (plan §3.0/§3.4). Fix: rebuild `capacity_estimator`/reset handler tracking inside the reset transaction.

## Low

1. **Observability gaps vs plan §12:** health JSON has no model-update mode field, no `automatic_dispatch=false` beside scheduler output, `next_test_timestamp` not renamed to `eligible_for_operator_test_at`; the terminal `recorded_only` disposition isn't surfaced in health (`src/monitor_config.py:383-399`).
2. **Doc drift:** README.md:61 and `docs/internal/CONTEXT.md:23` still claim "polls every 10 seconds" while `POLL_INTERVAL=1` (`src/monitor_config.py:46`); stale comments in `src/ema_filter.py:21,99`.
3. **Dir-mode mismatch:** `MonitorDaemon` creates `model_dir` with default umask (`src/monitor.py:85`); if it lands 0755, the journal's `_validate_parent` rejects it (`src/discharge_journal.py:536-537`) → silent degraded journal on setups not installed via install.sh (which chmods 0700).
4. **Shutdown loses the final cached observation:** `discharge_collector.shutdown()` closes the event without flushing `_cached_observation`, yet the end payload's `last_confirmed_timestamp` can reference a never-persisted sample (`src/discharge_collector.py:225-245`).
5. **`pending_replay` latch:** a journal-start failure yields `event_id=None`; `_mark_applied(None, …)` latches `_pending_replay=True` forever (`src/monitor.py:447-450`), misleading health for a condition replay can never fix.
6. **No two-writer test** (plan scenario 17 / "второй писатель не стартует"): `_acquire_writer_lock` is implemented but untested; add a direct second-instance test.
7. **Dead code:** `discharge_collector._observed_duration()` and `_last_confirmed_monotonic` are write-only; legacy public model setters remain (plan defers cleanup to step 18 — acceptable).
8. **Post-READY degradation doesn't update systemd STATUS** (`src/monitor.py:1019-1022` only fires before READY), so systemd keeps showing "ready" during later outages.

## What looks solid

Capture-only immutability (only model write is the fresh-install save at `src/monitor.py:208`); strict current-schema loader with fail-fast and no self-heal/rename/clamp; epoch isolation in replay, counters, and cohorts; journal durability (fdatasync, torn-tail truncation under lock, fail-closed mid-file corruption, seq validation, exactly-once `mark_applied`); terminal recorded-only via the applied marker with persisted hash; scheduler command prohibition (`SchedulerModeError` before any socket use, no production path to `execute`); SagTracker frozen (no `ir_k`/RLS/history writes); transactional `reset_baseline` with rollback; installer stop-before-mutation; explicit output paths in composition roots. Test coverage for schema validation, journal replay/torn-tail, epoch filtering, BaselineReset, and scheduler modes is thorough.

**Bottom line:** fix H1 (watchdog starvation on partial NUT responses, with tests) and M1–M3 before shipping Release A.
