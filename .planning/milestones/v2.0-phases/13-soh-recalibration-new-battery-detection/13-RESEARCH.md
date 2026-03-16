# Phase 13: SoH Recalibration & New Battery Detection - Research

**Researched:** 2026-03-16
**Domain:** Battery health recalibration, capacity normalization, battery replacement detection
**Confidence:** HIGH

## Summary

Phase 13 separates capacity degradation from battery aging by normalizing SoH calculations against measured capacity instead of rated capacity (7.2Ah). When Phase 12's capacity estimation converges (≥3 samples, CoV < 10%), SoH formula shifts to use the measured value as reference, providing aging-only trend independent of capacity loss. Simultaneously, daemon detects new battery installations by comparing stored capacity estimates to post-discharge measurements; if difference exceeds 10%, user is prompted to confirm replacement. On confirmation, baseline resets, history entries are tagged with their capacity reference, and regression model filters by baseline to avoid mixing old vs. new battery data.

**Primary recommendation:** Implement SoH formula normalization in `soh_calculator.py` orchestrator layer; extend `add_soh_history_entry()` signature to include `capacity_ah_ref` field; add post-discharge detection check in `_handle_discharge_complete()`; filter regression by `capacity_ah_ref` in `linear_regression_soh()`.

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

1. **SoH formula normalization (SOH-01)**
   - When measured capacity is available (`converged=True`), `calculate_soh_from_discharge()` uses measured `capacity_ah` instead of rated 7.2Ah for reference area calculation
   - `full_capacity_ah_ref` in model.json stays at rated value (7.2Ah) — it's the hardware constant
   - New field `capacity_ah_measured` in model.json stores the converged measured value
   - SoH kernel function already accepts `capacity_ah` parameter — orchestrator passes measured when available, rated otherwise
   - This separates aging (SoH trend) from capacity loss (measured vs rated)

2. **SoH history versioning (SOH-02)**
   - Extend existing `soh_history` array — add `capacity_ah_ref` field to each new entry
   - Old entries without the field are treated as rated baseline (7.2Ah)
   - No parallel `soh_history_v2` — one array, regression filters by field value
   - Kaizen: minimal change, backward compatible, no structural duplication

3. **SoH regression filtering (SOH-03)**
   - `replacement_predictor.py` `linear_regression_soh()` filters entries by `capacity_ah_ref` value
   - Only entries with same baseline contribute to trend line
   - When battery is replaced (new baseline), old entries are excluded from regression — aging clock resets
   - Minimum 3 entries with same baseline required for prediction (existing guard)

