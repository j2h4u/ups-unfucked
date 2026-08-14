# Cross-AI Result: glm-5-3

Execution attempts:

- shared: /home/j2h4u/.opencode/bin/opencode run --model zai-coding-plan/glm-5.3 --agent plan --format json --variant max --attach http://127.0.0.1:56453 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T201608Z-standard/20260814T201619Z/inputs/natural-blackout-learning-implementation.md --title cross-ai-glm-5-3 -- 'Goal / decision to support: Review the complete uncommitted Release A implementation against docs/plans/natural-blackout-learning-implementation.md and the current source diff. Inspect source and tests, not only the plan. Return GO only when there are no actionable Critical, High, or Medium findings. Focus on capture-only scientific immutability; strict current schema with no runtime migration, fallback, self-heal, or startup rewrite; current battery epoch isolation; durable append-only journal, replay and torn-tail recovery; exactly-once recorded_only terminal markers; one-writer locking; READY/watchdog/systemd behavior; one-second physical polling and ten-second durable sampling; explicit output paths; structural scheduler command prohibition; transactional BaselineReset; installer stop-before-state-mutation; and regression risk. Do not edit repository source files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'

Command:

```bash
/home/j2h4u/.opencode/bin/opencode run --model zai-coding-plan/glm-5.3 --agent plan --format json --variant max --attach http://127.0.0.1:56453 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T201608Z-standard/20260814T201619Z/inputs/natural-blackout-learning-implementation.md --title cross-ai-glm-5-3 -- 'Goal / decision to support: Review the complete uncommitted Release A implementation against docs/plans/natural-blackout-learning-implementation.md and the current source diff. Inspect source and tests, not only the plan. Return GO only when there are no actionable Critical, High, or Medium findings. Focus on capture-only scientific immutability; strict current schema with no runtime migration, fallback, self-heal, or startup rewrite; current battery epoch isolation; durable append-only journal, replay and torn-tail recovery; exactly-once recorded_only terminal markers; one-writer locking; READY/watchdog/systemd behavior; one-second physical polling and ten-second durable sampling; explicit output paths; structural scheduler command prohibition; transactional BaselineReset; installer stop-before-state-mutation; and regression risk. Do not edit repository source files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'
```

Output:

# Adversarial review — Release A uncommitted implementation

**Verdict: NO-GO** — 2 High and 3 Medium actionable findings. Static review only (bash was restricted to `git diff/status/log/show`; the test suite was not executed).

## High

**H-1. Watchdog heartbeat skipped on partial NUT responses → systemd kill/restart loop.**
`_poll_once` early-returns when voltage/load is missing (src/monitor.py:872-878) *before* `sd_notify("WATCHDOG=1")` (src/monitor.py:963). `get_ups_vars` (src/nut_client.py:159-183) returns whatever VAR lines arrive — a stalled driver or truncated dummy-ups data yields a valid response without `battery.voltage`/`ups.load` and does **not** raise, so the degraded-path heartbeat (src/monitor.py:1012-1017) never runs. With `WatchdogSec=120` + `Restart=on-failure` (systemd/ups-battery-monitor.service:12,19-20) this becomes an endless kill/restart loop with a stale virtual UPS — exactly what plan §Этап 0.6 forbids, and the "частичный ответ без voltage/load" case from test scenario 21 has no test (grep confirms).

**H-2. Per-second full-journal replays on the export path; cost grows with journal size.**
`write_health_snapshot` and `_build_virtual_metrics` each invoke the journal provider every 1-second tick (src/virtual_ups_exporter.py:99-190; src/monitor.py:947-953). `_journal_counters()` (src/monitor.py:470-499) does one `journal.replay()` plus one **additional full replay per event** via `observed_duration()` (src/discharge_journal.py:241-264). That is ≥2×(N+1) full-file reads/parses per second against a deliberately never-rotated journal (100k records / 64 MB caps). During a long blackout — when loop latency feeds LB/shutdown timing — and across years of events, this progressively starves the safety loop. Counters need caching (recompute on append or per reporting interval, not per tick).

## Medium

**M-1. No one-time state-conversion tool or runbook.** Startup now hard-fails on any pre-conversion `model.json` (src/model.py:409-433 — by design and correctly so), but nothing in scripts/, install.sh, or docs performs/documents the plan-required one-time deploy conversion (backup → add `battery_epoch_id` + full key set → verify). The first Release A start on the current host fails until the operator hand-crafts a valid file with no verified instrument.

**M-2. Plan-mandated pre-release `ir_k` comparison golden test is absent.** Plan §Этап 0 requires comparing persisted `ir_k` (saturated-biased 0.025), `k=0`, and a conservative envelope, and blocks Release A until the effect direction is golden-tested. No such harness exists in tests/ (only sag-freeze tests); safety still compensates with the biased value (src/monitor.py:773).

**M-3. Plan-listed golden scenarios untested:** second concurrent writer (scenario 17 — lock code at src/monitor.py:283-302 looks correct but is never tested against a real second holder) and runtime READY-withheld/degraded/watchdog behavior during a ≥120 s NUT outage (only unit-file directives are asserted, tests/test_systemd_integration.py:159-165).

## Low

- **L-1.** Daemon creates `model_dir` with default 0755 (src/monitor.py:85); the journal then rejects the parent (src/discharge_journal.py:536-537) → durable capture silently absent if deployed without install.sh (which chmods 700). Use `mkdir(mode=0o700)`.
- **L-2.** Monitor runs SchedulerManager in default `proposal_only` (src/scheduler_manager.py:246-247) while Release A's stated mode is `capture_only`; dispatch is structurally blocked either way (well tested), but the surfaced mode string differs from the plan.
- **L-3.** docs/USER-SCENARIOS.md:104-116 drift: claims LUT unchanged and R_internal "cleared separately", but `reset_baseline()` (src/model.py:679-758) resets the LUT to standard+anchor, clears `r_internal_history`/`discharge_events`, and rotates the epoch UUID — none of which the table mentions.
- **L-4.** Dead scientific paths retained (`DischargeHandler.update_battery_health`/`handle_discharge_complete`, `model.calibration_write/calibration_batch_flush`, monitor delegates src/monitor.py:647-655) — matches the plan's staged cleanup but is latent regression surface.
- **L-5.** `startup_degraded` is computed and returned by the provider but dropped by the exporter, so health.json never shows it.

## Verified strengths

Strict current schema with no clamp/backup/self-heal; epoch isolation implemented and tested (tests/test_monitor_integration.py:140-216); terminal `recorded_only` markers via the existing `applied` record with persisted hash, idempotent and replay-stable; torn-tail recovery under `LOCK_EX` with `ftruncate`+`fdatasync`; IR learning frozen with tests; scheduler dispatch structurally prohibited with tests; transactional `BaselineReset` with rollback; installer stops the service before state mutation (tested); explicit output paths with only `main()` knowing `/run`.
