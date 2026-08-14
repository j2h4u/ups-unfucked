# Cross-AI Result: deepseek-v4-pro

Execution attempts:

- shared: /home/j2h4u/.opencode/bin/opencode run --model deepseek/deepseek-v4-pro --agent plan --format json --variant max --attach http://127.0.0.1:51607 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T183906Z/inputs/natural-blackout-learning-implementation.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T183906Z/inputs/deepseek-v4-pro.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T183906Z/inputs/glm-5-2.md --title cross-ai-deepseek-v4-pro -- 'Goal / decision to support: Fresh standard convergence gate after upgrading the standard GLM reviewer from 5.2 to 5.3. Verify the battery-epoch exclusion for candidates/cohorts/trends, mapped legacy dispositions, operator-sanctioned fingerprint changes, runtime-relative capacity direction wording, and the full implementation plan against current source. Return GO only when no actionable Critical/High/Medium findings remain. Do not edit repository files outside the cross-ai output directory.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'

Command:

```bash
/home/j2h4u/.opencode/bin/opencode run --model deepseek/deepseek-v4-pro --agent plan --format json --variant max --attach http://127.0.0.1:51607 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T183906Z/inputs/natural-blackout-learning-implementation.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T183906Z/inputs/deepseek-v4-pro.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T183906Z/inputs/glm-5-2.md --title cross-ai-deepseek-v4-pro -- 'Goal / decision to support: Fresh standard convergence gate after upgrading the standard GLM reviewer from 5.2 to 5.3. Verify the battery-epoch exclusion for candidates/cohorts/trends, mapped legacy dispositions, operator-sanctioned fingerprint changes, runtime-relative capacity direction wording, and the full implementation plan against current source. Return GO only when no actionable Critical/High/Medium findings remain. Do not edit repository files outside the cross-ai output directory.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'
```

Output:

## Final convergence gate (GLM 5.3 upgrade pass): NO-GO — one actionable Medium, three Lows

All five amendment groups from the previous round are present in `docs/plans/natural-blackout-learning-implementation.md` and consistent with the working tree. The Medium is a granularity hole inside the new battery-epoch gate itself.

### Findings

**Medium-1 — epoch gate has day granularity; same-day pre-reset events pass into the new battery's epoch**
Plan:360–362 gates candidates/cohorts/trends by "start-record … не старше persisted `battery_install_date`/времени последнего `BaselineReset`", and Stage 3 (plan:589–591) has `BaselineReset` "обновляет `battery_install_date`". The only persisted epoch marker in the model is `battery_install_date` — a `YYYY-MM-DD` string (src/model.py:45, 534, 556–560; initialized once at src/monitor.py:158–159); nothing persists a precise reset timestamp, and the reset spec doesn't add one. With date granularity, an old-battery terminal event in the morning and a `BaselineReset` later the same day share the calendar date, so the pre-replacement event passes the "не старше" test and becomes candidate/cohort/trend evidence for the new battery — the exact cross-epoch pollution the gate exists to prevent (false `possible_decline` for a fresh battery; an old-battery event corroborating a later downward proposal whose derived SoH passes the §3.4 predicate). Fix: Stage 3 must persist a precise ISO epoch timestamp on `BaselineReset`, and §5.4 must compare start-record wall timestamps against it, not against the day-granularity install date.

**Low-1 — Release D summary still uses field-decrease wording**
Plan:820–822: "automatic path принимает только уменьшение" contradicts the normative predicate §3.4 (plan:199–203), which Stage 5 now cites correctly (plan:638–640). On a host with legacy stored `soh` below `measured/rated`, a capacity decrease is rightly rejected even though the field decreased. Align the Release D bullet with the SoH-relative rule.

**Low-2 — `src/motd_status.py` omitted from the explicit-path change list**
Stage 0.4 (plan:436–440) lists `virtual_ups.py`, `virtual_ups_exporter.py`, `monitor_config.py`, but `motd_status.py:25` hardcodes its own copy of `HEALTH_ENDPOINT_PATH`. Smoke (§10) forbids `/run` and utilities must receive private paths; add `motd_status.py` to the list.

**Low-3 — invariant wording vs legacy markers**
Plan:722 ("`applied` marker … содержит окончательный disposition") is falsified by pre-Stage 1 legacy markers without `disposition`; Stage 1 (plan:500–502) resolves display via mapping. Wording should read "содержит или отображает" to avoid the internal contradiction.

### Amendment verification (source-verified)

| Amendment | Plan | Source |
|---|---|---|
| Battery-epoch exclusion | §5.4 plan:360–362; Stage 3 plan:591 | `battery_install_date` persisted (src/model.py:45, 556–560); reset currently doesn't touch it (src/monitor.py:514–551) — gap closed by plan, subject to Medium-1 |
| Mapped legacy dispositions | plan:500–502 | replay skips marked events before hash compare (src/monitor.py:269–270); `mark_applied` hash-mismatch raise (src/discharge_journal.py:333–336) unreachable for marked events |
| Operator-sanctioned fingerprint changes | §12 plan:857–859 | fingerprint fields match model state; reset writes `soh`/`soh_history`/`capacity_ah_measured` (src/monitor.py:520–528) |
| Runtime-relative direction predicate | §3.4 plan:199–203; Stage 5 plan:638–640 | `get_soh()` is the runtime multiplier (src/model.py:729–735) |
| Pre-OB ChargeReadiness snapshot | plan:370–372 | start record written at first OB (src/discharge_collector.py:273–296) |
| Sanctioned startup repair | plan:429–431 | repair is direct `set_soh(1.0)` (src/monitor.py:385–388); unconditional startup wipe real (src/monitor.py:224–225) |
| Terminal markers, watchdog, IR envelope, journal v1 | plan:495–504, 444–454, 462–467 | `WATCHDOG=1` only after successful poll (src/monitor.py:812); `Type=notify`/`WatchdogSec=120`/`Restart=on-failure` (systemd/ups-battery-monitor.service:11–22); `ir_compensate` sign makes `min(k,0)` conservative (src/ema_filter.py:170); `SCHEMA_VERSION=1`, 64 KiB line, free-form payload (src/discharge_journal.py:25–27, 602–612) |

All other prior findings (journal compatibility, snapshot sizing budget, rollback, smoke isolation, single-writer lock) remain mutually consistent. After the Medium-1 amendment the plan is GO for implementation.
