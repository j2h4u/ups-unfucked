# Phase 26: model.json learned-state hygiene — Context

**Gathered:** 2026-06-04
**Status:** Ready for planning
**Source:** ROADMAP Phase 26 scope + code investigation (PRD-equivalent express path)

<domain>
## Phase Boundary

`model.json` must persist ONLY learned per-battery state (category ③). This phase removes two
categories of non-learned data from the persisted `ModelState` schema:

- **① Configuration/spec** — read from `config.toml` / `constants.py` at runtime; never persisted.
- **② Derived caches** — recomputed at runtime each poll; never persisted. They feed the health
  endpoint / operator report; they never gate scheduler or safety decisions.

The `ModelState` TypedDict (added in Phase 25) shrinks to learned-state-only. Because Phase 25's
strict loader (`_reject_unknown_state_keys`) rejects any key not in `KNOWN_STATE_KEYS`, the existing
on-disk `model.json` (which still carries ① and ② keys) will be rejected after the schema shrinks —
so deploy requires the same stop → strip-keys → start mechanic Phase 25 established, OR a one-time
key strip. State regenerates per the no-backward-compat / single-host policy.

This is a pure-hygiene refactor: no new product behavior. The daemon's computed outputs
(health.json, NUT virtual UPS, MOTD, battery-health.py) must remain byte-for-byte equivalent in
their VALUES — only the SOURCE of those values changes from "persisted then read" to "computed live".
</domain>

<decisions>
## Implementation Decisions (locked — from ROADMAP + code investigation)

### Category ① — Config/spec to REMOVE from persisted model.json
- **`full_capacity_ah_ref`** — already overwritten from `config.capacity_ah` on every start
  (`monitor.py:129`: `self.battery_model.state["full_capacity_ah_ref"] = config.capacity_ah`).
  The persisted copy is never authoritative → pure redundancy. Remove from schema; `get_capacity_ah()`
  must source the value from the config-injected runtime value, NOT from `self.state`.
  - The `_validate_and_clamp_fields` `full_capacity_ah_ref` validation block (model.py:383-393)
    is removed with the key.
  - The `_default_vrla_lut` `full_capacity_ah_ref` seed (model.py:456) is removed.
- **`physics.nominal_voltage`** (12.0) and **`physics.nominal_power_watts`** (`NOMINAL_POWER_WATTS`) —
  fixed spec, never learned. Remove from the persisted `physics` blob.
  - `physics` is a MIXED blob: `peukert_exponent`, `ir_compensation.k_volts_per_percent`, and
    `rls_state` ARE learned (category ③) and MUST STAY. Split the blob — do not drop it.
  - **CRITICAL:** `get_nominal_voltage()` / `get_nominal_power_watts()` are load-bearing getters
    called across 6+ modules (sag_tracker, capacity_estimator, discharge_handler, monitor,
    runtime_calculator consumers). They must keep working, sourcing from `constants.py` instead of
    persisted state. `NOMINAL_POWER_WATTS` already exists in constants; add a `NOMINAL_VOLTAGE = 12.0`
    constant (currently 12.0 is hardcoded in PhysicsParams default and `_sync_physics_from_state`).
  - `_sync_physics_to_state` (model.py:319-332) must stop writing nominal_voltage/nominal_power_watts.
  - `_sync_physics_from_state` (model.py:289-317) must stop reading them from state.
  - `PhysicsParams` dataclass: nominal_voltage/nominal_power_watts become non-persisted spec values
    sourced from constants (keep the getters returning them; decide whether they remain dataclass
    fields fed by constants or become direct constant reads — planner's discretion, but getters
    must return the constant values).

### Category ② — Derived caches to STOP persisting (recompute at runtime)
- **`scheduled_test_timestamp`, `scheduled_test_reason`, `test_block_reason`** — scheduler OUTPUT.
  Health endpoint already reads these live from `scheduler_manager.last_scheduling_reason` /
  `last_next_test_timestamp` (virtual_ups_exporter.py:56-57), NOT from model.json. Gates key off
  `last_upscmd_timestamp` / `last_upscmd_status` (category ③), never these. `update_scheduling_state`
  (model.py:865-878) currently persists them — stop persisting. The in-memory
  `scheduler_manager.last_*` properties already carry the live values for the health snapshot.
  - `battery-health.py:37` reads `scheduled_test_timestamp` only as a FALLBACK when health.json
    lacks `next_test_timestamp` — that fallback becomes dead (health.json always has it) → remove.
  - `_apply_defaults` setdefaults (model.py:367-369) and `_validate_and_clamp_fields` string-type
    checks (model.py:395-411) for these keys are removed.
