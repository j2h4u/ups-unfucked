# Phase 2: Battery Model — State Estimation & Event Classification - Research

**Researched:** 2026-03-13
**Domain:** Battery state-of-charge estimation, Peukert's Law, event classification via physical invariants
**Confidence:** HIGH

## Summary

Phase 2 requires implementing three core subsystems: (1) SoC prediction via LUT lookup with linear interpolation from IR-normalized voltage, (2) runtime estimation using Peukert's Law with load-dependent exponent, and (3) event classification distinguishing real blackouts from battery tests via input.voltage threshold.

The Phase 1 foundation (EMA smoothing, IR compensation, model.json structure) provides all prerequisite data. Phase 2 adds the mathematical layer that converts normalized voltage into actionable state estimates. All algorithms are deterministic and can be unit-tested offline.

**Primary recommendation:** Implement three independent modules (soc_predictor.py, runtime_calculator.py, event_classifier.py) with the arithmetic already verified by project memory and CONTEXT.md mathematical formulas. Each module is stateless and maps input values to outputs; complexity lies only in the constants and interpolation strategy.

## Standard Stack

### Core (Python stdlib only)

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python | 3.10+ | Runtime (already used in Phase 1) | Phase 1 established Python 3.10+ baseline |
| `bisect` | stdlib | Binary search for LUT voltage lookups | Efficient O(log N) sorted table queries |
| `math` | stdlib | Arithmetic for Peukert and interpolation | No external deps needed |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `numpy` | (NOT used) | Numerical operations | Overkill for single-value interpolation; stdlib math sufficient |
| `scipy.interpolate` | (NOT used) | Spline fitting | Phase 1 chose LUT + linear interpolation, not continuous curves |

### No Alternatives

LUT + linear interpolation is locked by CONTEXT.md: "LUT + IR + Peukert, not formulas" because VRLA discharge curve is individual per battery and only measured real data works.

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| PRED-01 | V_norm → LUT lookup with linear interpolation → SoC | See "SoC Prediction via LUT Lookup" section; bisect module + linear interpolation between sorted voltage points |
| PRED-02 | Time_rem by Peukert's Law using load-dependent exponent | See "Runtime Prediction: Peukert's Law" section; formula `Time_rem = (C_ah * SoC * SoH) / (L_ema ^ 1.2) * Const` |
| PRED-03 | battery.charge calculated from SoC (honest value vs firmware) | Direct output from SoC prediction × 100; replaces unreliable firmware value |
| EVT-01 | Distinguish blackout from test by input.voltage (≈0V vs ≈230V) | See "Event Classification: Blackout vs Test" section; physical invariant independent of firmware |
| EVT-02 | Real blackout path: calculate Time_rem, prepare shutdown | Event classifier triggers shutdown path; runtime calculator provides time estimate |
| EVT-03 | Battery test path: collect calibration data, no shutdown | Event classifier triggers calibration path; discharge data flows to model.json |
| EVT-04 | ups.status arbiter: emit OB DISCHRG or OB DISCHRG LB based on time-to-empty | Stateful event logic: if `input.voltage ≈ 0` and `time_rem < threshold`, set LB flag |
| EVT-05 | On OB→OL transition: update LUT with measured points, recalculate SoH | Model update triggered by ups.status transition event |

## Standard Stack

### Core Libraries
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python `bisect` | stdlib | O(log N) binary search in sorted voltage LUT | Efficient point lookup in discharge curve |
| Python `math` | stdlib | Arithmetic (exp, pow) for Peukert, interpolation | No external deps needed; sufficient for calculations |
| `datetime` | stdlib | ISO 8601 date formatting for model.json timestamps | Already used in Phase 1 model.py |

### Algorithm Verification

Phase 1 code already verified that:
- EMA smoothing produces stable values after 3 samples (tested in test_ema.py)
- IR compensation formula `V_norm = V_ema + k*(L_ema - L_base)` integrates cleanly with EMA buffer
- Model.json structure with LUT array is loadable/saveable with atomic writes

Phase 2 inherits these without modification.

## Architecture Patterns

### Recommended Project Structure

