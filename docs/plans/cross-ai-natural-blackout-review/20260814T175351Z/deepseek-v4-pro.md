# Cross-AI Result: deepseek-v4-pro

Execution attempts:

- shared: /home/j2h4u/.opencode/bin/opencode run --model deepseek/deepseek-v4-pro --agent plan --format json --variant max --attach http://127.0.0.1:37453 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T175351Z/inputs/natural-blackout-learning-implementation.md --title cross-ai-deepseek-v4-pro -- 'Goal / decision to support: Review this implementation plan against the actual repository source. Find concrete correctness, safety, battery-model identifiability, architecture, sequencing, testability, and overengineering problems. Cite source paths and line numbers. Return GO only if there are no actionable concerns; otherwise return NO-GO with prioritized fixes. Do not edit repository files outside the cross-ai output directory.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'

Command:

```bash
/home/j2h4u/.opencode/bin/opencode run --model deepseek/deepseek-v4-pro --agent plan --format json --variant max --attach http://127.0.0.1:37453 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-natural-blackout-review/20260814T175351Z/inputs/natural-blackout-learning-implementation.md --title cross-ai-deepseek-v4-pro -- 'Goal / decision to support: Review this implementation plan against the actual repository source. Find concrete correctness, safety, battery-model identifiability, architecture, sequencing, testability, and overengineering problems. Cite source paths and line numbers. Return GO only if there are no actionable concerns; otherwise return NO-GO with prioritized fixes. Do not edit repository files outside the cross-ai output directory.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'
```

Output:

# Review: natural-blackout-learning-implementation.md vs. repository source

**Verdict: NO-GO.** The plan's diagnosis of the current code is accurate and its two-loop architecture and release sequencing are sound, but there are one Critical and three High actionable gaps that must be fixed before implementation. Full read boundary respected; all refs are to repo files under `/home/j2h4u/repos/j2h4u/ups-battery-monitor`.

Plan diagnosis verified against source (accurate, not findings):
- All events close with `model_processing_eligible=False` — `src/discharge_collector.py:291,471,523,546`; handler requires `controlled_capacity_test` — `src/discharge_handler.py:411-414`; no production producer exists (only tests construct it).
- `pending_replay` bug is real: end payload `evidence_class="operational"` (`src/discharge_collector.py:522`) matches neither `{"operational_partial","operational_gapped"}` nor lifecycle `closed_restart_recovered`, so replay falls through to `apply_completed_discharge` → `skipped` → `self._pending_replay = True` forever — `src/monitor.py:262-281`.
- `READY=1` before first successful NUT read — `src/monitor.py:836`.
- Virtual UPS production path is a library default — `src/virtual_ups.py:22,55-59`; exporter passes no path — `src/virtual_ups_exporter.py:44`.
- No single-writer protection anywhere (journal `O_APPEND` open has no `flock` — `src/discharge_journal.py:439-476`; model is last-writer-wins via tempfile+rename — `src/model.py:160-225`; the hazard is even acknowledged in `justfile:59-61`).
- SagTracker writes `ir_k`/RLS on the first OL→OB sag — `src/sag_tracker.py:197-203`, with the bias documented at `185-196`.
- `CapacityEstimator` returns delivered segment charge, not full capacity — `src/capacity_estimator.py:125`; SoH labels derive from the LUT — `src/soh_calculator.py:73-97`; Peukert calibrated from partial duration as if full — `src/discharge_handler.py:453-471`.

## Critical

