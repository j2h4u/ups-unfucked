# Requirements: v3.2 Honest Monitoring & Diagnostic Verification

**Milestone goal:** Retract the v3.0 "active desulfation via scheduled deep discharges" mechanism (premise disproven — discharge forms sulfate, charging reverses it, and the daemon has no charge control), reframe the scheduler to occasional diagnostic capacity verification, fix the scheduler bootstrap deadlock, and correct the docs.

**Evidence basis (see ADR):** Battery University BU-804b, Power Designers Sibex, Vertiv BattCon, Schneider/APC, Lifeline, IEEE-1188. Verified live: CyberPower UT850 NUT exposes only beeper/driver/load/shutdown/test.battery.start.* — no charge-voltage control.

---

## In Scope

### Retraction (remove disproven-premise code)

- **RET-01** — Remove `src/battery_math/sulfation.py` (`compute_sulfation_score`, `estimate_recovery_delta`) and all production callers.
- **RET-02** — Remove `src/battery_math/cycle_roi.py` (`compute_cycle_roi`) and all production callers.
- **RET-03** — Remove blackout-credit-as-desulfation logic and recovery_delta-as-desulfation-evidence from the discharge path and scheduler.
- **RET-04** — Remove `sulfation_score` / `cycle_roi` (and sulfation-confidence/recovery) fields from the health endpoint, model.json schema/validation, MOTD output, and journald discharge events. Remove the tests that exercise the deleted code (`test_sulfation*.py`, `test_cycle_roi.py`, related scheduler assertions).

### Scheduler reframe (diagnostic-only)

- **SCH-01** — `evaluate_test_scheduling` proposes a test as a **diagnostic capacity/SoH verification**, driven by a simple time-based cadence (≈365 days since last test / IEEE-1188-style), NOT by a sulfation score or cycle ROI.
- **SCH-02** — The trigger reads the **persistent** `last_upscmd_timestamp` from model.json (restart-safe); a daemon restart must not re-trigger a test, and there is no dependency on in-memory sulfation state. This eliminates the confirmed bootstrap deadlock.
- **SCH-03** — Retain safety gates (SoH floor ≥60%, grid-stability cooldown, rate-limit). Default/first test is **quick** (low-risk); deep only rarely if explicitly warranted.

### Documentation

- **DOC-01** — Remove "actively fights sulfation / extends life via desulfation / desulfation tests" claims from PROJECT.md (done at milestone start), README.md, and ROADMAP/MILESTONES; replace with "honest monitoring + periodic diagnostic capacity verification."
- **DOC-02** — Add an ADR recording the premise reversal, the evidence, and the no-charge-control fact.

### Operator reporting

- **RPT-01** — Add a "Maintenance & schedule" section to `scripts/battery-health.py` (human-readable: next diagnostic test, last test run, IR trend, capacity/SoH) reading model.json + the health endpoint — no JSON hand-parsing.

---

## Acceptance Criteria (goal-backward)

- The daemon **never** self-initiates a discharge for "desulfation"; the only autonomous discharge is a rare diagnostic capacity test gated by safety.
- No `sulfation_score` / `cycle_roi` code or fields remain anywhere (vulture clean; grep clean across src/tests/scripts).
- Scheduler bootstrap deadlock is gone: with only short blackouts and no prior test, the daemon still schedules a first diagnostic test on cadence.
- Docs contain no active-desulfation claims; ADR present.
- `battery-health.py` shows the maintenance schedule.
- ruff/pyright/vulture clean; full pytest green; daemon restarts active.

## Out of Scope

- Any charge-side control (float voltage, equalization) — not exposed by CyberPower NUT.
- Backward compatibility / migration of old model.json — single-host, state regenerates (no compat shims).
- Re-deriving the electrochemistry — already cross-checked this cycle (captured in the ADR).

---

## Phase 26 — model.json learned-state hygiene (added 2026-06-04)

**Phase goal:** `model.json` persists ONLY learned per-battery state (category ③). Configuration/spec
(category ①) is read from `config.toml` / `constants.py` at runtime; derived caches (category ②) are
recomputed each poll. The `ModelState` schema shrinks to learned-state-only, and the Phase 25 strict
loader still passes against a real on-disk file after a one-time key strip. No backward-compat
(state regenerates); no behavioral change to computed outputs — only their source.

### In Scope

- **HYG-01** — Remove category ① config/spec from the persisted `ModelState` schema and on-disk
  `model.json`: `full_capacity_ah_ref`, `physics.nominal_voltage`, `physics.nominal_power_watts`.
  Split the mixed `physics` blob so learned keys (`peukert_exponent`, `ir_compensation.k_volts_per_percent`,
  `rls_state`) stay persisted.
- **HYG-02** — Source ①'s values from runtime config/constants instead of `self.state`:
  `get_capacity_ah()` returns the config-injected `capacity_ah` (not `state["full_capacity_ah_ref"]`,
  not round-tripped through `self.state`); `get_nominal_voltage()` / `get_nominal_power_watts()` return
  values from `constants.py` (add `NOMINAL_VOLTAGE = 12.0`). All existing getter callers keep working.
- **HYG-03** — Stop persisting category ② derived caches: `scheduled_test_timestamp`,
  `scheduled_test_reason`, `test_block_reason`, `capacity_converged`, `replacement_due`. Remove their
  schema keys, `_apply_defaults` setdefaults, and `_validate_and_clamp_fields` checks.
- **HYG-04** — Recompute ②'s health/operator/NUT-export values live each poll: the health endpoint,
  NUT virtual-UPS export, MOTD, and `battery-health.py` produce the SAME values from live in-memory
  computation (scheduler `last_*` properties, `get_convergence_status()`, and a live
  `replacement_due` regression on `soh_history`) — no read from persisted ② keys. Remove the now-dead
  `battery-health.py` `scheduled_test_timestamp` fallback and update `full_capacity_ah_ref` reads to
  prefer config `capacity_ah`.
- **HYG-05** — Update/remove tests that asserted persistence/round-trip of the removed keys
  (`tests/test_model.py`, `tests/test_health_endpoint_v16.py`, and any others); keep learned-state and
  live-output coverage. The strict loader's unknown-key rejection still passes against a stripped file.

### Acceptance Criteria (goal-backward)

- `model.json` written by the daemon contains none of: `full_capacity_ah_ref`,
  `physics.nominal_voltage`, `physics.nominal_power_watts`, `scheduled_test_timestamp`,
  `scheduled_test_reason`, `test_block_reason`, `capacity_converged`, `replacement_due`.
- `ModelState` / `KNOWN_STATE_KEYS` no longer declare those keys; `_reject_unknown_state_keys`
  passes against a freshly-regenerated (stripped) on-disk file.
- health.json, NUT `battery.replacement.due`, MOTD, and `battery-health.py` show the same values as
  before for the same inputs, now computed live (verified by test).
- `get_capacity_ah()` / `get_nominal_voltage()` / `get_nominal_power_watts()` return correct values
  with no persisted backing; all callers unaffected.
- A deployment note documents the stop → strip-keys → start sequence (or model.json delete);
  no migration code is added.
- ruff / pyright / vulture clean; full `uv run pytest` green; daemon restarts active.

### Out of Scope

- Migration of old model.json — single-host, state regenerates (no compat shims).
- Adaptive cooldown / scheduler behavior changes — Phase 26 is pure persistence hygiene.
- Touching learned-state (category ③) semantics, including `capacity_ah_measured`.

---
*Created: 2026-06-03 — milestone v3.2*
*Updated: 2026-06-04 — added Phase 26 (HYG-01..05) learned-state hygiene requirements*
