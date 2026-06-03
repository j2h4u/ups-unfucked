# Phase 26 Research: model.json learned-state hygiene

**Researched:** 2026-06-04
**Method:** Direct source investigation (rg + Read across src/, scripts/, tests/). Pure-refactor of
an existing codebase — no external-library research needed; the authority is the code itself.

This phase moves category ① (config/spec) and category ② (derived caches) out of the persisted
`ModelState`. The hard constraint: **computed outputs keep the same VALUES, only their source changes
from persisted→live.** Below are the concrete, file:line-grounded mechanics the planner needs.

---

## 1. HYG-01/02 — `full_capacity_ah_ref` → runtime config injection

### Current state
- `monitor.py:129`: `self.battery_model.state["full_capacity_ah_ref"] = config.capacity_ah` — the
  persisted value is overwritten from config on EVERY daemon start, so it is never authoritative.
- `model.py:656`: `get_capacity_ah()` returns `self.state.get("full_capacity_ah_ref", RATED_CAPACITY_AH)`.
- `model.py:383-393`: `_validate_and_clamp_fields` validates/repairs `full_capacity_ah_ref`.
- `model.py:456`: `_default_vrla_lut` seeds it.

### All `get_capacity_ah()` callers (must keep working)
- Production: `monitor.py:142, 217, 361, 505`; `soh_calculator.py:96`; `discharge_handler.py:260`.
- Tests: mock the method (`tests/test_monitor*.py`, `tests/test_soh_calculator.py`,
  `tests/test_model.py:109,120,136,258` assert `== 7.2`). Mocks are unaffected; the 4 `test_model.py`
  asserts must move to the new injection mechanism (see below) — set via constructor/attribute,
  default `RATED_CAPACITY_AH`.

### Recommended mechanism (cleanest, no state round-trip)
Add a `capacity_ah` attribute to `BatteryModel`:
- Constructor signature gains `capacity_ah: float = RATED_CAPACITY_AH` → `self.capacity_ah = capacity_ah`.
- `get_capacity_ah()` returns `self.capacity_ah` (NOT `self.state[...]`).
- `monitor.py:_init_battery_model_and_estimators`: replace the `state["full_capacity_ah_ref"] = ...`
  line by passing `capacity_ah=config.capacity_ah` to `BatteryModel(model_path, capacity_ah=...)`,
  OR set `self.battery_model.capacity_ah = config.capacity_ah` right after construction. Constructor
  injection is cleaner and fail-fast (no mutable post-init reach-in).
- Remove the schema key `full_capacity_ah_ref`, the validation block (383-393), and the
  `_default_vrla_lut` seed (456).
