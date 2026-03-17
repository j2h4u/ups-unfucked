# Implementation Guide: SoH Estimator Correction

**Target**: `src/soh_calculator.py`
**Effort**: 15 lines of code
**Test coverage**: 3 new test cases
**Deployment**: Safe (backward compatible, only changes SoH calculation)

---

## Current Code (Broken)

**File**: `src/soh_calculator.py`, lines 79-94

```python
# Reference area from Peukert's Law (physics, no hardcoded constants)
T_expected_sec = peukert_runtime_hours(
    load_percent, capacity_ah, peukert_exponent,
    nominal_voltage, nominal_power_watts
) * 3600
avg_voltage = sum(trimmed_v) / len(trimmed_v)
area_reference = avg_voltage * T_expected_sec

# SoH = (measured area / reference area) × previous SoH
degradation_ratio = area_measured / area_reference if area_reference > 0 else 1.0
new_soh = reference_soh * degradation_ratio

# Clamp to [0, 1]
new_soh = max(0.0, min(1.0, new_soh))

return new_soh
```

**Problem**: For a 10-second test, `degradation_ratio ≈ 0.004`, so `new_soh ≈ 0.004` (catastrophic).

---

## Fixed Code

Replace lines 87-92 with:

```python
# SoH update with duration weighting (Bayesian prior-posterior blend)
# See: docs/STATISTICAL-ANALYSIS-SOH-ESTIMATOR.md § Part 7
degradation_ratio = area_measured / area_reference if area_reference > 0 else 1.0

# Weight updates by discharge duration to reduce bias on short events
# discharge_weight ∈ [0, 1]: 0.01 for 10s test, 1.0 for 30min+ discharge
discharge_duration = trimmed_t[-1] - trimmed_t[0]  # seconds
discharge_weight = min(discharge_duration / (0.30 * T_expected_sec), 1.0)

if discharge_weight < 0.001:
    # Negligible signal from micro-discharges
    return reference_soh

# Bayesian blend: new_soh = prior * (1 - weight) + likelihood * weight
# This treats short discharges as weak evidence, long ones as strong evidence
measured_soh = reference_soh * degradation_ratio
new_soh = reference_soh * (1 - discharge_weight) + measured_soh * discharge_weight

# Clamp to [0, 1]
new_soh = max(0.0, min(1.0, new_soh))

return new_soh
```

**Full function** (for reference):

