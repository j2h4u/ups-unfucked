# Cross-AI Result: glm-5-2

Execution attempts:

- shared: /home/j2h4u/.opencode/bin/opencode run --model zai-coding-plan/glm-5.2 --agent plan --format json --variant max --attach http://127.0.0.1:38021 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T181116Z/inputs/natural-blackout-learning-implementation.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T181116Z/inputs/deepseek-v4-pro.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T181116Z/inputs/glm-5-2.md --title cross-ai-glm-5-2 -- 'Goal / decision to support: Second-pass review of the revised implementation plan against the actual repository. Verify that every prior Critical/High finding in the two attached reviews is concretely resolved, and find any new correctness, shutdown-safety, identifiability, journal compatibility, migration, sequencing, testability, or overengineering issue. Cite source and plan lines. Return GO only if no actionable Critical/High/Medium issue remains; otherwise return NO-GO with exact plan amendments. Do not edit repository files outside the cross-ai output directory.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'

Command:

```bash
/home/j2h4u/.opencode/bin/opencode run --model zai-coding-plan/glm-5.2 --agent plan --format json --variant max --attach http://127.0.0.1:38021 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T181116Z/inputs/natural-blackout-learning-implementation.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T181116Z/inputs/deepseek-v4-pro.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T181116Z/inputs/glm-5-2.md --title cross-ai-glm-5-2 -- 'Goal / decision to support: Second-pass review of the revised implementation plan against the actual repository. Verify that every prior Critical/High finding in the two attached reviews is concretely resolved, and find any new correctness, shutdown-safety, identifiability, journal compatibility, migration, sequencing, testability, or overengineering issue. Cite source and plan lines. Return GO only if no actionable Critical/High/Medium issue remains; otherwise return NO-GO with exact plan amendments. Do not edit repository files outside the cross-ai output directory.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'
```

Output:

# Second-pass review: revised plan vs. repository

**Verdict: NO-GO** — close: every prior Critical/High is concretely resolved, but two new actionable Medium issues in Stage 0 remain (both small plan amendments, no redesign).

## Prior findings — verification

| Prior finding | Resolution in revised plan | Source verification |
|---|---|---|
| DS C1 / GLM H2: proposal target + direction guard | Resolved — writes existing `capacity_ah_measured`; runtime keeps rated capacity in Peukert base, `soh = clamp(measured/rated, 0, 1)`, no double count; auto-apply downward-only with golden differential "ни один LB не может стать позже" (plan:192-205, 591-599, 761-764; scenarios 22-24) | `capacity_ah_measured` is an existing `KNOWN_STATE_KEYS` member (src/model.py:39, 62) — no schema growth; coupling verified multiplicative via `soh` (src/battery_math/peukert.py:67-73, src/monitor.py:651-659) |
| DS H1 / GLM H1: 12h charge precondition unimplementable | Resolved — `ChargeReadinessTracker` persisted in start-record, OL-poll updates, reset on OB/CAL/gap/restart (conservative), hardware gates as constants pre-validated on OL-fixture (plan:255-265, 363-371) | Start payload is free-form dict (src/discharge_collector.py:273-295) — compatible |
| DS H2: N=2 proxy → mutation | Resolved — two matching proxies create only candidate/cohort; `CapacityUpdateProposal` requires independent current calibration with version + error bounds; Release D gated on it (plan:340-346, 583-599, 757-763) | Convergence/new-battery collision paths (src/discharge_handler.py:305-314, 813-843) explicitly defused (plan:596-600) |
| DS H3 / GLM H3: journal fail-closed + legacy `pending_replay` | Resolved — `disposition` payload field of existing `applied` record; no new record types/schema bump; legacy mapping in Stage 0.3 and Stage 1 classifier (plan:418-420, 469-484) | Payload fields are free-form in `_validate_record` (src/discharge_journal.py:602-612); old daemon skips applied-marked events (src/monitor.py:269-270); the `"operational"` fall-through bug confirmed at src/discharge_collector.py:522 / src/monitor.py:262-281 |
| GLM H4: READY gating boot failure | Mostly resolved — degraded startup, no fake OL, no start-limit loop, stale-LB window documented (plan:429-435) — **residual: new M2 below** | Unit: Type=notify, TimeoutStartSec=30 (systemd/ups-battery-monitor.service:11, 23) |
| DS/GLM M1 (ir_k), M2 (simulator), M3 (flock), M4 (BaselineReset) | Resolved — conservative envelope + Release A blocking golden test (plan:441-448); full simulator spec (plan:299-333); `flock(LOCK_EX|LOCK_NB)` on `model_dir/monitor.lock` for all writers (plan:426-428); sanctioned `reset_baseline()` (plan:189-190, 547) | ir_k write path confirmed (src/sag_tracker.py:197-203); upscmd dispatch confirmed (src/scheduler_manager.py:105, 470-476); virtual path library-default confirmed (src/virtual_ups.py:22, 55-59; src/virtual_ups_exporter.py:44) |

