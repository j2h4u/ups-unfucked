# Cross-AI Result: claude-opus-5

Execution attempts:

- direct: /home/j2h4u/.local/bin/claude -p 'Goal / decision to support: Premium final gate for the complete uncommitted Release A implementation after standard reviewers both returned GO. Inspect current source, tests, implementation plan, and attached final standard reports. Return GO only if no actionable Critical, High, or Medium correctness, safety, data-integrity, deployment, or architecture findings remain. Pay special attention to UPS shutdown safety, independent watchdog/READY semantics, capture-only scientific immutability, durable journal/exactly-once dispositions, battery epoch reset/isolation, strict state schema with one-time external conversion, lock/order of deployment, and whether the tests genuinely cover failure paths. Do not edit repository source files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.

Review the following copied context files in addition to repository evidence:
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T210725Z-premium/20260814T210737Z/inputs/natural-blackout-learning-implementation.md
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T210725Z-premium/20260814T210737Z/inputs/deepseek-v4-pro.md
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T210725Z-premium/20260814T210737Z/inputs/glm-5-3.md' --model opus --permission-mode plan --output-format json --effort low

Command:

```bash
/home/j2h4u/.local/bin/claude -p 'Goal / decision to support: Premium final gate for the complete uncommitted Release A implementation after standard reviewers both returned GO. Inspect current source, tests, implementation plan, and attached final standard reports. Return GO only if no actionable Critical, High, or Medium correctness, safety, data-integrity, deployment, or architecture findings remain. Pay special attention to UPS shutdown safety, independent watchdog/READY semantics, capture-only scientific immutability, durable journal/exactly-once dispositions, battery epoch reset/isolation, strict state schema with one-time external conversion, lock/order of deployment, and whether the tests genuinely cover failure paths. Do not edit repository source files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.

Review the following copied context files in addition to repository evidence:
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T210725Z-premium/20260814T210737Z/inputs/natural-blackout-learning-implementation.md
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T210725Z-premium/20260814T210737Z/inputs/deepseek-v4-pro.md
- /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T210725Z-premium/20260814T210737Z/inputs/glm-5-3.md' --model opus --permission-mode plan --output-format json --effort low
```

Output:

## Verdict: **NO-GO** — one High finding remains

Static review plus dynamic verification. Unlike the two standard reviewers, I was able to run the suite and reproduce behaviour: **649 passed** (`uv run pytest -q`), and I confirmed finding H-1 by executing the exporter directly.

---

### H-1 (High) — Fabricated `ups.status: OL` written to the virtual UPS on a partial NUT reply

`src/monitor.py:1150` writes the virtual UPS unconditionally after the voltage/load check, even when `physical_poll_valid` is `False`. `src/virtual_ups_exporter.py:185-187` then resolves the status as `current_metrics.ups_status_override or ups_data.get("ups.status", "OL")`. `CurrentMetrics.ups_status_override` defaults to `None` (`src/monitor_config.py:234`) and is only ever set once `_classify_event` succeeds, which requires both `ups.status` and `input.voltage` (`src/monitor.py:915-919`).

Reachability is real, not theoretical: `NUTClient._recv_until` **breaks and returns a truncated buffer without raising** when the peer closes mid-response (`src/nut_client.py:149-151`), so an upsd restart / driver reload during `LIST VAR` yields a partial dict containing `battery.voltage` and `ups.load` but no `ups.status`.

Reproduced:

```
override default: None
write ok: True
['ups.status: OL']
```

Failure scenario: monitor restarts during a blackout (deploy, crash, `Restart=on-failure`), first NUT reply is truncated → the daemon correctly withholds `READY` and reports `STATUS=degraded`, **but has already published `OL` to the `.dev` that upsmon consumes**, overwriting the preserved `OB DISCHRG LB` (`RuntimeDirectoryPreserve=restart`). This is exactly what plan §7.6 ("Нельзя подменять неизвестное состояние на `OL`") and the Release A pass criterion "нет ложного OL" forbid.

Both standard reviewers verified the READY/`STATUS` truth table but not what gets written to the safety surface on the same tick. The existing regression at `tests/test_monitor_integration.py:1006-1030` mocks `write_virtual_ups`, so it cannot catch this.

Fix direction: skip the virtual-UPS write when `physical_poll_valid` is `False`, or make the unknown-status fallback refuse to emit `OL`.

---

### M-1 (Medium) — Deployment runbook leaves the protection window unaddressed

`docs/RELEASE-A-DEPLOYMENT.md:10-19` stops the service, then asks the operator to invent the one-time conversion by hand ("operator-run one-off command or temporary tooling") with no worked transformation, no key-diff against the current production state, and no validation command. Two consequences:

