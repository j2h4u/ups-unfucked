# Phase 13: SoH Recalibration & New Battery Detection - Context

**Gathered:** 2026-03-16
**Status:** Ready for planning

<domain>
## Phase Boundary

Separate capacity degradation from battery aging by normalizing SoH against measured capacity instead of rated. Tag SoH history entries with their capacity baseline. Detect new battery installation post-discharge and prompt user via MOTD + marker flag. Does NOT add new reporting channels (Phase 14) or change capacity estimation algorithm (Phase 12).

</domain>

<decisions>
## Implementation Decisions

### SoH formula normalization (SOH-01)
- When measured capacity is available (converged=True), `calculate_soh_from_discharge()` uses measured capacity_ah instead of rated 7.2Ah for reference area calculation
- `full_capacity_ah_ref` in model.json stays at rated value (7.2Ah) — it's the hardware constant
- New field `capacity_ah_measured` in model.json stores the converged measured value
- SoH kernel function (`battery_math/soh.py`) already accepts `capacity_ah` parameter — orchestrator passes measured when available, rated otherwise
- This separates aging (SoH trend) from capacity loss (measured vs rated)

### SoH history versioning (SOH-02)
- Extend existing `soh_history` array — add `capacity_ah_ref` field to each new entry
- Old entries without the field are treated as rated baseline (7.2Ah)
- No parallel `soh_history_v2` — one array, regression filters by field value
- Kaizen: minimal change, backward compatible, no structural duplication

### SoH regression filtering (SOH-03)
- `replacement_predictor.py` `linear_regression_soh()` filters entries by `capacity_ah_ref` value
- Only entries with same baseline contribute to trend line
- When battery is replaced (new baseline), old entries are excluded from regression — aging clock resets
- Minimum 3 entries with same baseline required for prediction (existing guard)

### New battery detection mechanism
- Detection is POST-DISCHARGE (expert panel mandatory #5), not on daemon startup
- After each discharge, compare fresh capacity measurement to stored estimate
- If difference >10%, set `new_battery_detected` flag in model.json
- MOTD reads flag and shows alert: "Possible new battery detected — run `ups-battery-monitor --new-battery` to confirm"
- User confirms via `--new-battery` flag on next daemon restart (already wired in Phase 12)
- On confirmation: reset `capacity_estimates`, reset `soh_history` baseline, log "New battery event" to journald

### Baseline reset flow
- `--new-battery` flag (Phase 12 CAP-05) triggers reset: clear capacity_estimates[], set new capacity_ah_ref in soh_history entries going forward
- `new_battery_detected` flag (auto-detection) is informational only — does NOT auto-reset
- Two paths to reset: explicit CLI flag (user knows they replaced battery) or CLI flag after auto-detection prompt
- Both paths log to journald with before/after values

### Claude's Discretion
- Exact threshold tuning for >10% detection (could be 15% if measurement noise is high)
- Whether to store `new_battery_detected` in model.json or separate marker file
- MOTD alert wording and formatting
- Whether to clear `new_battery_detected` flag after user acknowledges via `--new-battery`

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### SoH calculation
- `src/battery_math/soh.py` — Pure kernel function `calculate_soh_from_discharge()`, current signature with `capacity_ah` parameter
- `src/soh_calculator.py` — Orchestrator-level SoH with logging, uses same formula

### Model persistence
- `src/model.py` — `BatteryModel` class: `soh_history`, `capacity_estimates`, `full_capacity_ah_ref`, `add_soh_history_entry()`, `get_convergence_status()`

### Replacement prediction
- `src/replacement_predictor.py` — `linear_regression_soh()` function that needs capacity_ah_ref filtering

### Daemon integration
- `src/monitor.py` lines 264-324 — `MonitorDaemon.__init__()` with `new_battery_flag` parameter, `new_battery_requested` flag storage
- `src/monitor.py` `_update_battery_health()` — where SoH update happens after discharge
- `src/monitor.py` `_handle_discharge_complete()` — where capacity estimation happens

### Expert panel recommendations
- `.planning/STATE.md` §Expert Review Results — Mandatory items #5 (post-discharge detection), #6 (model.json backward compat), #7 (SoH formula review)
- `.planning/STATE.md` §Known Limitations — Bayesian SoH inertia at cliff edge (accepted for v2.0)

### MOTD
- `motd/51-ups.sh` — Current UPS MOTD module, already shows capacity convergence progress

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `battery_math/soh.py:calculate_soh_from_discharge()` — already accepts `capacity_ah` parameter, just needs orchestrator to pass measured value
- `model.py:add_soh_history_entry(date, soh)` — extend signature to include `capacity_ah_ref`
- `model.py:get_convergence_status()` — already returns `converged` bool and `capacity_ah_ref` field
- `monitor.py:new_battery_requested` flag — already persisted in model.data from Phase 12

### Established Patterns
- Atomic model.json saves via `_safe_save()` — use same pattern for new fields
- History pruning pattern (soh_history capped at 30 entries) — apply same to new fields
- Kernel/orchestrator split: pure math in `battery_math/`, I/O and logging in `monitor.py`
- Marker flags in model.json (e.g., `capacity_converged`, `new_battery_requested`)

### Integration Points
- `_update_battery_health()` in monitor.py — add capacity_ah_ref to SoH history entry
- `_handle_discharge_complete()` — add new battery detection check after capacity measurement
- `linear_regression_soh()` — add capacity_ah_ref filter parameter
- `motd/51-ups.sh` — add new battery detection alert

</code_context>

<specifics>
## Specific Ideas

- Marker-file approach for new battery detection: daemon sets flag in model.json, MOTD reads it and shows alert
- No interactive prompts (daemon is systemd service without tty) — all communication via journald + MOTD
- Extend existing soh_history (not v2) — backward compatible, old entries without field = rated baseline

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>

---

*Phase: 13-soh-recalibration-new-battery-detection*
*Context gathered: 2026-03-16*
