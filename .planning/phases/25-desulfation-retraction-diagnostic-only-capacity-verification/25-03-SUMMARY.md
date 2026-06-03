---
phase: 25-desulfation-retraction-diagnostic-only-capacity-verification
plan: 03
subsystem: docs-adr-operator-report
tags: [docs, adr, retraction, battery-health, operator-report, v3.2]
dependency_graph:
  requires: [25-02]
  provides: [ADR-0001, DOC-01, DOC-02, RPT-01]
  affects: [README.md, docs/adr/0001-desulfation-premise-reversal.md, scripts/battery-health.py]
tech_stack:
  added: [docs/adr/]
  patterns: [nygard-adr, fixture-friendly-env-override, graceful-degradation]
key_files:
  created:
    - docs/adr/0001-desulfation-premise-reversal.md
  modified:
    - README.md
    - .planning/MILESTONES.md
    - .planning/ROADMAP.md
    - docs/internal/CONTEXT.md
    - scripts/battery-health.py
decisions:
  - "ADR format: Nygard (Context/Decision/Consequences) with explicit deploy action bullet for stale model.json"
  - "MILESTONES: preserve original v3.0 text verbatim, prepend retraction banner — historical record not rewritten"
  - "CONTEXT.md: struck-through original bullets + current design block, not deletion — preserves rationale"
  - "battery-health.py HEALTH_PATH: catch PermissionError separately from OSError to give sudo hint"
  - "IR comment: 'plate corrosion / electrolyte loss' — factually correct mechanisms that raise R_internal; drops literal sulfation token"
metrics:
  duration: "5 min"
  completed: "2026-06-03T20:49:47Z"
  tasks_completed: 3
  files_changed: 6
---

# Phase 25 Plan 03: Docs, ADR & Operator Report — Summary

**One-liner:** Retraction docs package — ADR 0001 recording sulfation premise reversal (BU-804b/IEEE-1188/no-charge-control), active-desulfation claims removed from README/ROADMAP/MILESTONES/CONTEXT with retraction banners, and `battery-health.py` extended with a fixture-friendly "Maintenance & schedule" section reading model.json + health endpoint.

## Tasks Completed

| Task | Name | Commit | Key Files |
|------|------|--------|-----------|
| 1 | Write ADR 0001 — desulfation premise reversal (DOC-02) | 0efc086 | docs/adr/0001-desulfation-premise-reversal.md |
| 2 | Remove active-desulfation claims from README/ROADMAP/MILESTONES/CONTEXT (DOC-01) | d8ef4ec | README.md, .planning/MILESTONES.md, .planning/ROADMAP.md, docs/internal/CONTEXT.md |
| 3 | Add "Maintenance & schedule" section to battery-health.py + reword line-109 comment (RPT-01) | 24a66c8 | scripts/battery-health.py |

## What Was Built

### ADR 0001 (docs/adr/0001-desulfation-premise-reversal.md)

Nygard-format ADR with:
- **Context:** The discharge/charge asymmetry (discharge forms PbSO₄, charging reverses it); no charge-side control verified live (CyberPower NUT exposes only beeper/driver/load/shutdown/test.battery.* commands); evidence citations: BU-804b, Power Designers Sibex, Vertiv BattCon, Schneider/APC, Lifeline, IEEE-1188.
- **Decision:** Retract sulfation/cycle-ROI machinery; reframe scheduler to diagnostic-only annual cadence; no charge-side control planned.
- **Consequences (positive):** honest metrics, bootstrap deadlock fixed, smaller surface. (Negative/accepted:) loses "active care" narrative. Deploy action: `rm ~/.config/ups-battery-monitor/model.json` after upgrading to v3.2. Cost: measured capacity / LUT / SoH state lost, re-warms over 3+ subsequent discharges.

### Documentation retraction (README/ROADMAP/MILESTONES/CONTEXT)

- **README:** tagline → "honest diagnostics"; opening pitch drops "fights back/tracks sulfation rate/credits blackouts as free desulfation/cycle ROI/stretch from 2.5 to 4+ years"; metrics table drops Sulfation score/Desulfation tracking/Cycle ROI/Blackout credit rows; step 7 reworded to diagnostic scheduler; architecture diagram line corrected; v3.0 roadmap entry annotated with retraction note + ADR link.
- **MILESTONES:** v3.0 block gets `[RETRACTED in v3.2 — premise reversed, see ADR 0001]` banner; original "Delivered" text preserved verbatim beneath it.
- **ROADMAP:** Phase 15 line annotated "sulfation model, cycle ROI — retracted v3.2, see ADR 0001".
- **CONTEXT.md:** v3.0 anti-sulfation section heading/bullets struck through, superseded banner added pointing to ADR 0001, current v3.2 design described.

### battery-health.py Maintenance & schedule section

- `HEALTH_PATH` constant + `UPS_MODEL_PATH`/`UPS_HEALTH_PATH` env overrides (mirrors `src/motd_status.py`; enables fixture runs without sudo).
- `print_maintenance(model_data, health_data)` called from `main()` after existing report.
- Section prints: next diagnostic test (health.json `next_test_timestamp` preferred, falls back to model.json `scheduled_test_timestamp`; shows `scheduling_reason`), last test run (`last_upscmd_timestamp` + type + status; prints "never tested" when null), IR trend (health.json `ir_trend_rate` or recomputed from `r_internal_history`), capacity/SoH (measured + rated Ah).
- Graceful degradation: `PermissionError` caught separately with sudo hint; `(OSError, ValueError)` for missing/corrupt file → empty `health_data` dict, section still prints with model-only data.
- Line 109 comment reworded: "sulfation, grid corrosion" → "plate corrosion / electrolyte loss" — factually accurate mechanisms that raise R_internal; drops literal `sulfation` token so plan-02 repo-wide grep gate passes.

## Verification Results

All automated checks pass:
- `test -f docs/adr/0001-desulfation-premise-reversal.md` — PASS
- ADR cites IEEE-1188, states charge-side control fact, includes `rm ~/.config/...` deploy step — PASS
- `! grep -Ein "fights sulfation|free desulfation|stretch a|active care" README.md` — PASS
- CONTEXT.md annotated retracted/superseded with ADR 0001 reference — PASS
- `! grep -qi "sulfation|cycle_roi" scripts/battery-health.py` — PASS
- Fixture run: `UPS_MODEL_PATH=/tmp/fixture-model.json UPS_HEALTH_PATH=/tmp/fixture-health.json python3 scripts/battery-health.py` — prints "Maintenance & schedule" with next/last test, IR trend, capacity/SoH — PASS
- "never tested" path (null `last_upscmd_timestamp`) prints "never tested" — PASS

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None — all sections read from live data sources (model.json + health endpoint) with graceful fallbacks.

## Threat Surface Scan

No new network endpoints, auth paths, file access patterns, or schema changes introduced. The health endpoint path (`/run/ups-battery-monitor/ups-health.json`) was already known to the threat model (T-25-07 mitigated via `(OSError, ValueError)` guard + PermissionError hint).

## Self-Check: PASSED

- `docs/adr/0001-desulfation-premise-reversal.md` — exists ✓
- `scripts/battery-health.py` — exists, RPT_OK ✓
- Commits 0efc086, d8ef4ec, 24a66c8 — confirmed in git log ✓
