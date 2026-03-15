# Architecture Patterns: Capacity Estimation Module

**Domain:** UPS Battery Monitor v2.0

**Researched:** 2026-03-15

---

## Recommended Architecture

### Component Overview

```
DischargeBuffer (existing)
    ↓ (on OB→OL transition)
CapacityEstimator (new module)
    ├─ Input: voltage/time/load arrays, config, LUT
    ├─ Algorithm: coulomb counting + voltage anchor + Peukert correction
    └─ Output: estimated_ah, confidence, metadata
    ↓
BatteryModel (existing, extended)
    ├─ add_capacity_estimate() — appends to capacity_estimates array
    ├─ get_measured_capacity() — weighted average of recent estimates
    ├─ get_capacity_confidence() — convergence tracker
    └─ trigger_new_battery_detection() — if measured ≠ stored >10%
    ↓
SoH Recalibration (existing code, updated formula)
    ├─ Old: SoH = area_measured / area_reference
    ├─ New: SoH = area_measured / (area_reference × measured/rated)
    └─ Recompute: all historical SoH when measured capacity converges
    ↓
MOTD / battery-health.py / Alerter (existing, enhanced output)
    └─ Display: measured vs rated, confidence, recommendation
```

### Data Model: BatteryModel Extensions

**New class methods:**

```python
class BatteryModel:
    def add_capacity_estimate(
        self,
        estimated_ah: float,
        confidence: float,
        discharge_soc: float,
        discharge_duration: float,
        metadata: Dict[str, Any]
    ) -> None:
        """
        Append capacity estimate to model.json.
        
        Updates:
        - capacity_estimates array
        - full_capacity_ah_measured (weighted avg of recent)
        - capacity_confidence (convergence metric)
        """
    
    def get_measured_capacity(self) -> float:
        """
        Return weighted average of capacity estimates.
        If <2 converged estimates: return None (use config value).
        """
    
    def is_capacity_converged(self) -> bool:
        """
        Return True if:
        - ≥2 deep discharges (ΔSoC > 50%), AND
        - Confidence ≥ 0.80
        """
    
    def trigger_new_battery_check(self, runtime_estimate: float) -> bool:
        """
        Return True if estimated_capacity differs from stored by >10%.
        If True, prompt user: "Is this a new battery?" (y/n).
        """
```

### Component Boundaries

| Component | Responsibility | Communicates With | Notes |
|-----------|---------------|-------------------|-------|
| **DischargeBuffer** | Collect voltage/time/load during OB state; reset on OL | monitor.py | Existing code — no changes |
| **CapacityEstimator** | Pure calculation: (V,I,t) → estimated_ah + confidence | BatteryModel (read LUT) | New module `capacity_estimator.py` |
| **BatteryModel** | Persist estimates; track convergence; detect new battery | CapacityEstimator, SoH calculator, alerter | Extended from v1.1 |
| **SoH Calculator** | Compute health from voltage curve; use measured capacity if available | BatteryModel (read measured_capacity) | Updated formula |
| **Monitor** | Orchestrate discharge collection → estimation → persistence | All above | Updated flow on OB→OL event |
| **Alerter / MOTD** | Display measured vs rated; confidence; replacement timeline | BatteryModel (read capacity fields) | Enhanced output |

---

## Data Flow: Discharge-to-Estimate Lifecycle

### Detailed Sequence

**1. During Blackout (OB state)**
```
monitor.py → poll upsc cyberpower every 5sec
  → detect ups.status = "OB DISCHRG"
  → enable discharge_buffer.collecting = True
  → append (voltage, time, load) to buffer arrays
```

**2. On Power Restoration (OL state)**
```
monitor.py → detect ups.status = "OL"
  → set discharge_buffer.collecting = False
  
  IF len(discharge_buffer.voltages) >= 2:
    → CALL capacity_estimator.estimate_capacity_from_discharge(
        voltages=buffer.voltages,
        times=buffer.times,
        loads=buffer.loads,
        lut=battery_model.lut,
        reference_capacity_ah=config.capacity_ah or battery_model.get_measured_capacity(),
        peukert_exponent=1.2  # fixed in v2
      )
    
    → IF estimate.confidence > 0.0:
        → battery_model.add_capacity_estimate(...)
        → battery_model.save()  # atomic write
```

**3. On Startup (New Battery Detection)**
```
monitor.py.__init__() → battery_model.load()
  
  IF battery_model.get_measured_capacity() is not None:
    estimated_now = estimate_capacity_from_last_discharge()  # from model.json
    
    IF |estimated_now - model.measured| > 10%:
      → PROMPT: "Is this a new battery? (y/n)"
      → IF "y":
          → battery_model.set_new_battery_baseline(estimated_now)
          → battery_model.recalculate_soh_history()
      → ELSE:
          → log warning: "Capacity mismatch — investigate"
```

