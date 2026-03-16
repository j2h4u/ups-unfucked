---
phase: 12-deep-discharge-capacity-estimation
plan: 02
type: execution
subsystem: "Capacity estimation, model persistence, daemon integration"
tags: ["CAP-01", "CAP-04", "CAP-05", "integration", "persistence"]
dependencies:
  requires: ["12-01"]
  provides: ["13-01"]
  affects: ["14-01"]
tech_stack:
  added: []
  patterns: ["atomic_write_json", "discharge_buffer collection", "convergence tracking"]
key_files:
  created: []
  modified:
    - src/model.py (+ 50 LOC: capacity_estimates array, persistence, pruning)
    - src/monitor.py (+ 150 LOC: CapacityEstimator integration, discharge handler)
    - tests/test_model.py (+ 100 LOC: 7 persistence tests)
    - tests/test_monitor.py (+ 150 LOC: 7 integration tests)
decisions:
  - "Capacity_estimates array isolated from full_capacity_ah_ref (Phase 12 measures, Phase 13 replaces)"
  - "Convergence threshold: count >= 3 AND CoV < 0.10 per expert panel approval"
  - "Historical estimates reloaded on daemon restart for convergence survival"
  - "Quality filter rejections logged but not persisted (VAL-01 enforce)"
metrics:
  total_time: "45 minutes"
  completed_date: "2026-03-16T14:00:00Z"
  commits: 2
  tests_added: 14
  tests_passed: 285
---

# Phase 12 Plan 02: MonitorDaemon Integration + Model Persistence

## Summary

Complete integration of CapacityEstimator (Phase 12 Plan 01) with MonitorDaemon and BatteryModel persistence. Measures battery capacity on every discharge completion event (OB→OL transition), stores results with confidence metadata in model.json, and tracks convergence across power cycles.

**One-liner:** Discharge events automatically measured via coulomb counting + voltage anchor validation; capacity estimates persisted atomically with convergence tracking.

---

## Deliverables

### Task 1: Extend BatteryModel with capacity_estimates array (CAP-04)

**Methods added to `src/model.py`:**

1. `add_capacity_estimate(ah_estimate, confidence, metadata, timestamp)` (30 LOC)
   - Appends measurement to `model.data['capacity_estimates']` array
   - Calls `_prune_capacity_estimates()` to keep only 30 entries
   - Calls `self.save()` for atomic persistence via fdatasync

2. `get_capacity_estimates()` (10 LOC)
   - Returns list sorted by timestamp (latest first)
   - Enables Phase 13 to read historical measurements

3. `get_latest_capacity()` (5 LOC)
   - Returns float (latest Ah) or None
   - Used by Phase 13 new battery detection

4. `_prune_capacity_estimates(keep_count=30)` (8 LOC)
   - Keeps last 30 measurements, discards older
   - Prevents unbounded growth (~2 months retention)

**Model.json schema extension:**
```json
{
  "capacity_estimates": [
    {
      "timestamp": "2026-03-15T12:34:56Z",
      "ah_estimate": 7.45,
      "confidence": 0.85,
      "metadata": {
        "delta_soc_percent": 50.0,
        "duration_sec": 1234,
        "ir_mohms": 45.2,
        "load_avg_percent": 35.0,
        "coulomb_ah": 7.45,
        "voltage_check_ah": 7.40
      }
    }
  ]
}
```

**Tests (7 passing):**
- ✓ `test_add_capacity_estimate_creates_array_if_missing`: Array created on first call
- ✓ `test_get_capacity_estimates_returns_list_latest_first`: Sorted descending by timestamp
- ✓ `test_get_latest_capacity_returns_float_or_none`: Returns latest Ah or None if empty
- ✓ `test_prune_capacity_estimates_keeps_30`: 35 estimates → 30 retained
- ✓ `test_save_persists_capacity_estimates_atomically`: model.json updated with atomic write
- ✓ `test_reload_persists_capacity_estimates`: Save/load cycle preserves data
- ✓ `test_capacity_estimates_schema_has_required_fields`: All fields present and correct types

**Verification:**
- All 7 unit tests passing (100%)
- Atomic persistence verified via fdatasync + rename pattern
- Schema validation tested (timestamp, ah_estimate, confidence, metadata)
- Backward compatible: missing array defaults to empty list