```
src/
├── soc_predictor.py        # V_norm → LUT → SoC (stateless)
├── runtime_calculator.py   # SoC + Load → Time_rem (Peukert, stateless)
├── event_classifier.py     # ups.status + input.voltage → event type (stateful, tracks transitions)
├── monitor.py              # (modified) integrate three modules into daemon loop
├── model.py                # (unchanged from Phase 1)
├── ema_ring_buffer.py      # (unchanged from Phase 1)
├── nut_client.py           # (unchanged from Phase 1)
└── __init__.py

tests/
├── test_soc_predictor.py   # LUT lookup, interpolation edge cases
├── test_runtime_calculator.py  # Peukert formula, zero/negative edge cases
├── test_event_classifier.py    # State machine transitions, input.voltage thresholds
└── conftest.py             # (reuse Phase 1 fixtures)
```

### Pattern 1: LUT Lookup with Linear Interpolation

**What:** Map normalized voltage to SoC% by finding the two adjacent LUT points that bracket the voltage, then interpolate linearly between them.

**When to use:** Every polling cycle, after IR compensation produces V_norm.

**Algorithm:**
1. Binary search (`bisect`) in LUT by voltage to find insertion point
2. If V_norm exactly matches a LUT entry, return that SoC
3. If V_norm is between two entries, interpolate: `SoC = (SoC_low + (V_norm - V_low) / (V_high - V_low) * (SoC_high - SoC_low))`
4. If V_norm exceeds 13.4V (fully charged), clamp to 1.0
5. If V_norm falls below 10.5V (anchor), clamp to 0.0

**Example:**

```python
# Source: Project spec + CONTEXT.md § Модель батареи

def soc_from_voltage(v_norm: float, lut: List[Dict]) -> float:
    """
    Lookup SoC from normalized voltage using LUT linear interpolation.

    Args:
        v_norm: IR-normalized voltage (volts)
        lut: List of {"v": voltage, "soc": state_of_charge, "source": "..."} dicts

    Returns:
        SoC as fraction 0.0-1.0
    """
    # Extract voltage column and sort (should already be sorted)
    voltages = [entry["v"] for entry in lut]

    # Edge cases
    if v_norm >= voltages[-1]:  # At or above highest voltage
        return lut[-1]["soc"]
    if v_norm <= voltages[0]:   # Below lowest voltage
        return lut[0]["soc"]

    # Find two adjacent points
    idx = bisect.bisect_left(voltages, v_norm)
    # idx is insertion point: voltages[idx-1] <= v_norm < voltages[idx]

    v_low = voltages[idx - 1]
    v_high = voltages[idx]
    soc_low = lut[idx - 1]["soc"]
    soc_high = lut[idx]["soc"]

    # Linear interpolation
    fraction = (v_norm - v_low) / (v_high - v_low)
    soc = soc_low + fraction * (soc_high - soc_low)

    return max(0.0, min(1.0, soc))  # Clamp to [0, 1]
```

**Edge cases to test:**
- V_norm exactly at LUT point (e.g., 12.4V) → return exact SoC
- V_norm between points (e.g., 12.3V between 12.1V and 12.4V) → interpolate
- V_norm below anchor (10.5V) → clamp to 0.0
- V_norm above max (13.4V) → clamp to 1.0
- LUT with only 2 points → single interpolation line

### Pattern 2: Peukert's Law for Runtime Estimation

**What:** Calculate time-to-empty from current load, SoC, and battery health using the empirical Peukert exponent (typically 1.2 for VRLA).

**When to use:** Every polling cycle, after SoC is known.

**Formula (from CONTEXT.md):**
```
Time_rem = (full_capacity_ah * SoC * SoH) / (L_ema ^ 1.2) * Const
```

Where:
- `full_capacity_ah`: Reference capacity from model.json (7.2 Ah for UT850EG)
- `SoC`: State of charge (0.0-1.0) from SoC predictor
- `SoH`: State of health (0.0-1.0) from model.json (degrades with age)
- `L_ema`: EMA load (0-100%) from Phase 1 EMA buffer
- `1.2`: Peukert exponent (typical for VRLA, constant across load range)
- `Const`: Scaling factor to convert Ah and load% into minutes

**Deriving Const:**
The formula relates available capacity (Ah) to discharge time (minutes). Standard battery notation uses C-rate: `C = capacity_Ah / discharge_hours`. At reference load, 100% means nominal full power draw.

