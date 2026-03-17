# Expert Panel: Phase 12.1 — Simulation, Functional Architecture, Daemon Refactoring

**Date:** 2026-03-15
**Trigger:** Phase 12.1 "Math Kernel Extraction & Formula Stability Tests" design review
**Focus:** (1) Simulation soundness, (2) Kernel/orchestrator split, (3) Safe daemon refactoring
**Panel:** 3 experts — Numerical Methods, Software Architect (functional), Embedded/Daemon

---

## Scope

**What:** Validate the math kernel extraction + simulation harness design for Phase 12.1.

**Blast radius:** `contained` — refactoring internal modules, no external API change.

**Decision type:** Stress-testing an existing plan before execution.

---

## Expert Analysis

### 🔢 Prof. Marchetti — Numerical Methods & Simulation

**Assessment:** The simulation approach is sound in principle — iterating pure functions with synthetic data is the standard way to test dynamical system stability. But there are three things that will bite you.

**Insight 1: Your system is NOT time-invariant, but the simulation treats it as if it is.**

Real battery degradation is path-dependent. A battery that did 50 shallow cycles then 1 deep cycle behaves differently from one that did 1 deep then 50 shallow. Your SoH calculator uses `reference_soh` (the previous SoH) as a Bayesian prior with duration-weighted blending (line 106-109 of soh_calculator.py). This means **order of events matters**. Testing with 20 identical discharges proves stability for *that specific path* but says nothing about shuffled orderings.

**Recommendation:** Add a permutation test. Generate 10 discharge events of varying depth (20%, 40%, 60%, 80%). Run them in 5 random orderings. Final state (SoH, Peukert) should agree within ±2% regardless of ordering. If it doesn't — your Bayesian prior is path-dependent in a way that could accumulate bias over a year.

**Insight 2: Your Lyapunov criterion is too loose.**

"±1% input → ±3% after 10 iterations" is a 3x amplification factor. Over 100 iterations (roughly 3 months of weekly blackouts), this compounds to 3^10 ≈ 59,000x if the amplification is *per iteration*. What you actually want to test is that the *per-iteration* amplification factor is < 1.0. The correct test:

```python
# For each iteration i:
#   divergence[i] = |perturbed_state[i] - baseline_state[i]|
#   ratio[i] = divergence[i] / divergence[i-1]
#   assert ratio[i] < 1.0 for all i > warmup_period
```

If ratio > 1.0 for even one iteration, the system is locally unstable at that point. The "±3% after 10" test would pass a system where ratio = 1.1 (grows 10% per iteration) because 1% × 1.1^10 = 2.6% < 3%. But 1% × 1.1^100 = 13,780%. Boom.

**Insight 3: Fixed random seeds are essential but insufficient.**

Use `random.seed(42)` for reproducibility, but also run with 5 different seeds. If results vary >1% between seeds at the same iteration count, your noise model is dominating the signal — the simulation is measuring randomness, not stability.

**Open question:** The `_weighted_average_by_voltage` function in soh_calculator.py uses `time.time()` for age-based decay (line 136). How does the simulation handle this? If you don't mock the clock, all LUT entries have the same age and the decay function becomes a no-op, giving you false confidence about LUT stability.

---

### 🏗️ Dr. Chen — Software Architect (Functional Systems)

**Assessment:** The kernel/orchestrator split is the right call. But I see a design tension that needs resolving before you write code, and one common trap.

**Insight 1: Don't make one `battery_math.py` — make a kernel *package*.**

One file will grow to 500+ LOC as capacity estimation joins. More importantly, the formulas have different *change frequencies*: Peukert's law hasn't changed in 100 years, SoH blending was revised 2 days ago. Mixing stable and volatile code in one file means every SoH tweak touches the same file as foundational physics. Instead:

```
src/battery_math/
    __init__.py          # Re-exports everything (single import point)
    peukert.py           # peukert_runtime_hours, runtime_minutes (stable, physics)
    soh.py               # calculate_soh, weighted_average_by_voltage (evolving, statistical)
    soc.py               # soc_from_voltage (stable, LUT interpolation)
    capacity.py          # estimate_capacity (Phase 12, new)
    calibration.py       # calibrate_peukert (extracted from monitor.py)
    types.py             # BatteryState dataclass (see below)
```