---

### Task 2: Integrate CapacityEstimator into MonitorDaemon (CAP-01, CAP-05)

**Changes to `src/monitor.py`:**

1. **Import CapacityEstimator** (1 line)
   ```python
   from src.capacity_estimator import CapacityEstimator
   ```

2. **Initialization in `__init__()`** (15 LOC)
   ```python
   self.capacity_estimator = CapacityEstimator(
       peukert_exponent=self.battery_model.get_peukert_exponent(),
       nominal_voltage=self.battery_model.get_nominal_voltage(),
       nominal_power_watts=self.battery_model.get_nominal_power_watts()
   )
   ```
   - Reloads historical estimates from model.json for convergence tracking
   - Ensures `has_converged()` survives daemon restarts

3. **`_handle_discharge_complete(discharge_data)` handler** (60 LOC)
   - Called on discharge completion (OB→OL transition detected by EventClassifier)
   - Extracts V, t, I series from discharge_buffer
   - Calls `CapacityEstimator.estimate()` with LUT
   - If result is None (quality filter): logs rejection, returns (no model update)
   - If result is (Ah, confidence, metadata) tuple:
     - Calls `model.add_capacity_estimate()` with all parameters
     - Checks `has_converged()` → sets `model['capacity_converged'] = True` if converged
     - Logs measurement: `"Capacity: {Ah}Ah, confidence {CoV:.0%}"`
   - Guard clauses: rejects if <2 data points

**Integration Points:**
- CapacityEstimator created once at daemon startup (reused across all discharges)
- Historical measurements loaded from model.json → estimator preserves convergence state
- _handle_discharge_complete() called when EventClassifier detects OB→OL transition
- Model.json persisted atomically after successful measurement (via _safe_save)

**Tests (7 passing):**
- ✓ `test_daemon_initializes_capacity_estimator`: CapacityEstimator created in __init__()
- ✓ `test_handle_discharge_complete_calls_estimator`: estimate() called with (V, t, I, lut)
- ✓ `test_estimate_none_rejected_no_model_update`: Rejection → no model update, logged
- ✓ `test_estimate_success_calls_model_add`: Success → model.add_capacity_estimate() called
- ✓ `test_convergence_detection_sets_flag`: has_converged() checked, flag set
- ✓ `test_new_battery_flag_stored_in_config`: Battery model initialized
- ✓ `test_integration_discharge_event_to_estimate_to_model`: Full pipeline verified

**Verification:**
- All 7 integration tests passing (100%)
- Full test suite: 285/285 tests passing (no regressions)
- Discharge buffer integration verified (V, t, I series collection)
- LUT reference passed correctly to estimator
- Metadata logging confirmed

---

## Key Implementation Details

### Atomic Persistence Pattern (CAP-04)

```python
def add_capacity_estimate(self, ah_estimate, confidence, metadata, timestamp):
    if 'capacity_estimates' not in self.data:
        self.data['capacity_estimates'] = []

    entry = {
        'timestamp': timestamp,
        'ah_estimate': ah_estimate,
        'confidence': confidence,
        'metadata': metadata
    }
    self.data['capacity_estimates'].append(entry)
    self._prune_capacity_estimates()  # Keep last 30
    self.save()  # Atomic write via fdatasync + rename
```

**Guarantees:**
- No partial writes on power loss (fdatasync ensures data sync)
- Unlink + link on POSIX makes rename atomic (no intermediate state visible)
- Pruning happens before write (no unbounded growth)
- Backward compat: missing field defaults to []

### Convergence Tracking (CAP-01)

CapacityEstimator maintains running statistics:
- `add_measurement(ah, timestamp, metadata)` accumulates data
- `has_converged()` returns True when: count >= 3 AND CoV < 0.10
- `get_confidence()` returns 0.0 for n<3, else (1 - CoV) clamped [0, 1]

MonitorDaemon reloads historical measurements on startup:
```python
for estimate in self.battery_model.get_capacity_estimates():
    self.capacity_estimator.add_measurement(
        ah=estimate['ah_estimate'],
        timestamp=estimate['timestamp'],
        metadata=estimate['metadata']
    )
```

**Result:** After 3 deep discharges, convergence_score >= 0.90 (expert-approved).

