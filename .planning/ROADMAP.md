# Roadmap: UPS Battery Monitor

## Milestones

- ✅ **v1.0 MVP** — Phases 1-6 (shipped 2026-03-14)
- ✅ **v1.1 Expert Panel Review Fixes** — Phases 7-11 (shipped 2026-03-14)
- ✅ **v2.0 Actual Capacity Estimation** — Phases 12-14 (shipped 2026-03-16)
- ✅ **v3.0 Active Battery Care** — Phases 15-17 (shipped 2026-03-20)
- ✅ **v3.1 Code Quality Hardening** — Phases 18-24 (shipped 2026-03-21)
- 🔵 **v3.2 Honest Monitoring & Diagnostic Verification** — Phase 25 (ACTIVE, started 2026-06-03)

## Phases

<details>
<summary>✅ v1.0 MVP (Phases 1-6) — SHIPPED 2026-03-14</summary>

- [x] Phase 1: Foundation — NUT Integration & Core Infrastructure (5/5 plans) — completed 2026-03-13
- [x] Phase 2: Battery Model — State Estimation & Event Classification (6/6 plans) — completed 2026-03-14
- [x] Phase 3: Virtual UPS & Safe Shutdown (4/4 plans) — completed 2026-03-14
- [x] Phase 4: Health Monitoring & Battery Degradation (2/2 plans) — completed 2026-03-14
- [x] Phase 5: Operational Setup & Systemd Integration (2/2 plans) — completed 2026-03-14
- [x] Phase 6: Calibration Mode (2/2 plans) — completed 2026-03-14

</details>

<details>
<summary>✅ v1.1 Expert Panel Review Fixes (Phases 7-11) — SHIPPED 2026-03-14</summary>

- [x] Phase 7: Safety-Critical Metrics (2 plans) — completed 2026-03-14
- [x] Phase 8: Architecture Foundation (4 plans) — completed 2026-03-15
- [x] Phase 9: Test Coverage (3 plans) — completed 2026-03-14
- [x] Phase 10: Code Quality & Efficiency (2 plans) — completed 2026-03-14
- [x] Phase 11: Polish & Future Prep (3 plans) — completed 2026-03-14

</details>

<details>
<summary>✅ v2.0 Actual Capacity Estimation (Phases 12-14) — SHIPPED 2026-03-16</summary>

- [x] Phase 12: Deep Discharge Capacity Estimation (4/4 plans) — completed 2026-03-16
- [x] Phase 12.1: Math Kernel Extraction & Formula Stability Tests (6/6 plans, INSERTED) — completed 2026-03-16
- [x] Phase 13: SoH Recalibration & New Battery Detection (2/2 plans) — completed 2026-03-16
- [x] Phase 14: Capacity Reporting & Metrics (3/3 plans) — completed 2026-03-16

</details>

<details>
<summary>✅ v3.0 Active Battery Care (Phases 15-17) — SHIPPED 2026-03-20</summary>

- [x] Phase 15: Foundation — NUT INSTCMD + test scheduler (5/5 plans) — completed 2026-03-17 *(sulfation model, cycle ROI — retracted v3.2, see [ADR 0001](../docs/adr/0001-desulfation-premise-reversal.md))*
- [x] Phase 16: Persistence & Observability — health.json, journald, MOTD (6/6 plans) — completed 2026-03-17
- [x] Phase 17: Scheduling Intelligence — daemon-controlled test scheduling (2/2 plans) — completed 2026-03-17

</details>

<details>
<summary>✅ v3.1 Code Quality Hardening (Phases 18-24) — SHIPPED 2026-03-21</summary>

- [x] Phase 18: Unify Coulomb Counting — single integrate_current() (1/1 plans) — completed 2026-03-20
- [x] Phase 19: Extract SagTracker — own module (1/1 plans) — completed 2026-03-20
- [x] Phase 20: Extract SchedulerManager — own module (1/1 plans) — completed 2026-03-20
- [x] Phase 21: Extract DischargeCollector — own module + sulfation split (2/2 plans) — completed 2026-03-20
- [x] Phase 22: Naming + Docs Sweep — renames + docstrings (2/2 plans) — completed 2026-03-20
- [x] Phase 23: Test Quality Rewrite — outcome assertions + DI (4/4 plans) — completed 2026-03-20
- [x] Phase 24: Temperature + Security Hardening — NUT probe, validation, auth docs (2/2 plans) — completed 2026-03-21

</details>

### 🔵 v3.2 Honest Monitoring & Diagnostic Verification (Phase 25) — ACTIVE

**Milestone goal:** Retract the v3.0 "active desulfation via scheduled deep discharges" mechanism (premise disproven — discharge *forms* sulfate, charging reverses it, and the daemon has no charge control on CyberPower), reframe the scheduler to rare diagnostic capacity verification, fix the scheduler bootstrap deadlock, and correct the docs. Single-host, fail-fast, no backward-compat (state regenerates).