## New findings

### Medium-1: Stage 0.6 degraded mode is killed by the systemd watchdog — "no restart loop" acceptance is unachievable as specified
`WATCHDOG=1` exists only at src/monitor.py:812, reached only at the end of a fully successful `_poll_once`; NUT exceptions are swallowed by the loop error handler (src/monitor.py:862-887) and missing voltage/load early-returns at :743-749 — both skip the ping. The unit is `Type=notify` with `WatchdogSec=120` and `Restart=on-failure` (systemd/ups-battery-monitor.service:12, 21). So the plan's "процесс остаётся живым… продолжает bounded polling" (plan:430-432) without NUT results in a watchdog kill at 120s → restart → kill again: a ~130s-cadence restart loop (StartLimitBurst=3/60s never trips because cycles exceed 60s). This directly fails the plan's own acceptance at plan:456, 638, 738; the plan never mentions WATCHDOG at all.
**Amendment:** Stage 0.6 must state that the degraded/bounded loop emits `WATCHDOG=1` on its own cadence independent of NUT success (this failure mode exists today for runtime outages too), and scenario 20 (plan:638) must be extended to an outage longer than 120s.

### Medium-2: retained startup `new_battery_detected` wipe contradicts the new scientific fingerprint and its capture-only alarm
src/monitor.py:224-225 unconditionally sets `state["new_battery_detected"] = False` and saves at every daemon start. The plan keeps `new_battery_detected`/`_timestamp` inside `scientific_fingerprint` (plan:436-439) and adds the alarm "изменение научного хеша в capture-only" (plan:795). On a host whose persisted model has the legacy flag `True` (set historically by src/discharge_handler.py:832-835), the first boot under Release A changes the scientific hash in capture-only and trips the alarm; it is also a direct scientific-field write that Stage 3 (plan:547) claims to have sanctioned.
**Amendment:** either drop `new_battery_detected`(+timestamp) from the fingerprint (it is a lifecycle flag, not science), or replace the unconditional init wipe with a sanctioned consume-on-read/acknowledgment in Stage 0.

### Low (residual, non-blocking)
- **L1:** `natural_policy_endpoint` (plan:114, 374) is unclassifiable from current end payloads — `shutdown()` writes only `lifecycle="closed_shutdown_requested"` (src/discharge_collector.py:216-225); Stage 1's collector list (plan:467-468) doesn't add the endpoint-cause marker. Misclassification defaults conservatively to partial/gapped → `recorded_only`, so safe direction.
- **L2:** `next_test_timestamp` (src/virtual_ups_exporter.py:70, src/motd_status.py:119) still advertises a test that proposal-only mode will never run; semantics not updated (GLM M5 residual).
- **L3:** frozen/biased IR median/trend stay published without a "frozen" marker in health/MOTD (DS L2 residual).
- **L4:** "первого успешного чтения" (plan:429) should explicitly define partial NUT responses (status present, voltage/load missing → src/monitor.py:743-749 writes nothing) as not-successful, else READY can still precede a fresh virtual write with a reachable upsd.

## Required amendments before GO
1. Stage 0.6: WATCHDOG pings from the degraded/bounded loop + scenario 20 extended past 120s (M1).
2. Stage 0.7: remove `new_battery_detected`(+timestamp) from `scientific_fingerprint` or sanction the startup wipe (M2).

Both are localized Stage 0 text changes; everything else in the plan is verified implementable against the current source.