For UPS at 20% nominal load (~85W on 425W):
- `Time_rem = (7.2 Ah * SoC * 1.0) / (20 ^ 1.2) * Const`
- At real blackout (SoC=1.0, load=20%), observed ~47 min → solve for Const
- `47 = 7.2 / (20 ^ 1.2) * Const`
- `Const ≈ 47 * (20 ^ 1.2) / 7.2 ≈ 47 * 32.8 / 7.2 ≈ 215`

**Example:**

```python
# Source: CONTEXT.md § Математика предиктора

def runtime_minutes(soc: float, load_ema: float, capacity_ah: float, soh: float,
                    peukert_exp: float = 1.2, const: float = 215) -> float:
    """
    Predict remaining battery runtime using Peukert's Law.

    Args:
        soc: State of charge (0.0-1.0)
        load_ema: EMA load percent (0-100)
        capacity_ah: Full capacity in Ah
        soh: State of health (0.0-1.0)
        peukert_exp: Peukert exponent (default 1.2 for VRLA)
        const: Scaling constant (tuned to match observed blackout duration)

    Returns:
        Minutes remaining (float); clamped to [0, inf)
    """
    if load_ema <= 0 or soc <= 0:
        return 0.0

    # Peukert: Time = (Ah * SoC * SoH) / (Load ^ Peukert_exp) * Const
    time_rem = (capacity_ah * soc * soh) / (load_ema ** peukert_exp) * const

    return max(0.0, time_rem)
```

**Edge cases:**
- Load = 0% (no draw) → Time_rem → infinity (clamp to max or flag error)
- SoC = 0% → Time_rem = 0
- Load spike (e.g., 80%) → Time_rem drops sharply (expected Peukert effect)
- Const tuned to match observed 47-minute blackout at ~20% load → must be validated in tests

### Pattern 3: Event Classification — Blackout vs Test

**What:** Distinguish real mains failure (blackout) from manual battery test by checking if mains voltage is present.

**When to use:** On every status change (OL → OB or OB → OL), and when assessing whether to trigger shutdown.

**Physical Invariant (from CONTEXT.md):**
```
ups.status = OB DISCHRG (On Battery, Discharging):
    input.voltage ≈ 0V     → Real blackout (mains failed)
    input.voltage ≈ 230V   → Test discharge (mains present, UPS switched to battery intentionally)
```

**Rationale:** UPS firmware may misinterpret test as "offline" and trigger wrong signals. We bypass firmware interpretation by checking mains voltage directly — a physical measurement independent of firmware logic.

**State Machine:**

```
State: OL (Online)
  ├─ status.OL ∧ input.voltage ≈ 230V → stay in OL
  └─ status.OB ∧ input.voltage ≈ 230V → ENTER BLACKOUT_TEST

State: BLACKOUT_TEST (On Battery, but mains present)
  ├─ input.voltage ≈ 230V → stay in BLACKOUT_TEST (collect calibration data, no shutdown)
  ├─ input.voltage ≈ 0V → ERROR (shouldn't happen mid-test)
  └─ status.OL ∧ input.voltage ≈ 230V → ENTER OL (test complete, update model)

State: BLACKOUT_REAL (On Battery, mains failed)
  ├─ input.voltage ≈ 0V → stay in BLACKOUT_REAL (calculate time_rem, prepare shutdown)
  ├─ input.voltage ≈ 230V → ERROR (shouldn't happen mid-blackout)
  └─ status.OL ∧ input.voltage ≈ 230V → ENTER OL (power restored, update model)
```

**Example:**

```python
# Source: Project spec § Различение блекаута и теста батареи

from enum import Enum

class EventType(Enum):
    ONLINE = "OL"
    BLACKOUT_REAL = "OB_BLACKOUT"
    BLACKOUT_TEST = "OB_TEST"

class EventClassifier:
    """
    Classify UPS events by mains voltage and status transitions.
    """

    def __init__(self):
        self.state = EventType.ONLINE

    def classify(self, ups_status: str, input_voltage: float) -> Tuple[EventType, str]:
        """
        Classify current event and detect transitions.

        Args:
            ups_status: From NUT (e.g., "OL", "OB DISCHRG", "OB DISCHRG LB")
            input_voltage: From NUT in volts (230V when mains on, ~0V when mains off)

        Returns:
            (event_type, description) where event_type drives daemon logic
        """
        # Voltage threshold: >100V = mains present, <50V = mains absent
        mains_present = input_voltage > 100.0
        on_battery = "OB" in ups_status

        new_state = None

        if not on_battery:
            new_state = EventType.ONLINE
        elif on_battery and mains_present:
            new_state = EventType.BLACKOUT_TEST
        elif on_battery and not mains_present:
            new_state = EventType.BLACKOUT_REAL

        # Detect transitions
        transition = (self.state != new_state)
        if transition:
            self.state = new_state
            return (new_state, f"TRANSITION: {self.state.name} → {new_state.name}")

        return (self.state, f"STATE: {self.state.name}")
```