- [x] **Phase 25: Desulfation Retraction → Diagnostic-Only Capacity Verification** — reframe scheduler + fix Catch-22, remove sulfation/cycle_roi machinery, correct docs/ADR + battery-health report (SCH-01..03, RET-01..04, DOC-01..02, RPT-01)

#### Phase 25: Desulfation Retraction → Diagnostic-Only Capacity Verification

**Goal**: The daemon no longer self-initiates discharges for "desulfation": the scheduler proposes only a rare diagnostic capacity/SoH test on a persistent time cadence (bootstrap deadlock gone), all disproven-premise sulfation/cycle_roi machinery is removed, and the docs reflect honest monitoring.
**Depends on**: Nothing (first phase of v3.2)
**Requirements**: SCH-01, SCH-02, SCH-03, RET-01, RET-02, RET-03, RET-04, DOC-01, DOC-02, RPT-01
**Plans** (waves — one phase, ordered reframe→delete→docs to avoid cross-phase edits to the same files):

  - Wave 1 — Scheduler reframe + Catch-22 fix (SCH-01..03): `evaluate_test_scheduling` → diagnostic time-based cadence; persistent `last_upscmd_timestamp` trigger; safety gates intact; first test `quick`.
  - Wave 2 — Desulfation retraction (RET-01..04): delete `sulfation.py`, `cycle_roi.py`, wiring, `sulfation_score`/`cycle_roi` fields (health/model/MOTD/journald), and their tests.
  - Wave 3 — Docs, ADR & operator report (DOC-01..02, RPT-01): correct README/premise claims, write ADR, add "Maintenance & schedule" to `battery-health.py`.

**Success Criteria** (what must be TRUE):

  1. The scheduler proposes tests on a simple ≈365-day IEEE-1188-style cadence with zero dependency on sulfation/cycle-ROI; with only short blackouts and no prior test it still schedules a first diagnostic test (deadlock gone); a daemon restart does not re-trigger.
  2. Safety gates hold (SoH floor ≥60%, grid-stability cooldown, rate-limit); default/first test is `quick`.
  3. `sulfation.py`, `cycle_roi.py` and all callers are gone; grep for `sulfation`/`cycle_roi` is clean across src/tests/scripts; `sulfation_score`/`cycle_roi` fields absent from health endpoint, model.json, MOTD, journald.
  4. Tests covering deleted code removed; full pytest green; ruff/pyright/vulture clean.
  5. README/ROADMAP/MILESTONES carry no active-desulfation claims; an ADR records the premise reversal + evidence (BU-804b, Vertiv BattCon, IEEE-1188) + no-charge-control fact; `battery-health.py` shows a "Maintenance & schedule" section.

**Plans**: 3 plans

- [x] 25-01-PLAN.md — Wave 1: reframe `evaluate_test_scheduling` to diagnostic ~365d cadence + fix bootstrap deadlock (SCH-01..03)
- [x] 25-02-PLAN.md — Wave 2: delete sulfation.py/cycle_roi.py, strip fields (health/model/MOTD/journald), remove blackout credit + dead tests (RET-01..04)
- [x] 25-03-PLAN.md — Wave 3: correct README/ROADMAP/MILESTONES, write ADR 0001, add "Maintenance & schedule" to battery-health.py (DOC-01..02, RPT-01)

## Progress

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Foundation | v1.0 | 5/5 | Complete | 2026-03-13 |
| 2. Battery Model | v1.0 | 6/6 | Complete | 2026-03-14 |
| 3. Virtual UPS | v1.0 | 4/4 | Complete | 2026-03-14 |
| 4. Health Monitoring | v1.0 | 2/2 | Complete | 2026-03-14 |
| 5. Operational Setup | v1.0 | 2/2 | Complete | 2026-03-14 |
| 6. Calibration Mode | v1.0 | 2/2 | Complete | 2026-03-14 |
| 7. Safety-Critical Metrics | v1.1 | 2/2 | Complete | 2026-03-14 |
| 8. Architecture Foundation | v1.1 | 4/4 | Complete | 2026-03-15 |
| 9. Test Coverage | v1.1 | 3/3 | Complete | 2026-03-14 |
| 10. Code Quality & Efficiency | v1.1 | 2/2 | Complete | 2026-03-14 |
| 11. Polish & Future Prep | v1.1 | 3/3 | Complete | 2026-03-14 |
| 12. Deep Discharge Capacity Estimation | v2.0 | 4/4 | Complete | 2026-03-16 |
| 12.1 Math Kernel & Stability Tests | v2.0 | 6/6 | Complete | 2026-03-16 |
| 13. SoH Recalibration & New Battery | v2.0 | 2/2 | Complete | 2026-03-16 |
| 14. Capacity Reporting & Metrics | v2.0 | 3/3 | Complete | 2026-03-16 |
| 15. Foundation | v3.0 | 5/5 | Complete | 2026-03-17 |
| 16. Persistence & Observability | v3.0 | 6/6 | Complete | 2026-03-17 |
| 17. Scheduling Intelligence | v3.0 | 2/2 | Complete | 2026-03-17 |
| 18. Unify Coulomb Counting | v3.1 | 1/1 | Complete | 2026-03-20 |
| 19. Extract SagTracker | v3.1 | 1/1 | Complete | 2026-03-20 |
| 20. Extract SchedulerManager | v3.1 | 1/1 | Complete | 2026-03-20 |
| 21. Extract DischargeCollector | v3.1 | 2/2 | Complete | 2026-03-20 |
| 22. Naming + Docs Sweep | v3.1 | 2/2 | Complete | 2026-03-20 |
| 23. Test Quality Rewrite | v3.1 | 4/4 | Complete | 2026-03-20 |
| 24. Temperature + Security Hardening | v3.1 | 2/2 | Complete | 2026-03-21 |
| 25. Desulfation Retraction → Diagnostic-Only Capacity Verification | v3.2 | 3/3 | Complete    | 2026-06-03 |