- **`capacity_converged`** — derived from `capacity_estimates` CoV via `get_convergence_status()`.
  `discharge_handler.py:468` writes `state["capacity_converged"] = True`; the health snapshot reads
  the LIVE `convergence_status.converged` (virtual_ups_exporter.py:52), so the persisted field is
  already redundant for the health path. Remove the write (discharge_handler.py:468) and the schema
  key. (Note `test_health_endpoint_v16.py` references `capacity_converged` — reconcile: the field is
  computed live in `get_convergence_status`/`HealthSnapshot`, not persisted.)
- **`replacement_due`** — derived from `soh_history` linear regression. THE TRICKIEST ② field:
  it is currently written ONLY on discharge completion (discharge_handler `_predict_replacement`,
  lines 228-232 via `set_replacement_due`), and read every poll by `motd_status.py:72` and
  `virtual_ups_exporter.py:77` (→ NUT `battery.replacement.due`). To stop persisting it, the
  regression must be recomputed at READ time (a method like `compute_replacement_due()` that runs
  `replacement_predictor.linear_regression_soh` on the live `soh_history` when converged) and the
  consumers must call that instead of reading persisted `replacement_due`. Remove `set_replacement_due`
  persistence; `get_replacement_due` becomes a live compute (or is replaced by the new method).
  Verify the recomputed value matches the previously-persisted value for the same inputs.

### Category ③ — Learned state that STAYS persisted (do NOT touch)
- `capacity_ah_measured` (learned SoH baseline — distinct from `full_capacity_ah_ref` config),
  `soh`, `soh_history`, `capacity_estimates`, `lut`, `r_internal_history`,
  `physics.peukert_exponent`, `physics.ir_compensation.k_volts_per_percent`, `physics.rls_state`,
  `battery_install_date`, `cycle_count`, `cumulative_on_battery_sec`, `discharge_events`,
  `last_upscmd_timestamp`, `last_upscmd_type`, `last_upscmd_status`, `new_battery_detected`,
  `new_battery_detected_timestamp`.

### Deploy mechanic (locked)
Schema shrink → strict loader rejects existing on-disk `model.json` (still has ①/② keys). Per the
Phase 25 precedent and no-backward-compat policy: deploy = stop daemon → strip the removed keys from
`~/.config/ups-battery-monitor/model.json` (or delete to regenerate) → start. A backup exists at
`~/.config/ups-battery-monitor/model.json.pre-v3.2-cleanup.bak`. Document the exact strip command in
the SUMMARY's deployment note. No migration code is written.

### Test impact (locked)
- `tests/test_model.py` has many tests asserting persistence/round-trip of the removed keys
  (scheduled_test_*, replacement_due round-trip, full_capacity_ah_ref validation, bool_keys
  including capacity_converged, string-field validation for scheduled_test_reason). Update or remove
  those that exercise now-removed persistence; keep learned-state coverage. Per project policy,
  remove tests for dead code alongside the code.
- `tests/test_health_endpoint_v16.py` references `capacity_converged` — keep the health-output
  assertion (value still surfaces via live HealthSnapshot) but ensure it no longer depends on a
  persisted model.json key.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Schema + persistence (primary)
- `src/model.py` — `ModelState` TypedDict (22-59), `KNOWN_STATE_KEYS` (64), `_reject_unknown_state_keys`
  (334-350), `_apply_defaults` (352-369), `_validate_and_clamp_fields` (371-422), `_sync_physics_from_state`
  (289-317), `_sync_physics_to_state` (319-332), `_default_vrla_lut` (441-495), `PhysicsParams` (105-118),
  `get_capacity_ah`/`get_nominal_voltage`/`get_nominal_power_watts` (500-504, 653-656),
  `update_scheduling_state` (865-878), `get/set_replacement_due` (530-536), `get_convergence_status` (762-811).