- `scripts/battery-health.py:92,157`: already prefer `model_data.get("capacity_ah")` then fall back
  to `full_capacity_ah_ref`. After removal the fallback is dead — change to read config `capacity_ah`
  from config.toml (or keep the `capacity_ah` key read; the script reads model.json, which won't have
  either key now — so it must read config.toml's `capacity_ah` or default RATED_CAPACITY_AH). Planner
  must decide: simplest is `model_data.get("capacity_ah", RATED_CAPACITY_AH)` → but model.json won't
  have it; reading config.toml in the script is the correct source. Document the chosen path.

---

## 2. HYG-01/02 — `physics.nominal_voltage` / `physics.nominal_power_watts` → constants

### Current state — MIXED blob (split, don't drop)
`physics` persists 5 things; only 2 are spec: `nominal_voltage` (12.0), `nominal_power_watts`
(`NOMINAL_POWER_WATTS`). Learned (KEEP persisted): `peukert_exponent`, `ir_compensation`, `rls_state`.

- `model.py:310-311`: `_sync_physics_from_state` reads them from state.
- `model.py:323-324`: `_sync_physics_to_state` writes them to state.
- `model.py:460-461`: `_default_vrla_lut` seeds them.
- `model.py:110-111`: `PhysicsParams` dataclass fields.
- `model.py:501,504`: getters `get_nominal_voltage()` / `get_nominal_power_watts()`.

### All getter callers (must keep working — none may break)
`get_nominal_voltage()` / `get_nominal_power_watts()` called in: `sag_tracker.py:157-158`,
`discharge_handler.py:189-190,263-264,309-310`, `monitor.py:140-141,508-509`. (capacity_estimator,
soh_calculator, peukert, runtime_calculator, integration, calibration take them as PARAMS — fed by
the getters above, so they are downstream and unaffected as long as getters return correct values.)

### Recommended mechanism
- Add `NOMINAL_VOLTAGE = 12.0` to `src/battery_math/constants.py` (single source of truth; kills the
  scattered magic 12.0 in model.py / runtime_calculator / peukert / calibration).
- `get_nominal_voltage()` returns `NOMINAL_VOLTAGE`; `get_nominal_power_watts()` returns
  `NOMINAL_POWER_WATTS` — direct constant reads, OR keep `PhysicsParams` fields defaulted FROM the
  constants but never persisted. Direct constant reads are simplest and least error-prone.
- `_sync_physics_from_state`: stop reading `nominal_voltage`/`nominal_power_watts` from state.
- `_sync_physics_to_state`: stop writing them — the persisted `physics` dict becomes
  `{peukert_exponent, ir_compensation, rls_state}` only.
- `_default_vrla_lut`: drop the two keys from the seeded `physics`.
- `PhysicsParams`: either drop the two fields (if getters read constants directly) or keep them
  defaulted from constants but exclude from to_state. Planner's call; getters MUST return the
  constant values.

---

## 3. HYG-03/04 — Derived caches (category ②)

### 3a. `scheduled_test_timestamp` / `scheduled_test_reason` / `test_block_reason`
- Written by `model.py:865-878` `update_scheduling_state`, called from `scheduler_manager.py:407`.
- The health endpoint reads the LIVE in-memory `scheduler_manager.last_scheduling_reason` /
  `last_next_test_timestamp` (`virtual_ups_exporter.py:56-57`), NOT model.json. Confirmed at
  `scheduler_manager.py:398-405` — both `last_*` and the model write come from the SAME
  `decision.*`, so dropping the persist loses nothing the health path uses.
- Gates key off `last_upscmd_timestamp` / `last_upscmd_status` (③), never these — confirmed in the
  scheduler. So they are pure scheduler OUTPUT cache.
- **Action:** Remove `update_scheduling_state`'s three `self.state[...]=` writes (keep the method as a
  no-op? No — delete the method and its caller-write, leaving only `last_*` property updates in
  scheduler_manager). Remove the three schema keys, the `_apply_defaults` setdefaults (367-369), and
  the `_validate_and_clamp_fields` string checks (395-411 for these three).
- `scripts/battery-health.py:37`: `next_ts = health.get("next_test_timestamp") or
  model.get("scheduled_test_timestamp")` — the fallback is now dead (model.json lacks the key,
  health.json always has it from the live snapshot). Drop the `or model_data.get(...)` clause.
- `tests/test_model.py:876-877,904-905,952-960,1050-1064,1113` assert persistence/round-trip/string-
  validation of these keys → delete those tests (dead-code removal per project policy).

### 3b. `capacity_converged` — ALREADY redundant
- `discharge_handler.py:468`: `self.battery_model.state["capacity_converged"] = True`.
- `write_health_endpoint` DOES emit `capacity_converged` (`monitor_config.py:338`) — but from
  `snapshot.capacity_converged`, which `virtual_ups_exporter.py:52` sets from the LIVE
  `convergence_status.converged` (`get_convergence_status()`, model.py:807). So the persisted state
  field is **never read for the health output** — it is dead write.
- **Action:** Remove the `state["capacity_converged"] = True` write (discharge_handler.py:468) and the
  schema key. The health.json `capacity_converged` key STAYS (live-sourced) — so
  `test_health_endpoint_v16.py:218-219` keeps passing unchanged. `test_model.py:1188`
  (`bool_keys = {"capacity_converged", ...}`) must drop `capacity_converged`.

