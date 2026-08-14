# Cross-AI Result: glm-5-2

Execution attempts:

- shared: /home/j2h4u/.opencode/bin/opencode run --model zai-coding-plan/glm-5.2 --agent plan --format json --variant max --attach http://127.0.0.1:37453 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T175351Z/inputs/natural-blackout-learning-implementation.md --title cross-ai-glm-5-2 -- 'Goal / decision to support: Review this implementation plan against the actual repository source. Find concrete correctness, safety, battery-model identifiability, architecture, sequencing, testability, and overengineering problems. Cite source paths and line numbers. Return GO only if there are no actionable concerns; otherwise return NO-GO with prioritized fixes. Do not edit repository files outside the cross-ai output directory.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'

Command:

```bash
/home/j2h4u/.opencode/bin/opencode run --model zai-coding-plan/glm-5.2 --agent plan --format json --variant max --attach http://127.0.0.1:37453 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T175351Z/inputs/natural-blackout-learning-implementation.md --title cross-ai-glm-5-2 -- 'Goal / decision to support: Review this implementation plan against the actual repository source. Find concrete correctness, safety, battery-model identifiability, architecture, sequencing, testability, and overengineering problems. Cite source paths and line numbers. Return GO only if there are no actionable concerns; otherwise return NO-GO with prioritized fixes. Do not edit repository files outside the cross-ai output directory.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'
```

Output:

# NO-GO — 4 High findings require plan amendments before execution

## Ground truth verification (plan claims vs. source)

All core diagnostic claims in the plan are **confirmed**:

| Claim | Evidence |
|---|---|
| Learning path dead: all events `model_processing_eligible=False` | src/discharge_collector.py:222,291,471,523; handler admits only `controlled_capacity_test` (src/discharge_handler.py:411-414); no producer of eligible events exists |
| SagTracker mutates `ir_k`/RLS from biased OL→OB sag; bias self-documented; clamp 0.025 | src/sag_tracker.py:19-21,185-203 |
| Scheduler can send `upscmd` (`quick` test) autonomously | src/scheduler_manager.py:105,470-476; src/battery_math/scheduler.py:201-207 |
| Normal events stuck in `pending_replay` forever | src/monitor.py:262-266 filters only `operational_partial`/`operational_gapped`, but normal closes write `evidence_class="operational"` (src/discharge_collector.py:522) → apply → `skipped` → `_pending_replay=True` (src/monitor.py:280-281), exposed in health (src/monitor.py:247). Tests pin the "operational" name (tests/test_discharge_collector.py:144,229) but no test covers replay of such events |
| `READY=1` before first successful NUT poll | src/monitor.py:836 vs. first poll in `_poll_once` |
| Production virtual-UPS path is a library default; only a pytest-env guard | src/virtual_ups.py:22,55-66 |
| No single-writer protection (no flock/lockfile anywhere in src/) | grep-verified |
| LUT circularity: SoC labels and SoH/DoD derive from the LUT itself | src/monitor.py:649; src/soh_calculator.py:73-75,94; src/discharge_handler.py:974-980 |
| `CapacityEstimator` returns segment-delivered Ah, not proven full capacity | src/capacity_estimator.py:104-142 |

Limitation: live `model.json` (`~/.config`) and `/run/ups-battery-monitor/*` are outside the review boundary; the "ir_k already at 0.025" claim is plausible from the clamp/saturation logic but unverifiable here.

## Findings

### High-1: Terminal-candidate gates require evidence the capture pipeline doesn't record — the new learning path is dead on arrival, same as the old one
§6 requires "continuous charging ≥12h before event" and "stable float voltage before transition" for `natural_terminal_candidate`. The journal records only OB-period samples; the start record contains only the first OB raw values (src/discharge_collector.py:274-294). No OL/float/charge history is persisted anywhere (model schema has no such field — src/model.py:24-57). With Stage 1–3 file lists unchanged, no event can ever qualify, so `UsableCapacityProposal` (the only implemented proposal) can never fire — reproducing the exact "eligible events never produced" failure the plan set out to fix.
**Fix:** add pre-event OL observation capture (charge-since timestamp / float-voltage window) to Stage 1's collector scope and model schema, or drop those gates for v1 and rely on the two-corroborating-events rule.

### High-2: `UsableCapacityProposal` has no defined target field; collides with baseline-lock/new-battery detection; model schema is fail-fast
The model exposes no "usable capacity at load X". Runtime prediction uses `get_capacity_ah()` = config rated value (src/model.py:737-744) plus SoH. If the proposal writes `capacity_ah_measured`, it collides with convergence baseline logic — a >10% divergence flags `new_battery_detected=True` (src/discharge_handler.py:306-314,813-835), so an aging battery would be misreported as replaced. If it writes SoH, it contradicts "SoH only as a later consequence". If it writes nothing read by `runtime_minutes`, Release C changes no prediction (violating §16 "изменили следующий прогноз только через существующую физическую формулу"). Additionally, `KNOWN_STATE_KEYS` rejects unknown keys at load (src/model.py:391-407): adding a new state key means a code rollback with the new model.json → `ModelLoadError` → daemon dead, contradicting §13 rollback ordering ("не накатывать старую модель поверх новой автоматически").
**Fix:** specify the new state key, its consumer in the prediction path, a conservative-direction clamp (proposals may only shorten predicted runtime), and a paired code+model rollback procedure.