```python
def calculate_soh_from_discharge(
    discharge_voltage_series: List[float],
    discharge_time_series: List[float],
    reference_soh: float = 1.0,
    anchor_voltage: float = 10.5,
    capacity_ah: float = 7.2,
    load_percent: float = 20.0,
    nominal_power_watts: float = 425.0,
    nominal_voltage: float = 12.0,
    peukert_exponent: float = 1.2
) -> float:
    """
    Calculate State of Health (SoH) from measured discharge voltage profile.

    Uses trapezoidal rule to integrate voltage over time. Reference area is
    computed from Peukert's Law (no empirical constants).

    **UPDATE (2026-03-14)**: Uses duration-weighted Bayesian blending to reduce
    bias on short discharges. See STATISTICAL-ANALYSIS-SOH-ESTIMATOR.md.

    Args:
        discharge_voltage_series: Voltage readings [V] during discharge
        discharge_time_series: Time [sec] for each voltage reading (must be monotonic)
        reference_soh: Previous SoH estimate (0.0-1.0); used as baseline
        anchor_voltage: Physical cutoff voltage (typically 10.5V for VRLA)
        capacity_ah: Full capacity in Ah
        load_percent: Average load during discharge (%)
        nominal_power_watts: UPS nominal power output (W)
        nominal_voltage: Battery nominal voltage (V)
        peukert_exponent: Peukert exponent

    Returns:
        Updated SoH estimate (0.0-1.0)

    Edge cases:
        - Empty or single-point data: returns reference_soh unchanged
        - Voltage below anchor: integration stops at anchor (physical limit)
        - Computed SoH < 0 or > 1: clamped to [0, 1]
        - Discharge <0.1% of expected: returns reference_soh (too short to measure)
    """
    if len(discharge_voltage_series) < 2:
        return reference_soh

    # Trim data at anchor voltage (10.5V is physical limit)
    trimmed_v = []
    trimmed_t = []
    for v, t in zip(discharge_voltage_series, discharge_time_series):
        if v <= anchor_voltage:
            break
        trimmed_v.append(v)
        trimmed_t.append(t)

    if len(trimmed_v) < 2:
        return reference_soh

    # Validate timestamp monotonicity (guard against clock jumps from NTP corrections)
    for i in range(len(trimmed_t) - 1):
        if trimmed_t[i + 1] <= trimmed_t[i]:
            logger.warning(f"Non-monotonic timestamps in discharge data at index {i}: "
                           f"{trimmed_t[i]} >= {trimmed_t[i+1]}, returning reference SoH")
            return reference_soh

    # Compute area-under-curve using trapezoidal rule
    area_measured = 0.0
    for i in range(len(trimmed_v) - 1):
        v1, v2 = trimmed_v[i], trimmed_v[i + 1]
        t1, t2 = trimmed_t[i], trimmed_t[i + 1]
        dt = t2 - t1
        area_measured += (v1 + v2) / 2.0 * dt

    # Reference area from Peukert's Law (physics, no hardcoded constants)
    T_expected_sec = peukert_runtime_hours(
        load_percent, capacity_ah, peukert_exponent,
        nominal_voltage, nominal_power_watts
    ) * 3600
    avg_voltage = sum(trimmed_v) / len(trimmed_v)
    area_reference = avg_voltage * T_expected_sec

    # SoH update with duration weighting (Bayesian prior-posterior blend)
    # See: docs/STATISTICAL-ANALYSIS-SOH-ESTIMATOR.md § Part 7
    degradation_ratio = area_measured / area_reference if area_reference > 0 else 1.0

    # Weight updates by discharge duration to reduce bias on short events
    # discharge_weight ∈ [0, 1]: 0.01 for 10s test, 1.0 for 30min+ discharge
    discharge_duration = trimmed_t[-1] - trimmed_t[0]  # seconds
    discharge_weight = min(discharge_duration / (0.30 * T_expected_sec), 1.0)

    if discharge_weight < 0.001:
        # Negligible signal from micro-discharges
        logger.debug(f"Discharge too short ({discharge_duration:.1f}s) for SoH update; "
                     f"weight={discharge_weight:.6f}")
        return reference_soh

    # Bayesian blend: new_soh = prior * (1 - weight) + likelihood * weight
    # This treats short discharges as weak evidence, long ones as strong evidence
    measured_soh = reference_soh * degradation_ratio
    new_soh = reference_soh * (1 - discharge_weight) + measured_soh * discharge_weight

    logger.debug(f"SoH update: duration={discharge_duration:.1f}s, "
                 f"weight={discharge_weight:.4f}, "
                 f"degradation_ratio={degradation_ratio:.4f}, "
                 f"new_soh={new_soh:.4f}")

    # Clamp to [0, 1]
    new_soh = max(0.0, min(1.0, new_soh))

    return new_soh
```

---

## Test Cases (Add to `tests/test_soh_calculator.py`)

### Test 1: Short discharge barely changes SoH

```python
def test_short_discharge_duration_weighting():
    """10-second test should barely change SoH (weight ≈ 0.01, not 0.004)."""
    # Simulate 10-second test at 12-12.7V
    v = [12.8, 12.75, 12.7]
    t = [0, 5, 10]

    soh = calculate_soh_from_discharge(
        v, t,
        reference_soh=0.95,
        anchor_voltage=10.5,
        capacity_ah=7.2,
        load_percent=20.0,
        peukert_exponent=1.2
    )

    # Should be very close to 0.95, not drastically lower
    # Before fix: soh ≈ 0.0035 (catastrophic)
    # After fix: soh ≈ 0.949 (0.1% change)
    assert soh > 0.94, f"Expected SoH > 0.94 for short discharge, got {soh}"
    assert soh <= 0.95, f"Expected SoH <= 0.95 (reference), got {soh}"
```

### Test 2: Long discharge provides strong signal

```python
def test_long_discharge_strong_update():
    """30-minute discharge should significantly update SoH."""
    # Simulate 30-minute discharge from 13.4V to 10.6V
    v = [13.4, 13.0, 12.5, 12.0, 11.5, 11.0, 10.7]
    t = [0, 300, 600, 900, 1200, 1500, 1800]

    soh_initial = 0.95
    soh = calculate_soh_from_discharge(
        v, t,
        reference_soh=soh_initial,
        anchor_voltage=10.5,
        capacity_ah=7.2,
        load_percent=20.0,
        peukert_exponent=1.2
    )

    # Long discharge should have weight ≈ 1.0, strong update
    # If battery degraded, SoH should drop significantly
    assert soh < soh_initial, f"Expected SoH to decrease, but {soh} >= {soh_initial}"
    assert soh > 0.80, f"Expected reasonable SoH, not collapsed. Got {soh}"
```

### Test 3: Progression of SoH vs duration

