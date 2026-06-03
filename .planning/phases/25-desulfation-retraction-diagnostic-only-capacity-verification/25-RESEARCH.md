# Phase 25 Research — Desulfation Retraction → Diagnostic-Only Capacity Verification

**Researched:** 2026-06-03
**Phase goal:** Daemon stops self-initiating "desulfation" discharges; scheduler proposes only a rare diagnostic capacity/SoH test on a persistent time cadence (deadlock gone); all sulfation/cycle_roi machinery removed; docs honest.

---

## 1. The bootstrap deadlock (root cause, confirmed)

`SchedulerManager._gather_scheduler_inputs()` (src/scheduler_manager.py:317-331) sources two
decision-driving inputs from the discharge handler:

```
"sulfation_score": self.discharge_handler.last_sulfation_score or 0.0,
"cycle_roi":       self.discharge_handler.last_cycle_roi or 0.0,
```

These `last_*` fields are `None`/`0.0` until a discharge completes
(`DischargeHandler._compute_sulfation_metrics`). On a fresh daemon with only short
blackouts, they stay `0.0`. The scheduler then falls through gates 6/7 of
`evaluate_test_scheduling` (src/battery_math/scheduler.py):

- Gate 6 (ROI): `cycle_roi < 0.2 and cycle_budget_remaining > 20` → **defer_test/marginal_roi**.
- Gate 7 (sulfation): `sulfation_score <= 0.40` → **defer_test/low_sulfation**.

So with no discharge data the scheduler **never proposes a test** → no discharge ever
happens → `last_*` never populate → permanent deadlock. The cadence trigger
(`days_since_last_test`) is already computed restart-safely from the persistent
`last_upscmd_timestamp` (model.json) — but it only feeds the *rate-limit* gate
(min 7 days), never a *propose* path. The fix is to make the time cadence the
**propose driver**, not a floor.

## 2. Persistent timestamp (SCH-02) — already restart-safe, reuse as-is

- `BatteryModel.get_last_upscmd_timestamp()` (src/model.py:885) reads `state["last_upscmd_timestamp"]`.
- Written by `update_upscmd_result()` (src/model.py:870-883) after every dispatch (OK or error).
- `SchedulerManager._calculate_days_since_last_test()` (src/scheduler_manager.py:289-301)
  already converts it to days, returning `inf` when never tested.
- Validated as a string in `_validate_and_clamp_fields()` (src/model.py:335). Corrupt → None → `inf`.

**Restart safety** is already guaranteed: the trigger derives only from this persistent
field plus wall-clock; nothing in-memory. A restart recomputes the same `days_since_last_test`.
The only change needed is to make `evaluate_test_scheduling` *propose* on the cadence rather
than only rate-limit on it.

## 3. New scheduler contract (SCH-01/03)

Drop params `sulfation_score`, `cycle_roi`. Keep `soh_fraction`, `days_since_last_test`,
`last_blackout_timestamp`, `cycle_budget_remaining`, `grid_stability_cooldown_hours`.
Drop `active_blackout_credit` (RET-03 deletes blackout credit entirely).

Proposed gate order (diagnostic cadence):
1. SoH floor (`< 0.60` → block_test/soh_floor, eligible +30d) — KEEP.
2. Rate limit (`days_since_last_test < MIN_DAYS_BETWEEN_TESTS` → defer/rate_limit) — KEEP, prevents re-trigger after a just-run test and after restart.
3. Grid stability (cooldown after recent blackout) — KEEP.
4. Cycle budget (`< CRITICAL_CYCLE_BUDGET` → block) — KEEP (cheap safety; cycle_budget_remaining is still produced by discharge_handler, defaulting to 100).
5. **Cadence propose:** if `days_since_last_test >= DIAGNOSTIC_TEST_INTERVAL_DAYS` (≈365, IEEE-1188) → **propose_test, test_type="quick", reason_code="diagnostic_cadence"**. Else → defer_test/within_cadence with `next_eligible` = now + (interval − days_since).
   - First test (never tested, `days_since_last_test == inf`) satisfies the cadence → proposes immediately (after rate-limit/SoH/grid gates), killing the deadlock.

Constants to remove: `ROI_THRESHOLD`, `DEEP_SULFATION_THRESHOLD`, `QUICK_SULFATION_THRESHOLD`.
Add: `DIAGNOSTIC_TEST_INTERVAL_DAYS = 365.0` (IEEE-1188 annual capacity/diagnostic cadence).
Keep `MIN_DAYS_BETWEEN_TESTS`, `SOH_FLOOR`, `CRITICAL_CYCLE_BUDGET`.
Default/first test = **quick** (SCH-03). Deep is out of scope for autonomous proposal now;
`test_type` Literal can keep "deep" for the dataclass but the engine only emits "quick".

`SchedulerManager.run_daily` / `_gather_scheduler_inputs` / `_execute_scheduler_decision`
must drop the sulfation_score/cycle_roi keys and the verbose-log lines that reference them
(src/scheduler_manager.py:235-236, 245-246, 355-356). `_get_last_natural_blackout` stays
(feeds grid-stability). `get_blackout_credit` call and `active_credit` key go away.

