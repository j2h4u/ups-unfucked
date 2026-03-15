# Technology Stack: Battery Capacity Estimation

**Project:** UPS Battery Monitor v2.0 — Actual Capacity Estimation

**Researched:** 2026-03-15

---

## Recommended Stack

### Core Framework
| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| Python | 3.13 | Capacity estimator algorithm | Already in use; stable, minimal dependencies. Numerical calculations via math library (no numpy needed). |

### Algorithm & Math Libraries
| Library | Version | Purpose | Why |
|---------|---------|---------|-----|
| Standard library `math` | builtin | Trapezoidal integration, variance calculations | Already used for SoH calculator. No external deps. |
| `statistics` | builtin | Mean, variance, confidence interval calculations | Available since Python 3.4. Lightweight alternative to numpy for small datasets. |

### Data Persistence
| Library | Version | Purpose | Why |
|---------|---------|---------|-----|
| `json` | builtin | Capacity estimates serialization in model.json | Already in use for model.json. Atomic write pattern via `src/model.py:atomic_write_json()`. |
| `datetime` | builtin | Timestamp tracking for discharge estimates | Already in use. Enables "refresh rate" (oldest vs newest discharges in confidence calculation). |

### No New Dependencies

**Why:** Capacity estimation is pure mathematical computation (coulomb integral, variance tracking, confidence thresholds). All required operations available in Python stdlib.

---

## Architecture: Data Flow

```
Discharge Event (OL → OB → OL)
    ↓
DischargeBuffer collects (voltage, time, load) samples
    ↓
On OB→OL transition: capacity_estimator.py:estimate_capacity_from_discharge()
    ├─ Read: discharge_buffer (V, I, t), config (capacity_ah, Peukert exponent)
    ├─ Compute: ΔSoC from voltage LUT lookup
    ├─ Compute: Q = ∫ I(t) dt / ΔSoC_normalized (coulomb counting)
    ├─ Compute: confidence = f(discharge_count, average_depth, variance)
    └─ Return: (estimated_ah, confidence, metadata)
    ↓
BatteryModel.add_capacity_estimate(estimated_ah, confidence, metadata)
    ├─ Append to model.json: capacity_estimates array
    ├─ Update: full_capacity_ah_measured (weighted average of recent estimates)
    ├─ Compute: capacity_confidence (convergence metric)
    └─ Trigger: new_battery_detection() if measured ≠ stored >10%
    ↓
SoH Recalibration (if converged)
    ├─ Old: SoH = measure_voltage_curve_area / reference_curve_area
    ├─ New: SoH = measure_voltage_curve_area / (reference_curve_area × measured_capacity / rated_capacity)
    └─ Persist: model.json updates SoH baseline
    ↓
MOTD, battery-health.py, Grafana (if enabled)
    └─ Display: "Capacity: 5.8Ah measured (2/3 deep discharges, high confidence)"
```

---

## New Module: capacity_estimator.py

**Location:** `src/capacity_estimator.py`

**Exports:**
```python
def estimate_capacity_from_discharge(
    discharge_voltage_series: List[float],      # V (e.g., [13.2, 13.0, 12.8, ...])
    discharge_time_series: List[float],         # seconds (e.g., [0, 10, 20, ...])
    discharge_load_percent: List[float],        # % (e.g., [10, 12, 11, ...])
    reference_capacity_ah: float = 7.2,         # Current reference (Ah)
    peukert_exponent: float = 1.2,             # Fixed for v2 (refined in v3)
    voltage_lut: Dict[float, float] = None     # From BatteryModel
) -> Dict[str, Any]:
    """
    Estimate battery capacity from measured discharge profile.

    Returns:
    {
        'estimated_capacity_ah': 5.8,
        'discharge_soc_percent': 65,
        'discharge_duration_sec': 2100,
        'confidence': 0.65,  # 0.0-1.0
        'method': 'coulomb_counting_with_voltage_anchor',
        'metadata': {
            'avg_load_percent': 11.5,
            'min_voltage': 10.8,
            'max_voltage': 13.2,
            'notes': 'Shallow discharge; confidence low. Need deeper cycle.'
        }
    }
    """
```

**Algorithm Pseudocode:**
```python
# 1. Determine voltage range and SoC window
v_initial = discharge_voltage_series[0]
v_final = discharge_voltage_series[-1]
soc_initial = voltage_lut.lookup(v_initial)       # % (e.g., 95%)
soc_final = voltage_lut.lookup(v_final)           # % (e.g., 30%)
delta_soc = soc_initial - soc_final               # % (e.g., 65%)

# 2. Reject if shallow (δSoC < 25%, or < 5 minutes)
if delta_soc < 25 or discharge_time_series[-1] < 300:
    return {"confidence": 0.0, "metadata": {"reason": "shallow_discharge"}}

# 3. Compute average load (accounting for varying discharge rate)
i_avg = mean(discharge_load_percent)  # % of rated current
i_avg_amps = (i_avg / 100) * (reference_capacity_ah / time_const_hours)

# 4. Coulomb integral: Q = ∫ I dt (with Peukert correction)
dt_seconds = discharge_time_series[-1] - discharge_time_series[0]
dt_hours = dt_seconds / 3600
q_coulomb = i_avg_amps * dt_hours                 # Ah (unadjusted)

# 5. Peukert correction: effective capacity = Q / (I/I_rated)^peukert
# For simplicity v2: use average load only (no integration over varying I)
i_rated = reference_capacity_ah  # C/1 discharge
peukert_factor = (i_avg / 100) ** (peukert_exponent - 1.0)
q_peukert_corrected = q_coulomb * peukert_factor

# 6. Normalize by ΔSoC (fractional capacity drawn)
estimated_capacity = q_peukert_corrected / (delta_soc / 100)

# 7. Confidence: function of depth + duration + voltage stability
depth_score = min(delta_soc / 60, 1.0)    # 1.0 at ≥60% DoD
duration_score = min(dt_seconds / 1800, 1.0)  # 1.0 at ≥30 min
stability_score = 1.0 - (std_dev(discharge_voltage_series) / mean(...))
confidence = (depth_score + duration_score + stability_score) / 3

return {
    'estimated_capacity_ah': round(estimated_capacity, 1),
    'discharge_soc_percent': round(delta_soc, 1),
    'discharge_duration_sec': int(dt_seconds),
    'confidence': round(confidence, 2),
    'metadata': {...}
}
```