### 3c. `replacement_due` — THE TRICKY ONE (write-on-discharge, read-every-poll)
- Written ONLY on discharge completion: `discharge_handler.py:230,232` via `set_replacement_due`
  inside `_predict_replacement` (212-234), which runs `linear_regression_soh` when converged.
- Read EVERY poll: `motd_status.py:72` (`model.get_replacement_due()`),
  `virtual_ups_exporter.py:77` → NUT `battery.replacement.due` (line 92).
- `linear_regression_soh(soh_history, threshold_soh=0.80, capacity_ah_ref=None)` (replacement_predictor.py:12):
  - `soh_history` — live category ③ state (`get_soh_history()`).
  - `threshold_soh` — = config `soh_alert` (the same value injected as `discharge_handler.soh_threshold`,
    discharge_handler.py:64). The live computer needs this threshold wired from config.
  - `capacity_ah_ref` — the per-entry baseline; `_predict_replacement` passes the discharge's
    `capacity_ah_ref`. For a live read-time recompute, pass `None` (use all entries) OR the current
    `get_capacity_ah()` — MUST match what `_predict_replacement` passed to keep values identical.
    Investigate: `_predict_replacement` passes `capacity_ah_ref` from `_compute_soh` (the measured
    baseline for that discharge). To reproduce the SAME value live, the recompute should pass the same
    `capacity_ah_ref` the most recent SoH entry used — i.e. read it from the latest `soh_history`
    entry's `capacity_ah_ref` field (added by `add_soh_history_entry`, model.py:672-673). This is the
    equivalence-preserving choice and the validation fixture must assert it.
- **Recommended mechanism:** Add `BatteryModel.compute_replacement_due() -> str | None` that runs
  `linear_regression_soh(self.get_soh_history(), threshold_soh=<config soh_alert>, capacity_ah_ref=<latest
  entry baseline>)` and returns the date (or "overdue"/None). Wire `threshold_soh` in — either inject
  `soh_threshold` onto BatteryModel (like capacity_ah) or have the EXPORTER call linear_regression_soh
  directly with the config threshold it already has access to. Exporter-side compute keeps BatteryModel
  free of config; model-side method keeps the call site simple. Planner picks; the value MUST equal the
  previously-persisted one for identical inputs (proven by fixture, see §6).
- Remove `set_replacement_due` (or make it raise/unused), the `state["replacement_due"]` read in
  `get_replacement_due` (becomes the live compute), the discharge_handler write (230,232), and the
  schema key. `discharge_handler._predict_replacement` still RETURNS the prediction tuple for
  `_check_alerts` (used at 247-255) — keep that local return; just stop persisting it.
- `test_model.py:1206` (`set_replacement_due("2027-01-01")`) → delete/rewrite.

---

## 4. HYG-05 — Strict loader + on-disk reconciliation

- `_reject_unknown_state_keys` (model.py:334-350) derives `KNOWN_STATE_KEYS` from `ModelState`. After
  removing the 6 keys from the TypedDict, a freshly-regenerated `model.json` has only learned keys →
  loader passes. The EXISTING deployed file still has the removed keys → loader REJECTS it on first
  start after deploy. This is the intended fail-fast; deploy must strip first (§6).
- `_default_vrla_lut` no longer seeds `full_capacity_ah_ref` or physics spec keys → a fresh regen is
  already clean. Confirm the regen path produces a loader-clean file (validation gate).

## 5. Test impact inventory (HYG-05)

| File | What breaks | Disposition |
|------|-------------|-------------|
| `tests/test_model.py` | `get_capacity_ah()==7.2` (109,120,136,258) | rewrite to new injection (constructor capacity_ah) |
| `tests/test_model.py` | scheduled_test_* round-trip/validation (876-877,904-905,952-960,1050-1064,1113) | delete (dead persistence) |
| `tests/test_model.py` | `capacity_converged` in bool_keys (1188) | drop key from set |
| `tests/test_model.py` | `set_replacement_due` (1206) | delete/rewrite to compute_replacement_due |
| `tests/test_model.py` | full_capacity_ah_ref validation tests (if any) | delete |
| `tests/test_health_endpoint_v16.py` | `capacity_converged` (43,218-219) | KEEP — live-sourced, still emitted |
| `tests/test_motd_status.py` / `tests/test_monitor*.py` | replacement_due / get_capacity_ah mocks | verify still green; adjust if they assert persistence |