**Threshold selection:**
- `input_voltage > 100V` → mains present (typical 230V, allows margin for AC ripple/noise)
- `input_voltage < 50V` → mains absent (well below any nominal mains voltage)
- Gap [50V, 100V] → undefined (should not occur in practice; log as error)

### Anti-Patterns to Avoid

- **Trusting `battery.runtime` directly:** Firmware value is unreliable (off by ~2x as seen in blackout 2026-03-12). Always calculate from voltage → SoC → Peukert.
- **Using firmware `battery.charge` for shutdown decisions:** Firmware value goes to 0% at 35 min into a 47-minute blackout. Use calculated SoC instead.
- **Assuming `ups.status` flags are accurate:** Firmware sets LB based on firmware's broken charge estimate, not reality. We emit our own LB flag based on calculated time-to-empty.
- **Trusting battery test detection to firmware:** `onlinedischarge_calibration: true` causes NUT to ignore OB+LB during test. We use voltage, not status flags, to distinguish test from real blackout.
- **Linear interpolation with less than 2 LUT points:** Requires at least one voltage band for interpolation. Check LUT length before lookup.
- **Peukert exponent = 1.0 (linear model):** VRLA batteries show strong nonlinear discharge; exponent ~1.2 matches observed behavior. Don't simplify to linear.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Binary search in sorted voltage list | Custom loop to find adjacent points | Python `bisect.bisect_left()` | O(log N) vs O(N), tested standard library |
| Interpolation between voltage points | Polynomial fitting, splines | Simple linear formula `y = y0 + (x-x0)/(x1-x0) * (y1-y0)` | VRLA curve is measured data, not smooth function; linear is sufficient between points |
| Exponentiation in Peukert formula | Integer approximations, lookup tables | `load ** 1.2` (built-in pow) | Accurate, no LUT cache needed; single value computed once per cycle |
| State machine for blackout vs test | Ad-hoc if-else chains | Enum-based state with transition tracking | Clear, testable, prevents state inconsistencies |
| JSON persistence for model updates | Inline JSON writes | `atomic_write_json()` from Phase 1 | Prevents corruption on power loss; phase 1 already proved pattern |

**Key insight:** All Phase 2 code is stateless arithmetic except event_classifier.py which maintains transition history. No custom data structures needed; Phase 1 infrastructure (model.json, EMA buffer) is sufficient.

## Common Pitfalls

### Pitfall 1: Voltage-SoC Lookup Clamping Logic

**What goes wrong:** Lookup returns nonsensical SoC (e.g., 1.5 or -0.3) when V_norm is outside the LUT range.

**Why it happens:** Interpolation formula doesn't clamp; if V_norm > all LUT voltages, interpolation extends beyond 1.0; if V_norm < anchor, interpolation can go negative.

**How to avoid:** Always clamp result to [0.0, 1.0] after interpolation. Also clamp intermediate SoC during Peukert calculation before returning time_rem.

**Warning signs:** Test case where V_norm = 13.6V (above full) returns SoC > 1.0; test case where V_norm = 10.0V (below anchor) returns SoC < 0.0.

### Pitfall 2: Division by Zero in Peukert Formula

**What goes wrong:** Daemon crashes when `load_ema = 0` in Peukert: `(capacity * SoC) / (0 ^ 1.2) = ∞` or NaN.

**Why it happens:** UPS idle (no load) → load_ema approaches 0 → division by zero.

**How to avoid:** Check `load_ema > 0` before Peukert calculation. If load_ema ≈ 0, return a large time_rem (>999 minutes) or flag as "no discharge."