---

## Model Schema Update

**Current model.json structure:**
```json
{
  "full_capacity_ah_ref": 7.2,
  "soh_history": [
    { "date": "2026-03-10", "soh": 0.98 }
  ],
  "lut": [
    [13.5, 100],
    [13.0, 95],
    ...
  ]
}
```

**New schema (v2):**
```json
{
  "full_capacity_ah_ref": 7.2,
  "full_capacity_ah_measured": 5.8,
  "capacity_converged": true,
  "capacity_confidence": 0.95,
  
  "capacity_estimates": [
    {
      "timestamp": "2026-03-12T18:40:00Z",
      "method": "coulomb_counting_voltage_anchor",
      "estimated_ah": 5.5,
      "discharge_soc_percent": 45,
      "discharge_duration_sec": 1200,
      "confidence": 0.65,
      "avg_load_percent": 10.0,
      "metadata": {
        "min_voltage": 10.8,
        "max_voltage": 13.2,
        "note": "shallow discharge"
      }
    },
    {
      "timestamp": "2026-03-14T20:15:00Z",
      "method": "coulomb_counting_voltage_anchor",
      "estimated_ah": 5.8,
      "discharge_soc_percent": 65,
      "discharge_duration_sec": 2100,
      "confidence": 0.92,
      "avg_load_percent": 15.0,
      "metadata": {
        "min_voltage": 10.5,
        "max_voltage": 13.4
      }
    }
  ],
  
  "soh_history": [
    { "date": "2026-03-10", "soh": 0.98 },
    { "date": "2026-03-14", "soh": 0.97 }
  ],
  
  "lut": [...]
}
```

---

## Configuration Additions

**File:** `config.toml`

```toml
# Battery capacity estimation (v2.0+)

# Minimum depth-of-discharge (%) to trigger capacity estimate
min_discharge_depth_for_estimate = 25

# Minimum discharge duration (seconds) to trigger estimate
min_discharge_duration_for_estimate = 300  # 5 minutes

# Number of deep discharges (>50% DoD) needed for "converged" status
min_discharges_for_convergence = 2

# Confidence threshold (0.0-1.0) to mark as converged
min_confidence_for_convergence = 0.80

# Detect new battery if measured capacity differs by >N% from stored
new_battery_threshold_percent = 10
```

---

## Integration Points in Existing Code

### monitor.py
- On `OB→OL` transition (discharge complete): call `capacity_estimator.estimate_capacity_from_discharge()`
- Pass `discharge_buffer` (voltages, times, loads)
- Store result via `battery_model.add_capacity_estimate()`

### soh_calculator.py
- Add optional parameter `measured_capacity_ah` (defaults to config value)
- Formula change: `SoH = area_measured / (area_reference × measured_capacity / rated_capacity)`

### alerter.py / MOTD
- New alert condition: "Capacity converged after N discharges"
- New metric: "Measured capacity vs rated" (e.g., "5.8Ah measured vs 7.2Ah rated")

### battery-health.py
- Add field: `"capacity_ah_measured": 5.8`
- Add field: `"capacity_confidence": 0.95`
- Add field: `"estimated_replacement_date": "2028-03-15"` (recalc with measured SoH)

---

## Testing Strategy

### Unit Tests
- `test_capacity_estimator.py`: Synthetic discharge profiles (known I/t/V, expected Q)
- `test_model_schema.py`: model.json serialization/deserialization with new fields
- `test_confidence_calculation.py`: Confidence scores for varying discharge depths

### Integration Tests
- Replay real `discharge_buffer` from 2026-03-12 blackout
- Verify estimate ≈ 5.8Ah (or match field notes if available)
- Verify model.json writes atomically

### Validation
- Compare estimate against known reference if available
- Track estimate variance across first 3-5 discharges (should stabilize)

---

## No External Dependencies

**Benefit:** Minimal attack surface, no version conflicts, easier packaging.

**Tradeoff:** Pure Python implementation (no numpy) means slightly more code for basic stats (mean, variance). Acceptable for v2 (small sample sizes, <100 estimates in typical lifetime).

**Future (v3+):** If heavy statistical modeling needed (Bayesian estimation, hierarchical models), consider adding `scipy` or `numpy`. Not justified for v2.

---

## Sources

- Python 3.13 Standard Library: `math`, `statistics`, `json`, `datetime`
- Existing code: `src/soh_calculator.py` (trapezoidal integration pattern)
- Existing code: `src/model.py` (atomic write pattern)
- Battery equations: Peukert's Law, coulomb counting integral (see FEATURES.md sources)

---

**Status:** Ready for implementation phase. All required functionality available in Python stdlib. No blocking dependencies.