## 4. Sulfation/cycle_roi removal surface (RET-01..04) — full inventory

**Delete files:** `src/battery_math/sulfation.py`, `src/battery_math/cycle_roi.py`.

**src/battery_math/__init__.py** — drop imports/exports: `compute_cycle_roi`,
`compute_sulfation_score`, `estimate_recovery_delta`, `SulfationState` (lines 2, 7, 16-19).

**src/discharge_handler.py** — heaviest. Remove:
- imports (lines 18, 20-24);
- `DischargeMetrics` fields tied to sulfation/roi: `sulfation_state`, `roi`, `sulfation_score_r`,
  `recovery_delta_r`, `roi_r` (keep `days_since_deep_r`, `ir_trend_r` if still surfaced —
  see below; `days_since_deep`/`ir_trend_rate` are independent IR signals worth keeping);
- `last_sulfation_score`, `last_sulfation_confidence`, `last_recovery_delta`, `last_cycle_roi`
  instance fields (lines 110-115); keep `last_cycle_budget_remaining` (scheduler safety gate),
  `last_days_since_deep`, `last_ir_trend_rate` (IR diagnostics still valid);
- `_score_and_persist_sulfation`, `_compute_sulfation_metrics` (the `compute_sulfation_score`/
  `compute_cycle_roi`/`estimate_recovery_delta` calls), `_persist_sulfation_and_discharge`
  (the `append_sulfation_history` + `sulfation_score`/`recovery_delta`/`cycle_roi` writes),
  `_assess_sulfation_confidence`, `_grant_blackout_credit`;
- `recovery_delta`/`sulfation_score`/`sulfation_confidence`/`cycle_roi` keys from
  `_log_discharge_complete` (journald, lines 448-451).
- The discharge pipeline still needs to: compute SoH, Peukert calibration, replacement
  prediction, alerts, append a `discharge_event` (without `cycle_roi`), update IR trend.
  Restructure `update_battery_health` to call a slimmed persist path.

**src/model.py** — remove:
- `setdefault("sulfation_history", [])`, `setdefault("roi_history", [])`,
  `setdefault("blackout_credit", None)` (lines 299, 301, 308);
- `append_sulfation_history` (561-576);
- `cycle_roi` from `append_discharge_event` docstring (588) — and ensure callers stop passing it;
- `set_blackout_credit`/`clear_blackout_credit`/`get_blackout_credit` (835-852, 889-891);
- `sulfation_history`/`roi_history` from `_validate_and_clamp_fields` list-check and from
  `save()` cap list (606-607 + the missing_keys/required list near 353);
- the `blackout_credit` dict-validation block (367-373).

**src/monitor_config.py** — remove `CurrentMetrics`/`HealthSnapshot` fields
`sulfation_score`, `sulfation_confidence`, `recovery_delta`, `cycle_roi` and their
health_data lines (303-304, 307-308, 344-345, 348-349). Keep `days_since_deep`,
`ir_trend_rate`, `cycle_budget_remaining`, `scheduling_reason`, `next_test_timestamp`
(legit diagnostics). Update the docstring on line 75 ("ROI threshold, sulfation").

**src/virtual_ups_exporter.py** — drop the snapshot args `sulfation_score`,
`sulfation_confidence`, `recovery_delta`, `cycle_roi` (lines 53-58).

**src/monitor.py** — lines 376-377 comment about "desulfation credit" on battery
replacement; the actual `clear_blackout_credit` call there must be removed with the method.
Confirm during execution whether a real call exists (grep `blackout_credit` in monitor.py).

**src/motd_status.py** — remove `sulfation_pct` mapping (line 82) and the doc line (12).

**scripts/motd/55-sulfation.sh** — delete the module entirely (it renders sulfation_pct +
next-test). Replace with nothing, OR repurpose to a "next diagnostic test" line. Decision:
delete the sulfation module; next-test countdown can live in the existing 51-ups-health.sh
or be dropped (MOTD is status-only). RET-04 says remove MOTD output → delete the file.

## 5. Tests (RET-04)

**Delete wholesale** (exercise only deleted code):
`tests/test_sulfation.py`, `tests/test_sulfation_persistence.py`,
`tests/test_sulfation_offline_harness.py`, `tests/test_cycle_roi.py`.

**Surgical edits** (mixed — keep the non-sulfation assertions, drop sulfation/roi ones):
- `tests/test_scheduler.py` — full rewrite: the engine signature changed. New tests cover
  cadence-propose, first-test-from-inf, SoH floor, rate-limit, grid cooldown, cycle budget,
  restart-no-retrigger (days_since just under interval → defer).
- `tests/test_scheduler_manager.py` — drop sulfation_score/cycle_roi from patched inputs;
  keep precondition/dispatch tests.
- `tests/test_discharge_handler.py`, `tests/test_discharge_event_logging.py` — drop
  sulfation/cycle_roi/recovery_delta assertions; keep SoH/Peukert/discharge_event assertions.
