# Phase 18: Unify Coulomb Counting — Research

## RESEARCH COMPLETE

**Researched:** 2026-03-20
**Focus:** Map all coulomb counting / avg_load call sites, identify extraction path, flag risks

---

## 1. Current State: Two Implementations

### Implementation A: Per-step trapezoidal (accurate)
**Location:** `src/capacity_estimator.py:157-193` — `CapacityEstimator._integrate_current()`

```python
def _integrate_current(self, load_percent, time_sec, nominal_power_watts, nominal_voltage):
    # Trapezoidal rule: I = (load% / 100) × P / V, then ∫I dt / 3600
    for i in range(len(load_percent) - 1):
        i_avg = (current_start + current_end) / 2.0
        dt = time_sec[i+1] - time_sec[i]
        ah_total += i_avg * dt / 3600.0
```

- Uses per-step load values from discharge buffer
- Trapezoidal rule (IEEE-1106 standard)
- F27 bias: uses nominal voltage (12V), not actual — documented, intentional, ~4% overestimate
- Called once: `capacity_estimator.py:93`

### Implementation B: Scalar average (approximation)
**Location:** `src/discharge_handler.py:225-229` — `DischargeHandler._avg_load()`

```python
def _avg_load(self, discharge_buffer):
    if discharge_buffer.loads:
        return sum(discharge_buffer.loads) / len(discharge_buffer.loads)
    return self.reference_load_percent
```

- Computes a single scalar average, loses per-step variation
- Called at 4 sites in `discharge_handler.py`: lines 139, 209, 366, 435

### Implementation C: Inline scalar average
**Location:** `src/discharge_handler.py:435`

```python
avg_load = (sum(discharge_buffer.loads) / len(discharge_buffer.loads)
            if discharge_buffer.loads else 0.0)
```

- `_log_discharge_prediction()` doesn't even use `_avg_load()` — has its own inline version
- Slightly different fallback (0.0 vs reference_load_percent)

---

## 2. avg_load Call Site Map

| Line | Method | Purpose | Current source |
|------|--------|---------|----------------|
| 139 | `_compute_soh()` | Passed to `soh_calculator.calculate_soh_from_discharge()` | `_avg_load()` |
| 209 | `_check_alerts()` | Passed to `runtime_minutes()` for runtime threshold check | `_avg_load()` **DUPLICATE** |
| 366 | `_auto_calibrate_peukert()` | Passed to `calibrate_peukert()` | `_avg_load()` |
| 435 | `_log_discharge_prediction()` | Logged in structured event | Inline computation |

### The ARCH-02 bug
`_complete_discharge()` (line 92) calls:
1. `_compute_soh()` → computes avg_load at line 139
2. `_check_alerts()` → recomputes avg_load at line 209

Both use `_avg_load(discharge_buffer)` with identical input. The fix: compute once in `_complete_discharge()`, pass as parameter.

---

## 3. Downstream consumers that use avg_load

| Consumer | File | What it does with avg_load |
|----------|------|---------------------------|
| `soh_calculator.calculate_soh_from_discharge()` | `src/soh_calculator.py` | Uses load_percent to compute expected runtime |
| `runtime_minutes()` | `src/battery_math/peukert.py` | Peukert-corrected runtime from SoC+load |
| `calibrate_peukert()` | `src/battery_math/calibration.py` | Computes current from avg_load_percent |

**Key insight:** `calibrate_peukert()` converts avg_load back to current via `I = avg_load/100 * P/V` — the same formula as `_integrate_current()`. This is where scalar averaging loses accuracy: the Peukert effect is non-linear, so `f(avg(x)) ≠ avg(f(x))`.

---

## 4. Extraction Plan

### Step 1: Create `src/battery_math/integration.py`
Move `_integrate_current()` from `CapacityEstimator` to standalone function `integrate_current()`.

Signature stays the same (already takes primitives, no class state used):
```python
def integrate_current(load_percent: List[float], time_sec: List[float],
                      nominal_power_watts: float, nominal_voltage: float) -> float:
```

### Step 2: Update `battery_math/__init__.py`
Add `integrate_current` to exports.

### Step 3: Update `CapacityEstimator`
Replace `self._integrate_current(...)` with `integrate_current(...)` import from battery_math.

### Step 4: Fix _check_alerts (ARCH-02)
In `_complete_discharge()`, compute avg_load once and pass to `_check_alerts()`:
```python
avg_load = self._avg_load(discharge_buffer)
# ... use avg_load in _compute_soh() ...
self._check_alerts(soh_after, replacement_prediction, discharge_buffer, avg_load)
```

Change `_check_alerts` signature:
```python
def _check_alerts(self, soh_new, replacement_prediction, discharge_buffer, avg_load):
    # Remove: avg_load = self._avg_load(discharge_buffer)
```

### Step 5: Unit test for accuracy comparison
Create test that feeds a variable-load sequence through both approaches (per-step vs scalar average) and asserts the per-step result is more accurate.

---

## 5. Risk Assessment

### Low risk
- `_integrate_current()` is a pure function with no side effects — safe to extract
- `_avg_load()` is simple and well-tested — changing call pattern is straightforward
- All changes are internal refactoring — no external API changes

### Watch out for
- `_log_discharge_prediction()` line 435 has its own inline avg_load — should this also use `_avg_load()`? Different fallback (0.0 vs reference_load_percent). This is a logging-only method, so the impact is low, but consistency matters.
- Tests in `test_capacity_estimator.py` test `_integrate_current` indirectly via `estimate_from_discharge()` — after extraction, these tests still pass because `CapacityEstimator` will import from `battery_math`.
- `test_monitor_integration.py:107` has `test_avg_load_from_discharge_buffer` — should still pass since `_avg_load()` behavior doesn't change.

### Not in scope
- Replacing scalar avg_load with per-step integration everywhere (e.g., in `calibrate_peukert`) — this would change behavior. Phase 18 unifies the integration function and fixes the double computation. Making all consumers use per-step integration is a behavior change that could be a follow-up if desired.

---

## 6. Validation Architecture

### Test Strategy
1. **Unit test: `integrate_current()` standalone** — same inputs as existing `test_coulomb_integration_synthetic`, verifies extraction didn't break anything
2. **Accuracy comparison test** — variable-load sequence, per-step vs scalar, assert per-step is more accurate (Success Criterion 3)
3. **Regression: full test suite** — all 476+ tests pass (Success Criterion 4)
4. **grep verification** — no remaining `_integrate_current` method on any class, `integrate_current` exists in `battery_math/`

### Acceptance criteria (grep-verifiable)
- `src/battery_math/integration.py` contains `def integrate_current(`
- `src/battery_math/__init__.py` contains `integrate_current`
- `src/capacity_estimator.py` does NOT contain `def _integrate_current(`
- `src/discharge_handler.py:_check_alerts` signature contains `avg_load`
- `src/discharge_handler.py:_check_alerts` does NOT contain `self._avg_load(`

---

*Research complete: 2026-03-20*