**C1. `UsableCapacityProposal` has no defined, direction-guarded entry into the runtime/LB path, and the plan's acceptance criteria contradict its own safety rule.**
The plan requires (a) the proposal change "только доступную ёмкость при указанной нагрузке" and (b) "изменили следующий прогноз только через существующую физическую формулу" (`docs/plans/natural-blackout-learning-implementation.md:489,789`). But the existing formula has no such parameter: `runtime_minutes()` takes `capacity_ah` = **config-rated** value (`src/runtime_calculator.py:30-48`; fed from `self.battery_model.get_capacity_ah()` which returns the config constant, `src/model.py:737-744`, call site `src/monitor.py:651-659`) and `soh`. The plan forbids writing to `capacity_ah_measured` (plan §5.3) yet never says where the learned usable capacity is stored or how it reaches `runtime_minutes`. The only paths available to an implementer are (i) swap rated for learned capacity or (ii) lower SoH — both lengthen or shorten predicted runtime with no error-direction guarantee. Since the plan's own §1 principle 6 forbids delaying safe shutdown "без отдельного доказанного решения", and §16.5 mandates a prediction change, the plan is self-contradictory at its most safety-relevant point. Fix: specify the coupling (e.g. `effective_capacity = min(rated, usable)` or explicit SoH base derivation), require a **direction guard** (a learned value may only reduce predicted runtime relative to the frozen snapshot), and add golden tests proving LB timing can only move earlier.

## High

**H1. The "12 hours continuous float before event" precondition is unimplementable with current capture.**
Plan §6 (`docs/plans/...md:349`) requires "непрерывное нахождение на зарядке перед событием; начальное предложение — 12 часов", and Stage 3 acceptance depends on it (two corroborated terminal events). The journal captures only OB events; the start record keeps 4 instantaneous raw fields (`src/discharge_collector.py:273-295,328-335`). Nothing records OL/float history, so full-charge state cannot be verified ex post. Terminal candidates would rest on an unfalsifiable assumption, and the usable-capacity number derived from them inherits an unquantified systematic bias. Fix: add an OL-side "time-since-last-float-break / float voltage history" capture (or an explicit recharge-tracker) to Stage 0/1, and make the 12h value a persisted, evidence-backed quantity rather than a placeholder.

**H2. N=2 proposal from load%-proxy data: agreement gate filters noise, not shared bias.**
Plan §6 promotion: two events, load within 10 pp, capacity agreement within 15% (`docs/plans/...md:369-372`). Both events share the same systematic proxy error (load% × Pnom/Vnom ignores inverter loss, self-consumption, and the UPS load definition — plan admits this in §2). Agreement is expected even when both are wrong; the 15% gate validates repeatability, not accuracy. The 0.5 confidence cap (§6) is honest, but the plan still commits a model mutation (which, per C1, feeds a safety-relevant prediction). Fix: require the proposal to be direction-safe (e.g., only accept candidates that imply *lower* usable capacity than rated × current SoH) or hold proposals at `recorded_only` until an independently calibrated current model exists (Stage 5), making Stage 3 a "candidate + manual approval" gate rather than automatic application.

**H3. Release A's `pending_replay=false` acceptance is unreachable on existing deployments without a legacy evidence-class mapping.**
Stage 0 fixes the *new* close path, but existing production journals contain end payloads with `evidence_class="operational"` (`src/discharge_collector.py:522,542`) and `"operational_partial"` (`:221`), while the replay skip-set in `src/monitor.py:262-268` matches only `operational_partial`/`operational_gapped`. On any host with a prior event closed as `"operational"`, restart will still set `pending_replay=True` (monitor.py:280-281), failing Release A pass criteria (`docs/plans/...md:447-452`). Stage 1's determinism requirement also needs the live `CompletedDischarge` class and the journaled end payload to be produced by one code path (today they're constructed separately, `src/discharge_collector.py:511-548`). Fix: add an explicit legacy→new class mapping applied during replay, and make the classifier the single producer of both.

## Medium