- `tests/test_model.py` — drop sulfation_history/roi_history/blackout_credit assertions.
- `tests/test_health_endpoint_v16.py` — drop sulfation_score/cycle_roi/recovery_delta keys.
- `tests/test_motd_status.py` — drop sulfation_pct.
- `tests/test_monitor.py`, `tests/test_dispatch.py`, `tests/test_year_simulation.py` —
  drop sulfation/credit references; year-sim must still run (it drives discharges).

Final gates: `grep -rn 'sulfation\|cycle_roi' src tests scripts` returns nothing;
`uvx ruff check`, `uvx pyright`/pyright, `uvx vulture` clean; `uv run pytest` green.

## 6. Docs (DOC-01/02) + ADR convention

- **No existing ADR directory.** Establish `docs/adr/` with `0001-desulfation-premise-reversal.md`
  (Nygard-style: Context / Decision / Consequences). Record: premise (discharge forms sulfate,
  charging reverses it; daemon has no charge control — CyberPower UT850 NUT exposes only
  beeper/driver/load/shutdown/test.battery.start.*), evidence (BU-804b, Power Designers Sibex,
  Vertiv BattCon, Schneider/APC, Lifeline, IEEE-1188), and the reframe to diagnostic-only.
- **README.md** — rewrite lines 13, 48-52, 66, 82, 160: remove "fights back / fights sulfation /
  desulfation tracking / cycle ROI / blackout credit as free desulfation / stretch 2.5→4 yr".
  Replace with honest monitoring + periodic diagnostic capacity verification framing.
- **ROADMAP.md / MILESTONES.md** — PROJECT.md already done at milestone start. Sweep ROADMAP
  v3.0 retro language and any MILESTONES desulfation claims. (MILESTONES.md existence: confirm
  during execution; only README.md + docs/ exist at top level — MILESTONES may be under .planning.)
- **docs/GLOSSARY.md:34** "plate sulfation (irreversible damage)" is a *factual* physics note,
  not an active-desulfation claim — leave it (or soften), do not remove.

## 7. battery-health.py "Maintenance & schedule" (RPT-01)

`scripts/battery-health.py` (121 lines) reads `~/.config/ups-battery-monitor/model.json`
via `json.loads(MODEL_PATH.read_text())` and shells `upsc` for nameplate. It does NOT
currently read the health endpoint. RPT-01 wants a "Maintenance & schedule" section
(next diagnostic test, last test run, IR trend, capacity/SoH) reading model.json + health
endpoint "no JSON hand-parsing" — i.e., reuse the same `json.loads(Path.read_text())` helper
already used for model.json, applied to the health endpoint path
(`HEALTH_ENDPOINT_PATH` — confirm location, likely `~/.config/ups-battery-monitor/health.json`).
Pull: `scheduled_test_timestamp`/`scheduled_test_reason` + `last_upscmd_timestamp`/`_type`/`_status`
(model.json), `next_test_timestamp`/`scheduling_reason`/`ir_trend_rate` (health.json),
SoH/capacity (already shown). Add a `def print_maintenance(model_data, health_data)` and call
from `main()`. IR trend already partially shown (R_internal trend, lines 113-117) — reuse.

## 8. Wave ordering rationale

reframe (Wave 1) → delete (Wave 2) → docs (Wave 3) is intentional: Wave 1 rewrites
`scheduler.py` + `scheduler_manager.py` to stop reading sulfation/roi, so Wave 2 can delete
those modules without leaving a dangling caller mid-phase. Wave 2 touches discharge_handler/
model/monitor_config/exporter/motd + tests. Wave 3 is docs-only (README/ADR/battery-health.py)
— zero overlap with code files, so it could parallelize, but it is kept last so docs describe
the *final* shipped behavior. **File-overlap check:** Wave 1 owns scheduler.py +
scheduler_manager.py; Wave 2 owns discharge_handler/model/monitor_config/exporter/motd_status +
tests (incl. test_scheduler*.py rewrite); Wave 3 owns README/docs/battery-health.py. The only
risk: test_scheduler.py rewrite belongs with the Wave 1 signature change, not Wave 2 deletion.
Decision: **test_scheduler.py rewrite goes in Wave 1** (same file-concern as scheduler.py);
Wave 2 handles only the sulfation/cycle_roi test deletions + surgical edits to the other tests.

## Validation Architecture

- **Scheduler cadence (SCH-01/02):** unit tests on `evaluate_test_scheduling` — first test from
  `inf`, propose at ≥interval, defer just under interval (restart no-retrigger), SoH floor block,
  rate-limit defer, grid cooldown defer, cycle-budget block. Pure function, fully offline.
- **Retraction completeness (RET-01..04):** `grep -rn 'sulfation\|cycle_roi' src tests scripts`
  == empty; `uvx vulture src` clean; health.json/model.json no longer contain the fields
  (assert in test_health_endpoint + test_model).
- **Docs (DOC-01/02):** `grep -in 'fights sulfation\|desulfation\|cycle roi' README.md` == empty;
  `test -f docs/adr/0001-*.md`.
- **Operator report (RPT-01):** run `python scripts/battery-health.py` against a fixture model.json
  + health.json; assert "Maintenance & schedule" header + next/last test lines present.
- **Regression:** full `uv run pytest` green; `uvx ruff check`; pyright clean.