Orchestrators do `from src.battery_math import calculate_soh, calibrate_peukert` — one import path, multiple source files.

**Insight 2: The kernel needs a state type — but a frozen one.**

The simulation currently plans to pass a "dict of state" between iterations. This will rot. Define:

```python
@dataclass(frozen=True)
class BatteryState:
    soh: float
    peukert_exponent: float
    capacity_ah_rated: float
    capacity_ah_measured: float | None
    lut: tuple[tuple[float, float, str], ...]  # Immutable LUT snapshot
    cycle_count: int
    cumulative_on_battery_sec: float
```

`frozen=True` is the key. Every kernel function takes a `BatteryState` and returns a *new* `BatteryState` (or a result + unchanged state). The orchestrator converts between `BatteryState` and `model.json` / `BatteryModel`. The simulation just chains states. This makes circular dependencies **structurally impossible** — you can see in the function signature what goes in and what comes out. If `calculate_soh` returns a new state with a modified `capacity_ah_rated`, that's visible at the type level.

**Trap to avoid:** Don't put LUT mutation in the kernel. The kernel reads LUT, the orchestrator writes LUT. `soc_from_voltage(voltage, lut)` is already correct — LUT is an *input*, not state the kernel modifies. Keep it that way. `interpolate_cliff_region` is the one function that mutates LUT — it stays in the orchestrator layer or becomes a pure `lut_in → lut_out` transform.

**Open question:** `soh_calculator._weighted_average_by_voltage` uses `time.time()` internally. This breaks purity. Should the simulation inject a clock, or should the function take `current_time` as a parameter?

---

### ⚙️ Kowalski — Embedded Systems / Daemon

**Assessment:** Refactoring monitor.py is safe *if* you treat it as a pure extraction, not a redesign. The daemon has been running in production — don't touch its state machine. Extract math, leave orchestration exactly as-is.

**Insight 1: `_auto_calibrate_peukert` extraction is simpler than it looks.**

Looking at the actual code (lines 507-567), there are only 4 values the math needs: `actual_duration_sec`, `avg_load`, `current_soh`, and current model params (`capacity_ah`, `peukert_exponent`, `nominal_voltage`, `nominal_power_watts`). Everything else (`self.discharge_buffer`, `self.ema_buffer`) is just *data collection* that happens before the math. The clean cut:

```python
# battery_math/calibration.py
def calibrate_peukert(
    actual_duration_sec: float,
    avg_load_percent: float,
    current_soh: float,
    capacity_ah: float,
    current_exponent: float,
    nominal_voltage: float = 12.0,
    nominal_power_watts: float = 425.0,
    error_threshold: float = 0.10,
    exponent_bounds: tuple[float, float] = (1.0, 1.4)
) -> float | None:
    """Return new exponent if error > threshold, else None."""
```

Monitor.py becomes:
```python
def _auto_calibrate_peukert(self, current_soh):
    # Data collection (orchestrator responsibility)
    if len(self.discharge_buffer.times) < 2:
        return
    actual_duration = self.discharge_buffer.times[-1] - self.discharge_buffer.times[0]
    avg_load = self.ema_buffer.load
    if avg_load is None or avg_load <= 0 or actual_duration < 60:
        return

    # Math (kernel)
    new_exp = calibrate_peukert(
        actual_duration, avg_load, current_soh,
        self.battery_model.get_capacity_ah(),
        self.battery_model.get_peukert_exponent(),
        self.battery_model.get_nominal_voltage(),
        self.battery_model.get_nominal_power_watts()
    )

    # Side effect (orchestrator responsibility)
    if new_exp is not None:
        logger.info(f"Peukert calibrated: {self.battery_model.get_peukert_exponent():.3f} → {new_exp:.3f}")
        self.battery_model.set_peukert_exponent(new_exp)
        _safe_save(self.battery_model)
```

That's it. The guard clauses (< 2 samples, < 60s, invalid load) stay in the orchestrator because they're about *data availability*, not math.

**Insight 2: Test the orchestrator too, not just the kernel.**

