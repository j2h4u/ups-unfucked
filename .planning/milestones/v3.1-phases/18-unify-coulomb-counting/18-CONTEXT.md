# Phase 18: Unify Coulomb Counting - Context

**Gathered:** 2026-03-20
**Status:** Ready for planning

<domain>
## Phase Boundary

All coulomb counting in the codebase uses a single accurate implementation with per-step load support. No duplicate integration logic remains. `_check_alerts` receives `avg_load` as a parameter instead of recomputing it.

Requirements: ARCH-01, ARCH-02.

</domain>

<decisions>
## Implementation Decisions

### Function placement (ARCH-01)
- Extract `_integrate_current()` from `CapacityEstimator` class (capacity_estimator.py:157) into a standalone function in `src/battery_math/`
- Function signature: accept raw arrays (load_percent, time_sec, nominal_power_watts, nominal_voltage) — not DischargeBuffer. Math functions stay decoupled from domain objects
- Export from `battery_math/__init__.py` alongside existing functions

### Accuracy unification (ARCH-01)
- Per-step trapezoidal integration is the canonical implementation — it's more accurate than scalar average for variable loads
- All call sites that currently use scalar avg_load for coulomb counting must switch to per-step `integrate_current()`
- The existing trapezoidal implementation in capacity_estimator.py is the reference — move it, don't rewrite it
- Design principle: "When choosing between implementations, pick the one with greater accuracy"

### avg_load propagation (ARCH-02)
- `_check_alerts()` in discharge_handler.py currently recomputes `avg_load` via `self._avg_load(discharge_buffer)` (line 209) — this is the double computation
- Fix: add `avg_load` parameter to `_check_alerts()`, compute once in calling code, pass through
- The `_avg_load()` helper itself stays (it's used in other places) — just stop calling it redundantly in `_check_alerts`

### Claude's Discretion
- Exact module file name for the new function (e.g., `integration.py` or add to existing `calibration.py`)
- Whether to add a convenience wrapper that accepts DischargeBuffer (if it simplifies call sites without violating decoupling)
- Unit test structure for the extracted function

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Coulomb counting implementations
- `src/capacity_estimator.py` lines 157-193 — Current per-step trapezoidal `_integrate_current()` (source of truth for the algorithm)
- `src/discharge_handler.py` lines 225-229 — `_avg_load()` scalar averaging (the approximation to replace)
- `src/discharge_handler.py` lines 190-223 — `_check_alerts()` with duplicate avg_load computation (ARCH-02 target)

### Downstream consumers of avg_load
- `src/battery_math/calibration.py` lines 12, 42, 50 — `calibrate_peukert()` uses `avg_load_percent` parameter
- `src/discharge_handler.py` lines 139, 366, 435 — Multiple `_avg_load()` call sites in discharge handling

### Requirements
- `.planning/REQUIREMENTS.md` — ARCH-01, ARCH-02 definitions

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/battery_math/` package: Established home for pure math functions (peukert, calibration, regression, sulfation, cycle_roi)
- `capacity_estimator.py:_integrate_current()`: Working trapezoidal integration — move, don't rewrite
- `battery_math/__init__.py`: Centralized exports — add `integrate_current` here

### Established Patterns
- All battery_math functions are pure: no class state, no side effects, accept primitives and return primitives
- F27 bias (nominal voltage vs actual) is documented and intentional — preserve the same bias in unified function
- IEEE-1106 trapezoidal rule is the integration standard used

### Integration Points
- `CapacityEstimator.estimate_from_discharge()` (line 93) — current caller of `_integrate_current()`
- `DischargeHandler._complete_discharge()` (line 139) — computes avg_load for SoH calculation
- `DischargeHandler._check_alerts()` (line 209) — recomputes avg_load (ARCH-02 fix point)
- `DischargeHandler._calibrate_peukert()` (line 366) — uses avg_load for Peukert calibration

</code_context>

<specifics>
## Specific Ideas

No specific requirements — the implementation is well-defined by the existing code and requirements. Key constraint: the F27 bias (using nominal voltage 12V instead of actual battery voltage) is intentional and documented — the unified function must preserve this behavior.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 18-unify-coulomb-counting*
*Context gathered: 2026-03-20*