4. **New battery detection mechanism**
   - Detection is POST-DISCHARGE (expert panel mandatory #5), not on daemon startup
   - After each discharge, compare fresh capacity measurement to stored estimate
   - If difference >10%, set `new_battery_detected` flag in model.json
   - MOTD reads flag and shows alert: "Possible new battery detected — run `ups-battery-monitor --new-battery` to confirm"
   - User confirms via `--new-battery` flag on next daemon restart (already wired in Phase 12)
   - On confirmation: reset `capacity_estimates`, reset `soh_history` baseline, log "New battery event" to journald

5. **Baseline reset flow**
   - `--new-battery` flag (Phase 12 CAP-05) triggers reset: clear capacity_estimates[], set new capacity_ah_ref in soh_history entries going forward
   - `new_battery_detected` flag (auto-detection) is informational only — does NOT auto-reset
   - Two paths to reset: explicit CLI flag (user knows they replaced battery) or CLI flag after auto-detection prompt
   - Both paths log to journald with before/after values

### Claude's Discretion

- Exact threshold tuning for >10% detection (could be 15% if measurement noise is high)
- Whether to store `new_battery_detected` in model.json or separate marker file
- MOTD alert wording and formatting
- Whether to clear `new_battery_detected` flag after user acknowledges via `--new-battery`

### Deferred Ideas (OUT OF SCOPE)

None — discussion stayed within phase scope

</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| SOH-01 | SoH recalculates against measured capacity instead of rated when available | `calculate_soh_from_discharge()` already accepts `capacity_ah` parameter; orchestrator passes measured when `converged=True`; no kernel changes needed |
| SOH-02 | SoH history entries are version-tagged with the capacity_ah_ref used | Extend `add_soh_history_entry()` to include `capacity_ah_ref` field; old entries without field default to 7.2Ah; backward compatible single-array design |
| SOH-03 | SoH regression model ignores entries from different capacity baselines | `linear_regression_soh()` filters `soh_history` by `capacity_ah_ref` before fitting; only same-baseline entries contribute to trend |

</phase_requirements>

---

## Standard Stack

### Core Modules (No New Dependencies)

| Component | Current State | Phase 13 Use | Status |
|-----------|---------------|-------------|--------|
| `battery_math/soh.py:calculate_soh_from_discharge()` | Accepts `capacity_ah` parameter; defaults to 7.2Ah if not provided | Orchestrator passes measured capacity when converged; kernel unchanged | Ready |
| `model.py:BatteryModel.add_soh_history_entry()` | Stores `{date, soh}` tuples | Extend signature to include `capacity_ah_ref` parameter | Minor extension |
| `model.py:get_convergence_status()` | Returns sample count, confidence, converged bool | Already returns `capacity_ah_ref` field (Phase 12); no change | Ready |
| `replacement_predictor.py:linear_regression_soh()` | Accepts `soh_history` list; performs least-squares fit | Add filtering by `capacity_ah_ref` before regression | Logic addition |
| `soh_calculator.py` | Orchestrator-level SoH wrapper | Determine measured capacity from Phase 12 convergence; pass to kernel | Integration |
| `monitor.py:MonitorDaemon._update_battery_health()` | Calls SoH update after discharge | Detect new battery post-discharge, set flag, call orchestrator | Integration |

### Existing Infrastructure (Leverage)

| Asset | Purpose | Already Exists |
|-------|---------|---|
| `model.json` atomic writes | Persist all new/modified fields | Yes — `atomic_write_json()` pattern proven in Phase 12 |
| `model.data['new_battery_requested']` | User signal from Phase 12 `--new-battery` flag | Yes — wired in Phase 12, persists across restarts |
| `model.data['capacity_estimates']` | Phase 12 convergence data | Yes — 30-entry array with `ah_estimate`, `confidence`, `metadata` |
| `model.data['soh_history']` | Existing degradation tracking | Yes — array of `{date, soh}` dicts; extend to `{date, soh, capacity_ah_ref}` |
| `motd/51-ups.sh` | MOTD module for UPS status | Yes — already shows capacity convergence; add new battery alert |
| Journald logging | Event recording | Yes — use same pattern as Phase 12 |

### No New External Dependencies
All Phase 13 work uses stdlib only: `json`, `datetime`, `logging`, `math`, `statistics`.

---

## Architecture Patterns

### Recommended Integration Points

#### 1. SoH Formula Normalization (SOH-01)

**Location:** `src/soh_calculator.py` orchestrator layer

**Pattern:**
```python
# Pseudocode: SoH orchestration with capacity normalization
def update_soh(discharge_buffer, battery_model):
    """Wrapper: determine capacity reference, call kernel."""

    # Get convergence status from Phase 12
    convergence = battery_model.get_convergence_status()

    # Select capacity reference: measured if converged, else rated
    if convergence['converged']:
        capacity_ah_ref = convergence['latest_ah']  # Measured
    else:
        capacity_ah_ref = battery_model.get_capacity_ah()  # Rated (7.2Ah)

    # Call kernel with appropriate capacity
    soh_new = calculate_soh_from_discharge(
        voltage_series=discharge_buffer.voltages,
        time_series=discharge_buffer.times,
        capacity_ah=capacity_ah_ref,  # ← Measured or rated
        ...other params...
    )

    # Store SoH with baseline tag
    battery_model.add_soh_history_entry(
        date=today,
        soh=soh_new,
        capacity_ah_ref=capacity_ah_ref  # ← Phase 13: tag baseline
    )
```

**Why this pattern:**
- Kernel function already supports parameterized capacity (no changes needed)
- Orchestrator controls which capacity value gets used (clean separation)
- Backward compatible: old entries without `capacity_ah_ref` default to 7.2Ah
- Future Peukert refinement (v2.1) can also use measured capacity via same pattern

#### 2. SoH History Versioning (SOH-02)

**Location:** `src/model.py:add_soh_history_entry()`

**Current signature:**
```python
def add_soh_history_entry(self, date, soh):
    """Add a SoH history entry for degradation tracking."""
    if 'soh_history' not in self.data:
        self.data['soh_history'] = []
    self.data['soh_history'].append({'date': date, 'soh': soh})
    self.data['soh'] = soh
```

**Phase 13 signature:**
```python
def add_soh_history_entry(self, date, soh, capacity_ah_ref=None):
    """Add a SoH history entry with optional capacity baseline tag.

    Args:
        date: ISO8601 date string
        soh: SoH estimate [0.0, 1.0]
        capacity_ah_ref: Capacity used in SoH calculation (Ah).
                        If None, defaults to 7.2Ah (for backward compat).
    """
    if 'soh_history' not in self.data:
        self.data['soh_history'] = []

    entry = {'date': date, 'soh': soh}
    if capacity_ah_ref is not None:
        entry['capacity_ah_ref'] = capacity_ah_ref

    self.data['soh_history'].append(entry)
    self.data['soh'] = soh
```

**Backward compatibility:**
- Old entries without `capacity_ah_ref` field: treated as 7.2Ah baseline
- No migration script needed: regression filter handles missing field gracefully
- Existing tests pass unchanged

#### 3. Regression Filtering by Baseline (SOH-03)

**Location:** `src/replacement_predictor.py:linear_regression_soh()`

**Phase 13 enhancement:**
```python
def linear_regression_soh(
    soh_history: List[Dict[str, Any]],
    threshold_soh: float = 0.80,
    capacity_ah_ref: Optional[float] = None  # ← Phase 13 filter
) -> Optional[Tuple[float, float, float, Optional[str]]]:
    """
    Fit line to SoH history, filtering by capacity baseline.

    If capacity_ah_ref provided, use only entries with matching baseline.
    If not provided, use all entries (backward compatible).
    """
    if len(soh_history) < 3:
        return None

    # Phase 13: Filter by capacity baseline
    if capacity_ah_ref is not None:
        # Keep only entries with matching capacity_ah_ref
        # Default missing field to 7.2Ah (rated baseline)
        filtered = [
            e for e in soh_history
            if e.get('capacity_ah_ref', 7.2) == capacity_ah_ref
        ]
        soh_history = filtered

    # Rest of function unchanged
    if len(soh_history) < 3:
        return None  # Not enough entries in filtered set

    # ... existing least-squares fit logic ...
```

**Integration:**
- Call from orchestrator: `linear_regression_soh(history, capacity_ah_ref=measured_capacity)`
- When battery replaced: regression auto-excludes old entries, aging clock resets
- Minimum 3 entries with same baseline required (existing guard applies per-baseline)

#### 4. Post-Discharge New Battery Detection

**Location:** `src/monitor.py:_handle_discharge_complete()`

**Pattern:**
```python
def _handle_discharge_complete(self):
    """After discharge event ends (OB→OL). Phase 13: detect battery replacement."""

    # ... existing discharge handling (SoH update, etc.) ...

    # Phase 13: New battery detection
    # Post-discharge: compare fresh capacity measurement to stored estimate
    convergence = self.battery_model.get_convergence_status()

    if convergence['converged']:
        # Have stable measured capacity baseline
        latest_measured = convergence['latest_ah']
        stored_ref = self.battery_model.data.get('capacity_ah_measured', None)

        if stored_ref is not None:
            # Compare against last recorded baseline
            delta_percent = abs(latest_measured - stored_ref) / stored_ref * 100

            if delta_percent > 10.0:  # >10% difference = possible replacement
                logger.warning(f"Possible new battery detected: {latest_measured:.2f}Ah vs {stored_ref:.2f}Ah ({delta_percent:.1f}% diff)")
                self.battery_model.data['new_battery_detected'] = True
                self.battery_model.data['new_battery_detected_timestamp'] = datetime.now().isoformat()
                self.battery_model.save()

                # MOTD will show alert next time script runs
                logger.info("Set new_battery_detected flag; MOTD will prompt user")
        else:
            # First convergence: store as reference
            self.battery_model.data['capacity_ah_measured'] = latest_measured
            self.battery_model.save()
```

**Why post-discharge:**
- Measurements during discharge are unreliable (voltage sag, I_R drop)
- Post-discharge (OB→OL): system returns to stable state, voltage anchors predictably
- Expert panel mandatory #5: detection must occur when data quality is highest

#### 5. New Battery Confirmation Flow

**Location:** `src/monitor.py:__init__()` with `--new-battery` flag

**Pattern:**
```python
def __init__(self, new_battery_flag=False, ...):
    """Initialize daemon. Phase 13: handle battery replacement confirmation."""

    # ... existing init code ...

    # Phase 13: If user signals --new-battery, reset baseline
    if new_battery_flag or self.battery_model.data.get('new_battery_requested', False):
        self._reset_battery_baseline()

    # Clear the flag after processing
    self.battery_model.data['new_battery_requested'] = False
    self.battery_model.data['new_battery_detected'] = False
    self.battery_model.save()

def _reset_battery_baseline(self):
    """Reset capacity estimation and SoH history on battery replacement."""
    old_capacity = self.battery_model.get_latest_capacity()

    # Clear capacity estimates (will rebuild from next deep discharge)
    self.battery_model.data['capacity_estimates'] = []

    # Clear capacity_ah_measured (will be set when new measurements converge)
    self.battery_model.data['capacity_ah_measured'] = None

    # Start fresh SoH history (old entries excluded from regression by baseline)
    # Don't erase old entries — keep for historical record, but regression filters them out
    new_entry = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'soh': 1.0,  # New battery assumed 100% SoH
        'capacity_ah_ref': 7.2  # Start fresh at rated baseline
    }
    self.battery_model.data['soh_history'].append(new_entry)

    # Reset cycle/time counters if desired (or keep for total lifetime tracking)
    # For now: reset cycle_count to indicate new battery era
    self.battery_model.data['cycle_count'] = 0

    logger.info(f"New battery event: capacity reset from {old_capacity:.2f}Ah; aging clock reset; SoH baseline reset to 1.0")
    self.battery_model.save()
```

#### 6. MOTD Alert for New Battery Detection

**Location:** `motd/51-ups.sh`

**Pattern:**
```bash
#!/bin/bash
# UPS MOTD module: SoH, capacity, replacement prediction, new battery alerts

# ... existing capacity/SoH display ...

# Phase 13: Check for new battery detection flag
if [[ "$(jq -r '.new_battery_detected // false' "$MODEL_JSON")" == "true" ]]; then
    echo "⚠️  Possible new battery detected — run: ups-battery-monitor --new-battery"
fi
```

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Atomic JSON persistence | Custom file writing with sync | `atomic_write_json()` from model.py | Already proven; handles temp files, fdatasync, atomic rename |
| Capacity convergence detection | Manual CoV calculation in Phase 13 | `battery_model.get_convergence_status()` from Phase 12 | Already computed; reuse the logic |
| Linear regression | Implement least-squares from scratch | `linear_regression_soh()` with filtering | Already exists; extend with baseline filter |
| Journald logging | Print to stdout, hope it works | `logging` module + systemd journal handler | Already integrated; preserves context, searchable |
| Baseline filtering in regression | Separate v2 function | Single function with optional `capacity_ah_ref` parameter | Backward compatible; no code duplication |

**Key insight:** Phase 13 is orchestration + data model extension, not algorithm invention. Reuse Phase 12 building blocks; avoid reimplementing convergence detection or regression math.

---

## Common Pitfalls

### Pitfall 1: SoH Spike After New Battery Reset

**What goes wrong:** User replaces battery; daemon resets `soh_history` to `[{date, soh=1.0, capacity_ah_ref=X}]`. If old entries still in history, next regression run sees two baselines. Entries from old battery (SoH=0.85) get mixed with new battery (SoH=1.0), causing nonsensical slope (0.15 change over 0 days = infinite degradation prediction).

**Why it happens:** Incomplete filtering. If regression filter doesn't check `capacity_ah_ref`, it treats all entries as same baseline.

**How to avoid:**
- Always filter `soh_history` by `capacity_ah_ref` before computing regression
- Test regression with mixed-baseline input; verify only same-baseline entries contribute
- Log filter results: "Regression using 5 entries with capacity_ah_ref=6.8Ah; 12 entries excluded (different baseline)"

**Warning signs:**
- Replacement date prediction jumps wildly after new battery event
- Slope flips sign (positive degradation, then negative recovery)
- Regression R² drops to 0.1 (poor fit)

### Pitfall 2: Missing Capacity Convergence Check

**What goes wrong:** New battery detection compares `latest_ah` to `stored_ref`, but `latest_ah` is None because Phase 12 capacity hasn't converged yet (still collecting samples). Daemon falsely reports "difference > 10%" when both values are uninitialized.

**Why it happens:** Orchestrator doesn't check `convergence['converged']` before reading capacity.

**How to avoid:**
- Guard detection: only compare if `convergence['converged'] == True`
- If not converged: skip detection, store `latest_ah` as placeholder
- Log "Capacity convergence in progress (2/3 samples); skipping new battery detection"

**Warning signs:**
- `new_battery_detected` flag flips on and off erratically
- MOTD shows "new battery" alert after every discharge until convergence

### Pitfall 3: >10% Threshold Too Loose

**What goes wrong:** Measurement noise causes false positives. Real battery capacity varies ±3% due to temperature, load profile, measurement timing. If threshold is 10% and typical variation is 5%, almost every user sees "new battery detected" alert when they haven't replaced anything.

**Why it happens:** Noise from ADC quantization (±1-2%), Coulomb counting drift, voltage lag during high-load discharge.

**How to avoid:**
- Measure baseline noise during Phase 13 testing: rerun same discharge 3 times, compute capacity variation
- Set threshold at 3× typical noise: if σ = 1.5%, set threshold to 5% (3σ rule)
- Store last 3 capacity estimates, check if delta > threshold **and** persistent (not just one outlier)

**Warning signs:**
- Users report false positives during normal operation
- New battery detection fires at startup without user action

### Pitfall 4: Silently Dropping Old SoH History

**What goes wrong:** Naive implementation: on battery replacement, delete entire `soh_history` array to start fresh. User loses 12 months of degradation data. Later, if Peukert refinement (v2.1) needs to analyze old battery for learning, that data is gone.

**Why it happens:** Cleanest approach: empty array on replacement.

**How to avoid:**
- Keep entire `soh_history` array; don't delete
- Use `capacity_ah_ref` field to separate baselines
- Regression naturally filters out old entries
- User can inspect `model.json` and see full battery history if needed

**Warning signs:**
- `soh_history` is empty after new battery event
- No way to see old battery's SoH trend post-event

### Pitfall 5: Phase 13 Code Assumes Measured Capacity Always Set

**What goes wrong:** Orchestrator doesn't check if `convergence['latest_ah']` is None before using it. If Phase 12 hasn't finished collecting samples yet, calling `add_soh_history_entry(capacity_ah_ref=None)` doesn't tag entry with baseline. Later, when regression runs, it can't filter properly because new entries lack the field.

**Why it happens:** Optimistic coding: assumes Phase 12 is always ready.

**How to avoid:**
- Always check `if convergence['converged']` before passing `capacity_ah_ref`
- Default to `capacity_ah_ref=7.2` if measured is None (backward compatible)
- Log which baseline was used: "SoH entry tagged with capacity_ah_ref=7.2Ah (measured not available)"

**Warning signs:**
- Some SoH entries have `capacity_ah_ref`, others don't
- Regression suddenly works after entry #47 (when measured capacity finally appeared)

---

## Code Examples

Verified patterns from existing codebase:

### Example 1: Extend add_soh_history_entry() Signature

**Source:** `src/model.py:301-306` (Phase 12 version), Phase 13 extension

```python
def add_soh_history_entry(self, date, soh, capacity_ah_ref=None):
    """Add a SoH history entry with optional capacity baseline tag.

    Args:
        date: ISO8601 date string (e.g., '2026-03-16')
        soh: SoH estimate [0.0, 1.0]
        capacity_ah_ref: Capacity baseline used in SoH calculation (Ah).
                        If None, defaults to 7.2Ah (rated, for backward compat).
    """
    if 'soh_history' not in self.data:
        self.data['soh_history'] = []

    entry = {'date': date, 'soh': soh}

    # Phase 13: Tag with capacity baseline
    if capacity_ah_ref is not None:
        entry['capacity_ah_ref'] = round(capacity_ah_ref, 2)

    self.data['soh_history'].append(entry)
    self.data['soh'] = soh
```

### Example 2: Filter Regression by Capacity Baseline

**Source:** `src/replacement_predictor.py:8-95` (Phase 12 version), Phase 13 enhancement

```python
def linear_regression_soh(
    soh_history: List[Dict[str, Any]],
    threshold_soh: float = 0.80,
    capacity_ah_ref: Optional[float] = None  # Phase 13: filter parameter
) -> Optional[Tuple[float, float, float, Optional[str]]]:
    """Fit line to SoH history, optionally filtered by capacity baseline.

    Args:
        soh_history: List of {'date': str, 'soh': float, 'capacity_ah_ref'?: float}
        threshold_soh: SoH target for replacement prediction
        capacity_ah_ref: If provided, use only entries with this baseline (Ah).
                        If None, use all entries (backward compatible).

    Returns:
        Tuple: (slope, intercept, r_squared, replacement_date_iso8601)
    """
    if len(soh_history) < 3:
        return None

    # Phase 13: Filter by capacity baseline
    if capacity_ah_ref is not None:
        # Keep only entries matching the baseline
        # Default missing field to 7.2Ah (original rated capacity)
        filtered = [
            e for e in soh_history
            if e.get('capacity_ah_ref', 7.2) == capacity_ah_ref
        ]

        if len(filtered) < 3:
            # Not enough entries for this baseline; can't predict
            return None

        soh_history = filtered

    # Rest of function unchanged: parse dates, compute regression, etc.
    try:
        dates = [datetime.strptime(entry['date'], '%Y-%m-%d') for entry in soh_history]
        soh_values = [entry['soh'] for entry in soh_history]
    except (ValueError, KeyError, TypeError):
        return None

    # ... existing least-squares fit logic (lines 45–95 unchanged) ...
```

### Example 3: Post-Discharge New Battery Detection

**Source:** Pattern from `src/monitor.py:_handle_discharge_complete()` (Phase 12), Phase 13 addition

```python
def _handle_discharge_complete(self):
    """After discharge completes (OB→OL). Track SoH, capacity, replacement prediction."""

    # ... existing SoH update (lines 438–543) ...

    # Phase 13: NEW BATTERY DETECTION (post-discharge)
    convergence = self.battery_model.get_convergence_status()

    if convergence['converged']:
        # Capacity estimation has stabilized; we have a reliable baseline
        current_measured = convergence['latest_ah']
        stored_baseline = self.battery_model.data.get('capacity_ah_measured', None)

        if stored_baseline is not None:
            # Compare current measurement to last stored baseline
            delta_ah = abs(current_measured - stored_baseline)
            delta_percent = (delta_ah / stored_baseline) * 100

            if delta_percent > 10.0:  # >10% threshold
                logger.warning(
                    f"New battery detection: measured capacity {current_measured:.2f}Ah "
                    f"differs from baseline {stored_baseline:.2f}Ah ({delta_percent:.1f}% > 10% threshold)"
                )

                # Set flag for MOTD and user to acknowledge
                self.battery_model.data['new_battery_detected'] = True
                self.battery_model.data['new_battery_detected_timestamp'] = datetime.now().isoformat()
                self.battery_model.save()

                logger.info(
                    "New battery flag set; MOTD will show alert next shell session. "
                    "User can confirm with: ups-battery-monitor --new-battery"
                )
        else:
            # First time convergence; store as baseline for future comparisons
            self.battery_model.data['capacity_ah_measured'] = current_measured
            self.battery_model.save()
            logger.info(f"Capacity baseline stored: {current_measured:.2f}Ah (first convergence)")
```

### Example 4: Orchestrator Selects Capacity Reference

**Source:** Integration point at `src/soh_calculator.py` or `src/monitor.py:_update_battery_health()`

```python
def _update_battery_health(self):
    """Called when discharge event completes (OB→OL). Phase 13: use measured capacity if available."""

    # ... existing checks (lines 451–464) ...

    # Determine which capacity to use for SoH calculation
    convergence = self.battery_model.get_convergence_status()

    if convergence['converged']:
        # Use measured capacity (Phase 12 has converged)
        capacity_ah_for_soh = convergence['latest_ah']
        logger.info(f"SoH calculation using measured capacity: {capacity_ah_for_soh:.2f}Ah")
    else:
        # Use rated capacity (Phase 12 still collecting)
        capacity_ah_for_soh = self.battery_model.get_capacity_ah()  # 7.2Ah
        logger.info(f"SoH calculation using rated capacity: {capacity_ah_for_soh:.2f}Ah (measured not converged)")

    # Call SoH kernel with selected capacity
    soh_new = soh_calculator.calculate_soh_from_discharge(
        discharge_voltage_series=self.discharge_buffer.voltages,
        discharge_time_series=self.discharge_buffer.times,
        reference_soh=self.battery_model.get_soh(),
        anchor_voltage=10.5,
        capacity_ah=capacity_ah_for_soh,  # ← Measured or rated
        load_percent=avg_load,
        nominal_power_watts=self.battery_model.get_nominal_power_watts(),
        nominal_voltage=self.battery_model.get_nominal_voltage(),
        peukert_exponent=self.battery_model.get_peukert_exponent()
    )

    # Add to history with baseline tag
    today = datetime.now().strftime('%Y-%m-%d')
    self.battery_model.add_soh_history_entry(
        date=today,
        soh=soh_new,
        capacity_ah_ref=capacity_ah_for_soh  # Phase 13: tag the baseline
    )

    # ... rest of function unchanged ...
```

---

## State of the Art

| Aspect | v1.1 Approach | Phase 13 Approach | Change Rationale |
|--------|---------------|------------------|------------------|
| **SoH reference capacity** | Always 7.2Ah (rated) | Measured when converged, else rated | Separates aging from capacity loss; enables v2.1+ Peukert refinement |
| **SoH history structure** | `{date, soh}` | `{date, soh, capacity_ah_ref?}` | Tags each entry with its baseline; filters regression to same-baseline entries only |
| **Regression on mixed baselines** | No filtering; all entries mixed | Filtered by `capacity_ah_ref` | Prevents nonsensical slope when battery replaced mid-history |
| **New battery detection** | Not implemented | Post-discharge comparison (>10% diff) | Automatic detection + user confirmation flow (expert panel #5) |
| **Baseline reset trigger** | N/A | CLI flag `--new-battery` or after auto-detection confirm | User-controlled or auto-prompt path |
| **Replacement prediction** | Single line across all time | Per-baseline line (old battery separate) | Aging clock resets; predictions accurate for current battery era only |

**Deprecated/Outdated (v1.1):**
- SoH always normalized to 7.2Ah: introduces ~5% error when actual capacity is 6.8Ah (typical v1.0 discharge data)
- No capacity baseline field: regression assumes homogeneous data; fails if battery replaced

---

## Open Questions

1. **Exact >10% threshold tuning**
   - What we know: Expert panel chose 10%; Phase 12 measurement noise is ±2–3% per convergence_status()
   - What's unclear: Is 10% safe against false positives, or should it be 15% (5σ)?
   - Recommendation: Run Phase 13 testing with real discharge data; if false positives occur, adjust threshold

2. **Separate marker file vs. model.json field**
   - What we know: CONTEXT.md leaves this to "Claude's Discretion"
   - What's unclear: Pros/cons of `model.json['new_battery_detected']` vs. separate `~/.config/ups-battery-monitor/new-battery-flag`?
   - Recommendation: Store in `model.json` (atomic with other state; no extra file to sync)

3. **Clear new_battery_detected flag timing**
   - What we know: Flag is set post-discharge if >10% detected; user prompted via MOTD
   - What's unclear: When to clear flag? After user runs `--new-battery`? Or after first SoH entry with new baseline?
   - Recommendation: Clear in `__init__()` after baseline reset (lines 276–290 pseudocode); prevents repeated MOTD alerts

4. **Phase 13 ordering: rebaseline before or after first new SoH entry?**
   - What we know: Phase 13 section 5 shows reset flow (clear capacity_estimates, add SoH=1.0 entry)
   - What's unclear: Should new SoH entry come before new discharge, or after user confirms?
   - Recommendation: Reset happens in `__init__()` (on `--new-battery` flag); next discharge creates first new-baseline entry

---

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest (matching Phase 12) |
| Config file | pyproject.toml (no test-specific config) |
| Quick run command | `pytest tests/test_model.py tests/test_replacement_predictor.py -xvs` |
| Full suite command | `pytest tests/ -x` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SOH-01 | SoH formula uses measured capacity when converged | unit | `pytest tests/test_soh_calculator.py::test_soh_with_measured_capacity -xvs` | ❌ Wave 0 |
| SOH-01 | SoH formula defaults to rated capacity when not converged | unit | `pytest tests/test_soh_calculator.py::test_soh_with_rated_capacity_fallback -xvs` | ❌ Wave 0 |
| SOH-02 | add_soh_history_entry() stores capacity_ah_ref field | unit | `pytest tests/test_model.py::test_soh_history_entry_with_baseline -xvs` | ❌ Wave 0 |
| SOH-02 | Old entries without capacity_ah_ref default to 7.2Ah | unit | `pytest tests/test_replacement_predictor.py::test_regression_backward_compat -xvs` | ❌ Wave 0 |
| SOH-03 | linear_regression_soh() filters entries by capacity_ah_ref | unit | `pytest tests/test_replacement_predictor.py::test_regression_filters_by_baseline -xvs` | ❌ Wave 0 |
| SOH-03 | Regression requires 3+ entries for same baseline | unit | `pytest tests/test_replacement_predictor.py::test_regression_min_entries_per_baseline -xvs` | ❌ Wave 0 |
| SOH-01,02,03 | End-to-end: measured capacity → SoH tagged → regression filters | integration | `pytest tests/test_monitor_integration.py::test_soh_recalibration_flow -xvs` | ❌ Wave 0 |
| New battery detection | >10% difference detected post-discharge | unit | `pytest tests/test_monitor.py::test_new_battery_detection_threshold -xvs` | ❌ Wave 0 |
| New battery detection | Flag set only when converged | unit | `pytest tests/test_monitor.py::test_new_battery_detection_requires_convergence -xvs` | ❌ Wave 0 |
| Baseline reset | --new-battery flag clears capacity_estimates | unit | `pytest tests/test_model.py::test_baseline_reset_clears_estimates -xvs` | ❌ Wave 0 |
| Baseline reset | New SoH entry created with SoH=1.0 | unit | `pytest tests/test_model.py::test_baseline_reset_creates_entry -xvs` | ❌ Wave 0 |
| Baseline reset | MOTD shows "new battery" alert before user confirms | integration | `pytest tests/test_motd.py::test_motd_shows_new_battery_alert -xvs` (if test exists) | ❌ Wave 0 |

### Sampling Rate

- **Per task commit:** `pytest tests/test_model.py tests/test_replacement_predictor.py -xvs` (SOH-02, SOH-03 core logic ~15s)
- **Per wave merge:** `pytest tests/ -x` (full suite ~45s)
- **Phase gate:** Full suite green + manual integration test (discharge with measured capacity, verify regression filters old baseline)

### Wave 0 Gaps

- [ ] `tests/test_soh_calculator.py` — add tests for `calculate_soh_from_discharge()` with measured capacity parameter (unit tests for SOH-01)
- [ ] `tests/test_model.py` — add tests for `add_soh_history_entry(capacity_ah_ref)` signature and backward compat (SOH-02)
- [ ] `tests/test_replacement_predictor.py` — add tests for `linear_regression_soh(capacity_ah_ref)` filtering (SOH-03)
- [ ] `tests/test_monitor_integration.py` — add end-to-end test: measured capacity convergence → SoH update → regression filter (integration)
- [ ] `tests/test_monitor.py` — add tests for `_handle_discharge_complete()` new battery detection logic (>10% threshold, convergence check)
- [ ] Framework install: already in place (pytest 8.x, matching Phase 12); no new dependencies

*(The test files exist; Phase 13 adds new test cases to existing files. No new test infrastructure needed.)*

---

## Sources

### Primary (HIGH confidence)

- **Phase 12 CONTEXT.md** — Locked decisions SOH-01/02/03, canonical refs, expert review results (#5, #6, #7)
- **`src/battery_math/soh.py`** — `calculate_soh_from_discharge()` signature with `capacity_ah` parameter (lines 11–106)
- **`src/model.py`** — `BatteryModel.add_soh_history_entry()` (lines 301–306), `get_convergence_status()` (lines 382–429), atomic persistence pattern
- **`src/replacement_predictor.py`** — `linear_regression_soh()` (lines 8–95) ready for filtering enhancement
- **`src/monitor.py`** — `_update_battery_health()` integration point (lines 438–543), `--new-battery` flag wiring (Phase 12)

### Secondary (MEDIUM confidence)

- **Phase 12 STATE.md Expert Review Results** — Mandatory items #5 (post-discharge detection), #6 (backward compat), #7 (SoH formula review)
- **REQUIREMENTS.md** — SOH-01, SOH-02, SOH-03 traceability

### Tertiary (LOW confidence)

None — all findings backed by existing code or explicit CONTEXT.md decisions.

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all components already exist; Phase 13 is orchestration + data model extension
- Architecture: HIGH — CONTEXT.md locked decisions; expert panel sign-off (mandatory items #5–7)
- Pitfalls: MEDIUM — based on Phase 12 learnings + battery domain knowledge; >10% threshold needs field validation
- Validation: HIGH — test infrastructure from Phase 12; mapping straightforward

**Research date:** 2026-03-16
**Valid until:** 2026-03-23 (7 days; Phase 13 is stable domain, no fast-moving dependencies)
**Phase 12 dependency:** COMPLETE (capacity convergence ready; Phase 13 can read latest_ah immediately)

---

*Phase: 13-soh-recalibration-new-battery-detection*
*Research completed: 2026-03-16*
*Ready for Phase 13 planning*