**4. SoH Recalibration (When Converged)**
```
WHENEVER battery_model.is_capacity_converged() == True:
  FOR EACH soh_history entry:
    old_soh = entry['soh']
    recompute_soh = old_soh / (measured_capacity / rated_capacity)
    entry['soh'] = recompute_soh
  
  battery_model.save()
```

**5. Output (MOTD/API)**
```
motd/51-ups.sh, battery-health.py
  → Read: battery_model.full_capacity_ah_measured
  → Read: battery_model.full_capacity_ah_ref
  → Read: battery_model.capacity_confidence
  
  Display:
    "Capacity: 5.8Ah measured (2/3 discharges, 95% confidence)
             vs 7.2Ah rated. SoH: 94%."
```

---

## Patterns to Follow

### Pattern 1: Coulomb Counting with Voltage Anchor

**What:** Integrate current over time; reset cumulative error using voltage LUT as reference point.

**When:** Every discharge event where ΔV > 0.5V (reliable LUT lookup).

**Why:** Coulomb-only counting accumulates ADC noise and sensor bias. Voltage provides absolute reference (10.5V anchor = 0% SoC by definition). Combining both gives accuracy of ±5-10% without requiring additional sensors.

**Example (pseudocode):**
```python
# Method: Coulomb Counting with Voltage Anchor

# Phase 1: Coulomb integral
i_avg_amps = mean(load_percent) / 100 * rating_ah
dt_hours = (t_final - t_init) / 3600
q_coulomb = i_avg_amps * dt_hours

# Phase 2: ΔSoC from voltage LUT
v_init, v_final = voltage_series[0], voltage_series[-1]
soc_init = voltage_to_soc_lut.lookup(v_init)
soc_final = voltage_to_soc_lut.lookup(v_final)
delta_soc_frac = (soc_init - soc_final) / 100

# Phase 3: Peukert correction
# (simple version: constant load)
i_rated = rating_ah
i_factor = (i_avg / i_rated)
peukert_exp = 1.2
peukert_factor = i_factor ** (peukert_exp - 1)
q_corrected = q_coulomb * peukert_factor

# Phase 4: Estimate
capacity_estimated = q_corrected / delta_soc_frac

return capacity_estimated
```

**Anti-pattern:** Coulomb-only (no voltage anchor) → cumulative error → estimates drift over months.

---

### Pattern 2: Confidence Scoring (Multi-Factor)

**What:** Combine discharge depth, duration, and voltage stability into single confidence metric.

**When:** After computing capacity estimate from any discharge.

**Why:** User needs to know: "Is this preliminary (1 short blackout) or solid (3 deep discharges)?" Confidence guides decision: accept estimate now, or wait for more data.

**Example:**
```python
def compute_confidence(
    delta_soc_percent: float,
    discharge_duration_sec: float,
    voltage_std_dev: float,
    discharge_count: int,
    discharge_count_deep: int
) -> float:
    """
    Confidence = f(depth, duration, stability, history)
    
    Ranges: 0.0 (useless) to 1.0 (very high confidence)
    """
    
    # Individual scores (0.0–1.0)
    depth_score = min(delta_soc_percent / 60, 1.0)  # 1.0 at 60%+
    duration_score = min(discharge_duration_sec / 1800, 1.0)  # 1.0 at 30min+
    stability_score = 1.0 - (voltage_std_dev / 0.5)  # smooth if σ < 0.5V
    stability_score = max(stability_score, 0.0)
    
    # Boost for multiple samples
    count_boost = min(discharge_count / 3, 0.3)  # +0.3 bonus at 3+ discharges
    deep_boost = min(discharge_count_deep / 2, 0.2)  # +0.2 at 2+ deep discharges
    
    # Weighted average
    confidence = (
        0.4 * depth_score +
        0.3 * duration_score +
        0.2 * stability_score +
        0.05 * count_boost +
        0.05 * deep_boost
    )
    
    return min(confidence, 1.0)
```

**Interpretation:**
- `0.0–0.3`: Preliminary (shallow/short discharge). Useful but high variance.
- `0.3–0.7`: Moderate (deeper discharge or multiple samples accumulating).
- `0.7–1.0`: High (2+ deep discharges or 10+ moderate discharges). Use for predictions.

---

### Pattern 3: Weighted Capacity Averaging (Newer = Higher Weight)

**What:** When multiple estimates exist, compute weighted average where recent samples have more weight (newer battery may be in different condition than old data).