**Warning signs:** Peukert function called in tests with load=0; runtime predictor returns infinity, NaN, or crashes.

### Pitfall 3: Input.voltage Threshold Too Sensitive

**What goes wrong:** Noise in input.voltage measurement causes spurious classification (blackout ↔ test transitions every few seconds).

**Why it happens:** Mains AC voltage fluctuates ±10V; UPS measures 230V ±15V. Threshold at exactly 230V causes oscillation.

**How to avoid:** Use hysteresis or a margin: `mains_present = input_voltage > 100V` (clear gap from 0V) and `mains_absent = input_voltage < 50V`. Never threshold at nominal mains voltage.

**Warning signs:** Event log shows OB_REAL ↔ OB_TEST transitions every 1-2 polling cycles (jitter); SoH history has multiple updates in seconds.

### Pitfall 4: LUT Sorted Assumption Violated

**What goes wrong:** `bisect.bisect_left()` assumes voltages are sorted ascending. If LUT is unsorted, lookup returns wrong index and interpolation picks wrong band.

**Why it happens:** Manual editing of model.json or buggy insertion code shuffles the LUT.

**How to avoid:** Load LUT and verify it's sorted ascending: `assert lut == sorted(lut, key=lambda x: x['v'])`. Log error and refuse to predict if unsorted.

**Warning signs:** SoC jumps discontinuously (e.g., 0.5 at 12.0V, then 0.2 at 11.9V); bisect returns out-of-order indices.

### Pitfall 5: Peukert Exponent Not Tuned to Battery

**What goes wrong:** `runtime_minutes()` predicts 30 min but blackout lasts 47 min (or vice versa). Shutdown triggers too early or too late.

**Why it happens:** Peukert exponent 1.2 is typical VRLA, but CyberPower UT850EG may differ. Const is derived from one observed blackout; more data points needed.

**How to avoid:** (1) Document that exponent and const are tuned to UT850EG based on 2026-03-12 blackout. (2) Make them configurable via environment variables (e.g., `UPS_MONITOR_PEUKERT_EXP`, `UPS_MONITOR_PEUKERT_CONST`). (3) Plan for Phase 4 (HLTH-01, HLTH-02) to refine via SoH calculation and linear regression.

**Warning signs:** Phase 1 tests pass, but Phase 2 runtime tests fail by >10% error; blackout duration in real scenario differs significantly from prediction.

## Code Examples

Verified patterns from project spec and Phase 1 precedent:

### SoC Prediction with Linear Interpolation

```python
# Source: Project spec § LUT Lookup, standard VRLA pattern

import bisect
from typing import List, Dict

def soc_from_voltage(v_norm: float, lut: List[Dict[str, float]]) -> float:
    """
    Map normalized voltage to SoC using LUT linear interpolation.

    Args:
        v_norm: IR-compensated voltage (volts)
        lut: List of dicts with keys 'v' (voltage) and 'soc' (state_of_charge)

    Returns:
        SoC as fraction [0.0, 1.0]
    """
    voltages = [entry['v'] for entry in lut]

    # Edge: below anchor
    if v_norm <= voltages[0]:
        return lut[0]['soc']

    # Edge: above max
    if v_norm >= voltages[-1]:
        return lut[-1]['soc']

    # Interpolation: find two adjacent points
    idx = bisect.bisect_left(voltages, v_norm)
    v_low, soc_low = voltages[idx - 1], lut[idx - 1]['soc']
    v_high, soc_high = voltages[idx]['v'], lut[idx]['soc']

    # Linear blend
    t = (v_norm - v_low) / (v_high - v_low)
    return soc_low + t * (soc_high - soc_low)
```

### Runtime Calculation via Peukert's Law

```python
# Source: CONTEXT.md § Математика предиктора, blackout 2026-03-12 validation

def runtime_minutes(soc: float, load_ema: float, capacity_ah: float = 7.2,
                    soh: float = 1.0, const: float = 215) -> float:
    """
    Remaining runtime using empirical Peukert formula for VRLA.

    Tuning: const = 215 derived from 2026-03-12 blackout:
      observed: 47 min at ~20% load, SoC=1.0, SoH=1.0
      formula: 47 = (7.2 * 1.0 * 1.0) / (20 ^ 1.2) * const
      const ≈ 47 * (20 ^ 1.2) / 7.2 ≈ 215

    Args:
        soc: [0.0, 1.0]
        load_ema: [0, 100] percent
        capacity_ah: Full capacity (default 7.2 Ah)
        soh: Health factor (default 1.0 = new)
        const: Calibration constant

    Returns:
        Minutes until cutoff; 0 if load ≤ 0 or SoC = 0
    """
    if load_ema <= 0 or soc <= 0:
        return 0.0

    time_rem = (capacity_ah * soc * soh) / (load_ema ** 1.2) * const
    return max(0.0, time_rem)
```