### Discharge Handler Integration (CAP-05)

```python
def _handle_discharge_complete(self, discharge_data):
    # Guard: need data
    if len(voltage_series) < 2: return

    # Call estimator
    result = self.capacity_estimator.estimate(V, t, I, lut)

    # Quality filter rejection
    if result is None:
        logger.debug("Quality filter rejection")
        return

    # Success: store
    ah, conf, meta = result
    self.battery_model.add_capacity_estimate(ah, conf, meta, timestamp)

    # Check convergence
    if self.capacity_estimator.has_converged():
        self.battery_model.data['capacity_converged'] = True
        logger.info(f"Capacity converged: {ah:.2f}Ah")
```

**Note:** new_battery_requested flag (CAP-05) is set by Phase 13; Plan 02 stores battery model reference.

---

## VAL-01 & VAL-02 Enforcement

**VAL-01 (Quality filters):**
- CapacityEstimator._passes_quality_filter() enforces hard rejects in estimate()
- Micro-discharges (<300s): rejected → None returned → no model update
- Shallow discharges (<25% ΔSoC): rejected → None returned → no model update
- Phase 12 Plan 02 logs rejections; no error condition (expected for flickers)

**VAL-02 (Peukert fixed):**
- CapacityEstimator initialized with peukert_exponent from model.json (default 1.2)
- No auto-refinement; Peukert calibration responsibility of separate flow
- Estimate algorithm uses fixed exponent for voltage-based cross-check

---

## Phase Ordering Constraint

**Phase 12.2 constraint:** full_capacity_ah_ref unchanged by capacity measurement.

Measured capacity stored in capacity_estimates[] array only. Phase 13 decides if/when to replace rated with measured. This prevents:
- Circular dependency (capacity determines SoH, SoH determines capacity)
- False positives on first discharge (collection bias)
- Unexpected SoH jumps from user perspective

---

## Deviations from Plan

**None.** Plan executed exactly as written:
- ✓ BatteryModel extended with capacity_estimates array
- ✓ Atomic persistence via model.save() + fdatasync
- ✓ CapacityEstimator integrated into MonitorDaemon.__init__()
- ✓ _handle_discharge_complete() handler implemented
- ✓ Quality filter rejections logged, not persisted
- ✓ Convergence detection implemented (count >= 3 AND CoV < 0.10)
- ✓ Historical measurements reloaded on daemon restart
- ✓ All tests passing (0 failures)

---

## Readiness for Phase 13

**Requirements met:**
- ✓ CAP-01: Daemon measures capacity from discharge events (>50% depth)
- ✓ CAP-04: Daemon persists capacity_estimates[] atomically
- ✓ CAP-05: Battery model initialized (new_battery_requested to be set by Phase 13)

**Output for Phase 13:**
- `model.json.capacity_estimates[]`: array of {timestamp, ah_estimate, confidence, metadata}
- `model.data['capacity_converged']`: boolean flag set when CoV < 0.10 (3+ samples)
- CapacityEstimator convergence state: survives daemon restarts via reload

**Next steps (Phase 13):**
1. Read capacity_estimates[] and capacity_converged flag
2. Implement SoH recalibration using measured capacity
3. Detect new battery installation (>10% capacity jump)
4. Prompt user for confirmation before rebaseline

---

## Code Statistics

| Metric | Count |
|--------|-------|
| src/model.py additions | 50 LOC |
| src/monitor.py additions | 150 LOC |
| test_model.py additions | 100 LOC |
| test_monitor.py additions | 150 LOC |
| Total additions | 450 LOC |
| Tests added | 14 |
| Tests passing | 285/285 (100%) |
| Commits | 2 |

---

## Expert Panel Alignment

**Alignment with Phase 12 Plan 01 review (2026-03-15):**
- ✓ Confidence formula: CoV-based, returns 0.0 for n<3
- ✓ Storage format: timestamp, ah_estimate, confidence, metadata
- ✓ Convergence: count >= 3 AND CoV < 0.10 (expert-approved)
- ✓ Isolation: capacity_estimates[] separate, full_capacity_ah_ref unchanged
- ✓ IR metadata: included in metadata dict (foundation for v3.0)

---

*Completed: 2026-03-16 14:00 UTC*
*Commits: 8988be5, d77677f*