### High-3: Journal schema evolution is fail-closed; terminal dispositions as new record types brick replay and rollback
`RECORD_TYPES` is closed (src/discharge_journal.py:26); `_validate_record` rejects unknown record types and unknown `schema_version` (src/discharge_journal.py:566-574). Stage 1's "терминальный результат обработки и однозначный replay" implies new record types or a schema bump; both make an older daemon refuse the whole journal (degraded capture), and §13's "не понижать и не обрезать журнал" + code rollback = broken journal under the old version.
**Fix:** encode `applied/recorded_only/rejected` dispositions inside existing `end` payloads (no new record type, no schema bump), and document legacy-value mapping (`operational`, `operational_partial`, `operational_gapped`, `closed_restart_recovered`) so replay of existing journals doesn't change meaning mid-stream.

### High-4: READY-gating changes the boot failure mode and does not actually close the stale-LB window for upsmon
Unit is `Type=notify`, `TimeoutStartSec=30`, `Restart=on-failure`, `StartLimitBurst=3/60s` (systemd/ups-battery-monitor.service:7-8,21-23). Deferring `READY=1` until first successful NUT read means any upsd outage >30s at start → start-limit kill → no virtual-UPS refresh at all, exactly during NUT/power trouble. And upsmon reads `/run/.../ups-virtual.dev` directly (`RuntimeDirectoryPreserve=restart` keeps it across restarts); systemd readiness is irrelevant to it, so the acceptance "старый LB не переживает готовность новой службы" still leaves a stale-`OB DISCHRG LB` → FSD window that READY-gating cannot close.
**Fix:** define startup behavior when NUT is unreachable (bounded wait + explicit stale-file invalidation strategy, e.g., truncate/remove `.dev` at start), adjust `TimeoutStartSec`/start-limit interplay, and state the residual upsmon window honestly.

### Medium
- **M1 — ir_k migration deferred out of Release A while the biased value stays in the safety path.** `ir_compensate` output feeds SoC/runtime/LB (src/monitor.py:644-659); biased-high ir_k inflates V_norm → delays LB — the unsafe direction the plan itself documents (src/sag_tracker.py:185-196). Freezing writes at Release A locks 0.025 in for weeks. Fold the conservative-value decision into Release A or gate Release A on the offline-diff result.
- **M2 — Scientific-fields hash underspecified.** Whole-file hash (`get_persisted_hash`, src/model.py:696-706) flips on daily scheduler saves (src/scheduler_manager.py:490) and the unconditional startup save/flag wipe (src/monitor.py:224-225). Define a canonical subset hash excluding non-scientific keys.
- **M3 — Single-writer scope too narrow.** Three write surfaces (model, journal, `/run` outputs incl. health at src/monitor_config.py:58); lock must cover all and fail before first write.
- **M4 — `natural_policy_endpoint` marker.** `shutdown()` closes as `operational_partial` unconditionally (src/discharge_collector.py:209-229); recording the virtual-LB moment requires collector changes not listed in Stage 1's file scope.
- **M5 — capture-only consumers.** `next_test_timestamp`/scheduling semantics in health & MOTD change when dispatch is proposal-only; not in any stage's acceptance.

### Low
- `LearningDecision` with 8 per-field gates + closed proposal union + cohorts for a slice with exactly one proposal type — trim to history/residuals/capacity until the second proposal type exists (plan's own kaizen rules).
- `--new-battery` / `_reset_battery_baseline` (src/monitor.py:514-551) interaction with new candidates/usable-capacity state unspecified.
- Startup `new_battery_detected=False` wipe + unconditional save (src/monitor.py:224-225) silently clears a pending flag across restarts — fix while Stage 0 touches monitor anyway.
- Residual math should read per-record `monotonic_ns` from journal envelopes, not payload wall `timestamp` (src/discharge_journal.py:100), for §4's clock-jump requirement.
- Naming: `evidence_classifier.py` vs existing `event_classifier.py` invites confusion.

## Verdict

The diagnosis is accurate and the staging, invariants, and test harness are strong. But High-1/2/3 mean the plan's central deliverable (the first typed capacity proposal from natural terminal events) is either unreachable or lands on an undefined field with rollback hazards, and High-4 trades one boot-time safety gap for another. **NO-GO** until the four High fixes are folded in; all are plan amendments, not redesigns.