### Phase 26: model.json learned-state hygiene: move config/spec and derived caches out of persisted battery state

**Goal:** `model.json` persists ONLY learned per-battery state (category ③). Configuration/spec (category ①) is read from `config.toml`/`constants.py` at runtime and not persisted; derived caches (category ②) are recomputed and not persisted. The `ModelState` schema shrinks to learned-state-only, and the strict loader still passes against a real on-disk file after a one-time key strip.

**Requirements**: HYG-01, HYG-02, HYG-03, HYG-04, HYG-05
**Depends on:** Phase 25 (ModelState schema + strict load validation must exist first)
**Status:** Planned (2026-06-04)
**Plans:** 1/2 plans executed

  - Wave 1 — Category ① config/spec (HYG-01, HYG-02): add `NOMINAL_VOLTAGE` constant; source
    nominal voltage/power from `constants.py`; inject `capacity_ah` into `BatteryModel` from config;
    drop `full_capacity_ah_ref` + `physics.nominal_voltage`/`nominal_power_watts` from the schema,
    validation, default seed; point `battery-health.py` rated-capacity at config.toml.

  - Wave 2 — Category ② derived caches (HYG-03, HYG-04, HYG-05): stop persisting
    `scheduled_test_*`/`test_block_reason` (delete `update_scheduling_state`; scheduler keeps live
    `last_*`), `capacity_converged` (health reads live convergence), and `replacement_due` (new
    `compute_replacement_due()` live regression, threshold 0.80 to preserve equivalence); remove dead
    persistence tests, add a replacement_due-equivalence test + strict-loader regen/old-file-reject
    gates; document the one-time stop→strip-keys→start deploy.

**Scope (evidence-based classification from the Phase 25 wrap-up discussion):**

- **① Config/spec to REMOVE from model.json** (read from config/constants instead):
  - `full_capacity_ah_ref` — already overwritten from `config.capacity_ah` on every start (`monitor.py:129`); the persisted copy is never authoritative → pure redundancy.
  - `physics.nominal_voltage` (12.0), `physics.nominal_power_watts` (`NOMINAL_POWER_WATTS`) — fixed spec, never learned. NOTE: `physics` is a MIXED blob — `peukert_exponent` / `ir_compensation.k_volts_per_percent` / `rls_state` ARE learned (category ③) and MUST stay. Split the blob, don't drop it.
- **② Derived caches to STOP persisting** (recompute at runtime; they feed the health endpoint, never gate decisions):
  - `scheduled_test_timestamp`, `scheduled_test_reason`, `test_block_reason` — scheduler OUTPUT; gates key off `last_upscmd_timestamp`/`last_upscmd_status`, not these.
  - `capacity_converged` — derived from `capacity_estimates` CoV.
  - `replacement_due` — derived from `soh_history` regression.

**Known gotchas:**

- The health endpoint (`monitor_config.py` `HealthSnapshot` / `write_health_endpoint`) currently reads ②'s values from `model.json`; after this change it must populate them from the live in-memory computation each poll (and on the first poll after restart). Touches `tests/test_health_endpoint_v16.py`.
- Changing `ModelState` to drop these keys means the strict loader will reject the existing on-disk `model.json` (which still has them) → deploy needs the same stop → strip keys → start sequence used in Phase 25, OR a one-time strip. Backup at `~/.config/ups-battery-monitor/model.json.pre-v3.2-cleanup.bak`.
- `capacity_ah_measured` (learned SoH baseline, category ③) is distinct from `full_capacity_ah_ref` (config) — do not conflate them.

Plans:

- [x] 26-01-PLAN.md — Wave 1: category ① config/spec out of model.json — NOMINAL_VOLTAGE constant,
  capacity_ah injection, split physics blob, drop full_capacity_ah_ref/nominal_voltage/nominal_power_watts (HYG-01, HYG-02)

- [ ] 26-02-PLAN.md — Wave 2: category ② derived caches out of model.json — drop scheduled_test_*/test_block_reason/capacity_converged,
  live compute_replacement_due, strict-loader gates + deploy strip note (HYG-03, HYG-04, HYG-05)

---

*Roadmap updated: 2026-06-03 — v3.2 Honest Monitoring & Diagnostic Verification milestone started (Phase 25, active; reframe→delete→docs as internal waves)*