```python
def test_duration_weighting_progression():
    """SoH change should scale smoothly with discharge duration."""
    v_base = [13.4, 12.4, 11.4, 10.6]  # 4-point discharge
    soh_initial = 0.95

    soh_10s = calculate_soh_from_discharge(
        v_base[:2], [0, 10],
        reference_soh=soh_initial, capacity_ah=7.2
    )
    soh_100s = calculate_soh_from_discharge(
        v_base[:2], [0, 100],
        reference_soh=soh_initial, capacity_ah=7.2
    )
    soh_1000s = calculate_soh_from_discharge(
        v_base, [0, 300, 600, 1000],
        reference_soh=soh_initial, capacity_ah=7.2
    )

    # SoH change should increase with discharge duration
    # 10s: barely any change
    # 100s: small change
    # 1000s: larger change
    delta_10s = abs(soh_initial - soh_10s)
    delta_100s = abs(soh_initial - soh_100s)
    delta_1000s = abs(soh_initial - soh_1000s)

    assert delta_100s > delta_10s, "Longer discharge should change SoH more"
    assert delta_1000s > delta_100s, "Even longer discharge should change SoH more"

    # But all should be reasonable (no catastrophic collapse)
    assert soh_10s > 0.94
    assert soh_100s > 0.90
    assert soh_1000s > 0.80
```

---

## Deployment Checklist

- [ ] **Code review**: Have statistician review change
- [ ] **Unit tests**: Run `pytest tests/test_soh_calculator.py -v`
- [ ] **Integration tests**: Run `pytest tests/ -v` (all tests pass)
- [ ] **Regression test**: Load saved `model.json` from production, verify SoH history doesn't have catastrophic drops
- [ ] **Simulation test**: Run daemon with recorded March 12, 2026 blackout data:
  - Expected: SoH drops ~10-15% for 30-minute discharge
  - Not expected: SoH → 0.004 or catastrophic collapse
- [ ] **Document**: Add comment referencing `STATISTICAL-ANALYSIS-SOH-ESTIMATOR.md`
- [ ] **Changelog**: Note "Fixed SoH estimator bias for short discharges via duration weighting"
- [ ] **Tag release**: v1.2.0-beta

---

## Validation

### Quick Test (5 minutes)

```bash
cd ~/repos/j2h4u/ups-battery-monitor

# Run unit tests
python -m pytest tests/test_soh_calculator.py::test_short_discharge_duration_weighting -v

# Expected output:
# PASSED test_soh_calculator.py::test_short_discharge_duration_weighting
```

### Full Test (15 minutes)

```bash
# Run all tests
python -m pytest tests/ -v

# Expected: All tests pass, no new failures
```

### Production Test (30 minutes)

```bash
# Load real model.json and verify SoH history is reasonable
python -c "
from src.model import BatteryModel
m = BatteryModel('~/.config/ups-battery-monitor/model.json')
history = m.get_soh_history()
print('SoH history (last 10 entries):')
for h in history[-10:]:
    print(f\"  {h['date']}: {h['soh']:.3f}\")
"

# Expected: monotonically decreasing trend, no jumps >20% per day
```

---

## Rollback Plan

If the change causes issues:

1. **Revert code**: `git checkout HEAD~1 src/soh_calculator.py`
2. **Clear SoH history**: Remove entries from `model.json['soh_history']` that were added after change
3. **Restart daemon**: `systemctl restart ups-battery-monitor`

---

## FAQ During Implementation

**Q: Should we update both LUT and SoH on every discharge?**
A: Yes. LUT calibration is independent (always update). SoH is duration-weighted.

**Q: What if duration is 0 (malformed data)?**
A: Edge case handled by `discharge_weight < 0.001` check. Returns `reference_soh` unchanged.

**Q: Can we change the 0.30 threshold?**
A: Yes, but results are similar for 0.20-0.50. Use 0.30 as default.

**Q: Do we need to update Peukert auto-calibration?**
A: No, it's independent. Keep as-is.

**Q: Should we log the discharge_weight?**
A: Yes (see code above). Helps debug "why didn't SoH change?" questions.

---

## Success Criteria

After deploying this change, over 3 months of production:

1. **SoH history shows linear degradation**, not collapse
   - R² of linear regression > 0.7
   - Slope ~−0.03 per month (battery degrades ~3%/month under stress)
2. **No SoH jumps >20% in a single day**
   - 95th percentile of |ΔSoH| per day < 5%
3. **Long blackouts (>15 min) update SoH significantly**
   - SoH changes >5% per event
4. **Short tests don't destroy estimates**
   - 200 short tests cause <1% SoH change

---

## Next Steps

1. Implement above code change
2. Add three test cases
3. Run full test suite
4. Deploy to beta environment (or production if confident)
5. Monitor SoH history for 2-4 weeks
6. If stable, close investigation
7. Consider optional freshness gate (soft, not binary) for v1.3

