# Phase 18: Unify Coulomb Counting - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-20
**Phase:** 18-unify-coulomb-counting
**Areas discussed:** Function placement, Accuracy unification, avg_load propagation
**Mode:** --auto (recommended defaults selected automatically)

---

## Function Placement

| Option | Description | Selected |
|--------|-------------|----------|
| Standalone in battery_math/ | Pure function alongside existing math (peukert, calibration, etc.) | ✓ |
| Method on CapacityEstimator | Keep as class method, import from capacity_estimator | |
| New integration module | Dedicated integration.py in battery_math/ | |

**User's choice:** [auto] Standalone in battery_math/ (recommended default)
**Notes:** Matches existing pattern — all battery_math functions are pure, no class state. DischargeBuffer stays out of math layer.

---

## Accuracy Unification

| Option | Description | Selected |
|--------|-------------|----------|
| Per-step everywhere | All coulomb counting uses trapezoidal per-step integration | ✓ |
| Keep scalar for alerts | Only capacity estimation uses per-step; alerts keep scalar average | |

**User's choice:** [auto] Per-step everywhere (recommended default)
**Notes:** Design principle: "When choosing between implementations, pick the one with greater accuracy." Scalar average loses information on variable loads.

---

## avg_load Propagation

| Option | Description | Selected |
|--------|-------------|----------|
| Pass as parameter | Compute once, pass avg_load to _check_alerts() | ✓ |
| Cache on instance | Store avg_load as instance attribute after first computation | |

**User's choice:** [auto] Pass as parameter (recommended default)
**Notes:** Matches ARCH-02 requirement exactly. Explicit parameter passing is clearer than cached state.

---

## Claude's Discretion

- Exact file placement within battery_math/ (new file vs existing)
- Unit test structure for extracted function
- Optional convenience wrapper for DischargeBuffer

## Deferred Ideas

None.