- `src/battery_math/constants.py` — `RATED_CAPACITY_AH`, `NOMINAL_POWER_WATTS` (add `NOMINAL_VOLTAGE`).

### Runtime config injection
- `src/monitor.py:127-145` — `_init_battery_model_and_estimators`: `state["full_capacity_ah_ref"] = config.capacity_ah`
  (line 129), estimator wiring from getters (140-141).
- `src/monitor_config.py` — `Config` dataclass (`capacity_ah` field), `load_config` (118-187),
  `HealthSnapshot` (286-309), `write_health_endpoint` (317-365).

### Category ② consumers (must move to live compute)
- `src/virtual_ups_exporter.py` — `write_health_snapshot` (40-62, live ② sources), `_build_virtual_metrics`
  `replacement_due` read (77, 92).
- `src/discharge_handler.py` — `_predict_replacement` (212-234, replacement_due write),
  `_handle_capacity_convergence` (459-477, capacity_converged write).
- `src/scheduler_manager.py:398-411` — `update_scheduling_state` call + `last_*` properties.
- `src/motd_status.py:72,82` — replacement_due / next_test_timestamp reads.
- `scripts/battery-health.py:37,92,157` — scheduled_test_timestamp fallback + full_capacity_ah_ref reads.

### Getter consumers of nominal_voltage/nominal_power_watts (must keep working)
- `src/sag_tracker.py`, `src/capacity_estimator.py`, `src/discharge_handler.py`, `src/monitor.py`
  (call `get_nominal_voltage()` / `get_nominal_power_watts()`).

### Tests
- `tests/test_model.py` (persistence/round-trip assertions for removed keys),
  `tests/test_health_endpoint_v16.py` (capacity_converged), `tests/test_motd_status.py`,
  `tests/test_monitor.py`, `tests/test_discharge_handler.py`.

### Policy / format
- `.planning/REQUIREMENTS.md` (v3.2 milestone, no-backward-compat / state-regenerates policy).
- `.planning/phases/25-desulfation-retraction-diagnostic-only-capacity-verification/25-02-PLAN.md`
  (plan format, threat-model conventions, stale-model.json deploy-note precedent).
- `CLAUDE.md` / `AGENTS.md` (single-host, fail-fast, typed config; `uv run pytest`; ruff + vulture;
  restart with `sudo systemctl restart ups-battery-monitor`, schema changes need stop→strip→start).
</canonical_refs>

<specifics>
## Specific Ideas

- Add `NOMINAL_VOLTAGE = 12.0` to `src/battery_math/constants.py` so nominal_voltage stops being a
  magic 12.0 scattered across model.py / runtime_calculator / peukert / calibration.
- `get_capacity_ah()` should return a runtime config value. Cleanest path: store `capacity_ah` on the
  `BatteryModel` instance (injected by monitor at init, replacing the `state[...] = config.capacity_ah`
  line) so `get_capacity_ah()` reads `self.capacity_ah` not `self.state["full_capacity_ah_ref"]`.
  Planner picks the exact injection mechanism but it MUST NOT round-trip through `self.state`.
- For `replacement_due`: introduce `compute_replacement_due()` on `BatteryModel` (or have the exporter
  call `replacement_predictor.linear_regression_soh` directly) so the value is derived from live
  `soh_history` + `soh_threshold` at read time. Note the regression needs `threshold_soh` and
  `capacity_ah_ref` — wire those from config/live state, not persisted `replacement_due`.
- Final grep gate: after the change, `rg "full_capacity_ah_ref|scheduled_test_timestamp|scheduled_test_reason|test_block_reason|capacity_converged"` should be clean across src (replacement_due
  may survive as the compute method name — scope the grep to the persisted-key string usage).
- Provide a one-time strip command in the SUMMARY using `python3 -c` or `jq` to remove the six top-level
  keys + two physics keys from the deployed model.json.
</specifics>

<deferred>
## Deferred Ideas

- Adaptive cooldown derivation from discharge_events statistics (separate future idea — not this phase).
- Any migration tooling for old model.json — out of scope (state regenerates, no compat shims).
- Re-deriving the physics constants — fixed spec, no change.
</deferred>

---

*Phase: 26-model-json-learned-state-hygiene-move-config-spec-and-derive*
*Context gathered: 2026-06-04 via ROADMAP-PRD express path + code investigation*