**M1. IR freeze ships a known-biased safety parameter while the remediation is deferred outside the release.**
Stage 0 stops `ir_k` learning, but `_compute_metrics` keeps using the persisted (plan-asserted saturated 0.025) value in `ir_compensate` (`src/monitor.py:644`; `src/ema_filter.py:148-170`). The code documents this value as systematically overestimated (`src/sag_tracker.py:185-196`), which inflates v_norm → SoC → runtime → delayed LB. Release A gates (`docs/plans/...md:469`) don't bound this effect; the "separate migration" has no gate tying it to any release. Fix: either pull the conservative ir_k correction into Release A (the plan already sketches the offline comparison) or add an acceptance criterion that the frozen value's effect on LB timing is bounded and monitored.

**M2. Stage 2 residual definitions lack the forward-simulator specification.**
`voltage_residual = observed - predicted` requires predicting voltage(t) from the frozen snapshot, but the only forward model is `runtime_minutes(soc, load, ...)` — there is no voltage trajectory or segment-duration predictor in the codebase. The "predicted segment duration" quantity is undefined (duration to reach what endpoint?). Without specifying the simulation formula (SoC(t) depletion model, SoC→voltage via LUT inversion, IR term, load treatment), the acceptance "фиксированный replay выдаёт ожидаемые отклонения" (`docs/plans/...md:468`) is untestable. Fix: write the forward-simulation spec in Stage 2 with a pure reference implementation before residuals.

**M3. Single-writer protection has no concrete mechanism.**
Plan §7.0 step 5 and smoke scenario 16 require a second writer to fail cleanly, but no design is given. Journal opens with `O_APPEND` and no `flock` (`src/discharge_journal.py:444-461`); a second daemon replays the open event, appends a reboot gap with its own boot id (`src/discharge_collector.py:405-451`), and interleaves records that pass validation (boot_id is per-record). Fix: specify an `flock(LOCK_EX|LOCK_NB)` on the journal fd (and an equivalent guard on the model/virtual-UPS writers), with the error path and test before Stage 0 closes.

**M4. Stage 3 architectural test conflicts with existing legitimate direct state mutations.**
"Прямые setters и записи в `battery_model.state` запрещаются" (`docs/plans/...md:493`) collides with `_reset_battery_baseline` (`src/monitor.py:514-551`, six direct fields) and `monitor.py:224` (`new_battery_detected=False` at init). The plan never migrates the `--new-battery` reset onto the sanctioned writer path. Fix: add a `BaselineReset` sanctioned mutation (or equivalent) to the Stage 3 file list.

## Low

- **L1.** "Научная часть модели" hash is undefined: today `get_persisted_hash` hashes the whole file (`src/model.py:696-706`), and observability/acceptance (plan §12, Release A) reference a scientific-subset hash. Define the field subset explicitly.
- **L2.** Freezing `r_internal_history` leaves stale IR median and trend published with no "frozen" marker (`src/virtual_ups_exporter.py:199-214`, `scripts/battery-health.py:349-360`). Reporting should flag IR as frozen/biased.
- **L3.** `READY` gating must handle partial NUT responses: `_poll_once` early-returns on missing voltage/load before any virtual-UPS write (`src/monitor.py:743-749`), so "свежая запись виртуального UPS" before readiness needs a defined behavior for that case.
- **L4.** Mild over-engineering risk: `LearningDecision` (8 fields) plus domain/application/infrastructure target layout for a ~20-file single daemon; the plan's own hedging (§3.4 "разносить лишь когда границы работают") mitigates, but the target structure at `docs/plans/...md:227-238` should be explicitly optional.
- **L5.** Smoke forbids `/run/ups-battery-monitor` but `HEALTH_ENDPOINT_PATH` and motd defaults are module-level constants (`src/monitor_config.py:58`, `src/motd_status.py:28`); the smoke must inject these too, not just the virtual-UPS path.

**Prioritized fix order:** C1 (prediction coupling + direction guard) → H3 (legacy mapping, unblocks Release A) → H1 (charge-history capture) → H2 (proposal safety gate) → M1 (ir_k remediation gating) → M2 (simulator spec) → M3 (flock design) → M4 (reset path), then L-items.