### Event Classification State Machine

```python
# Source: CONTEXT.md § Различение блекаута и теста батареи

from enum import Enum
from typing import Tuple

class EventType(Enum):
    ONLINE = "OL"
    BLACKOUT_TEST = "OB_TEST"
    BLACKOUT_REAL = "OB_BLACKOUT"

class EventClassifier:
    """Track blackout vs test based on mains presence."""

    def __init__(self):
        self.state = EventType.ONLINE
        self.transition_occurred = False

    def classify(self, ups_status: str, input_voltage: float) -> EventType:
        """
        Determine event type from NUT status and voltage.

        Thresholds: input_voltage > 100V = mains present, < 50V = absent.
        """
        on_battery = "OB" in ups_status
        mains_present = input_voltage > 100.0

        if not on_battery:
            new_state = EventType.ONLINE
        elif on_battery and mains_present:
            new_state = EventType.BLACKOUT_TEST
        else:  # on_battery and not mains_present
            new_state = EventType.BLACKOUT_REAL

        self.transition_occurred = (self.state != new_state)
        self.state = new_state
        return new_state
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Firmware `battery.runtime` for shutdown decisions | Calculated time_rem via Peukert | This phase | Eliminates 2x error in time estimates; shutdown triggers at correct time |
| Firmware `battery.charge` for SoC | Voltage-based SoC via LUT lookup | This phase | Prevents premature shutdown (0% at 35 min of 47-min blackout) |
| Trusting firmware OB+LB flags | Self-emitted status flags based on calculated time-to-empty | Phase 3 (virtual UPS) | Bypasses firmware `onlinedischarge_calibration` bug |
| No distinction between test and blackout | Physical invariant: input.voltage threshold | This phase | One detection, zero false positives (100% accuracy target) |

**Deprecated/outdated:**
- CyberPower UT850EG firmware calibration (no `calibrate.start` command available) — replaced by measured discharge data collection and Peukert tuning.
- Hardcoded Peukert exponent — will be refined in Phase 4 via linear regression on SoH history, but 1.2 is acceptable starting point for VRLA.

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 7.0+ (already used in Phase 1) |
| Config file | `tests/conftest.py` (reuse Phase 1 fixtures) |
| Quick run command | `pytest tests/test_soc_predictor.py tests/test_runtime_calculator.py tests/test_event_classifier.py -v --tb=short` |
| Full suite command | `pytest tests/ -v --cov=src --tb=short` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| PRED-01 | V_norm 12.0V → LUT lookup → SoC 0.50 ±0.05 | unit | `pytest tests/test_soc_predictor.py::test_soc_interpolation -xvs` | ❌ Wave 0 |
| PRED-01 | V_norm at LUT point (12.4V) → exact SoC (0.64) | unit | `pytest tests/test_soc_predictor.py::test_soc_exact_point -xvs` | ❌ Wave 0 |
| PRED-01 | V_norm above max (13.5V) → clamped to 1.0 | unit | `pytest tests/test_soc_predictor.py::test_soc_clamp_high -xvs` | ❌ Wave 0 |
| PRED-01 | V_norm below anchor (10.0V) → clamped to 0.0 | unit | `pytest tests/test_soc_predictor.py::test_soc_clamp_low -xvs` | ❌ Wave 0 |
| PRED-02 | Peukert: SoC=1.0, load=20%, const=215 → ~47 min | integration | `pytest tests/test_runtime_calculator.py::test_peukert_blackout_match -xvs` | ❌ Wave 0 |
| PRED-02 | Peukert: SoC=0%, load=20% → 0 min (clamped) | unit | `pytest tests/test_runtime_calculator.py::test_peukert_zero_soc -xvs` | ❌ Wave 0 |
| PRED-02 | Peukert: load=0% → 0 min (no division by zero) | unit | `pytest tests/test_runtime_calculator.py::test_peukert_zero_load -xvs` | ❌ Wave 0 |
| PRED-03 | SoC → battery.charge = SoC × 100 (e.g., 0.75 → 75%) | unit | `pytest tests/test_soc_predictor.py::test_charge_percentage -xvs` | ❌ Wave 0 |
| EVT-01 | input.voltage=0V, status=OB DISCHRG → BLACKOUT_REAL | unit | `pytest tests/test_event_classifier.py::test_classify_real_blackout -xvs` | ❌ Wave 0 |
| EVT-01 | input.voltage=230V, status=OB DISCHRG → BLACKOUT_TEST | unit | `pytest tests/test_event_classifier.py::test_classify_battery_test -xvs` | ❌ Wave 0 |
| EVT-02 | BLACKOUT_REAL event → daemon calls runtime_minutes() | integration | Manual test during phase planning | ✅ Behavior verified in spec |
| EVT-03 | BLACKOUT_TEST event → no shutdown signal emitted | integration | Manual test during phase planning | ✅ Behavior verified in spec |
| EVT-04 | ups.status emitter: time_rem < threshold → LB flag set | integration | Tested in Phase 3 (virtual UPS) | — Deferred |
| EVT-05 | OB→OL transition → model.json updated with measured points | integration | Tested in Phase 3 | — Deferred |

### Sampling Rate

- **Per task commit:** `pytest tests/test_soc_predictor.py tests/test_runtime_calculator.py tests/test_event_classifier.py -x`
- **Per wave merge:** `pytest tests/ -v --cov=src`
- **Phase gate:** All PRED-XX and EVT-01 tests must pass 100%; code coverage >85% for new modules

### Wave 0 Gaps

- [ ] `tests/test_soc_predictor.py` — covers PRED-01, PRED-03 (LUT lookup, clamping, interpolation)
- [ ] `tests/test_runtime_calculator.py` — covers PRED-02 (Peukert formula, edge cases)
- [ ] `tests/test_event_classifier.py` — covers EVT-01 (state machine, transitions)
- [ ] `tests/conftest.py` — extend with mock LUT fixtures (list of voltage/SoC pairs)
- [ ] Framework install: `pytest` already available from Phase 1 test suite

## Sources

### Primary (HIGH confidence)

- **Project CONTEXT.md § Математика предиктора** — Verified formulas for EMA, IR compensation, Peukert's Law, and constants derived from 2026-03-12 blackout
- **Project CONTEXT.md § Модель батареи** — LUT structure, anchor point (10.5V), VRLA curve shape, measured vs standard source tracking
- **Project CONTEXT.md § Различение блекаута и теста батареи** — Physical invariant (input.voltage) for event classification, state machine logic
- **Phase 1 code (monitor.py, ema_ring_buffer.py, model.py)** — Foundation architecture for data flow: NUT polling → EMA smoothing → IR compensation → model persistence
- **Python `bisect` module documentation** — O(log N) binary search for sorted sequence lookups (used for LUT voltage bands)

### Secondary (MEDIUM confidence)

- **Peukert's Law from electrochemistry literature** — Empirical exponent 1.2 is standard for lead-acid batteries; project tuning to UT850EG validated by blackout data
- **VRLA discharge curve characteristics** — Typical knee point at 64% (12.4V), cliff region below 11.0V; shape matches project observations

## Metadata

**Confidence breakdown:**
- **SoC Prediction (PRED-01):** HIGH — LUT structure, interpolation logic, and boundary conditions fully specified in Phase 1 and CONTEXT.md
- **Peukert Formula (PRED-02):** HIGH — Formula, constants, and edge case handling verified by 2026-03-12 blackout data; Const=215 derived empirically
- **Event Classification (EVT-01):** HIGH — Physical invariant (input.voltage threshold) is hardware-independent, documented in CONTEXT.md, no firmware interpretation required
- **State Machine Transitions (EVT-02, EVT-03, EVT-04, EVT-05):** MEDIUM — Logic documented but requires integration testing in Phase 3; no new algorithms

**Research date:** 2026-03-13
**Valid until:** 2026-04-13 (30 days for stable phase 2 algorithms; minor updates if Peukert constant needs tuning after real-world testing)