**When:** Computing `full_capacity_ah_measured` from capacity_estimates array.

**Why:** v2 expects 2-3 samples over weeks. Weights down old estimates as battery may degrade or load profile may change.

**Example:**
```python
def compute_weighted_capacity(capacity_estimates: List[Dict]) -> float:
    """
    Return weighted average of recent capacity estimates.
    Weight = function(recency, confidence).
    """
    if not capacity_estimates or len(capacity_estimates) < 1:
        return None
    
    # Use only recent estimates (last 6 months)
    cutoff = datetime.now() - timedelta(days=180)
    recent = [e for e in capacity_estimates if e['timestamp'] > cutoff]
    
    if not recent:
        return None
    
    total_weight = 0
    weighted_sum = 0
    
    for est in recent:
        # Weight = confidence × recency_factor
        recency_factor = 1.0 + (est['timestamp'] - recent[0]['timestamp']).days / 180
        weight = est['confidence'] * recency_factor
        
        weighted_sum += est['estimated_ah'] * weight
        total_weight += weight
    
    return weighted_sum / total_weight if total_weight > 0 else None
```

---

## Anti-Patterns to Avoid

### Anti-Pattern 1: Estimating from Every Discharge

**What:** Update capacity estimate after every blackout (shallow or deep).

**Why bad:** Short discharges (2-5 min, ΔSoC 10-20%) have high noise. Estimates swing ±20%. False sense of precision.

**Instead:** Require minimum depth (ΔSoC > 25%) or duration (>5 min) before updating. Flag preliminary vs converged estimates clearly.

### Anti-Pattern 2: Peukert Exponent Embedded in Capacity Estimate

**What:** Refine both capacity AND Peukert exponent simultaneously (circular dependency).

**Why bad:** Can't distinguish: "battery is small" vs "discharge curve is unusual" → oscillating estimates.

**Instead (v2):** Fix Peukert at 1.2. Measure capacity. In v3 (separate milestone CAL2-02): refine exponent using stable capacity as anchor.

### Anti-Pattern 3: Ignoring Voltage LUT Uncertainty

**What:** Use coulomb counting alone, trusting ADC ± 0.1V.

**Why bad:** Cumulative integration error over 30+ minutes of discharge → estimates drift 10-15%.

**Instead:** Anchor to voltage LUT. Use 10.5V (VRLA cutoff) as zero-error reference point every discharge. Voltage uncertainty is much lower than coulomb counting error.

### Anti-Pattern 4: Automatic SoH Rebaseline Without User Awareness

**What:** On first capacity convergence, silently recompute all historical SoH.

**Why bad:** User may not understand why SoH jumped from 85% to 95%. Looks like bug.

**Instead:** Log clearly: "Battery capacity converged to 5.8Ah. Recalculating SoH relative to new baseline. Previous SoH 85% (vs rated) → 94% (vs measured)." Document in MOTD + journald.

### Anti-Pattern 5: Requiring Manual Deep Discharge for Capacity

**What:** "Please run a calibration discharge to measure capacity."

**Why bad:** User on stable grid waits 6+ months. Contradicts "zero manual intervention."

**Instead:** Accumulate from natural blackouts. Require 1 deep (ΔSoC > 50%) or 10+ shallow (ΔSoC > 20%), whichever comes first. Show progress: "1/2 deep discharges needed; or 3/10 shallow."

---

## Scalability Considerations

| Concern | At 100 users | At 10K users | At 1M users |
|---------|--------------|--------------|-------------|
| **model.json size (capacity_estimates array)** | ~10KB per UPS (50-100 estimates over 2 years) | Same per UPS | Same per UPS |
| **Computation time (estimate)** | <10ms (trapezoidal + variance) | <10ms | <10ms |
| **Storage write frequency** | ~1x per blackout (rare on stable grids) | Same | Same |
| **Memory (DischargeBuffer)** | ~500KB at 500 samples × 8 bytes | Same (buffer fixed size) | Same |
| **Cross-brand benchmarking (future v3)** | Start collecting | Analyze trends per brand | Publish reports |

**Bottleneck (if ever):** Not computation, but data collection. Stable grids produce slow capacity convergence. Acceptable tradeoff (user gets honest SoH anyway).

---

## Sources

- Coulomb counting: Battery University (BU-904)
- Voltage anchor concept: [MDPI Energies Vol. 15 No. 21](https://www.mdpi.com/1996-1673/15/21/8172)
- Peukert's Law: Classic electrochemistry (established 1897)
- Confidence scoring: Multi-factor decision theory (Dempster–Shafer)

---

**Status:** Ready for detailed design phase. No architectural blockers. Implementation can follow STACK.md tech choices.