Everyone's excited about testing pure math. But the most dangerous bugs are in the orchestrator layer — the *wiring*. What if monitor.py passes `capacity_ah_measured` where it should pass `capacity_ah_rated`? What if it calls `calibrate_peukert` with `avg_load` from the *current* poll instead of the *discharge average*? These are integration bugs that kernel tests don't catch.

Write at least 3 orchestrator-level tests:
1. Mock discharge event → verify `calibrate_peukert` called with correct arguments (not just "called")
2. Mock discharge event → verify `capacity_ah` passed to SoH calculator is `full_capacity_ah_ref`, not measured
3. Verify `calibrate_peukert` result is applied to model *only* when non-None

**Insight 3: Don't break the systemd watchdog.**

monitor.py has `sd_notify('WATCHDOG=1')` in the poll loop (line 854). If your refactoring makes `_update_battery_health()` or `_auto_calibrate_peukert()` significantly slower (unlikely, but logging changes could do it), the watchdog will kill the daemon. Run the daemon under systemd in a test environment after refactoring and verify it survives 10 poll cycles.

**Open question:** `_auto_calibrate_peukert` currently fires *inside* `_update_battery_health` (line 499). After extraction, does it still fire at the same point in the event sequence? The ordering matters — if SoH updates *before* Peukert calibration, the new SoH feeds into `current_soh` parameter. If you accidentally swap the order, the math is different.

---

## Panel Conflicts

| Topic | Position A | Position B | Resolution |
|-------|-----------|-----------|------------|
| One file vs package | Kaizen instinct: one file, YAGNI | Architect: package, prevents rot | **Package.** We already have 4 source files to consolidate. But `__init__.py` re-exports everything so callers don't care. Cost: ~10 minutes. Benefit: clear boundaries forever. |
| `time.time()` in weighted_average | Simulation Expert: must mock clock | Daemon Expert: inject `current_time` param | **Inject parameter.** Add `current_time: float = None` defaulting to `time.time()`. Zero behavior change for daemon, full control for simulator. Cleaner than mock. |
| Perturbation threshold | Plan says ±1%→±3% after 10 | Simulation Expert: must check per-iteration ratio < 1.0 | **Per-iteration ratio.** The ±3% aggregate test is dangerously loose. Check `divergence[i] / divergence[i-1] < 1.0` for each step after warmup. |
| frozen BatteryState | Architect: mandatory for safety | Daemon Expert: conversion overhead | **Frozen for kernel, mutable for orchestrator.** Kernel functions take/return frozen BatteryState. Orchestrator converts to/from BatteryModel. Conversion is trivial (~5 fields). Safety >> convenience. |

## Recommended Plan

**Key decisions:**

1. **`src/battery_math/` as a package, not a single file** — 5 focused modules + `types.py` with frozen `BatteryState`. Re-exports via `__init__.py`. (Architect)

2. **Inject `current_time` parameter** into `_weighted_average_by_voltage` instead of mocking `time.time()`. Default to `time.time()` for backward compat. (Both Simulation + Daemon experts agree)

3. **Per-iteration Lyapunov test**, not aggregate threshold — check `divergence[i]/divergence[i-1] < 1.0` for each step. The ±3% aggregate test stays as a secondary smoke test but the per-iteration check is the real gate. (Simulation Expert)

4. **Add permutation test** — 10 events in 5 random orderings, final state agrees within ±2%. Catches path-dependent bias in Bayesian SoH blending. (Simulation Expert)

5. **Orchestrator wiring tests** — 3 tests that verify monitor.py passes correct arguments to kernel functions (rated capacity, not measured; discharge avg load, not current; etc.). These catch the exact bug the previous expert panel identified. (Daemon Expert)

6. **`interpolate_cliff_region` becomes a pure `lut → lut` transform** — stays callable from kernel but doesn't touch model. Orchestrator calls it and writes result. (Architect)

**Open items:**

- **Ordering of SoH → Peukert within `_update_battery_health`**: must be preserved exactly. Write a test that asserts the call order.
- **Success criterion for seed variance**: run year simulation with 5 seeds, results must agree within 1%. If not, noise model needs tuning before stability tests are meaningful.

---

**Status:** All recommendations incorporated into Phase 12.1 ROADMAP.md and STATE.md on 2026-03-15.