## 6. Deploy strip command (HYG-04 deploy note)

One-time, stop daemon first. Remove 6 top-level + 2 physics keys, keep learned state:
```bash
sudo systemctl stop ups-battery-monitor
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home()/".config/ups-battery-monitor/model.json"
d = json.load(open(p))
for k in ("full_capacity_ah_ref","scheduled_test_timestamp","scheduled_test_reason",
          "test_block_reason","capacity_converged","replacement_due"):
    d.pop(k, None)
phys = d.get("physics", {})
for k in ("nominal_voltage","nominal_power_watts"):
    phys.pop(k, None)
json.dump(d, open(p,"w"), indent=2)
print("stripped", p)
PY
sudo systemctl start ups-battery-monitor
```
(Backup already at `~/.config/ups-battery-monitor/model.json.pre-v3.2-cleanup.bak`. Alternative:
delete model.json entirely — but that discards learned SoH/LUT/capacity baselines and re-warms from
scratch, so the surgical strip above is preferred to preserve category ③.)

---

## Validation Architecture (Nyquist)

**Risk being validated:** moving value-sourcing from persisted→live could silently change a computed
output (health.json key, NUT `battery.replacement.due`, MOTD line, battery-health.py output). The
phase contract is byte-equivalence of VALUES for identical inputs.

**Observation points (where to sample the signal):**
1. **`get_capacity_ah()`** — sample return value before/after injection refactor with the same config
   `capacity_ah`. Assert equal (and equal to config, not to RATED_CAPACITY_AH unless config defaults).
2. **`get_nominal_voltage()` / `get_nominal_power_watts()`** — assert they return `NOMINAL_VOLTAGE` /
   `NOMINAL_POWER_WATTS` and that a downstream consumer (e.g. peukert runtime calc) yields the same
   number as pre-refactor for a fixed input.
3. **`write_health_endpoint` output dict** — fixture a daemon poll with a converged `capacity_estimates`
   and assert `capacity_converged`, `next_test_timestamp`, `scheduling_reason` carry the live values
   (no read from model.json). Key set unchanged.
4. **`replacement_due` equivalence (highest-risk signal)** — the critical fixture: a `model.json` with
   a converged `soh_history` (≥3 points, R²≥0.5, negative slope) whose persisted `replacement_due` was
   written by the OLD code. After refactor, `compute_replacement_due()` (or exporter-side compute) on
   the SAME `soh_history` + same `threshold_soh` + same `capacity_ah_ref` MUST return the identical
   date string. Add an explicit regression test asserting `computed == persisted_baseline`.
5. **Strict-loader regen** — load a freshly `_default_vrla_lut`-generated state through `load()` and
   assert `_reject_unknown_state_keys` passes (no removed key leaks back into the default seed).
6. **Stripped-file load** — fixture an old-schema model.json, run the §6 strip, assert it then loads
   clean and learned keys survive.

**Sampling sufficiency:** these are deterministic pure-function outputs — a single fixture per signal
at the boundary (converged vs not-converged for replacement_due; with/without config override for
capacity_ah) is sufficient to catch divergence; no time-series sampling needed. The replacement_due
equivalence fixture is the Nyquist-critical one (the only signal whose recompute path is non-trivial).

---

## Open decisions left to the planner (bounded)
- capacity_ah injection: constructor arg vs post-init attribute set (recommend constructor).
- replacement_due recompute home: `BatteryModel.compute_replacement_due()` (needs threshold injected)
  vs exporter-side direct `linear_regression_soh` call (threshold already in scope). Either is correct;
  pick one and apply consistently to BOTH consumers (motd_status, virtual_ups_exporter).
- battery-health.py capacity source: read config.toml `capacity_ah` vs default RATED_CAPACITY_AH.

## RESEARCH COMPLETE