1. **Unbounded window with no battery protection.** `systemctl stop` drops `RuntimeDirectory` (`RuntimeDirectoryPreserve=restart` covers restarts only), so `/run/ups-battery-monitor/ups-virtual.dev` disappears and the virtual UPS goes stale for the whole manual window. On a host documented as having frequent blackouts, the runbook needs a time bound, a re-check for grid stability, and an explicit statement that shutdown protection is degraded. (I could not read `/etc/nut` — outside the stated boundary — so the exact upsmon target is asserted from project context, not verified here.)
2. Step 4 says "validate with the current model loader" but gives no invocation, while the loader is now strict on all 17 keys, UUID, types, ranges, `physics` and `lut` (`src/model.py:388-441`). A hand-built file that misses one nested field fails startup at the worst moment.

This is a runbook defect, not a code defect, but it is the deployment gate the release depends on.

---

### L-1 (Low) — New pytest config in `pyproject.toml` is dead

The added `[tool.pytest.ini_options]` block (`pyproject.toml:56-61`) is never read: `pytest.ini` exists and wins precedence (`configfile: pytest.ini` in the run header). `tmp_path_retention_policy`/`count` are silently ignored, so the stated "leave no `/tmp` residue on a shared host" intent is not achieved. Move the two keys into `pytest.ini`.

### L-2 (Low) — `pytest.ini:7` sets `timeout = 30` but `pytest-timeout` is not installed

Emits `PytestConfigWarning: Unknown config option: timeout` on every run; tests have no timeout at all. A hanging test will block CI indefinitely.

### L-3 (Low) — Full journal replay on every 1 s tick

`DischargeJournal.replay()` reads and JSON-parses the entire file (`src/discharge_journal.py:367-485`) and is invoked at least once per poll via the tick cache. Bounded by `DEFAULT_MAX_BYTES = 64 MiB`, and growth is slow (~1 record/10 s only during OB), so it is not a Release A blocker — but the cost is unbounded-in-practice over years.

### L-4 (Low) — `render_motd` propagates `ModelLoadError`

`src/motd_status.py:82` constructs `BatteryModel` with no guard; a strict-schema rejection turns every interactive login into a traceback. Fail-loud is defensible, but the MOTD module is the wrong place to learn it.

---

### Re-verified as genuinely closed

Prior findings from the two standard reports hold up under independent inspection: watchdog heartbeat in `finally` outside all validation branches (`monitor.py:1041-1046`, 2 s NUT timeout vs `WatchdogSec=120` — no restart risk); READY truth table (`monitor.py:1159-1171`); single-writer `O_NOFOLLOW` + `LOCK_EX|LOCK_NB` released on constructor failure and `__del__` (`monitor.py:286-318`), and only `monitor.py:105` ever constructs a journal, so no unlocked second writer exists; strict 17-key schema with no rename/backup/clamp/default-injection (`model.py:388-441`); `reset_baseline` as one transaction with rollback and preserved `last_upscmd_*` audit (`model.py:679-746`); foreign-epoch open events closed `history_only` (`discharge_collector.py:428-467`); torn-tail `ftruncate`+`fdatasync` with mid-file corruption a hard error (`discharge_journal.py:425-437`, `487-501`); `mark_applied` exactly-once with same-hash idempotence and conflict error (`discharge_journal.py:339-365`); scientific mutation structurally unreachable — nothing in `src/` ever produces `evidence_class == "controlled_capacity_test"`, which `_validate_application_input` requires (`discharge_handler.py:413`); `SchedulerModeError` at the single dispatch boundary (`scheduler_manager.py:110-117`); `SagTracker` no longer writes the model.

Journal failure-path coverage is genuine, not decorative — `tests/test_discharge_journal.py` exercises torn tail vs unknown schema, mid-file corruption, sticky degradation, byte caps, symlink/non-regular targets, and open-event `mark_applied` rejection; `tests/test_discharge_application.py` covers save-failure rollback and post-commit failure semantics.

### Accepted known limitation (not a new finding)

A restart mid-blackout publishes `OB DISCHRG LB` immediately, because `_handle_event_transition` passes `time_rem_minutes or 0` and the 2-minute safety floor fires until the EMA stabilises (`monitor.py:735-739`, `virtual_ups.py:118-120`). This is the conservative-shutdown risk the plan names explicitly in §7.6, mitigated by the "deploy only after preflight physical OL" rule.

---

**Bottom line:** the safety architecture, immutability guarantees, and journal semantics are in good shape. H-1 is a small, well-localised fix, and M-1 is a runbook edit. Both should land before the 24-hour acceptance window opens.
